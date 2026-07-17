# common/browser.py
import os
import shutil
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Playwright
import time
import random
from common.xvfb import ensure_xvfb

# --- Dynamic paths & executables --------------------------------------------
# Allow override from env when needed (e.g., inside container)
#   HEADFUL_PROFILE_DIR=/app/chrome-profile
#   CHROME_EXECUTABLE=/usr/bin/google-chrome
_repo_root = Path(__file__).resolve().parents[1]  # .../scrapers
_default_profile = _repo_root / "chrome-profile"

USER_DATA_DIR = Path(os.getenv("HEADFUL_PROFILE_DIR", str(_default_profile)))
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)  # ensure it exists

# Prefer env var; else detect Chrome; else None → Playwright's bundled Chromium
CHROME_EXEC = os.getenv("CHROME_EXECUTABLE") or shutil.which("google-chrome") or shutil.which("chrome") or None

# ---------------------------------------------------------------------------

STEALTH_JS = r"""
(() => {
  try {
    // Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Ensure chrome runtime exists
    if (!window.chrome) {
      window.chrome = { runtime: {} };
    }

    // Leave everything else to real Chrome
    // No platform/vendor/WebGL/plugin lies
  } catch (e) {}
})();
"""


LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    "--force-color-profile=srgb",
    "--lang=en-US,en",
    "--use-gl=egl",
    "--ignore-gpu-blocklist",
    "--enable-webgl",
    "--password-store=basic",
    "--no-sandbox",
    "--webrtc-ip-handling-policy=default_public_interface_only",
    "--force-webrtc-ip-handling-policy=default_public_interface_only",
]

async def open_real_chrome(slow_mo: int = 80, proxy: dict | None = None):
    from playwright.async_api import async_playwright
    ensure_xvfb()
    base = Path(os.getenv("CHROME_PROFILE_BASE", "/tmp/chrome-profiles"))
    base.mkdir(parents=True, exist_ok=True)

    session_dir = base / f"run-{int(time.time())}-{os.getpid()}-{random.randint(1000,9999)}"
    session_dir.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=False,
        slow_mo=slow_mo,
        proxy=proxy,
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            "--lang=en-US,en",
            "--password-store=basic",
            "--no-sandbox",
        ],
        viewport=None,                 # 🔑 Let Chrome decide
        accept_downloads=True,
    )

    # 🔑 Inject minimal stealth BEFORE pages load
    await context.add_init_script(STEALTH_JS)

    return pw, context

async def close_all(pw: Playwright, ctx: BrowserContext):
    try:
        await ctx.close()
    finally:
        await pw.stop()
