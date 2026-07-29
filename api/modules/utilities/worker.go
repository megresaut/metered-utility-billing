// Job dispatch — copied from ra-avm's api/modules/utilities/worker.go and
// edited in place per TECHNICAL_PLAN.md:
//   - stripped: Buildium/QBO publishing, Outlook 2FA pollers, Slack alerts,
//     Redis batch counters, WebSocket hub events, .cursor debug logging
//   - org_id threaded through job claiming and bill insertion
//   - resolveSecrets() base64/env scheme replaced with AES-256-GCM decryption
//     from utility_accounts (see crypto.go)
//   - hybrid dispatch (generic capabilities/argv path + hardcoded provider
//     branches) intentionally kept as-is, not consolidated
package utilities

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

// -------- Public worker entry --------

func (m *Module) StartWorker(ctx context.Context) {
	// simple ticker to claim jobs respecting caps
	t := time.NewTicker(750 * time.Millisecond)
	go func() {
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				m.tryClaimAndRun(ctx)
			}
		}
	}()
}

// Basic cap tracking (in-proc). Single-instance MVP.
var (
	workerMu          sync.Mutex
	runningGlobal     int
	runningByProvider = map[string]int{}
	aquarionLastStart time.Time
)

func (m *Module) tryClaimAndRun(ctx context.Context) {
	workerMu.Lock()
	atCap := runningGlobal >= m.cfg.GlobalConcurrency
	workerMu.Unlock()
	if atCap {
		return
	}

	// claim the next job that does not violate provider caps
	tx, err := m.sys.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return
	}
	defer tx.Rollback(ctx)

	var jobID, acctID, orgID int64
	var status string
	var params []byte
	row := tx.QueryRow(ctx, `
		update scrape_jobs
		set status='running', started_at=now(), attempt=attempt+1
		where id = (
			select id from scrape_jobs
			where status='queued'
			order by requested_at asc
			for update skip locked
			limit 1
		)
		returning id, utility_account_id, org_id, status, params`)
	if err := row.Scan(&jobID, &acctID, &orgID, &status, &params); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return
		}
		return
	}

	// resolve providerCode for cap checks
	var providerCode string
	if err := tx.QueryRow(ctx, `
		select p.code
		from utility_accounts ua join providers p on p.id = ua.provider_id
		where ua.id=$1`, acctID).Scan(&providerCode); err != nil {
		return
	}

	workerMu.Lock()
	cap := m.cfg.ProviderCaps[providerCode]
	if cap == 0 {
		cap = 1
	}
	overProviderCap := runningByProvider[providerCode] >= cap
	// Special handling for Aquarion: enforce 2-minute delay between jobs
	aquarionBlocked := strings.EqualFold(providerCode, "aquarion") &&
		time.Since(aquarionLastStart) < 2*time.Minute
	workerMu.Unlock()

	if overProviderCap || aquarionBlocked {
		if aquarionBlocked {
			log.Printf("[worker] Aquarion job %d delayed - must wait 2 minutes since last Aquarion job", jobID)
		}
		// revert claim (put back to queued)
		_, _ = tx.Exec(ctx, `update scrape_jobs set status='queued', attempt=attempt-1, started_at=null where id=$1`, jobID)
		_ = tx.Commit(ctx)
		return
	}

	if err := tx.Commit(ctx); err != nil {
		return
	}

	// bump counters
	workerMu.Lock()
	runningGlobal++
	runningByProvider[providerCode]++
	if strings.EqualFold(providerCode, "aquarion") {
		aquarionLastStart = time.Now()
	}
	workerMu.Unlock()

	// run the job async
	go func(jobID, acctID, orgID int64, provider string, params []byte) {
		defer func() {
			workerMu.Lock()
			runningGlobal--
			runningByProvider[provider]--
			workerMu.Unlock()
		}()

		period := "latest"
		var pj map[string]any
		if err := json.Unmarshal(params, &pj); err == nil {
			if v, ok := pj["period"].(string); ok {
				period = v
			}
		}

		ctxBG := context.Background()

		res, err := m.runPython(provider, acctID, period)
		if err != nil {
			msg := err.Error()
			_, _ = m.sys.Exec(ctxBG, `update scrape_jobs set status='failed', finished_at=now(), error_message=$2 where id=$1`, jobID, msg)
			m.HandleScheduledScrapeFailure(ctxBG, jobID, acctID, msg)
			log.Printf("[worker] job %d (%s) failed: %s", jobID, provider, truncErr(msg))
			return
		}

		// store PDF + insert bill
		objKey, sha := storeLocal(m.cfg.StoreRoot, provider, acctID, res)

		billID, errIns := m.insertBill(ctxBG, orgID, acctID, provider, res, objKey, sha)
		if errIns != nil {
			msg := fmt.Sprintf("insert bill failed: %v", errIns)
			if isDuplicateBillError(errIns.Error()) {
				msg = "This bill has already been imported for this account and billing cycle. Please try again next cycle."
			}
			// Duplicate-key means the scraper pulled the same bill we already
			// have (provider hasn't posted a newer one yet). Funnel through
			// HandleScheduledScrapeFailure so dup-key gets the same "+7d up to
			// 3 retries, then back off to the tentative +1mo" treatment as any
			// other recoverable scrape failure.
			m.HandleScheduledScrapeFailure(ctxBG, jobID, acctID, msg)
			_, _ = m.sys.Exec(ctxBG,
				`update scrape_jobs set status='failed', finished_at=now(), error_message=$2 where id=$1`,
				jobID, msg,
			)
			log.Printf("[worker] job %d (%s): %s", jobID, provider, msg)
			return
		}

		// Bill landed — advance the per-account scrape schedule.
		m.RecomputeNextScrapeAfterSuccess(ctxBG, acctID)

		_, _ = m.sys.Exec(ctxBG,
			`update scrape_jobs set status='succeeded', finished_at=now() where id=$1`,
			jobID,
		)
		log.Printf("[worker] job %d (%s) succeeded: bill %d, %d cents, pdf %s",
			jobID, provider, billID, res.AmountCents, objKey)
	}(jobID, acctID, orgID, providerCode, params)
}

// ----------------------------
// 2FA handoff (aquarion, winwaste)
// ----------------------------
//
// ra-avm polled an Outlook mailbox for the emailed code and wrote it to a
// handoff file the Python scraper watches. The Outlook integration is
// AVMRE-specific and was stripped; for MVP an operator drops the code into
// the handoff file by hand while the scraper waits (it polls for the file).

func (m *Module) announce2FAHandoff(provider, handoffBase string) {
	codePath := filepath.Join(handoffBase, "2fa_code.txt")
	log.Printf("[%s][2fa] if the portal emails a 2FA code, write it to %s while the scraper waits", provider, codePath)
}

// -------- Python dispatch (provider-aware) --------

func (m *Module) runPython(provider string, acctID int64, period string) (*pyResult, error) {
	switch provider {
	case "optimum":
		return m.runOptimum(acctID, period)
	case "aquarion":
		return m.runAquarion(acctID, period)
	case "fios":
		return m.runFios(acctID, period)
	case "cng", "scg", "rwa", "fdwd", "ttd", "opt", "snew", "wpca":
		// require address-based scraping (username/password + address)
		return m.runCngOrUinet(provider, acctID, period)
	case "eversource":
		return m.runEversource(acctID, period)
	case "uinet":
		return m.runUinet(acctID, period)
	case "winwaste":
		return m.runWinWaste(acctID, period)
	case "santaguida":
		return m.runSantaguida(acctID, period)
	default:
		// generic: python -m providers.<code>.scraper from the scrapers root
		return m.runModuleProvider(provider, acctID, period)
	}
}

func (m *Module) runWinWaste(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	handoffBase, _ := filepath.Abs(filepath.Join(m.cfg.HandoffDir, "winwaste"))
	_ = os.MkdirAll(handoffBase, 0o755)

	// Remove any stale 2FA file so only a fresh code is picked up this run
	_ = os.Remove(filepath.Join(handoffBase, "2fa_code.txt"))
	m.announce2FAHandoff("winwaste", handoffBase)

	// Build extra argv from capabilities
	argv, err := m.buildArgvFromCapabilities(
		capsJSON,
		map[string]string{
			"job.period":       period,
			"account.username": acct.Username,
		},
		secrets,
	)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	// Base args expected by Win Waste scraper
	args := []string{
		"-m", m.cfg.PyModuleBase + ".winwaste.scraper",
		"--username", acct.Username,
		"--password", secrets["password"],
		"--account-number", acct.AccountNumber,
		"--json",
	}

	// Append capability-derived args (headful, debug, slow-mo, etc.)
	args = append(args, argv...)

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = append(m.pyEnv(scrapersRoot), "WINWASTE_HANDOFF_DIR="+handoffBase)

	log.Printf("[winwaste][exec] starting scrape acct=%d", acctID)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("winwaste timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("winwaste exec: %w\n%s", err, tail(out))
	}

	log.Printf("[winwaste][exec] finished")
	return decodePyResult(out)
}

func (m *Module) runSantaguida(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// Build extra argv from capabilities (headful, debug, slow-mo, etc.)
	argv, err := m.buildArgvFromCapabilities(
		capsJSON,
		map[string]string{
			"job.period":       period,
			"account.username": acct.Username,
		},
		secrets,
	)
	if err != nil {
		return nil, fmt.Errorf("santaguida render argv: %w", err)
	}

	// Base args expected by Santaguida scraper (--account-number for dropdown match)
	args := []string{
		"-m", m.cfg.PyModuleBase + ".santaguida.scraper",
		"--username", acct.Username,
		"--password", secrets["password"],
		"--account-number", acct.AccountNumber,
		"--json",
	}
	args = append(args, argv...)

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	downloadDir := filepath.Join(m.cfg.HandoffDir, "santaguida")
	_ = os.MkdirAll(downloadDir, 0o755)

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = append(m.pyEnv(scrapersRoot), "SANTAGUIDA_DOWNLOAD_DIR="+downloadDir)

	log.Printf("[santaguida][exec] starting scrape acct=%d", acctID)

	out, err := cmd.CombinedOutput()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("santaguida timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("santaguida exec: %w\n%s", err, tail(out))
	}
	log.Printf("[santaguida][exec] finished")
	return decodePyResult(out)
}

func (m *Module) runAquarion(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	log.Printf("[aquarion] runAquarion start acctID=%d", acctID)

	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	handoffBase, _ := filepath.Abs(filepath.Join(m.cfg.HandoffDir, "aquarion"))
	_ = os.MkdirAll(handoffBase, 0o755)

	// Remove any stale 2FA file so only a fresh code is picked up this run
	_ = os.Remove(filepath.Join(handoffBase, "2fa_code.txt"))
	m.announce2FAHandoff("aquarion", handoffBase)

	argv, err := m.buildArgvFromCapabilities(
		capsJSON,
		map[string]string{
			"job.period":       period,
			"account.username": acct.Username,
		},
		secrets,
	)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	args := []string{
		"--username", acct.Username,
		"--password", secrets["password"],
		"--account-number", acct.AccountNumber,
		"--json",
	}
	args = append(args, argv...)

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	args = append([]string{"-m", m.cfg.PyModuleBase + ".aquarion.scraper"}, args...)

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = append(m.pyEnv(scrapersRoot), "AQUARION_HANDOFF_DIR="+handoffBase)

	log.Printf("[aquarion][exec] starting scrape acct=%d", acctID)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("aquarion timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("aquarion exec: %w\n%s", err, tail(out))
	}

	log.Printf("[aquarion][exec] finished acct=%d", acctID)
	return decodePyResult(out)
}

func (m *Module) runModuleProvider(provider string, acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()

	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	argv, err := m.buildArgvFromCapabilities(capsJSON, map[string]string{
		"job.period":              period,
		"account.username":        acct.Username,
		"account.service_address": acct.ServiceAddress,
	}, secrets)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	// Module name like "providers.frontier.scraper"
	modName := fmt.Sprintf("%s.%s.scraper", m.cfg.PyModuleBase, provider)

	args := []string{"-m", modName}
	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}
	if pw := secrets["password"]; pw != "" {
		args = append(args, "--password", pw)
	}
	args = append(args, argv...)
	args = append(args, "--json")

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = m.pyEnv(scrapersRoot)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("%s timeout\n%s", provider, tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("%s exec: %w\n%s", provider, err, tail(out))
	}
	return decodePyResult(out)
}

func (m *Module) loadOptimumAccountType(ctx context.Context, acctID int64) (string, error) {
	var meta []byte

	if err := m.sys.QueryRow(ctx, `
		select metadata
		from utility_accounts
		where id = $1
	`, acctID).Scan(&meta); err != nil {
		return "", fmt.Errorf("load utility_account metadata: %w", err)
	}

	// Default safely
	if len(meta) == 0 {
		return "business", nil
	}

	var mdata map[string]any
	if err := json.Unmarshal(meta, &mdata); err != nil {
		return "business", nil
	}

	if t, ok := mdata["type"].(string); ok && t != "" {
		return strings.ToLower(t), nil
	}

	return "business", nil
}

func (m *Module) runOptimum(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()

	// 1) Load account + caps + secrets
	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// 2) Load Optimum-specific account type
	optType, err := m.loadOptimumAccountType(ctx, acctID)
	if err != nil {
		return nil, err
	}

	var module string
	switch optType {
	case "personal":
		module = m.cfg.PyModuleBase + ".optimum.personal_scraper"
	case "business":
		module = m.cfg.PyModuleBase + ".optimum.scraper"
	default:
		return nil, fmt.Errorf("invalid optimum account type %q", optType)
	}

	// 3) Build argv from capabilities
	argv, err := m.buildArgvFromCapabilities(
		capsJSON,
		map[string]string{
			"job.period":              period,
			"account.username":        acct.Username,
			"account.service_address": acct.ServiceAddress,
		},
		secrets,
	)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	args := []string{"-m", module}

	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}
	if pw := secrets["password"]; pw != "" {
		args = append(args, "--password", pw)
	}

	args = append(args, argv...)
	args = append(args, "--json")

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	env := m.pyEnv(scrapersRoot)
	cmd.Env = env

	log.Printf("[optimum][exec] starting scrape acct=%d type=%s", acctID, optType)

	// Execute, with one-shot self-heal if a Docker-wrapped scraper container
	// is dead (only relevant when SCRAPER_CONTAINER is configured and
	// PYTHON_BIN is a `docker exec` wrapper; harmless otherwise).
	out, err := cmd.CombinedOutput()
	if err != nil && containerDeadFromDockerExec(out) {
		containerName := strings.TrimSpace(os.Getenv("SCRAPER_CONTAINER"))
		if containerName != "" {
			log.Printf("[runOptimum] scraper container %q appears dead; attempting `docker start` + retry", containerName)
			startCmd := exec.CommandContext(ctx, "docker", "start", containerName)
			if startOut, startErr := startCmd.CombinedOutput(); startErr != nil {
				log.Printf("[runOptimum] docker start %q failed: %v out=%q", containerName, startErr, string(startOut))
			} else {
				// Give the container time to come up before retrying.
				time.Sleep(3 * time.Second)
				cmd2 := exec.CommandContext(ctx, bin, args...)
				cmd2.Dir = scrapersRoot
				cmd2.Env = env
				out2, err2 := cmd2.CombinedOutput()
				if err2 == nil {
					log.Printf("[runOptimum] retry after container restart succeeded")
				}
				out, err = out2, err2
			}
		}
	}

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("optimum timeout (%s)\n%s", optType, tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("optimum exec (%s): %w\n%s", optType, err, tail(out))
	}

	return decodePyResult(out)
}

// containerDeadFromDockerExec returns true when the captured output matches
// the docker CLI error printed by `docker exec` against a stopped container.
func containerDeadFromDockerExec(out []byte) bool {
	s := string(out)
	return strings.Contains(s, "Error response from daemon") &&
		strings.Contains(s, "is not running")
}

func (m *Module) runCngOrUinet(provider string, acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	// 1) Load account + caps + secrets
	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// 2) Build extra argv from capabilities (proxy, slow-mo, debug, etc.)
	argv, err := m.buildArgvFromCapabilities(capsJSON, map[string]string{
		"job.period":              period,
		"account.username":        acct.Username,
		"account.service_address": acct.ServiceAddress,
	}, secrets)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	// 3) Module name like "providers.cng.scraper" or "providers.scg.scraper"
	modName := fmt.Sprintf("%s.%s.scraper", m.cfg.PyModuleBase, provider)

	args := []string{
		"-m", modName,
	}

	// Username (optional but usually present)
	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}

	// Password from secrets
	if pw := secrets["password"]; pw != "" {
		args = append(args, "--password", pw)
	}

	// ---- Address handling differences ---------------------------------
	// CNG / FDWD REQUIRE an address, but SCG DOES NOT.
	if provider == "cng" || provider == "fdwd" {
		addr := strings.TrimSpace(acct.ServiceAddress)
		if addr == "" {
			return nil, fmt.Errorf("%s account %d has empty service_address; cannot call scraper", provider, acctID)
		}
		args = append(args, "--address", addr)
	}
	// For "scg" we intentionally do NOT add --address at all.

	// capabilities-driven flags (e.g. --slow-mo 75, --debug, proxy flags, etc.)
	args = append(args, argv...)

	// Always request JSON output from the scraper
	args = append(args, "--json")

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = m.pyEnv(scrapersRoot)

	log.Printf("[%s][exec] starting scrape acct=%d", provider, acctID)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("%s timeout\n%s", provider, tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("%s exec: %w\n%s", provider, err, tail(out))
	}

	// Decode JSON result printed by the scraper
	return decodePyResult(out)
}

func (m *Module) runUinet(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	// 1) Load account + caps + secrets
	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// 2) Validate required fields
	if strings.TrimSpace(acct.ServiceAddress) == "" {
		return nil, fmt.Errorf("uinet account %d missing service_address", acctID)
	}
	if strings.TrimSpace(acct.AccountNumber) == "" {
		return nil, fmt.Errorf("uinet account %d missing account_number", acctID)
	}
	// 3) Build capability argv
	argv, err := m.buildArgvFromCapabilities(capsJSON, map[string]string{
		"job.period":              period,
		"account.username":        acct.Username,
		"account.service_address": acct.ServiceAddress,
		"account.account_number":  acct.AccountNumber,
	}, secrets)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	// 4) Base args
	args := []string{
		"-m", fmt.Sprintf("%s.uinet.scraper", m.cfg.PyModuleBase),
	}

	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}
	if pw := secrets["password"]; pw != "" {
		args = append(args, "--password", pw)
	}

	args = append(args,
		"--address", acct.ServiceAddress,
		"--account_number", acct.AccountNumber,
	)

	// capability flags
	args = append(args, argv...)

	// always JSON
	args = append(args, "--json")

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = m.pyEnv(scrapersRoot)

	log.Printf("[uinet][exec] starting scrape acct=%d", acctID)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("uinet timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("uinet exec: %w\n%s", err, tail(out))
	}

	return decodePyResult(out)
}

func extractJSONObject(b []byte) ([]byte, error) {
	start := bytes.IndexByte(b, '{')
	end := bytes.LastIndexByte(b, '}')

	if start == -1 || end == -1 || end <= start {
		return nil, fmt.Errorf("no JSON object found in output")
	}

	return b[start : end+1], nil
}

func normalizeEversourceDates(raw []byte) ([]byte, error) {
	jsonBytes, err := extractJSONObject(raw)
	if err != nil {
		return raw, nil // let decode fail normally
	}

	var m map[string]any
	if err := json.Unmarshal(jsonBytes, &m); err != nil {
		return raw, nil
	}

	convert := func(key string) {
		v, ok := m[key].(string)
		if !ok || v == "" {
			return
		}

		if t, err := time.Parse("01/02/06", v); err == nil {
			m[key] = t.Format("2006-01-02")
		}
	}

	convert("statement_date")
	convert("due_date")
	convert("period_start")
	convert("period_end")

	return json.Marshal(m)
}

func (m *Module) runEversource(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	// 1) Load account + caps + secrets
	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// Eversource REQUIRES address and account number
	addr := strings.TrimSpace(acct.ServiceAddress)
	if addr == "" {
		return nil, fmt.Errorf("eversource account %d has empty service_address", acctID)
	}
	if strings.TrimSpace(acct.AccountNumber) == "" {
		return nil, fmt.Errorf("eversource account %d has empty account_number", acctID)
	}

	// 2) Build capability-driven argv (slow-mo, debug, etc.)
	argv, err := m.buildArgvFromCapabilities(capsJSON, map[string]string{
		"job.period":              period,
		"account.username":        acct.Username,
		"account.service_address": acct.ServiceAddress,
		"account.account_number":  acct.AccountNumber,
	}, secrets)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	// 3) Python module
	modName := fmt.Sprintf("%s.eversource.scraper", m.cfg.PyModuleBase)

	args := []string{
		"-m", modName,
	}

	// Username
	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}

	// Password
	if pw := secrets["password"]; pw != "" {
		args = append(args, "--password", pw)
	}

	// Address (required)
	args = append(args, "--address", addr)

	// Account number (required)
	args = append(args, "--account-number", acct.AccountNumber)

	// Capability flags (e.g. --slow-mo 200)
	args = append(args, argv...)

	// Always JSON
	args = append(args, "--json")

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = m.pyEnv(scrapersRoot)

	log.Printf("[eversource][exec] starting scrape acct=%d", acctID)

	// Execute
	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("eversource timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("eversource exec: %w\n%s", err, tail(out))
	}
	normalized, err := normalizeEversourceDates(bytes.TrimSpace(out))
	if err != nil {
		return nil, fmt.Errorf("eversource date normalization failed: %w", err)
	}
	// Decode JSON
	return decodePyResult(normalized)
}

func (m *Module) runFios(acctID int64, period string) (*pyResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()

	// 1) Load account + caps + secrets
	acct, capsJSON, secrets, err := m.loadAcctCapsSecrets(ctx, acctID)
	if err != nil {
		return nil, err
	}

	// 2) Build extra argv from capabilities (proxy, slow-mo, debug, etc.)
	argv, err := m.buildArgvFromCapabilities(capsJSON, map[string]string{
		"job.period":              period,
		"account.username":        acct.Username,
		"account.service_address": acct.ServiceAddress,
	}, secrets)
	if err != nil {
		return nil, fmt.Errorf("render argv: %w", err)
	}

	args := []string{
		"-m", m.cfg.PyModuleBase + ".fios.scraper",
	}

	if acct.Username != "" {
		args = append(args, "--username", acct.Username)
	}
	if secrets["password"] != "" {
		args = append(args, "--password", secrets["password"])
	}
	// Fios asks a security question at login; the answer lives in the
	// account's encrypted secrets alongside the password.
	if secrets["sec_answer"] == "" {
		return nil, fmt.Errorf("fios account %d has no sec_answer stored; add it to the account credentials", acctID)
	}
	args = append(args, "--sec-answer", secrets["sec_answer"])

	// capabilities-driven flags (e.g. --slow-mo 75, --debug, proxy flags, etc.)
	args = append(args, argv...)

	// Always request JSON output from the scraper
	args = append(args, "--json")

	scrapersRoot := m.cfg.ScrapersDir
	bin := m.pythonBin()

	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Dir = scrapersRoot
	cmd.Env = m.pyEnv(scrapersRoot)

	log.Printf("[fios][exec] starting scrape acct=%d", acctID)

	out, err := cmd.CombinedOutput()

	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("fios timeout\n%s", tail(out))
	}
	if err != nil {
		return nil, fmt.Errorf("fios exec: %w\n%s", err, tail(out))
	}

	return decodePyResult(out)
}

// -------- Helpers / adapters --------

func (m *Module) pythonBin() string {
	if bin := strings.TrimSpace(m.cfg.PythonBin); bin != "" {
		return bin
	}
	return "python3"
}

// pyEnv builds the subprocess environment with PYTHONPATH set to the
// scrapers root so `from common...` imports resolve, plus the handoff and
// per-provider download dirs the scrapers read (their built-in defaults are
// Docker paths like /handoff that don't exist on a host machine).
func (m *Module) pyEnv(scrapersRoot string) []string {
	env := os.Environ()
	pyPath := scrapersRoot
	if cur := os.Getenv("PYTHONPATH"); cur != "" {
		pyPath = pyPath + string(os.PathListSeparator) + cur
	}
	env = append(env, "PYTHONPATH="+pyPath)

	handoff, _ := filepath.Abs(m.cfg.HandoffDir)
	_ = os.MkdirAll(handoff, 0o755)
	env = append(env, "RA_HANDOFF_DIR="+handoff)

	// <PREFIX>_DOWNLOAD_DIR for every provider; a couple use historical
	// prefixes that don't match their provider code.
	prefixes := []string{"CNG", "EVERSOURCE", "FDWD", "FIOS", "FRONTIER", "OPTIMUM",
		"RWATER", "SANTAGUIDA", "SCG", "SNEW", "TTD", "UI", "WINWASTE", "WPCA", "AQUARION", "STARLINK"}
	for _, p := range prefixes {
		dir := filepath.Join(handoff, strings.ToLower(p))
		_ = os.MkdirAll(dir, 0o755)
		env = append(env, p+"_DOWNLOAD_DIR="+dir)
	}
	env = append(env, "OPTIMUM_SNAP_DIR="+filepath.Join(handoff, "optimum"))
	return env
}

type pyResult struct {
	AmountCents int64  `json:"amount_cents"`
	PDFPath     string `json:"pdf_path"`

	// Raw JSON date strings from Python (e.g. "2025-10-13")
	StatementDateRaw string  `json:"statement_date"`
	PeriodStartRaw   *string `json:"period_start,omitempty"`
	PeriodEndRaw     *string `json:"period_end,omitempty"`
	DueDateRaw       *string `json:"due_date,omitempty"`

	// Parsed Go times used elsewhere in the worker / DB
	StatementDate time.Time  `json:"-"`
	PeriodStart   *time.Time `json:"-"`
	PeriodEnd     *time.Time `json:"-"`
	DueDate       *time.Time `json:"-"`

	StatementID string `json:"statement_id"`

	// tolerated alternates / extras from Python
	OK        *bool   `json:"ok,omitempty"`
	Error     *string `json:"error,omitempty"`
	AmountStr string  `json:"amount,omitempty"`
	FinalURL  string  `json:"final_url,omitempty"`
}

type acctRow struct {
	Username       string
	ServiceAddress string
	AccountNumber  string
}

func (m *Module) loadAcctCapsSecrets(ctx context.Context, acctID int64) (acctRow, []byte, map[string]string, error) {
	var a acctRow
	var caps []byte
	var username, svcAddr, acctNum *string
	var ciphertext, nonce []byte

	if err := m.sys.QueryRow(ctx, `
		select
			ua.username,
			ua.credential_ciphertext,
			ua.credential_nonce,
			ua.service_address,
			ua.account_number,
			p.capabilities
		from utility_accounts ua
		join providers p on p.id = ua.provider_id
		where ua.id = $1
	`, acctID).Scan(
		&username,
		&ciphertext,
		&nonce,
		&svcAddr,
		&acctNum,
		&caps,
	); err != nil {
		return a, nil, nil, fmt.Errorf("load acct/provider: %w", err)
	}
	if username != nil {
		a.Username = *username
	}
	if svcAddr != nil {
		a.ServiceAddress = *svcAddr
	}
	if acctNum != nil {
		a.AccountNumber = *acctNum
	}
	if len(ciphertext) == 0 {
		return a, nil, nil, fmt.Errorf("account %d has no stored credentials (manual-only account?)", acctID)
	}
	secrets, err := m.decryptSecrets(ciphertext, nonce)
	if err != nil {
		return a, nil, nil, fmt.Errorf("resolve secrets: %w", err)
	}
	return a, caps, secrets, nil
}

// buildArgvFromCapabilities reads providers.capabilities.launch.argv and renders placeholders.
func (m *Module) buildArgvFromCapabilities(capabilitiesJSON []byte, vals map[string]string, secrets map[string]string) ([]string, error) {
	type launchSpec struct {
		Launch struct {
			Argv []string `json:"argv"`
		} `json:"launch"`
	}
	var spec launchSpec
	if len(capabilitiesJSON) > 0 {
		_ = json.Unmarshal(capabilitiesJSON, &spec) // tolerate missing/malformed
	}
	// default argv if none provided in DB: include period if set and not "latest"
	if len(spec.Launch.Argv) == 0 && vals["job.period"] != "" && vals["job.period"] != "latest" {
		return []string{"--period", vals["job.period"]}, nil
	}

	lookup := map[string]string{}
	for k, v := range vals {
		lookup[k] = v
	}
	for k, v := range secrets {
		lookup["secret."+k] = v
	}

	out := make([]string, 0, len(spec.Launch.Argv))
	for _, tok := range spec.Launch.Argv {
		out = append(out, renderPlaceholders(tok, lookup))
	}
	return out, nil
}

func renderPlaceholders(s string, m map[string]string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] == '{' {
			if j := strings.IndexByte(s[i:], '}'); j > 1 {
				key := s[i+1 : i+j]
				if val, ok := m[key]; ok {
					b.WriteString(val)
				} else {
					b.WriteString("{" + key + "}")
				}
				i += j + 1
				continue
			}
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func parseFlexibleDate(s string) (time.Time, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, nil
	}

	layouts := []string{
		time.RFC3339, // "2006-01-02T15:04:05Z07:00"
		"2006-01-02", // "YYYY-MM-DD" (what Python is sending now)
	}

	var lastErr error
	for _, layout := range layouts {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC(), nil
		} else {
			lastErr = err
		}
	}
	return time.Time{}, lastErr
}

func decodePyResult(out []byte) (*pyResult, error) {
	// Try to isolate the JSON object in case stdout contains DEBUG lines.
	start := bytes.IndexByte(out, '{')
	end := bytes.LastIndexByte(out, '}')
	if start >= 0 && end > start {
		out = out[start : end+1]
	}

	var r pyResult
	if err := json.Unmarshal(out, &r); err != nil {
		return nil, fmt.Errorf("decode: %w\nraw: %s", err, snippet(out))
	}

	// ----- Parse dates from the raw strings -----

	if r.StatementDateRaw != "" {
		t, err := parseFlexibleDate(r.StatementDateRaw)
		if err != nil {
			return nil, fmt.Errorf("decode statement_date: %w\nraw: %s", err, snippet(out))
		}
		r.StatementDate = t
	}

	if r.PeriodStartRaw != nil && strings.TrimSpace(*r.PeriodStartRaw) != "" {
		t, err := parseFlexibleDate(*r.PeriodStartRaw)
		if err != nil {
			return nil, fmt.Errorf("decode period_start: %w\nraw: %s", err, snippet(out))
		}
		r.PeriodStart = &t
	}

	if r.PeriodEndRaw != nil && strings.TrimSpace(*r.PeriodEndRaw) != "" {
		t, err := parseFlexibleDate(*r.PeriodEndRaw)
		if err != nil {
			return nil, fmt.Errorf("decode period_end: %w\nraw: %s", err, snippet(out))
		}
		r.PeriodEnd = &t
	}

	if r.DueDateRaw != nil && strings.TrimSpace(*r.DueDateRaw) != "" {
		t, err := parseFlexibleDate(*r.DueDateRaw)
		if err != nil {
			return nil, fmt.Errorf("decode due_date: %w\nraw: %s", err, snippet(out))
		}
		r.DueDate = &t
	}

	// ----- Fallback logic for amount / statement_date -----

	if r.AmountCents <= 0 && r.AmountStr != "" {
		if f, err := strconv.ParseFloat(strings.TrimSpace(r.AmountStr), 64); err == nil {
			r.AmountCents = int64(math.Round(f * 100))
		}
	}

	if r.StatementDate.IsZero() {
		// If still zero (provider didn't send anything), default to now
		r.StatementDate = time.Now().UTC()
	}

	if r.PDFPath == "" || r.AmountCents <= 0 || r.StatementDate.IsZero() {
		return nil, fmt.Errorf("incomplete result\nraw: %s", snippet(out))
	}

	return &r, nil
}

func tail(b []byte) string {
	const N = 2000
	if len(b) <= N {
		return string(b)
	}
	return string(b[len(b)-N:])
}

func snippet(b []byte) string {
	const N = 400
	if len(b) <= N {
		return string(b)
	}
	return string(b[:N]) + "…"
}

// ---- storage + bills ----

func storeLocal(storeRoot, provider string, acctID int64, r *pyResult) (objKey, sha string) {
	// object key: bills/<provider>/<YYYY/MM>/<acctID>.pdf
	objKey = filepath.ToSlash(
		filepath.Join("bills", provider, r.StatementDate.Format("2006/01"), fmt.Sprintf("%d.pdf", acctID)),
	)

	dstPath := filepath.Join(storeRoot, objKey)

	// ensure dir
	if err := os.MkdirAll(filepath.Dir(dstPath), 0o755); err != nil {
		// fall back to fake key on error
		return fmt.Sprintf("bills/%s/%s/%d_fake.pdf", provider, r.StatementDate.Format("2006/01"), acctID), "deadbeef"
	}

	// copy file and compute sha256
	src, err := os.Open(r.PDFPath)
	if err != nil {
		return fmt.Sprintf("bills/%s/%s/%d_fake.pdf", provider, r.StatementDate.Format("2006/01"), acctID), "deadbeef"
	}
	defer src.Close()

	dst, err := os.Create(dstPath)
	if err != nil {
		return fmt.Sprintf("bills/%s/%s/%d_fake.pdf", provider, r.StatementDate.Format("2006/01"), acctID), "deadbeef"
	}
	defer dst.Close()

	h := sha256.New()
	if _, err := io.Copy(io.MultiWriter(dst, h), src); err != nil {
		return fmt.Sprintf("bills/%s/%s/%d_fake.pdf", provider, r.StatementDate.Format("2006/01"), acctID), "deadbeef"
	}
	sha = hex.EncodeToString(h.Sum(nil))
	return objKey, sha
}

// insertBill writes a bills row for a scraped utility bill, threading org_id
// and the account's property through for dashboard filtering.
func (m *Module) insertBill(ctx context.Context, orgID, acctID int64, provider string, r *pyResult, objKey, sha string) (int64, error) {
	var providerID, propertyID int64
	var vendorName string
	if err := m.sys.QueryRow(ctx, `
		select p.id, p.display_name, ua.property_id
		from utility_accounts ua
		join providers p on p.id = ua.provider_id
		where ua.id = $1
	`, acctID).Scan(&providerID, &vendorName, &propertyID); err != nil {
		return 0, fmt.Errorf("load provider id for account %d: %w", acctID, err)
	}

	var billID int64
	err := m.sys.QueryRow(ctx, `
	  insert into bills (
	    org_id,
	    utility_account_id,
	    provider_id,
	    property_id,
	    vendor_name,
	    statement_id,
	    statement_date,
	    amount_cents,
	    pdf_object_key,
	    sha256_pdf,
	    service_start,
	    service_end,
	    due_date,
	    status,
	    source
	  )
	  values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'outstanding','scrape')
	  returning id
	`,
		orgID,
		acctID,
		providerID,
		propertyID,
		vendorName,
		r.StatementID,
		r.StatementDate,
		r.AmountCents,
		objKey,
		sha,
		nullableTime(r.PeriodStart),
		nullableTime(r.PeriodEnd),
		nullableTime(r.DueDate),
	).Scan(&billID)
	if err != nil {
		return 0, fmt.Errorf("insert bill: %w", err)
	}
	return billID, nil
}

// helper
func nullableTime(t *time.Time) interface{} {
	if t == nil || t.IsZero() {
		return nil
	}
	return *t
}
