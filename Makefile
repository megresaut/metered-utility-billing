.PHONY: api web scrapers-setup admin-org admin-user db

db:
	createdb utility_billing_platform_local || true

scrapers-setup:
	./scrapers/setup.sh

api:
	cd api && go run ./cmd/server

web:
	cd web && npm run dev

# usage: make admin-org NAME="Acme PM"
admin-org:
	cd api && go run ./cmd/admin create-org --name "$(NAME)"

# usage: make admin-user ORG=1 EMAIL=you@acme.com PASSWORD=changeme
admin-user:
	cd api && go run ./cmd/admin create-user --org-id $(ORG) --email $(EMAIL) --password $(PASSWORD)
