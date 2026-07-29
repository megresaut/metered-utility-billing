// Package utilities: provider catalog, utility accounts (encrypted
// credentials), scrape-job dispatch (ported from ra-avm's worker), a simple
// DB-polled scheduler, and bill capture (scrape + manual PDF upload).
package utilities

import (
	"context"
	"encoding/json"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"ubp/db"
)

// SchedulerRequester labels cron-enqueued jobs; the failure/backoff policy
// only applies to these (manual triggers keep their plain fail behavior).
const SchedulerRequester = "scheduler-cron"

type Config struct {
	// Worker caps
	GlobalConcurrency int
	ProviderCaps      map[string]int

	// Files / paths
	StoreRoot    string
	ScrapersDir  string
	PythonBin    string
	PyModuleBase string
	HandoffDir   string

	// AES-256-GCM master key (32 bytes). Empty disables credential
	// storage/decryption — manual upload still works.
	CredMasterKey []byte
}

type Module struct {
	db  *pgxpool.Pool // request path (org-scoped via OrgDB middleware)
	sys *pgxpool.Pool // trusted cross-org path (worker, scheduler); bypasses RLS
	cfg Config
}

func NewModule(appPool, sysPool *pgxpool.Pool, cfg Config) *Module {
	return &Module{db: appPool, sys: sysPool, cfg: cfg}
}

func (m *Module) DB() *pgxpool.Pool { return m.db }
func (m *Module) Config() Config    { return m.cfg }

// q returns the executor a request's queries should use: the org-pinned
// connection from context (RLS-scoped) when present, else the app pool.
func (m *Module) q(ctx context.Context) db.Querier {
	if e := db.FromContext(ctx); e != nil {
		return e
	}
	return m.db
}

// EnqueueJob inserts a queued scrape_job. The billing period is stored in
// params JSON for the worker to read.
func (m *Module) EnqueueJob(ctx context.Context, orgID, acctID int64, period, requestedBy string) (int64, error) {
	if strings.TrimSpace(period) == "" {
		period = "latest"
	}
	paramsJSON, err := json.Marshal(map[string]string{"period": period})
	if err != nil {
		return 0, err
	}
	var jobID int64
	// Trusted insert with an explicit org_id (from the authed caller or the
	// scheduler); runs on the bypass pool so it works from both paths.
	err = m.sys.QueryRow(ctx, `
		INSERT INTO scrape_jobs
			(org_id, utility_account_id, requested_by, requested_at, status, attempt, max_attempts, params)
		VALUES
			($1, $2, $3, now(), 'queued', 0, 3, $4::jsonb)
		RETURNING id
	`, orgID, acctID, requestedBy, string(paramsJSON)).Scan(&jobID)
	return jobID, err
}
