#!/usr/bin/env bash
# One-shot scraper environment setup.
#
# patchright is installed in a second phase, after requirements.txt: its
# dependency pins (pyee/greenlet) conflict with playwright 1.47.0 on paper,
# but the patchright-last install order is the known-working combination the
# scrapers were built against (playwright 1.47.0 + patchright 1.58.0).
set -euo pipefail
cd "$(dirname "$0")"

# playwright 1.47.0 pins greenlet==3.0.3, which only ships wheels up to
# Python 3.12 — prefer 3.11 (what the scrapers were built against).
PY=python3
for cand in python3.11 python3.12; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
echo "using $PY ($($PY --version))"

"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install patchright==1.58.0
.venv/bin/python -m playwright install chromium

echo "scrapers env ready: $(pwd)/.venv"
