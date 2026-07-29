// Package properties: thin model — org_id, name, address.
package properties

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"ubp/db"
	"ubp/httpx"
	"ubp/middleware"
)

type Module struct{ db *pgxpool.Pool }

func New(db *pgxpool.Pool) *Module { return &Module{db: db} }

// q returns the org-pinned request connection when present, else the app pool.
func (m *Module) q(ctx context.Context) db.Querier {
	if e := db.FromContext(ctx); e != nil {
		return e
	}
	return m.db
}

func (m *Module) Routes(mux *http.ServeMux, authed func(http.HandlerFunc) http.Handler) {
	mux.Handle("GET /api/properties", authed(m.list))
	mux.Handle("POST /api/properties", authed(m.create))
	mux.Handle("PUT /api/properties/{id}", authed(m.update))
	mux.Handle("DELETE /api/properties/{id}", authed(m.remove))
}

type property struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	Address   string    `json:"address"`
	CreatedAt time.Time `json:"created_at"`

	AccountCount     int   `json:"account_count"`
	BillCount        int   `json:"bill_count"`
	OutstandingCents int64 `json:"outstanding_cents"`
}

func (m *Module) list(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	rows, err := m.q(r.Context()).Query(r.Context(), `
		SELECT p.id, p.name, coalesce(p.address, ''), p.created_at,
		       (SELECT count(*) FROM utility_accounts ua WHERE ua.property_id = p.id),
		       (SELECT count(*) FROM bills b WHERE b.property_id = p.id),
		       coalesce((SELECT sum(b.amount_cents) FROM bills b
		                 WHERE b.property_id = p.id AND b.status = 'outstanding'), 0)
		FROM properties p
		WHERE p.org_id = $1
		ORDER BY p.name
	`, orgID)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()

	out := []property{}
	for rows.Next() {
		var p property
		if err := rows.Scan(&p.ID, &p.Name, &p.Address, &p.CreatedAt,
			&p.AccountCount, &p.BillCount, &p.OutstandingCents); err != nil {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
		out = append(out, p)
	}
	httpx.JSON(w, http.StatusOK, out)
}

type propertyReq struct {
	Name    string `json:"name"`
	Address string `json:"address"`
}

func (m *Module) create(w http.ResponseWriter, r *http.Request) {
	var req propertyReq
	if err := httpx.Decode(r, &req); err != nil || strings.TrimSpace(req.Name) == "" {
		httpx.Error(w, http.StatusBadRequest, "name is required")
		return
	}
	orgID := middleware.OrgID(r.Context())
	var p property
	err := m.q(r.Context()).QueryRow(r.Context(), `
		INSERT INTO properties (org_id, name, address) VALUES ($1, $2, $3)
		RETURNING id, name, coalesce(address, ''), created_at
	`, orgID, strings.TrimSpace(req.Name), req.Address).Scan(&p.ID, &p.Name, &p.Address, &p.CreatedAt)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	httpx.JSON(w, http.StatusCreated, p)
}

func (m *Module) update(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	var req propertyReq
	if err := httpx.Decode(r, &req); err != nil || strings.TrimSpace(req.Name) == "" {
		httpx.Error(w, http.StatusBadRequest, "name is required")
		return
	}
	tag, err := m.q(r.Context()).Exec(r.Context(), `
		UPDATE properties SET name = $1, address = $2 WHERE id = $3 AND org_id = $4
	`, strings.TrimSpace(req.Name), req.Address, id, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "property not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (m *Module) remove(w http.ResponseWriter, r *http.Request) {
	id, err := httpx.PathID(r)
	if err != nil {
		httpx.Error(w, http.StatusBadRequest, "bad id")
		return
	}
	tag, err := m.q(r.Context()).Exec(r.Context(),
		`DELETE FROM properties WHERE id = $1 AND org_id = $2`, id, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	if tag.RowsAffected() == 0 {
		httpx.Error(w, http.StatusNotFound, "property not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}
