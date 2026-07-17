package utilities

import (
	"context"
	"log"
	"strings"
)

// Scheduling policy — written new for this project (the ra-avm files were
// read for the pattern only). Semantics kept: after a successful scrape the
// next slot derives from the newest service_end; scheduler-driven failures
// retry weekly up to a cap, then drift back to the tentative +1 month slot
// the cron set at enqueue time. All slots anchor at 06:00 America/New_York.
// billing_mode was stripped from providers per plan, so the arrears formula
// (service_end + 1 month + 1 day) applies to everyone.

const MaxConsecutiveScrapeFailures = 3

// RecomputeNextScrapeAfterSuccess sets next_scheduled_scrape_at from the
// account's newest bill service_end and resets the failure counter. If no
// bill has a service_end yet the slot is left unchanged — there is nothing
// to base a cadence on.
func (m *Module) RecomputeNextScrapeAfterSuccess(ctx context.Context, acctID int64) {
	const q = `
		UPDATE utility_accounts ua
		SET next_scheduled_scrape_at = (
			SELECT (((max(b.service_end) + INTERVAL '1 month' + INTERVAL '1 day')::timestamp
			         + TIME '06:00') AT TIME ZONE 'America/New_York')
			FROM bills b
			WHERE b.utility_account_id = ua.id AND b.service_end IS NOT NULL
		),
		    consecutive_scrape_failures = 0
		WHERE ua.id = $1
		  AND EXISTS (
		      SELECT 1 FROM bills b
		      WHERE b.utility_account_id = ua.id AND b.service_end IS NOT NULL
		  )
	`
	if _, err := m.db.Exec(ctx, q, acctID); err != nil {
		log.Printf("[scrape-schedule] recompute acct=%d failed: %v", acctID, err)
	}
}

// HandleScheduledScrapeFailure applies the retry policy when a scheduler-
// driven job fails: < cap → +7 days at 06:00 ET; at cap → reset counter and
// leave the tentative +1 month slot in place. Manual jobs are untouched.
func (m *Module) HandleScheduledScrapeFailure(ctx context.Context, jobID, acctID int64, errMsg string) {
	var requestedBy *string
	if err := m.db.QueryRow(ctx,
		`SELECT requested_by FROM scrape_jobs WHERE id = $1`, jobID,
	).Scan(&requestedBy); err != nil {
		return
	}
	if requestedBy == nil || *requestedBy != SchedulerRequester {
		return
	}

	var failures int
	if err := m.db.QueryRow(ctx, `
		UPDATE utility_accounts
		SET consecutive_scrape_failures = consecutive_scrape_failures + 1
		WHERE id = $1
		RETURNING consecutive_scrape_failures
	`, acctID).Scan(&failures); err != nil {
		log.Printf("[scrape-schedule] bump failure count acct=%d failed: %v", acctID, err)
		return
	}

	if failures >= MaxConsecutiveScrapeFailures {
		if _, err := m.db.Exec(ctx,
			`UPDATE utility_accounts SET consecutive_scrape_failures = 0 WHERE id = $1`, acctID,
		); err != nil {
			log.Printf("[scrape-schedule] reset failure count acct=%d failed: %v", acctID, err)
		}
		log.Printf("[scrape-schedule] acct=%d hit %d consecutive failures; waiting for next monthly cycle", acctID, failures)
		return
	}

	if _, err := m.db.Exec(ctx, `
		UPDATE utility_accounts
		SET next_scheduled_scrape_at =
		    (((now() + INTERVAL '7 days')::date::timestamp + TIME '06:00')
		     AT TIME ZONE 'America/New_York')
		WHERE id = $1
	`, acctID); err != nil {
		log.Printf("[scrape-schedule] set +7d retry acct=%d failed: %v", acctID, err)
	}

	if !isDuplicateBillError(errMsg) {
		log.Printf("[scrape-schedule] scrape failure acct=%d job=%d attempt=%d/%d: %s",
			acctID, jobID, failures, MaxConsecutiveScrapeFailures, truncErr(errMsg))
	}
}

// isDuplicateBillError: the bills_scrape_dedup unique index fired — the
// scraper pulled a bill we already have (no new bill posted yet). Routine,
// not actionable.
func isDuplicateBillError(errMsg string) bool {
	if errMsg == "" {
		return false
	}
	l := strings.ToLower(errMsg)
	return strings.Contains(l, "duplicate key") ||
		strings.Contains(l, "23505") ||
		strings.Contains(l, "already been imported")
}

func truncErr(s string) string {
	const max = 500
	if len(s) <= max {
		return s
	}
	return s[:max] + "…"
}
