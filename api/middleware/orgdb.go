package middleware

import (
	"context"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"ubp/db"
	"ubp/httpx"
)

// OrgDB pins a connection for the request and sets the app.org_id GUC on it to
// the authenticated org, so Postgres RLS restricts every query to that org
// (defense-in-depth behind the app-layer WHERE org_id filters). It must run
// AFTER Auth (which resolves org_id into context). The connection is reset and
// released when the request completes.
func OrgDB(pool *pgxpool.Pool, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		orgID := OrgID(r.Context())
		if orgID == 0 {
			httpx.Error(w, http.StatusUnauthorized, "invalid token")
			return
		}

		conn, err := pool.Acquire(r.Context())
		if err != nil {
			httpx.Error(w, http.StatusServiceUnavailable, "database unavailable")
			return
		}
		defer conn.Release()

		if _, err := conn.Exec(r.Context(),
			"SELECT set_config('app.org_id', $1, false)",
			strconv.FormatInt(orgID, 10)); err != nil {
			httpx.Error(w, http.StatusServiceUnavailable, "database unavailable")
			return
		}
		// Reset the GUC before the connection returns to the pool, so a pooled
		// connection can never carry one request's org into another's.
		defer func() {
			rctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			_, _ = conn.Exec(rctx, "SELECT set_config('app.org_id', '', false)")
		}()

		ctx := db.WithExecutor(r.Context(), conn)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}
