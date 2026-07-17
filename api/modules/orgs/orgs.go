// Package orgs: organizations, org_users, minimal auth. Login only — org and
// user creation is admin/CLI-driven (cmd/admin), no self-serve signup.
package orgs

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"

	"ubp/httpx"
	"ubp/middleware"
)

type Module struct {
	db        *pgxpool.Pool
	jwtSecret string
}

func New(db *pgxpool.Pool, jwtSecret string) *Module {
	return &Module{db: db, jwtSecret: jwtSecret}
}

func (m *Module) Routes(mux *http.ServeMux, authed func(http.HandlerFunc) http.Handler) {
	mux.HandleFunc("POST /api/login", m.login)
	mux.Handle("GET /api/me", authed(m.me))
	mux.Handle("GET /api/org-users", authed(m.listUsers))
}

type loginReq struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

func (m *Module) login(w http.ResponseWriter, r *http.Request) {
	var req loginReq
	if err := httpx.Decode(r, &req); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid body")
		return
	}
	req.Email = strings.ToLower(strings.TrimSpace(req.Email))

	var (
		userID  int64
		orgID   int64
		hash    string
		role    string
		orgName string
	)
	err := m.db.QueryRow(r.Context(), `
		SELECT u.id, u.org_id, u.password_hash, u.role, o.name
		FROM org_users u JOIN organizations o ON o.id = u.org_id
		WHERE u.email = $1
	`, req.Email).Scan(&userID, &orgID, &hash, &role, &orgName)
	if errors.Is(err, pgx.ErrNoRows) ||
		(err == nil && bcrypt.CompareHashAndPassword([]byte(hash), []byte(req.Password)) != nil) {
		httpx.Error(w, http.StatusUnauthorized, "invalid email or password")
		return
	}
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, "login failed")
		return
	}

	claims := middleware.Claims{
		OrgID: orgID,
		Role:  role,
		Email: req.Email,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   itoa(userID),
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(7 * 24 * time.Hour)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
		},
	}
	tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(m.jwtSecret))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, "token signing failed")
		return
	}

	httpx.JSON(w, http.StatusOK, map[string]any{
		"token": tok,
		"user": map[string]any{
			"id": userID, "email": req.Email, "role": role,
			"org": map[string]any{"id": orgID, "name": orgName},
		},
	})
}

func (m *Module) me(w http.ResponseWriter, r *http.Request) {
	orgID := middleware.OrgID(r.Context())
	var name string
	if err := m.db.QueryRow(r.Context(), `SELECT name FROM organizations WHERE id = $1`, orgID).Scan(&name); err != nil {
		httpx.Error(w, http.StatusNotFound, "org not found")
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"org":  map[string]any{"id": orgID, "name": name},
		"role": middleware.Role(r.Context()),
	})
}

func (m *Module) listUsers(w http.ResponseWriter, r *http.Request) {
	rows, err := m.db.Query(r.Context(), `
		SELECT id, email, role, created_at FROM org_users
		WHERE org_id = $1 ORDER BY created_at, id
	`, middleware.OrgID(r.Context()))
	if err != nil {
		httpx.Error(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()
	type userOut struct {
		ID        int64     `json:"id"`
		Email     string    `json:"email"`
		Role      string    `json:"role"`
		CreatedAt time.Time `json:"created_at"`
	}
	out := []userOut{}
	for rows.Next() {
		var u userOut
		if err := rows.Scan(&u.ID, &u.Email, &u.Role, &u.CreatedAt); err == nil {
			out = append(out, u)
		}
	}
	httpx.JSON(w, http.StatusOK, out)
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
