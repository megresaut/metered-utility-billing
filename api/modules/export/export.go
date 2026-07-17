// Package export: CSV bulk-export of bills — human-reviewable and
// bulk-import-ready (vendor, amount, dates, property, account number).
// Amounts are formatted in dollars for the file; storage stays in cents.
package export

import (
	"encoding/csv"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"ubp/httpx"
	"ubp/middleware"
)

type Module struct{ db *pgxpool.Pool }

func New(db *pgxpool.Pool) *Module { return &Module{db: db} }

func (m *Module) Routes(mux *http.ServeMux, authed func(http.HandlerFunc) http.Handler) {
	mux.Handle("GET /api/export/csv", authed(m.exportCSV))
}

func (m *Module) exportCSV(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())

	q := `
		SELECT coalesce(b.vendor_name, coalesce(p.display_name, '')),
		       b.amount_cents, b.statement_date, b.due_date, b.service_start, b.service_end,
		       coalesce(pr.name, ''), coalesce(ua.account_number, ''),
		       CASE WHEN b.status = 'outstanding' AND b.due_date IS NOT NULL AND b.due_date < current_date
		            THEN 'overdue' ELSE b.status END,
		       b.source, coalesce(p.category, '')
		FROM bills b
		LEFT JOIN properties pr ON pr.id = b.property_id
		LEFT JOIN utility_accounts ua ON ua.id = b.utility_account_id
		LEFT JOIN providers p ON p.id = b.provider_id
		WHERE b.org_id = $1`
	args := []any{orgID}
	if pid := httpx.QueryInt64(r, "property_id"); pid != nil {
		args = append(args, *pid)
		q += fmt.Sprintf(` AND b.property_id = $%d`, len(args))
	}
	switch r.URL.Query().Get("status") {
	case "outstanding":
		q += ` AND b.status = 'outstanding'`
	case "paid":
		q += ` AND b.status = 'paid'`
	}
	q += ` ORDER BY coalesce(b.statement_date, b.created_at::date) DESC, b.id DESC`

	rows, err := m.db.Query(r.Context(), q, args...)
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()

	w.Header().Set("Content-Type", "text/csv")
	w.Header().Set("Content-Disposition",
		fmt.Sprintf(`attachment; filename="bills-export-%s.csv"`, time.Now().Format("2006-01-02")))

	cw := csv.NewWriter(w)
	_ = cw.Write([]string{
		"Vendor", "Amount", "Statement Date", "Due Date",
		"Service Start", "Service End", "Property", "Account Number",
		"Status", "Source", "Category",
	})

	fdate := func(t *time.Time) string {
		if t == nil {
			return ""
		}
		return t.Format("2006-01-02")
	}

	for rows.Next() {
		var (
			vendor, property, acctNum, status, source, category string
			cents                                               int64
			stmt, due, svcStart, svcEnd                         *time.Time
		)
		if err := rows.Scan(&vendor, &cents, &stmt, &due, &svcStart, &svcEnd,
			&property, &acctNum, &status, &source, &category); err != nil {
			return
		}
		_ = cw.Write([]string{
			vendor,
			fmt.Sprintf("%d.%02d", cents/100, cents%100),
			fdate(stmt), fdate(due), fdate(svcStart), fdate(svcEnd),
			property, acctNum, status, source, category,
		})
	}
	cw.Flush()
}
