package middleware

import (
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// RateLimiter is a simple fixed-window, in-memory limiter keyed by an arbitrary
// string. It is sufficient for a single-instance beta (login brute-force
// defense); a multi-instance deployment would move this to a shared store.
type RateLimiter struct {
	mu     sync.Mutex
	hits   map[string]*window
	max    int
	period time.Duration
}

type window struct {
	count int
	reset time.Time
}

func NewRateLimiter(max int, period time.Duration) *RateLimiter {
	return &RateLimiter{hits: make(map[string]*window), max: max, period: period}
}

// Allow records an attempt for key and reports whether it is within the limit.
func (rl *RateLimiter) Allow(key string) bool {
	now := time.Now()
	rl.mu.Lock()
	defer rl.mu.Unlock()

	w := rl.hits[key]
	if w == nil || now.After(w.reset) {
		rl.hits[key] = &window{count: 1, reset: now.Add(rl.period)}
		rl.sweep(now)
		return true
	}
	w.count++
	return w.count <= rl.max
}

// sweep drops expired windows so the map can't grow unbounded. Called while the
// lock is held, on the (relatively rare) new-window path.
func (rl *RateLimiter) sweep(now time.Time) {
	for k, w := range rl.hits {
		if now.After(w.reset) {
			delete(rl.hits, k)
		}
	}
}

// ClientIP returns the best-effort client IP, honoring X-Forwarded-For (the
// first hop) since the API runs behind a platform proxy in production.
func ClientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if first := strings.TrimSpace(strings.SplitN(xff, ",", 2)[0]); first != "" {
			return first
		}
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}
