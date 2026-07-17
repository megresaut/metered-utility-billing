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

type Claims struct {
	OrgID int64  `json:"org_id"`
	Role  string `json:"role"`
	Email string `json:"email"`
	jwt.RegisteredClaims
}

// Auth validates the bearer token (or ?token= fallback, used by iframe/anchor
// PDF and CSV links that can't set headers) and stashes org/user in context.
func Auth(secret string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := ""
		if h := r.Header.Get("Authorization"); strings.HasPrefix(h, "Bearer ") {
			raw = strings.TrimPrefix(h, "Bearer ")
		} else if q := r.URL.Query().Get("token"); q != "" {
			raw = q
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

func Role(ctx context.Context) string {
	if v, ok := ctx.Value(ctxRole).(string); ok {
		return v
	}
	return ""
}

// CORS: the Vite dev server runs on :5174; allow it (and same-origin prod).
func CORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" {
			w.Header().Set("Access-Control-Allow-Origin", origin)
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
