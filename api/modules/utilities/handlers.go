package utilities

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"ubp/httpx"
	"ubp/middleware"
)

func (m *Module) Routes(mux *http.ServeMux, authed func(http.HandlerFunc) http.Handler) {
	mux.Handle("GET /api/providers", authed(m.listProviders))

	mux.Handle("GET /api/utility-accounts", authed(m.listAccounts))
	mux.Handle("POST /api/utility-accounts", authed(m.createAccount))
	mux.Handle("PUT /api/utility-accounts/{id}", authed(m.updateAccount))
	mux.Handle("DELETE /api/utility-accounts/{id}", authed(m.deleteAccount))

	mux.Handle("POST /api/scrape-jobs", authed(m.triggerScrape))
	mux.Handle("GET /api/scrape-jobs", authed(m.listJobs))
	mux.Handle("GET /api/scrape-jobs/{id}", authed(m.getJob))
	mux.Handle("POST /api/scheduler/run", authed(m.runSchedulerNow))

	m.billRoutes(mux, authed)
}

// -------- providers --------

type providerOut struct {
	ID          int64  `json:"id"`
	Code        string `json:"code"`
	DisplayName string `json:"display_name"`
	Category    string `json:"category"`
}

func (m *Module) listProviders(w http.ResponseWriter, r *http.Request) {
	rows, err := m.q(r.Context()).Query(r.Context(),
		`SELECT id, code, display_name, category FROM providers ORDER BY display_name`)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()
	out := []providerOut{}
	for rows.Next() {
		var p providerOut
		if err := rows.Scan(&p.ID, &p.Code, &p.DisplayName, &p.Category); err == nil {
			out = append(out, p)
		}
	}
	httpx.JSON(w, http.StatusOK, out)
}

// -------- utility accounts --------

type accountOut struct {
	ID             int64      `json:"id"`
	PropertyID     int64      `json:"property_id"`
	PropertyName   string     `json:"property_name"`
	ProviderID     int64      `json:"provider_id"`
	ProviderCode   string     `json:"provider_code"`
	ProviderName   string     `json:"provider_name"`
	AccountNumber  string     `json:"account_number"`
	ServiceAddress string     `json:"service_address"`
	Username       string     `json:"username"`
	HasCredentials bool       `json:"has_credentials"`
	Active         bool       `json:"active"`
	NextScrapeAt   *time.Time `json:"next_scrape_at"`
	LastRunAt      *time.Time `json:"last_run_at"`
	Failures       int        `json:"consecutive_failures"`
	CreatedAt      time.Time  `json:"created_at"`
}

func (m *Module) listAccounts(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	q := `
		SELECT ua.id, ua.property_id, pr.name, ua.provider_id, p.code, p.display_name,
		       ua.account_number, coalesce(ua.service_address, ''), coalesce(ua.username, ''),
		       ua.credential_ciphertext IS NOT NULL, ua.active,
		       ua.next_scheduled_scrape_at, ua.last_scheduled_run_at,
		       ua.consecutive_scrape_failures, ua.created_at
		FROM utility_accounts ua
		JOIN providers p ON p.id = ua.provider_id
		JOIN properties pr ON pr.id = ua.property_id
		WHERE ua.org_id = $1`
	args := []any{orgID}
	if pid := httpx.QueryInt64(r, "property_id"); pid != nil {
		q += ` AND ua.property_id = $2`
		args = append(args, *pid)
	}
	q += ` ORDER BY pr.name, p.display_name`

	rows, err := m.q(r.Context()).Query(r.Context(), q, args...)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()
	out := []accountOut{}
	for rows.Next() {
		var a accountOut
		if err := rows.Scan(&a.ID, &a.PropertyID, &a.PropertyName, &a.ProviderID, &a.ProviderCode,
			&a.ProviderName, &a.AccountNumber, &a.ServiceAddress, &a.Username,
			&a.HasCredentials, &a.Active, &a.NextScrapeAt, &a.LastRunAt,
			&a.Failures, &a.CreatedAt); err != nil {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
		out = append(out, a)
	}
	httpx.JSON(w, http.StatusOK, out)
}

type accountReq struct {
	PropertyID     int64          `json:"property_id"`
	ProviderID     int64          `json:"provider_id"`
	AccountNumber  string         `json:"account_number"`
	ServiceAddress string         `json:"service_address"`
	Username       string         `json:"username"`
	Password       string         `json:"password"`   // empty = manual-only (or keep existing on update)
	SecAnswer      string         `json:"sec_answer"` // e.g. fios security question
	Active         *bool          `json:"active"`
	Metadata       map[string]any `json:"metadata"`
}

func (m *Module) createAccount(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	var req accountReq
	if err := httpx.Decode(r, &req); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	if req.PropertyID == 0 || req.ProviderID == 0 || strings.TrimSpace(req.AccountNumber) == "" {
		httpx.Error(w, http.StatusBadRequest, "property_id, provider_id and account_number are required")
		return
	}

	// The property must belong to this org.
	var ok bool
	if err := m.q(r.Context()).QueryRow(r.Context(),
		`SELECT true FROM properties WHERE id = $1 AND org_id = $2`, req.PropertyID, orgID,
	).Scan(&ok); errors.Is(err, pgx.ErrNoRows) {
		httpx.Error(w, http.StatusNotFound, "property not found")
		return
	}

	var ciphertext, nonce []byte
	if req.Password != "" {
		secrets := map[string]string{"password": req.Password}
		if req.SecAnswer != "" {
			secrets["sec_answer"] = req.SecAnswer
		}
		var err error
		ciphertext, nonce, err = m.encryptSecrets(secrets)
		if err != nil {
			httpx.Error(w, http.StatusInternalServerError, "credential encryption failed: "+err.Error())
			return
		}
	}

	metaJSON := []byte("{}")
	if req.Metadata != nil {
		metaJSON, _ = json.Marshal(req.Metadata)
	}
	active := true
	if req.Active != nil {
		active = *req.Active
	}

	// Accounts with credentials get next_scheduled_scrape_at = now so the
	// next scheduler tick picks them up.
	var id int64
	err := m.q(r.Context()).QueryRow(r.Context(), `
		INSERT INTO utility_accounts
			(org_id, property_id, provider_id, account_number, service_address, username,
			 credential_ciphertext, credential_nonce, active, metadata, next_scheduled_scrape_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
		        CASE WHEN $7::bytea IS NOT NULL THEN now() ELSE NULL END)
		RETURNING id
	`, orgID, req.PropertyID, req.ProviderID, strings.TrimSpace(req.AccountNumber),
		nullIfEmpty(req.ServiceAddress), nullIfEmpty(req.Username),
		ciphertext, nonce, active, string(metaJSON)).Scan(&id)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{"id": id})
}

func (m *Module) updateAccount(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	var req accountReq
	if err := httpx.Decode(r, &req); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}

	active := true
	if req.Active != nil {
		active = *req.Active
	}
	tag, err := m.q(r.Context()).Exec(r.Context(), `
		UPDATE utility_accounts
		SET account_number = coalesce(nullif($1, ''), account_number),
		    service_address = $2,
		    username = $3,
		    active = $4
		WHERE id = $5 AND org_id = $6
	`, strings.TrimSpace(req.AccountNumber), nullIfEmpty(req.ServiceAddress),
		nullIfEmpty(req.Username), active, id, orgID)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "account not found")
		return
	}

	// Password provided → re-encrypt and reset the scrape schedule.
	if req.Password != "" {
		secrets := map[string]string{"password": req.Password}
		if req.SecAnswer != "" {
			secrets["sec_answer"] = req.SecAnswer
		}
		ciphertext, nonce, err := m.encryptSecrets(secrets)
		if err != nil {
			httpx.Error(w, http.StatusInternalServerError, "credential encryption failed: "+err.Error())
			return
		}
		if _, err := m.q(r.Context()).Exec(r.Context(), `
			UPDATE utility_accounts
			SET credential_ciphertext = $1, credential_nonce = $2,
			    consecutive_scrape_failures = 0,
			    next_scheduled_scrape_at = coalesce(next_scheduled_scrape_at, now())
			WHERE id = $3 AND org_id = $4
		`, ciphertext, nonce, id, orgID); err != nil {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (m *Module) deleteAccount(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	tag, err := m.q(r.Context()).Exec(r.Context(),
		`DELETE FROM utility_accounts WHERE id = $1 AND org_id = $2`, id, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "account not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}

// -------- scrape jobs --------

type triggerReq struct {
	UtilityAccountID int64  `json:"utility_account_id"`
	Period           string `json:"period"`
}

func (m *Module) triggerScrape(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	var req triggerReq
	if err := httpx.Decode(r, &req); err != nil || req.UtilityAccountID == 0 {
		httpx.Error(w, http.StatusBadRequest, "utility_account_id is required")
		return
	}

	// Account must belong to the org and have credentials.
	var hasCreds bool
	err := m.q(r.Context()).QueryRow(r.Context(), `
		SELECT credential_ciphertext IS NOT NULL FROM utility_accounts
		WHERE id = $1 AND org_id = $2
	`, req.UtilityAccountID, orgID).Scan(&hasCreds)
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.Error(w, http.StatusNotFound, "account not found")
		return
	}
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if !hasCreds {
		httpx.Error(w, http.StatusBadRequest, "account has no stored credentials (manual-only); upload the bill PDF instead")
		return
	}

	// Skip if a job is already in flight for this account.
	var inflight bool
	_ = m.q(r.Context()).QueryRow(r.Context(), `
		SELECT EXISTS (SELECT 1 FROM scrape_jobs
			WHERE utility_account_id = $1 AND status IN ('queued','running'))
	`, req.UtilityAccountID).Scan(&inflight)
	if inflight {
		httpx.Error(w, http.StatusConflict, "a scrape job is already queued or running for this account")
		return
	}

	jobID, err := m.EnqueueJob(r.Context(), orgID, req.UtilityAccountID, req.Period, "manual")
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusCreated, map[string]any{"job_id": jobID, "status": "queued"})
}

type jobOut struct {
	ID            int64      `json:"id"`
	AccountID     int64      `json:"utility_account_id"`
	AccountNumber string     `json:"account_number"`
	ProviderCode  string     `json:"provider_code"`
	ProviderName  string     `json:"provider_name"`
	PropertyName  string     `json:"property_name"`
	Status        string     `json:"status"`
	Attempt       int        `json:"attempt"`
	RequestedBy   string     `json:"requested_by"`
	RequestedAt   time.Time  `json:"requested_at"`
	StartedAt     *time.Time `json:"started_at"`
	FinishedAt    *time.Time `json:"finished_at"`
	ErrorMessage  string     `json:"error_message"`
}

const jobSelect = `
	SELECT sj.id, sj.utility_account_id, ua.account_number, p.code, p.display_name, pr.name,
	       sj.status, sj.attempt, coalesce(sj.requested_by, ''), sj.requested_at,
	       sj.started_at, sj.finished_at, coalesce(sj.error_message, '')
	FROM scrape_jobs sj
	JOIN utility_accounts ua ON ua.id = sj.utility_account_id
	JOIN providers p ON p.id = ua.provider_id
	JOIN properties pr ON pr.id = ua.property_id`

func scanJob(row pgx.Row) (jobOut, error) {
	var j jobOut
	err := row.Scan(&j.ID, &j.AccountID, &j.AccountNumber, &j.ProviderCode, &j.ProviderName,
		&j.PropertyName, &j.Status, &j.Attempt, &j.RequestedBy, &j.RequestedAt,
		&j.StartedAt, &j.FinishedAt, &j.ErrorMessage)
	return j, err
}

func (m *Module) listJobs(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	rows, err := m.q(r.Context()).Query(r.Context(),
		jobSelect+` WHERE sj.org_id = $1 ORDER BY sj.requested_at DESC LIMIT 100`, orgID)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()
	out := []jobOut{}
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
		out = append(out, j)
	}
	httpx.JSON(w, http.StatusOK, out)
}

func (m *Module) getJob(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	j, err := scanJob(m.q(r.Context()).QueryRow(r.Context(),
		jobSelect+` WHERE sj.id = $1 AND sj.org_id = $2`, id, middleware.OrgID(r.Context())))
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.Error(w, http.StatusNotFound, "job not found")
		return
	}
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, j)
}

func (m *Module) runSchedulerNow(w http.ResponseWriter, r *http.Request) {
	n := m.SchedulerRunOnce(r.Context())
	httpx.JSON(w, http.StatusOK, map[string]any{"enqueued": n})
}

func nullIfEmpty(s string) any {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return strings.TrimSpace(s)
}
