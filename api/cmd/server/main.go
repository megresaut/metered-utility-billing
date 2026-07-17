package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"ubp/config"
	"ubp/db"
	"ubp/httpx"
	"ubp/middleware"
	"ubp/modules/export"
	"ubp/modules/orgs"
	"ubp/modules/properties"
	"ubp/modules/utilities"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	pool, err := db.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer pool.Close()

	if err := db.Migrate(ctx, pool, cfg.MigrationsDir()); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	if len(cfg.CredMasterKey) == 0 {
		log.Printf("WARNING: CRED_MASTER_KEY not set — credential storage and scraping are disabled; manual upload still works")
	}
	if os.Getenv("ANTHROPIC_API_KEY") == "" {
		log.Printf("WARNING: ANTHROPIC_API_KEY not set — PDF upload extraction will fail")
	}

	util := utilities.NewModule(pool, utilities.Config{
		GlobalConcurrency: cfg.GlobalConcurrency,
		ProviderCaps:      cfg.ProviderCaps,
		StoreRoot:         cfg.StoreRoot,
		ScrapersDir:       cfg.ScrapersDir,
		PythonBin:         cfg.PythonBin,
		PyModuleBase:      cfg.PyModuleBase,
		HandoffDir:        cfg.HandoffDir,
		CredMasterKey:     cfg.CredMasterKey,
	})

	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, r *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	authed := func(h http.HandlerFunc) http.Handler {
		return middleware.Auth(cfg.JWTSecret, h)
	}

	orgs.New(pool, cfg.JWTSecret).Routes(mux, authed)
	properties.New(pool).Routes(mux, authed)
	util.Routes(mux, authed)
	export.New(pool).Routes(mux, authed)

	// Background: job worker + scrape scheduler.
	util.StartWorker(ctx)
	util.StartScheduler(ctx, time.Duration(cfg.SchedulerIntervalSec)*time.Second)

	srv := &http.Server{
		Addr:    ":" + cfg.Port,
		Handler: middleware.CORS(mux),
	}
	go func() {
		<-ctx.Done()
		shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shCtx)
	}()

	log.Printf("utility-billing-platform API listening on :%s", cfg.Port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}
