// Package httpx holds tiny JSON/HTTP helpers shared by all modules.
package httpx

import (
	"encoding/json"
	"net/http"
	"strconv"
)

func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func Error(w http.ResponseWriter, status int, msg string) {
	JSON(w, status, map[string]string{"error": msg})
}

func Decode(r *http.Request, v any) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// PathID parses the {id} path value of a request.
func PathID(r *http.Request) (int64, error) {
	return strconv.ParseInt(r.PathValue("id"), 10, 64)
}

// QueryInt64 returns the query param as int64, or nil if absent/invalid.
func QueryInt64(r *http.Request, key string) *int64 {
	if s := r.URL.Query().Get(key); s != "" {
		if n, err := strconv.ParseInt(s, 10, 64); err == nil {
			return &n
		}
	}
	return nil
}
