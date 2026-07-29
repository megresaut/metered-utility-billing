// Package middleware: JWT auth. Every authenticated request resolves org_id
// into the request context; repositories filter WHERE org_id = $1 off it.
package middleware

import (
	"context"
	"net/http"
	"strings"

	"github.com/golang-jwt/jwt/v5"

	"ubp/httpx"
)

type ctxKey string

const (
	ctxOrgID  ctxKey = "org_id"
	ctxUserID ctxKey = "user_id"
	ctxRole   ctxKey = "role"
)

// AudienceDownload marks a short-lived token that may appear in a URL query
// (?token=) for PDF/CSV links that can't set an Authorization header. Full
// session tokens carry no audience and are rejected in the query string, so a
// long-lived credential never lands in logs, history, or the Referer header.
const AudienceDownload = "download"

type Claims struct {
	OrgID int64  `json:"org_id"`
	Role  string `json:"role"`
	Email string `json:"email"`
	jwt.RegisteredClaims
}

func (c *Claims) hasAudience(want string) bool {
	for _, a := range c.Audience {
		if a == want {
			return true
		}
	}
	return false
}

// Auth validates the bearer token (or ?token= fallback for download links) and
// stashes org/user in context. A token presented via ?token= MUST be a
// short-lived download-scoped token; session tokens are accepted only via the
// Authorization header.
func Auth(secret string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := ""
		fromQuery := false
		if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
			raw = strings.TrimPrefix(h, "Bearer ")
		} else if q := r.URL.Query().Get("token"); q != "" {
			raw = q
			fromQuery = true
		}
		if raw == "" {
			httpx.Error(w, http.StatusUnauthorized, "missing bearer token")
			return
		}

		claims := &Claims{}
		tok, err := jwt.ParseWithClaims(raw, claims, func(t *jwt.Token) (any, error) {
			if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, jwt.ErrSignatureInvalid
			}
			return []byte(secret), nil
		})
		if err != nil || !tok.Valid || claims.OrgID == 0 {
			httpx.Error(w, http.StatusUnauthorized, "invalid token")
			return
		}
		// A session token must never be accepted from the URL query, and a
		// download token must never grant full API access via the header.
		if fromQuery != claims.hasAudience(AudienceDownload) {
			httpx.Error(w, http.StatusUnauthorized, "invalid token")
			return
		}

		ctx := context.WithValue(r.Context(), ctxOrgID, claims.OrgID)
		ctx = context.WithValue(ctx, ctxUserID, claims.Subject)
		ctx = context.WithValue(ctx, ctxRole, claims.Role)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// OrgID returns the org resolved by Auth. Zero means unauthenticated (never
// the case behind Auth).
func OrgID(ctx context.Context) int64 {
	if v, ok := ctx.Value(ctxOrgID).(int64); ok {
		return v
	}
	return 0
}

// UserID returns the authenticated user id (JWT subject) from context.
func UserID(ctx context.Context) string {
	if v, ok := ctx.Value(ctxUserID).(string); ok {
		return v
	}
	return ""
}

func Role(ctx context.Context) string {
	if v, ok := ctx.Value(ctxRole).(string); ok {
		return v
	}
	return ""
}

// CORS reflects an Origin only when it is on the configured allowlist, instead
// of echoing any Origin. Pass the dev server origin in development and the
// deployed frontend origin(s) in production (CORS_ALLOWED_ORIGINS).
func CORS(allowed []string, next http.Handler) http.Handler {
	allow := make(map[string]bool, len(allowed))
	for _, o := range allowed {
		allow[o] = true
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && allow[origin] {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Vary", "Origin")
			w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
