package utilities

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"ubp/httpx"
	"ubp/middleware"
)

func (m *Module) billRoutes(mux *http.ServeMux, authed func(http.HandlerFunc) http.Handler) {
	mux.Handle("GET /api/bills", authed(m.listBills))
	mux.Handle("GET /api/bills/summary", authed(m.billsSummary))
	mux.Handle("GET /api/bills/{id}", authed(m.getBill))
	mux.Handle("GET /api/bills/{id}/pdf", authed(m.servePDF))
	mux.Handle("PATCH /api/bills/{id}", authed(m.patchBill))
	mux.Handle("DELETE /api/bills/{id}", authed(m.deleteBill))
	mux.Handle("POST /api/bills/upload", authed(m.uploadBill))
}

type billOut struct {
	ID              int64      `json:"id"`
	PropertyID      *int64     `json:"property_id"`
	PropertyName    string     `json:"property_name"`
	AccountID       *int64     `json:"utility_account_id"`
	AccountNumber   string     `json:"account_number"`
	ProviderCode    string     `json:"provider_code"`
	VendorName      string     `json:"vendor_name"`
	Category        string     `json:"category"`
	AmountCents     int64      `json:"amount_cents"`
	StatementDate   *time.Time `json:"statement_date"`
	DueDate         *time.Time `json:"due_date"`
	ServiceStart    *time.Time `json:"service_start"`
	ServiceEnd      *time.Time `json:"service_end"`
	Status          string     `json:"status"` // outstanding | overdue | paid (overdue derived)
	Source          string     `json:"source"`
	ParseConfidence *int       `json:"parse_confidence"`
	CreatedAt       time.Time  `json:"created_at"`
}

const billSelect = `
	SELECT b.id, b.property_id, coalesce(pr.name, ''), b.utility_account_id,
	       coalesce(ua.account_number, ''), coalesce(p.code, ''),
	       coalesce(b.vendor_name, coalesce(p.display_name, '')), coalesce(p.category, ''),
	       b.amount_cents, b.statement_date, b.due_date, b.service_start, b.service_end,
	       CASE WHEN b.status = 'outstanding' AND b.due_date IS NOT NULL AND b.due_date < current_date
	            THEN 'overdue' ELSE b.status END,
	       b.source, b.parse_confidence, b.created_at
	FROM bills b
	LEFT JOIN properties pr ON pr.id = b.property_id
	LEFT JOIN utility_accounts ua ON ua.id = b.utility_account_id
	LEFT JOIN providers p ON p.id = b.provider_id`

func scanBill(row pgx.Row) (billOut, error) {
	var b billOut
	err := row.Scan(&b.ID, &b.PropertyID, &b.PropertyName, &b.AccountID, &b.AccountNumber,
		&b.ProviderCode, &b.VendorName, &b.Category, &b.AmountCents, &b.StatementDate,
		&b.DueDate, &b.ServiceStart, &b.ServiceEnd, &b.Status, &b.Source,
		&b.ParseConfidence, &b.CreatedAt)
	return b, err
}

func (m *Module) listBills(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	q := billSelect + ` WHERE b.org_id = $1`
	args := []any{orgID}

	if pid := httpx.QueryInt64(r, "property_id"); pid != nil {
		args = append(args, *pid)
		q += fmt.Sprintf(` AND b.property_id = $%d`, len(args))
	}
	switch r.URL.Query().Get("status") {
	case "outstanding":
		q += ` AND b.status = 'outstanding' AND (b.due_date IS NULL OR b.due_date >= current_date)`
	case "overdue":
		q += ` AND b.status = 'outstanding' AND b.due_date IS NOT NULL AND b.due_date < current_date`
	case "paid":
		q += ` AND b.status = 'paid'`
	}
	q += ` ORDER BY coalesce(b.statement_date, b.created_at::date) DESC, b.id DESC LIMIT 500`

	rows, err := m.db.Query(r.Context(), q, args...)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()
	out := []billOut{}
	for rows.Next() {
		b, err := scanBill(rows)
		if err != nil {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
		out = append(out, b)
	}
	httpx.JSON(w, http.StatusOK, out)
}

// billsSummary powers the dashboard header cards and per-property monthly
// spend chart.
func (m *Module) billsSummary(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())

	var outstanding, overdue, paid, total struct {
		Count int64
		Cents int64
	}
	err := m.db.QueryRow(r.Context(), `
		SELECT
		  count(*) FILTER (WHERE status = 'outstanding' AND (due_date IS NULL OR due_date >= current_date)),
		  coalesce(sum(amount_cents) FILTER (WHERE status = 'outstanding' AND (due_date IS NULL OR due_date >= current_date)), 0),
		  count(*) FILTER (WHERE status = 'outstanding' AND due_date < current_date),
		  coalesce(sum(amount_cents) FILTER (WHERE status = 'outstanding' AND due_date < current_date), 0),
		  count(*) FILTER (WHERE status = 'paid'),
		  coalesce(sum(amount_cents) FILTER (WHERE status = 'paid'), 0),
		  count(*),
		  coalesce(sum(amount_cents), 0)
		FROM bills WHERE org_id = $1
	`, orgID).Scan(&outstanding.Count, &outstanding.Cents, &overdue.Count, &overdue.Cents,
		&paid.Count, &paid.Cents, &total.Count, &total.Cents)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}

	// Monthly spend per property over the last 12 months (by statement date,
	// falling back to created_at for undated bills).
	type spendRow struct {
		Month        string `json:"month"`
		PropertyID   *int64 `json:"property_id"`
		PropertyName string `json:"property_name"`
		Cents        int64  `json:"cents"`
	}
	spend := []spendRow{}
	rows, err := m.db.Query(r.Context(), `
		SELECT to_char(date_trunc('month', coalesce(b.statement_date, b.created_at::date)), 'YYYY-MM'),
		       b.property_id, coalesce(pr.name, 'Unassigned'), sum(b.amount_cents)
		FROM bills b
		LEFT JOIN properties pr ON pr.id = b.property_id
		WHERE b.org_id = $1
		  AND coalesce(b.statement_date, b.created_at::date) >= date_trunc('month', current_date) - INTERVAL '11 months'
		GROUP BY 1, 2, 3
		ORDER BY 1
	`, orgID)
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var s spendRow
			if err := rows.Scan(&s.Month, &s.PropertyID, &s.PropertyName, &s.Cents); err == nil {
				spend = append(spend, s)
			}
		}
	}

	httpx.JSON(w, http.StatusOK, map[string]any{
		"outstanding": map[string]int64{"count": outstanding.Count, "cents": outstanding.Cents},
		"overdue":     map[string]int64{"count": overdue.Count, "cents": overdue.Cents},
		"paid":        map[string]int64{"count": paid.Count, "cents": paid.Cents},
		"total":       map[string]int64{"count": total.Count, "cents": total.Cents},
		"monthly_spend": spend,
	})
}

func (m *Module) getBill(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	b, err := scanBill(m.db.QueryRow(r.Context(),
		billSelect+` WHERE b.id = $1 AND b.org_id = $2`, id, middleware.OrgID(r.Context())))
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.Error(w, http.StatusNotFound, "bill not found")
		return
	}
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusOK, b)
}

func (m *Module) servePDF(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	var objKey string
	err = m.db.QueryRow(r.Context(),
		`SELECT pdf_object_key FROM bills WHERE id = $1 AND org_id = $2`,
		id, middleware.OrgID(r.Context())).Scan(&objKey)
	if errors.Is(err, pgx.ErrNoRows) {
		httpx.Error(w, http.StatusNotFound, "bill not found")
		return
	}
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	path := filepath.Join(m.cfg.StoreRoot, filepath.FromSlash(objKey))
	// Guard against traversal via a tampered object key.
	if rel, err := filepath.Rel(m.cfg.StoreRoot, path); err != nil || strings.HasPrefix(rel, "..") {
		httpx.Error(w, http.StatusBadRequest, "bad object key")
		return
	}
	w.Header().Set("Content-Type", "application/pdf")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`inline; filename="bill-%d.pdf"`, id))
	http.ServeFile(w, r, path)
}

type patchBillReq struct {
	Status *string `json:"status"` // outstanding | paid
}

func (m *Module) patchBill(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	var req patchBillReq
	if err := httpx.Decode(r, &req); err != nil || req.Status == nil {
		httpx.Error(w, http.StatusBadRequest, "status is required")
		return
	}
	if *req.Status != "outstanding" && *req.Status != "paid" {
		httpx.Error(w, http.StatusBadRequest, "status must be outstanding or paid")
		return
	}
	tag, err := m.db.Exec(r.Context(),
		`UPDATE bills SET status = $1 WHERE id = $2 AND org_id = $3`,
		*req.Status, id, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "bill not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (m *Module) deleteBill(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	tag, err := m.db.Exec(r.Context(),
		`DELETE FROM bills WHERE id = $1 AND org_id = $2`, id, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "bill not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}

// -------- manual PDF upload → AI extraction --------

// parsedInvoice mirrors scrapers/parsers/invoice_parser.py output.
type parsedInvoice struct {
	VendorRaw       string  `json:"vendor_raw"`
	AddressRaw      string  `json:"address_raw"`
	InvoiceNumber   *string `json:"invoice_number"`
	AmountCents     int64   `json:"amount_cents"`
	StatementDate   *string `json:"statement_date"`
	DueDate         *string `json:"due_date"`
	ServiceStart    *string `json:"service_start"`
	ServiceEnd      *string `json:"service_end"`
	ParseConfidence int     `json:"parse_confidence"`
	Error           string  `json:"error"`
}

func (m *Module) uploadBill(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())

	if err := r.ParseMultipartForm(32 << 20); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid multipart form")
		return
	}
	file, hdr, err := r.FormFile("file")
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "file field is required")
		return
	}
	defer file.Close()
	if !strings.HasSuffix(strings.ToLower(hdr.Filename), ".pdf") {
		httpx.Error(w, http.StatusBadRequest, "only PDF files are supported")
		return
	}

	var propertyID *int64
	if pid := httpx.QueryInt64(r, "property_id"); pid != nil {
		propertyID = pid
	} else if s := r.FormValue("property_id"); s != "" {
		if pid, err := parseInt64(s); err == nil {
			propertyID = &pid
		}
	}
	if propertyID != nil {
		var ok bool
		if err := m.db.QueryRow(r.Context(),
			`SELECT true FROM properties WHERE id = $1 AND org_id = $2`, *propertyID, orgID,
		).Scan(&ok); errors.Is(err, pgx.ErrNoRows) {
			httpx.Error(w, http.StatusNotFound, "property not found")
			return
		}
	}

	// Optional link to a utility account (sets provider + property).
	var accountID, providerID *int64
	if s := r.FormValue("utility_account_id"); s != "" {
		aid, err := parseInt64(s)
		if err == nil {
			var prov, prop int64
			err := m.db.QueryRow(r.Context(),
				`SELECT provider_id, property_id FROM utility_accounts WHERE id = $1 AND org_id = $2`,
				aid, orgID).Scan(&prov, &prop)
			if errors.Is(err, pgx.ErrNoRows) {
				httpx.Error(w, http.StatusNotFound, "utility account not found")
				return
			}
			if err == nil {
				accountID, providerID = &aid, &prov
				if propertyID == nil {
					propertyID = &prop
				}
			}
		}
	}

	// Store the PDF: uploads/<org>/<YYYY/MM>/<random>.pdf
	now := time.Now().UTC()
	rnd := make([]byte, 8)
	_, _ = rand.Read(rnd)
	objKey := filepath.ToSlash(filepath.Join("uploads",
		fmt.Sprintf("%d", orgID), now.Format("2006/01"), hex.EncodeToString(rnd)+".pdf"))
	dstPath := filepath.Join(m.cfg.StoreRoot, filepath.FromSlash(objKey))
	if err := os.MkdirAll(filepath.Dir(dstPath), 0o755); err != nil {
		httpx.Error(w, http.StatusInternalServerError, "store dir: "+err.Error())
		return
	}
	dst, err := os.Create(dstPath)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, "store file: "+err.Error())
		return
	}
	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(dst, h), file); err != nil {
		dst.Close()
		httpx.Error(w, http.StatusInternalServerError, "write file: "+err.Error())
		return
	}
	dst.Close()
	sha := hex.EncodeToString(h.Sum(nil))

	// AI extraction via the copied invoice parser.
	parsed, err := m.runInvoiceParser(r.Context(), dstPath)
	if err != nil {
		_ = os.Remove(dstPath)
		httpx.Error(w, http.StatusUnprocessableEntity, "extraction failed: "+err.Error())
		return
	}

	vendor := strings.TrimSpace(parsed.VendorRaw)
	if vendor == "" {
		vendor = "Unknown vendor"
	}

	var billID int64
	err = m.db.QueryRow(r.Context(), `
		INSERT INTO bills
			(org_id, utility_account_id, provider_id, property_id, vendor_name,
			 amount_cents, statement_date, due_date, service_start, service_end,
			 status, source, pdf_object_key, sha256_pdf, statement_id, parse_confidence)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'outstanding','upload',$11,$12,$13,$14)
		RETURNING id
	`, orgID, accountID, providerID, propertyID, vendor,
		parsed.AmountCents, dateOrNil(parsed.StatementDate), dateOrNil(parsed.DueDate),
		dateOrNil(parsed.ServiceStart), dateOrNil(parsed.ServiceEnd),
		objKey, sha, parsed.InvoiceNumber, parsed.ParseConfidence).Scan(&billID)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}

	b, err := scanBill(m.db.QueryRow(r.Context(),
		billSelect+` WHERE b.id = $1 AND b.org_id = $2`, billID, orgID))
	if err != nil {
		httpx.JSON(w, http.StatusCreated, map[string]any{"id": billID})
		return
	}
	httpx.JSON(w, http.StatusCreated, b)
}

// runInvoiceParser shells out to the copied Claude-based parser:
// python parsers/invoice_parser.py <pdf> → JSON on stdout.
func (m *Module) runInvoiceParser(ctx context.Context, pdfPath string) (*parsedInvoice, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()

	cmd := exec.CommandContext(ctx, m.pythonBin(),
		filepath.Join("parsers", "invoice_parser.py"), pdfPath)
	cmd.Dir = m.cfg.ScrapersDir
	cmd.Env = m.pyEnv(m.cfg.ScrapersDir)

	out, err := cmd.CombinedOutput()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("invoice parser timeout")
	}
	if err != nil {
		return nil, fmt.Errorf("invoice parser: %v\n%s", err, tail(out))
	}

	jsonBytes, err := extractJSONObject(out)
	if err != nil {
		return nil, fmt.Errorf("invoice parser produced no JSON: %s", snippet(out))
	}
	var p parsedInvoice
	if err := json.Unmarshal(jsonBytes, &p); err != nil {
		return nil, fmt.Errorf("invoice parser JSON decode: %v\n%s", err, snippet(jsonBytes))
	}
	if p.Error != "" {
		return nil, fmt.Errorf("invoice parser error: %s", p.Error)
	}
	if p.AmountCents <= 0 {
		return nil, fmt.Errorf("could not extract a positive amount from the PDF")
	}
	return &p, nil
}

func dateOrNil(s *string) any {
	if s == nil || strings.TrimSpace(*s) == "" {
		return nil
	}
	t, err := time.Parse("2006-01-02", strings.TrimSpace(*s))
	if err != nil {
		return nil
	}
	return t
}

func parseInt64(s string) (int64, error) {
	var n int64
	_, err := fmt.Sscanf(strings.TrimSpace(s), "%d", &n)
	return n, err
}
