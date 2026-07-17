package utilities

import (
	"context"
	"log"
	"time"

	"github.com/jackc/pgx/v5"
)

// Scheduler — written new for this project: a plain DB-polled cron (no Redis
// batching; per the plan, that entanglement was deliberately not ported).
// Each tick claims utility_accounts whose next_scheduled_scrape_at has
// elapsed and enqueues exactly one scrape_job per account. Accounts with an
// in-flight job are skipped, and the row is bumped to a tentative +1 month
// slot at enqueue so repeated ticks don't double-enqueue; the worker's
// success/failure hooks refine that slot afterwards.

func (m *Module) StartScheduler(ctx context.Context, interval time.Duration) {
	go func() {
		log.Printf("[utility-scheduler] started (interval=%s)", interval)
		m.SchedulerRunOnce(ctx)
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				log.Println("[utility-scheduler] stopped")
				return
			case <-t.C:
				m.SchedulerRunOnce(ctx)
			}
		}
	}()
}

// SchedulerRunOnce runs a single cycle and returns how many jobs were
// enqueued. Also used by the manual "run scheduler now" endpoint.
func (m *Module) SchedulerRunOnce(ctx context.Context) int {
	rows, err := m.db.Query(ctx, `
		SELECT ua.id, ua.org_id
		FROM utility_accounts ua
		WHERE ua.active = true
		  AND ua.credential_ciphertext IS NOT NULL
		  AND ua.next_scheduled_scrape_at IS NOT NULL
		  AND ua.next_scheduled_scrape_at <= now()
		  AND NOT EXISTS (
		      SELECT 1 FROM scrape_jobs sj
		      WHERE sj.utility_account_id = ua.id
		        AND sj.status IN ('queued', 'running')
		  )
		ORDER BY ua.next_scheduled_scrape_at ASC
	`)
	if err != nil {
		log.Printf("[utility-scheduler] find due accounts failed: %v", err)
		return 0
	}
	type due struct{ acctID, orgID int64 }
	var dues []due
	for rows.Next() {
		var d due
		if err := rows.Scan(&d.acctID, &d.orgID); err == nil {
			dues = append(dues, d)
		}
	}
	rows.Close()

	enqueued := 0
	for _, d := range dues {
		if m.enqueueDue(ctx, d.acctID, d.orgID) {
			enqueued++
		}
	}
	if enqueued > 0 {
		log.Printf("[utility-scheduler] enqueued %d job(s)", enqueued)
	}
	return enqueued
}

// enqueueDue re-checks due-ness under a row lock (so two instances can't
// both enqueue), stamps the tentative +1 month debounce slot, then enqueues.
func (m *Module) enqueueDue(ctx context.Context, acctID, orgID int64) bool {
	tx, err := m.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return false
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback(ctx)
		}
	}()

	var stillDue bool
	err = tx.QueryRow(ctx, `
		SELECT true
		FROM utility_accounts
		WHERE id = $1
		  AND active = true
		  AND next_scheduled_scrape_at IS NOT NULL
		  AND next_scheduled_scrape_at <= now()
		  AND NOT EXISTS (
		      SELECT 1 FROM scrape_jobs sj
		      WHERE sj.utility_account_id = $1
		        AND sj.status IN ('queued', 'running')
		  )
		FOR UPDATE SKIP LOCKED
	`, acctID).Scan(&stillDue)
	if err != nil {
		return false // no longer due, or another instance holds the lock
	}

	if _, err := tx.Exec(ctx, `
		UPDATE utility_accounts
		SET last_scheduled_run_at = now(),
		    next_scheduled_scrape_at =
		        (((now() + INTERVAL '1 month')::date::timestamp + TIME '06:00')
		         AT TIME ZONE 'America/New_York')
		WHERE id = $1
	`, acctID); err != nil {
		log.Printf("[utility-scheduler] bump schedule acct=%d: %v", acctID, err)
		return false
	}
	if err := tx.Commit(ctx); err != nil {
		return false
	}
	committed = true

	jobID, err := m.EnqueueJob(ctx, orgID, acctID, "latest", SchedulerRequester)
	if err != nil {
		log.Printf("[utility-scheduler] enqueue acct=%d: %v", acctID, err)
		return false
	}
	log.Printf("[utility-scheduler] enqueued job %d for acct %d", jobID, acctID)
	return true
}
