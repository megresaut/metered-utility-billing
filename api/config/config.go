package config

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// defaultJWTSecret is the local-dev fallback. Validate() refuses to boot with
// this value when APP_ENV=production.
const defaultJWTSecret = "dev-only-jwt-secret-change-me"

type Config struct {
	// AppEnv: "development" (default) or "production". Production enables the
	// boot-time safety checks in Validate().
	AppEnv string

	Port        string
	DatabaseURL string
	JWTSecret   string

	// CORSAllowedOrigins: exact origins allowed by the CORS middleware. In dev
	// this defaults to the Vite server; in prod set CORS_ALLOWED_ORIGINS to the
	// deployed frontend origin(s), comma-separated.
	CORSAllowedOrigins []string

	// CRED_MASTER_KEY: 64 hex chars (32 bytes) for AES-256-GCM credential
	// encryption. Required to create accounts with credentials or run scrapes.
	CredMasterKey []byte

	// Files / paths
	StoreRoot   string // where bill PDFs are stored
	ScrapersDir string // root of scrapers/ (contains providers/, common/, parsers/)
	PythonBin   string // python interpreter for scrapers + invoice parser
	PyModuleBase string // "providers"
	HandoffDir  string // 2FA code handoff dir (aquarion, winwaste)

	// Worker caps
	GlobalConcurrency int
	ProviderCaps      map[string]int

	// Scheduler
	SchedulerIntervalSec int
}

// Load reads .env (if present, without overriding real env vars) and builds
// the config with local-dev defaults so a fresh checkout runs with only
// CRED_MASTER_KEY / OPENROUTER_API_KEY supplied.
func Load() (*Config, error) {
	loadDotEnv(".env")
	loadDotEnv("../.env") // when running from api/

	root := repoRoot()

	cfg := &Config{
		AppEnv:      strings.ToLower(getenv("APP_ENV", "development")),
		Port:        getenv("PORT", "8090"),
		DatabaseURL: getenv("DATABASE_URL", "postgres://localhost:5432/utility_billing_platform_local?sslmode=disable"),
		JWTSecret:   getenv("JWT_SECRET", defaultJWTSecret),

		CORSAllowedOrigins: splitList(getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5174")),

		StoreRoot:    getenv("STORE_ROOT", filepath.Join(root, "data", "store")),
		ScrapersDir:  getenv("SCRAPERS_DIR", filepath.Join(root, "scrapers")),
		PythonBin:    getenv("PYTHON_BIN", filepath.Join(root, "scrapers", ".venv", "bin", "python")),
		PyModuleBase: getenv("PY_MODULE_BASE", "providers"),
		HandoffDir:   getenv("HANDOFF_DIR", filepath.Join(root, "data", "handoff")),

		GlobalConcurrency:    getint("GLOBAL_CONCURRENCY", 2),
		SchedulerIntervalSec: getint("SCHEDULER_INTERVAL_SEC", 3600),
		ProviderCaps:         map[string]int{},
	}

	// Default: 1 concurrent job per provider (portals dislike parallel logins).
	for _, code := range []string{"aquarion", "cng", "eversource", "fdwd", "fios", "frontier",
		"optimum", "rwa", "santaguida", "scg", "snew", "starlink", "ttd", "uinet", "winwaste", "wpca"} {
		cfg.ProviderCaps[code] = 1
	}

	if hexKey := strings.TrimSpace(os.Getenv("CRED_MASTER_KEY")); hexKey != "" {
		key, err := decodeHex(hexKey)
		if err != nil || len(key) != 32 {
			return nil, fmt.Errorf("CRED_MASTER_KEY must be 64 hex chars (32 bytes): %v", err)
		}
		cfg.CredMasterKey = key
	}

	return cfg, nil
}

func (c *Config) IsProduction() bool { return c.AppEnv == "production" }

// Validate enforces production safety invariants. It is a no-op in development
// (so a fresh checkout still runs with dev defaults), but in production it
// refuses to boot on any configuration that would be unsafe on the public
// internet: a forgeable JWT secret, missing credential-encryption key, or an
// unencrypted database connection.
func (c *Config) Validate() error {
	if !c.IsProduction() {
		return nil
	}
	var problems []string
	if c.JWTSecret == defaultJWTSecret || len(c.JWTSecret) < 32 {
		problems = append(problems, "JWT_SECRET must be set to a strong value (>=32 chars, not the dev default)")
	}
	if len(c.CredMasterKey) != 32 {
		problems = append(problems, "CRED_MASTER_KEY (64 hex chars) is required in production")
	}
	if strings.Contains(c.DatabaseURL, "sslmode=disable") {
		problems = append(problems, "DATABASE_URL must not use sslmode=disable in production")
	}
	if strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY")) == "" {
		problems = append(problems, "OPENROUTER_API_KEY is required in production (PDF extraction)")
	}
	if len(c.CORSAllowedOrigins) == 0 {
		problems = append(problems, "CORS_ALLOWED_ORIGINS must list the frontend origin(s) in production")
	}
	if len(problems) > 0 {
		return fmt.Errorf("invalid production config:\n  - %s", strings.Join(problems, "\n  - "))
	}
	return nil
}

// splitList parses a comma-separated env value into trimmed, non-empty entries.
func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func decodeHex(s string) ([]byte, error) {
	if len(s)%2 != 0 {
		return nil, fmt.Errorf("odd length")
	}
	out := make([]byte, len(s)/2)
	for i := 0; i < len(out); i++ {
		v, err := strconv.ParseUint(s[i*2:i*2+2], 16, 8)
		if err != nil {
			return nil, err
		}
		out[i] = byte(v)
	}
	return out, nil
}

// repoRoot walks up from cwd looking for the migrations/ dir so the server
// works whether launched from the repo root or from api/.
func repoRoot() string {
	wd, err := os.Getwd()
	if err != nil {
		return "."
	}
	cur := wd
	for i := 0; i < 4; i++ {
		if _, err := os.Stat(filepath.Join(cur, "migrations")); err == nil {
			return cur
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			break
		}
		cur = parent
	}
	return wd
}

func (c *Config) MigrationsDir() string {
	return filepath.Join(repoRoot(), "migrations")
}

func getenv(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func getint(key string, def int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// loadDotEnv parses KEY=VALUE lines; real environment variables win.
func loadDotEnv(path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.Trim(strings.TrimSpace(v), `"'`)
		if os.Getenv(k) == "" {
			os.Setenv(k, v)
		}
	}
}
