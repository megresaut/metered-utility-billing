# syntax=docker/dockerfile:1
#
# Single image for the Metered API. It runs one Go binary that is the API +
# in-process scrape worker + scheduler, so the image also carries the Python
# scraper env and a Chromium browser. It CANNOT run on serverless (Vercel
# Functions/Lambda) — it needs a persistent process and a real browser. Deploy
# it to a container host (Render, per render.yaml). The React frontend deploys
# separately to Vercel and talks to this API over HTTPS.

# ---- Stage 1: build the Go binaries ----
FROM golang:1.24-bookworm AS gobuild
WORKDIR /src/api
COPY api/go.mod api/go.sum ./
RUN go mod download
COPY api/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/server ./cmd/server \
 && CGO_ENABLED=0 GOOS=linux go build -o /out/admin ./cmd/admin

# ---- Stage 2: runtime with Python + Playwright + Chromium ----
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    SCRAPERS_DIR=/app/scrapers \
    PYTHON_BIN=/app/scrapers/.venv/bin/python \
    STORE_ROOT=/data/store \
    HANDOFF_DIR=/data/handoff \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

# Scraper Python env — reproduce scrapers/setup.sh's known-good pin order:
# playwright 1.47.0 first (from requirements.txt), then patchright 1.58.0 on
# top, then the Chromium browser + the OS libraries it needs (--with-deps).
COPY scrapers/requirements.txt /app/scrapers/requirements.txt
RUN python -m venv /app/scrapers/.venv \
 && /app/scrapers/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/scrapers/.venv/bin/pip install --no-cache-dir -r /app/scrapers/requirements.txt \
 && /app/scrapers/.venv/bin/pip install --no-cache-dir patchright==1.58.0 \
 && /app/scrapers/.venv/bin/python -m playwright install --with-deps chromium \
 && rm -rf /root/.cache/pip

# Application code + built binaries.
COPY scrapers/ /app/scrapers/
COPY migrations/ /app/migrations/
COPY --from=gobuild /out/server /app/server
COPY --from=gobuild /out/admin /app/admin

# PDF store + 2FA handoff live on the mounted persistent disk (/data).
RUN mkdir -p /data/store /data/handoff

# Render injects PORT; config.go reads it (defaults to 8090 locally).
EXPOSE 8090
CMD ["/app/server"]
