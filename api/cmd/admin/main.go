// Admin CLI — org/org_user creation is admin-driven (no self-serve signup).
//
// Usage:
//	go run ./cmd/admin create-org --name "Acme Property Management"
//	go run ./cmd/admin create-user --org-id 1 --email admin@acme.com --password secret [--role admin]
//	go run ./cmd/admin list-orgs
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"

	"golang.org/x/crypto/bcrypt"

	"ubp/config"
	"ubp/db"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	ctx := context.Background()
	// Admin writes org tables directly (no request org context), so it uses the
	// RLS-bypass pool.
	pool, err := db.ConnectBypass(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer pool.Close()
	if err := db.Migrate(ctx, pool, cfg.MigrationsDir()); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	switch os.Args[1] {
	case "create-org":
		fs := flag.NewFlagSet("create-org", flag.ExitOnError)
		name := fs.String("name", "", "organization name")
		_ = fs.Parse(os.Args[2:])
		if strings.TrimSpace(*name) == "" {
			log.Fatal("--name is required")
		}
		var id int64
		if err := pool.QueryRow(ctx,
			`INSERT INTO organizations (name) VALUES ($1) RETURNING id`, strings.TrimSpace(*name),
		).Scan(&id); err != nil {
			log.Fatalf("create org: %v", err)
		}
		fmt.Printf("created organization %d: %s\n", id, *name)

	case "create-user":
		fs := flag.NewFlagSet("create-user", flag.ExitOnError)
		orgID := fs.Int64("org-id", 0, "organization id")
		email := fs.String("email", "", "login email")
		password := fs.String("password", "", "login password")
		role := fs.String("role", "admin", "role: admin | member")
		_ = fs.Parse(os.Args[2:])
		if *orgID == 0 || *email == "" || *password == "" {
			log.Fatal("--org-id, --email and --password are required")
		}
		hash, err := bcrypt.GenerateFromPassword([]byte(*password), bcrypt.DefaultCost)
		if err != nil {
			log.Fatalf("hash: %v", err)
		}
		var id int64
		if err := pool.QueryRow(ctx, `
			INSERT INTO org_users (org_id, email, password_hash, role)
			VALUES ($1, $2, $3, $4) RETURNING id
		`, *orgID, strings.ToLower(strings.TrimSpace(*email)), string(hash), *role).Scan(&id); err != nil {
			log.Fatalf("create user: %v", err)
		}
		fmt.Printf("created user %d: %s (org %d, role %s)\n", id, *email, *orgID, *role)

	case "reset-password":
		fs := flag.NewFlagSet("reset-password", flag.ExitOnError)
		email := fs.String("email", "", "login email")
		password := fs.String("password", "", "new password")
		_ = fs.Parse(os.Args[2:])
		if *email == "" || *password == "" {
			log.Fatal("--email and --password are required")
		}
		hash, err := bcrypt.GenerateFromPassword([]byte(*password), bcrypt.DefaultCost)
		if err != nil {
			log.Fatalf("hash: %v", err)
		}
		tag, err := pool.Exec(ctx,
			`UPDATE org_users SET password_hash = $1 WHERE email = $2`,
			string(hash), strings.ToLower(strings.TrimSpace(*email)))
		if err != nil {
			log.Fatalf("reset password: %v", err)
		}
		if tag.RowsAffected() == 0 {
			log.Fatalf("no user with email %s", *email)
		}
		fmt.Printf("password reset for %s\n", *email)

	case "list-orgs":
		rows, err := pool.Query(ctx, `
			SELECT o.id, o.name, count(u.id)
			FROM organizations o LEFT JOIN org_users u ON u.org_id = o.id
			GROUP BY o.id, o.name ORDER BY o.id`)
		if err != nil {
			log.Fatalf("list orgs: %v", err)
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			var name string
			var users int64
			_ = rows.Scan(&id, &name, &users)
			fmt.Printf("%d\t%s\t(%d users)\n", id, name, users)
		}

	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  admin create-org     --name <name>
  admin create-user    --org-id <id> --email <email> --password <pw> [--role admin|member]
  admin reset-password --email <email> --password <pw>
  admin list-orgs`)
	os.Exit(1)
}
