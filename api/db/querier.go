package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Querier is the subset of pgx query methods shared by *pgxpool.Pool,
// *pgxpool.Conn, and pgx.Tx. Repositories depend on this so a request can run
// its queries on an org-pinned connection (RLS-scoped) while background code
// runs on the bypass pool — without changing the query call sites.
type Querier interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
	QueryRow(context.Context, string, ...any) pgx.Row
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
	BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error)
}

type ctxKey string

const executorKey ctxKey = "db.executor"

// WithExecutor returns a context carrying the Querier a request's queries
// should use (the org-pinned connection set by the OrgDB middleware).
func WithExecutor(ctx context.Context, q Querier) context.Context {
	return context.WithValue(ctx, executorKey, q)
}

// FromContext returns the request-scoped Querier, or nil if none was pinned.
func FromContext(ctx context.Context) Querier {
	if q, ok := ctx.Value(executorKey).(Querier); ok {
		return q
	}
	return nil
}

// ConnectBypass opens a pool whose every connection is pre-set to bypass RLS
// (app.org_id='bypass'). Used for trusted, cross-org paths: the scrape worker,
// scheduler, migrations, and the admin CLI. Namespaced custom GUCs can be set
// without prior definition, so this is safe even before 003_rls.sql runs.
func ConnectBypass(ctx context.Context, url string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, err
	}
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
		_, err := c.Exec(ctx, "SET app.org_id = 'bypass'")
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping: %w", err)
	}
	return pool, nil
}
