#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optimum Business scraper (Bright Data residential proxy preferred; network PDF capture)

Flow:
- Launch Playwright Chromium (Bright Data residential proxy via BRIGHT_PROXY_* env, no storage_state)
- Always perform login:
    * Go to optimum.net
    * Click "Sign in"
    * Fill username/password
    * Submit
    * Detect that we're in the Optimum Business portal (business.optimum.net + top nav)
- After we're authenticated and land in the business portal:
    * Click "My Account" from the blue navbar (it's an <a> with an <img>, no visible text)
    * On that My Account page:
        - Extract current amount due ($321.11 etc.)
        - Click "View Statements" (go to Statements Available / bill history)
    * On "Statements Available":
        - Get first row
        - Parse Amount Due
        - Click the Statement Date link (first row)
          which opens that statement PDF viewer tab/window
        - Capture both the new Page object and the URL at the moment it opened
          (before Chrome changes it to about:blank)
    * Download PDF using multiple strategies:
        1. Listen for network response with content-type: application/pdf
           and save the bytes.
        2. GET the captured bill URL directly with self.context.request.
        3. Fallback toolbar click / blob/data scraping / iframe src scan.

Return Result:
    {
      ok: True/False,
      error: ...,
      amount: "321.11",
      amount_cents: 32111,
      pdf_path: "/path/to/file.pdf",
      final_url: "...",
    }
"""

import asyncio
import base64
import json
import os
import random
import re
import sys
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urljoin
from datetime import datetime  # <-- add
from pypdf import PdfReader
import tempfile
import json


from common.stealth import get_playwright_module, install_stealth, apply_stealth_to_page, LAUNCH_ARGS

_async_playwright, _ENGINE = get_playwright_module()

# Import types from whichever engine we're using
try:
    from patchright.async_api import (
        TimeoutError as PWTimeoutError,
        Page,
        Browser,
        BrowserContext,
        Download,
    )
except ImportError:
    from playwright.async_api import (
        TimeoutError as PWTimeoutError,
        Page,
        Browser,
        BrowserContext,
        Download,
    )


# ------------ constants / paths ------------
HERE = Path(__file__).resolve()
SNAP_DIR = Path(os.getenv("OPTIMUM_SNAP_DIR", str(HERE.parent / "snaps"))).expanduser().resolve()
SNAP_DIR.mkdir(parents=True, exist_ok=True)

SCRAPERS_ROOT = HERE.parents[2] if len(HERE.parents) > 2 else HERE.parent
ENV_FILE = SCRAPERS_ROOT / "env"  # optional creds store

OPTIMUM_HOME_URL = "https://www.optimum.net/"  # landing before clicking Sign in

_dl_env = os.getenv("OPTIMUM_DOWNLOAD_DIR", "").strip()
if _dl_env:
    DEFAULT_DL_DIR = Path(_dl_env).resolve()
elif Path("/handoff").exists():
    DEFAULT_DL_DIR = Path("/handoff/optimum").resolve()
else:
    DEFAULT_DL_DIR = Path("./downloads/optimum").resolve()

DEFAULT_DL_DIR.mkdir(parents=True, exist_ok=True)

AMOUNT_RE = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})", re.I)


# ------------ helpers ------------


def _make_chrome_profile_with_pdf_disabled() -> str:
    """
    Create a temporary Chrome user profile with PDF viewer disabled.
    Returns path to user-data-dir.
    """
    profile_dir = tempfile.mkdtemp(prefix="pw-chrome-profile-")

    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)

    prefs = {
        "plugins": {
            "always_open_pdf_externally": True
        }
    }

    prefs_path.write_text(json.dumps(prefs))
    return profile_dir


def load_kv_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ if not already set."""
    try:
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    except Exception:
        pass


def _money_to_cents(txt: str) -> Tuple[Optional[str], Optional[int]]:
    """
    pull first $321.11 style amount from txt.
    returns ("321.11", 32111)
    """
    if not txt:
        return None, None
    m = AMOUNT_RE.search(txt)
    if not m:
        return None, None
    clean = m.group(0).replace("$", "").replace(",", "").strip()
    try:
        cents = int(round(float(clean) * 100))
    except Exception:
        cents = None
    return clean, cents


def _rand_delay_ms(lo=35, hi=110) -> int:
    return random.randint(lo, hi)


async def _pause(page: Page, lo=120, hi=280):
    await page.wait_for_timeout(random.randint(lo, hi))


# ------------ dataclasses ------------

@dataclass
class Config:
    username: str
    password: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    login_retries: int = 2          # total login attempts before giving up
    retry_delay_s: float = 5.0      # seconds between retry attempts

    # Slightly-human fingerprint bits
    timezone_id: str = os.getenv("OPTIMUM_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # NEW date fields
    statement_date: Optional[str] = None  # ISO YYYY-MM-DD
    due_date: Optional[str] = None        # keep None for now
    period_start: Optional[str] = None    # keep None for now
    period_end: Optional[str] = None      # keep None for now



# ------------ main scraper class ------------

class OptimumScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg

        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.log(f"[boot] SNAP_DIR: {SNAP_DIR}")
        self.download_dir: Path = DEFAULT_DL_DIR

        # NEW: track whether we launched real Chrome + a unified close function
        self._using_real_chrome: bool = False
        self._close_fn = None

    # ----- logging -----
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ----- browser/context helpers -----

    def _rand_viewport(self):
        widths = [1280, 1366, 1440, 1536, 1600]
        heights = [720, 768, 800, 900]
        return random.choice(widths), random.choice(heights)

    def _build_launch_kwargs(self):
        # PWDEBUG mode → always show UI + devtools
        if os.getenv("PWDEBUG"):
            return dict(
                headless=False,
                devtools=True,
                slow_mo=self.cfg.slow_mo_ms,
            )

        # Normal behavior
        return dict(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--headless=new" if self.cfg.headless else "",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )


    def _build_context_kwargs(self):
        """
        Create context kwargs. NO storage_state injection.
        UA deliberately omitted — let real Chrome provide its own accurate UA.
        """
        w, h = self._rand_viewport()
        ctx = {
            "accept_downloads": True,
            "viewport": {"width": w, "height": h},
            "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
            "locale": "en-US",
            "timezone_id": self.cfg.timezone_id,
        }
        # NOTE: intentionally not loading storage_state
        return ctx

    async def _install_stealth(self):
        """Apply comprehensive stealth patches from shared module."""
        await install_stealth(self.context)


    async def _snap(self, label: str) -> Optional[str]:
        """
        Save a full-page PNG to SNAP_DIR with a UTC timestamp and label.
        Works in Docker (headful or headless).
        """
        try:
            if not self.page:
                return None
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            fname = f"{ts}-{label}.png".replace("/", "_")
            path = SNAP_DIR / fname
            await self.page.screenshot(path=str(path), full_page=True)
            self.log(f"[snap] wrote {path}")
            return str(path)
        except Exception as e:
            self.log("[snap] failed:", repr(e))
            return None

    def _parse_us_date(self, txt: str) -> Optional[str]:
        """
        Parse dates like 11/03/2025 or 10/1/25 into ISO YYYY-MM-DD.
        """
        if not txt:
            return None
        txt = txt.strip()
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", txt)
        if not m:
            return None
        month, day, year = map(int, m.groups())
        if year < 100:  # assume 2000s for 2-digit years
            year += 2000
        try:
            dt = datetime(year, month, day)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d")


    def _parse_long_us_date(self, txt: str) -> Optional[str]:
        """
        Parse dates like 'November 05, 2025' or 'Nov 5, 2025' into ISO YYYY-MM-DD.
        """
        if not txt:
            return None
        txt = txt.strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None


        # ----- lifecycle -----

    async def _launch_browser_and_context(self):
        """
        Light-footprint launch: bundled Chromium + ephemeral BrowserContext.

        We previously used `launch_persistent_context` with a real-Chrome
        channel and a tempdir-backed user-data-dir. That combo was the main
        driver of container OOMs in production (each scrape kept a Chrome
        profile on disk + the heavier real-Chrome process tree). Cloudflare
        Turnstile is still handled — CapSolver submits the token directly
        via `_try_solve_turnstile`, so we don't need real Chrome for stealth.

        PDF viewer is DISABLED via `--disable-pdf-extension` so PDFs are
        forced to download (used to be done via a custom Chrome prefs file).
        """
        # Clean up any old browser/context
        await self._close()

        if not self._pw:
            self._pw = await _async_playwright().start()
        self.log(f"[launch] engine={_ENGINE}")

        w, h = self._rand_viewport()

        launch_kwargs = {
            "headless": self.cfg.headless,
            "slow_mo": self.cfg.slow_mo_ms,
            "args": LAUNCH_ARGS + ["--disable-pdf-extension"],
        }

        # PWDEBUG override → always visible + devtools
        if os.getenv("PWDEBUG"):
            launch_kwargs["headless"] = False
            launch_kwargs["devtools"] = True

        self.browser = await self._pw.chromium.launch(**launch_kwargs)

        # Residential proxy — skip if OPTIMUM_NO_PROXY=1 (VPS IP not blocked by Optimum auth).
        _no_proxy = os.getenv("OPTIMUM_NO_PROXY", "").strip().lower() in ("1", "true", "yes")
        _ctx_kwargs = dict(
            accept_downloads=True,
            viewport={"width": w, "height": h},
            device_scale_factor=random.choice([1, 1.25, 1.5, 2]),
            locale="en-US",
            timezone_id=self.cfg.timezone_id,
        )
        if not _no_proxy:
            res_server = os.getenv("IPROYAL_RES_SERVER", "http://geo.iproyal.com:12321").strip()
            res_user   = os.getenv("IPROYAL_RES_USER", "m7InJNYS4b6PkjwT").strip()
            res_pass   = os.getenv("IPROYAL_RES_PASS", "4x2Vx50o7ijTMI3d").strip()
            _ctx_kwargs["proxy"] = {"server": res_server, "username": res_user, "password": res_pass}

        self.context = await self.browser.new_context(**_ctx_kwargs)

        self.page = await self.context.new_page()

        # Patch obvious automation fingerprints. install_stealth detects
        # patchright and skips add_init_script (which is broken on patchright
        # persistent contexts — we're not using one anymore, but keep the
        # helper's behavior consistent across both scrapers).
        await self._install_stealth()
        await apply_stealth_to_page(self.page)

        if self.cfg.debug:
            self.page.on(
                "console",
                lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr),
            )

        async def _closer():
            try:
                if self.context:
                    await self.context.close()
            except Exception:
                pass
            try:
                if self.browser:
                    await self.browser.close()
            except Exception:
                pass
            try:
                if self._pw:
                    await self._pw.stop()
            except Exception:
                pass

        self._close_fn = _closer
        self._using_real_chrome = False

    async def _close(self):
        # Unified closer if set (handles both real Chrome and headless flows)
        if self._close_fn:
            try:
                await self._close_fn()
            except Exception:
                pass

        # Reset handles
        self.context = None
        self.browser = None
        self.page = None
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    async def __aenter__(self):
        # Always launch fresh context; NO storage_state
        await self._launch_browser_and_context()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] closing")
        await self._close()

    # ----- public main -----

    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        # Always login (no session reuse)
        self.log("[run] starting explicit login flow")
        if not await self._login():
            await self._snap("no-sign-in")
            return Result(ok=False, error="Login failed (captcha / creds / block)")

        # Go to My Account
        if not await self._goto_my_account():
            return Result(ok=False, error="Could not open My Account summary")

        # Scrape current amount due BEFORE clicking statements
        amount_str, amount_cents = await self._scrape_account_amount()

        # Navigate to Statements Available
        if not await self._goto_statements():
            return Result(
                ok=False,
                error="Could not open Statements Available",
                amount=amount_str,
                amount_cents=amount_cents,
            )

        # Open latest statement (FORCED DOWNLOAD)
        (
            stmt_amount_str,
            stmt_amount_cents,
            statement_date,
            pdf_path,
        ) = await self._open_latest_statement()

        final_amount_str = stmt_amount_str or amount_str
        final_amount_cents = stmt_amount_cents or amount_cents

        if not pdf_path:
            return Result(
                ok=False,
                error="Statement PDF download failed",
                amount=final_amount_str,
                amount_cents=final_amount_cents,
                pdf_path=None,
                final_url=self.page.url,
                statement_date=statement_date,
                due_date=None,
                period_start=None,
                period_end=None,
            )

        # Parse dates from PDF
        due_date = None
        period_start = None
        period_end = None
        try:
            due_date, period_start, period_end = self._parse_dates_from_pdf(pdf_path)
        except Exception as e:
            self.log("[pdf] date parse failed:", repr(e))

        return Result(
            ok=True,
            error=None,
            amount=final_amount_str,
            amount_cents=final_amount_cents,
            pdf_path=pdf_path,
            final_url=None,  # no viewer URL anymore
            statement_date=statement_date,
            due_date=due_date,
            period_start=period_start,
            period_end=period_end,
        )


    # ----- portal detection helper -----

    async def _is_logged_in_portal(self) -> bool:
        """
        Heuristic: are we inside the Optimum Business portal?

        We'll say "yes" if ANY of these is true:
        - URL contains business.optimum.net
        - We see top menu bar #tpTopMenuBar AND the My Account anchor
        <a id="tn1" href="/myaccount"><img ...></a>
        """
        page = self.page

        try:
            if "business.optimum.net" in page.url.lower():
                return True
        except Exception:
            pass

        try:
            nav_bar = page.locator("#tpTopMenuBar")
            if await nav_bar.count() > 0:
                acct_link = page.locator('#tpTopMenuBar a[href="/myaccount"], #tpTopMenuBar a#tn1')
                if await acct_link.count() > 0:
                    return True
        except Exception:
            pass

        return False

    # ----- login -----

    async def _try_solve_turnstile(self):
        """
        Solve Cloudflare Turnstile. Primary strategy: click the interactive checkbox
        in the browser so the token is issued for our own IP (no IP mismatch with
        the form submission). Falls back to CapSolver if the widget doesn't
        auto-resolve within 12 seconds.
        """
        page = self.page

        # Check if Turnstile is present at all
        sitekey = None
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                for part in frame.url.split("/"):
                    if part.startswith("0x"):
                        sitekey = part
                        break
                break

        turnstile_container = page.locator('[class*="cf-turnstile"]')
        turnstile_iframe = page.locator('iframe[src*="challenges.cloudflare.com"]')

        has_container = await turnstile_container.count() > 0
        has_iframe = await turnstile_iframe.count() > 0

        if not sitekey and not has_container and not has_iframe:
            self.log("[turnstile] no Turnstile detected on page")
            return

        self.log(f"[turnstile] detected sitekey={sitekey}, trying browser click-solve first")

        # Strategy 1: click the checkbox inside the Turnstile iframe so Cloudflare
        # validates from our actual IP — avoids token/IP mismatch on submission.
        clicked = False
        try:
            cf_frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            checkbox = cf_frame.locator('input[type="checkbox"]')
            if await checkbox.count() > 0:
                await checkbox.click(timeout=5000)
                clicked = True
                self.log("[turnstile] clicked checkbox inside iframe")
        except Exception as e:
            self.log(f"[turnstile] iframe checkbox click failed: {e}")

        if not clicked:
            try:
                if has_container:
                    await turnstile_container.first.click(timeout=5000)
                    clicked = True
                    self.log("[turnstile] clicked outer cf-turnstile container")
            except Exception as e:
                self.log(f"[turnstile] container click failed: {e}")

        # Wait up to 12s for the captcha hidden input to get a value (auto-solved)
        solved_in_browser = False
        try:
            captcha_input = page.locator('input[name="captcha"], input[name="cf-turnstile-response"]')
            for _ in range(24):  # 24 x 500ms = 12s
                await page.wait_for_timeout(500)
                val = await captcha_input.first.input_value() if await captcha_input.count() > 0 else ""
                if val and len(val) > 10:
                    self.log(f"[turnstile] browser auto-solved! token length={len(val)}")
                    solved_in_browser = True
                    break
        except Exception as e:
            self.log(f"[turnstile] waiting for auto-solve failed: {e}")

        if solved_in_browser:
            return

        # Strategy 2: CapSolver fallback
        capsolver_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
        if not sitekey or not capsolver_key:
            self.log("[turnstile] CapSolver fallback skipped (no sitekey or API key)")
            return

        self.log("[turnstile] browser auto-solve failed, falling back to CapSolver")
        try:
            token = await self._capsolver_solve_turnstile(capsolver_key, sitekey, page.url)
        except Exception as e:
            self.log(f"[turnstile] CapSolver solve failed: {e}")
            return

        if not token:
            self.log("[turnstile] CapSolver returned empty token")
            return

        self.log(f"[turnstile] CapSolver token ({len(token)} chars), injecting")

        injected = await page.evaluate(
            """(token) => {
                const selectors = [
                    'input[name="cf-turnstile-response"]',
                    'textarea[name="cf-turnstile-response"]',
                    'input[name="captcha"]',
                    'input[name="g-recaptcha-response"]',
                ];
                let found = false;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ) || Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        );
                        if (nativeInputValueSetter && nativeInputValueSetter.set) {
                            nativeInputValueSetter.set.call(el, token);
                        } else {
                            el.value = token;
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        found = true;
                    }
                }
                const widget = document.querySelector('[data-callback]');
                if (widget) {
                    const cbName = widget.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {
                        try { window[cbName](token); } catch(e) {}
                    }
                }
                if (window.__cfTurnstileCallback) {
                    try { window.__cfTurnstileCallback(token); } catch(e) {}
                }
                return found;
            }""",
            token,
        )

        if injected:
            self.log("[turnstile] CapSolver token injected into captcha field(s)")
        else:
            self.log("[turnstile] WARNING: could not find captcha input to inject token")

        await _pause(page, 500, 900)

    async def _capsolver_solve_turnstile(
        self, api_key: str, sitekey: str, page_url: str, timeout_s: int = 120
    ) -> Optional[str]:
        """
        Call CapSolver API to solve a Cloudflare Turnstile challenge.
        Uses AntiTurnstileTask (with proxy) when proxy env vars are set so the
        token is solved from the same IP the browser uses — required for
        interactive (managed) Turnstile. Falls back to ProxyLess otherwise.
        Returns the solved token string.
        """
        create_url = "https://api.capsolver.com/createTask"
        result_url = "https://api.capsolver.com/getTaskResult"

        # Use the same residential proxy session as the browser so the Turnstile
        # token is solved from the same IP that submits the login form.
        import re as _re
        res_server = os.getenv("IPROYAL_RES_SERVER", "http://geo.iproyal.com:12321").strip()
        res_user   = os.getenv("IPROYAL_RES_USER", "m7InJNYS4b6PkjwT").strip()
        res_pass   = os.getenv("IPROYAL_RES_PASS", "4x2Vx50o7ijTMI3d").strip()
        m_proxy = _re.match(r"https?://([^:]+):(\d+)", res_server)
        proxy_address  = m_proxy.group(1) if m_proxy else "geo.iproyal.com"
        proxy_port     = int(m_proxy.group(2)) if m_proxy else 12321
        proxy_login = res_user
        proxy_password = res_pass
        use_proxy = not os.getenv("OPTIMUM_NO_PROXY", "").strip().lower() in ("1", "true", "yes")

        if use_proxy:
            task = {
                "type": "AntiTurnstileTask",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "metadata": {"type": "turnstile"},
                "proxyType": "http",
                "proxyAddress": proxy_address,
                "proxyPort": proxy_port,
                "proxyLogin": proxy_login,
                "proxyPassword": proxy_password,
            }
        else:
            task = {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "metadata": {"type": "turnstile"},
            }

        # Create task
        payload = json.dumps({
            "clientKey": api_key,
            "task": task,
        }).encode()

        req = urllib.request.Request(
            create_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())

        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"CapSolver createTask error: {data.get('errorDescription', data)}")

        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"CapSolver returned no taskId: {data}")

        self.log(f"[capsolver] task created: {task_id}")

        # Poll for result
        poll_payload = json.dumps({
            "clientKey": api_key,
            "taskId": task_id,
        }).encode()

        elapsed = 0
        interval = 3
        while elapsed < timeout_s:
            await asyncio.sleep(interval)
            elapsed += interval

            req2 = urllib.request.Request(
                result_url,
                data=poll_payload,
                headers={"Content-Type": "application/json"},
            )
            resp2 = urllib.request.urlopen(req2, timeout=30)
            result = json.loads(resp2.read())

            status = result.get("status", "")
            if status == "ready":
                token = result.get("solution", {}).get("token", "")
                self.log(f"[capsolver] solved in ~{elapsed}s")
                return token
            elif result.get("errorId", 0) != 0:
                raise RuntimeError(f"CapSolver error: {result.get('errorDescription', result)}")

            self.log(f"[capsolver] polling... {elapsed}s")

        raise RuntimeError(f"CapSolver timeout after {timeout_s}s")

    async def _captcha_present(self) -> bool:
        """
        Detect reCAPTCHA, Cloudflare Turnstile, or generic robot checks.
        """
        page = self.page
        # reCAPTCHA iframe
        try:
            iframe_cap = page.locator('iframe[src*="recaptcha" i]')
            if await iframe_cap.count() > 0 and await iframe_cap.first.is_visible():
                return True
        except Exception:
            pass
        # Cloudflare Turnstile iframe
        try:
            turnstile = page.locator('iframe[src*="challenges.cloudflare.com"]')
            if await turnstile.count() > 0 and await turnstile.first.is_visible():
                return True
        except Exception:
            pass
        # Turnstile widget container
        try:
            turnstile_div = page.locator('[class*="cf-turnstile"]')
            if await turnstile_div.count() > 0 and await turnstile_div.first.is_visible():
                return True
        except Exception:
            pass
        # "I'm not a robot" text
        try:
            robot_txt = page.get_by_text(re.compile(r"I['\u2019]m not a robot", re.I))
            if await robot_txt.count() > 0 and await robot_txt.first.is_visible():
                return True
        except Exception:
            pass
        # "checkbox challenge" text
        try:
            chall_txt = page.get_by_text(re.compile(r"checkbox challenge", re.I))
            if await chall_txt.count() > 0 and await chall_txt.first.is_visible():
                return True
        except Exception:
            pass
        return False

    # ----- login -----

    async def _login(self) -> bool:
        """
        Login with retry. Uses CapSolver to solve Turnstile if present.
        """
        for attempt in range(1, self.cfg.login_retries + 1):
            self.log(f"[login] attempt {attempt}/{self.cfg.login_retries}")

            # Relaunch browser for a clean slate on retries
            if attempt > 1:
                self.log(f"[login] waiting {self.cfg.retry_delay_s}s before retry")
                await asyncio.sleep(self.cfg.retry_delay_s)
                await self._launch_browser_and_context()

            status = await self._attempt_login()
            if status == "ok":
                return True

            self.log(f"[login] attempt {attempt} result: {status}")

        return False

    async def _attempt_login(self) -> str:
        """
        Returns:
        "ok"      -> landed on portal
        "captcha" -> captcha detected
        "fail"    -> creds rejected / can't reach portal
        """
        page = self.page
        self.log("[login] navigating to Optimum home for login flow")
        try:
            await page.goto(OPTIMUM_HOME_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            self.log("[login] home goto timeout")
            return "fail"
        except Exception as e:
            self.log("[login] home goto err:", repr(e))
            return "fail"

        # click "Sign in" in the header
        sign_in = await self._first_present([
            page.get_by_role("link", name=re.compile(r"^\s*Sign\s*in\s*$", re.I)),
            page.get_by_role("button", name=re.compile(r"^\s*Sign\s*in\s*$", re.I)),
            page.locator('a:has-text("Sign in")'),
        ])
        if not sign_in:
            self.log("[login] could not find Sign in link/button")
            return "fail"

        try:
            await sign_in.hover()
            await _pause(page, 100, 220)
            await sign_in.click()
        except Exception:
            try:
                await sign_in.click(force=True)
            except Exception as e:
                self.log("[login] could not click Sign in:", repr(e))
                return "fail"

        # wait for login form
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

        # detect captcha early — try to solve, then continue regardless
        if await self._captcha_present():
            self.log("[login] captcha visible pre-typing, attempting to solve")
            await self._try_solve_turnstile()
            await page.wait_for_timeout(2000)
            self.log("[login] continuing with login after solve attempt")

        # locate username / password inputs
        username_input = await self._first_visible([
            page.get_by_label(re.compile(r"Username", re.I)),
            page.get_by_placeholder(re.compile(r"Username", re.I)),
            page.locator('input[name*="user" i]'),
            page.locator('input[type="text"]'),
        ])
        password_input = await self._first_visible([
            page.get_by_label(re.compile(r"Password", re.I)),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.locator('input[type="password"]'),
        ])
        if not username_input or not password_input:
            self.log("[login] could not find username/password inputs")
            return "fail"

        # human-ish typing
        await username_input.click()
        for ch in self.cfg.username:
            await username_input.type(ch, delay=_rand_delay_ms())
        await _pause(page)

        await password_input.click()
        for ch in self.cfg.password:
            await password_input.type(ch, delay=_rand_delay_ms())
        await _pause(page)

        # Try to solve Turnstile "Verify you are human" checkbox before submit
        await self._try_solve_turnstile()

        # Click Continue / Submit
        cont_btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"Continue", re.I)),
            page.locator('button[type="submit"]'),
            page.locator('input[type="submit"]'),
        ])
        if cont_btn:
            try:
                await cont_btn.hover()
                await _pause(page, 100, 220)
                await cont_btn.click()
            except Exception:
                try:
                    await password_input.press("Enter")
                except Exception:
                    pass
        else:
            try:
                await password_input.press("Enter")
            except Exception:
                self.log("[login] no Continue button and Enter failed")
                return "fail"

        # let it redirect into the business portal
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Wait for Turnstile to auto-resolve (if present)
        try:
            turnstile = page.locator(
                'iframe[src*="challenges.cloudflare.com"], [class*="cf-turnstile"]'
            )
            if await turnstile.count() > 0:
                self.log("[login] Turnstile detected, waiting up to 30s for auto-resolve…")
                try:
                    await turnstile.first.wait_for(state="hidden", timeout=30000)
                    self.log("[login] Turnstile resolved")
                except Exception:
                    self.log("[login] Turnstile did not auto-resolve within 30s")
        except Exception:
            pass

        # captcha after submit?
        if await self._captcha_present():
            self.log("[login] captcha visible after submit")
            return "captcha"

        # Allow portal JS/nav to hydrate
        try:
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Are we in the portal?
        if not await self._is_logged_in_portal():
            self.log("[login] portal heuristics say we're NOT logged in")
            return "fail"

        return "ok"

    # ----- navigation after login -----

    async def _goto_my_account(self) -> bool:
        """
        Click the My Account nav item in the top blue bar.
        """
        page = self.page

        myacct_link = page.locator('#tpTopMenuBar a[href="/myaccount"], #tpTopMenuBar a#tn1')

        try:
            await myacct_link.first.wait_for(state="visible", timeout=15000)
        except Exception:
            self.log("[acct] can't find My Account link (#tpTopMenuBar a[href='/myaccount'])")
            return False

        try:
            await myacct_link.first.hover()
            await _pause(page, 120, 240)
            await myacct_link.first.click()
        except Exception:
            try:
                await myacct_link.first.click(force=True)
            except Exception as e:
                self.log("[acct] click My Account failed:", repr(e))
                return False

        # Wait for the My Account page (with balance, PAY MY BILL, View Statements)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(800)

        return True

    async def _scrape_account_amount(self) -> Tuple[Optional[str], Optional[int]]:
        """
        On the My Account page:
            - big grey box with "$321.11" inside <div class="mrBtm5 font22"><label>$321.11</label>...</div>
        Try that first; fallback to a panel containing "Last Statement".
        """
        page = self.page

        amount_text = ""
        try:
            summary_amount_block = page.locator('div.mrBtm5.font22')
            if await summary_amount_block.count() > 0:
                amount_text = await summary_amount_block.first.inner_text()
        except Exception:
            pass

        if not amount_text:
            try:
                summary_panel = await self._first_present([
                    page.locator("div").filter(has_text=re.compile(r"Last Statement", re.I)),
                    page.locator("section").filter(has_text=re.compile(r"Last Statement", re.I)),
                ])
                amount_text = await summary_panel.inner_text() if summary_panel else ""
            except Exception:
                amount_text = ""

        amount_str, amount_cents = _money_to_cents(amount_text)
        self.log(f"[acct] parsed summary amount: {amount_str}")
        return amount_str, amount_cents

    async def _goto_statements(self) -> bool:
        page = self.page

        # Prefer the visible link by accessible name
        link = page.get_by_role("link", name=re.compile(r"^\s*View\s+Statements\s*$", re.I)).first
        try:
            await link.wait_for(state="visible", timeout=15000)
            await link.scroll_into_view_if_needed()
            await _pause(page, 120, 240)
            await link.click()
        except Exception:
            # Fallback 1: visible anchors with the href and/or class "blue"
            vis = page.locator('a[href*="/myaccount/billpay"]:visible').first
            try:
                await vis.wait_for(state="visible", timeout=5000)
                await vis.scroll_into_view_if_needed()
                await vis.click()
            except Exception:
                # Fallback 2: force a JS click on the first visible candidate
                try:
                    el = page.locator('a[href*="/myaccount/billpay"]:visible').first
                    await el.wait_for(state="visible", timeout=3000)
                    handle = await el.element_handle()
                    await page.evaluate("(e) => e.click()", handle)
                except Exception as e:
                    self.log("[stmts] click View Statements failed:", repr(e))
                    # Fallback 3: direct navigation (handles proxy-caused partial renders)
                    self.log("[stmts] trying direct navigation to /myaccount/billpay; current url=", page.url)
                    await self._snap("stmts-before-direct-nav")
                    try:
                        await page.goto(
                            "https://business.optimum.net/myaccount/billpay",
                            timeout=20000,
                            wait_until="domcontentloaded",
                        )
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception as nav_err:
                        self.log("[stmts] direct nav failed:", repr(nav_err))
                        return False

        # Wait for the Statements table
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        try:
            await page.locator("table tbody tr").first.wait_for(timeout=15000)
            return True
        except Exception:
            self.log("[stmts] statements table not visible after nav; url=", page.url)
            await self._snap("stmts-no-table")
            return False

    async def _open_latest_statement(
        self,
    ) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
        """
        Clicks the newest statement and captures the PDF via browser download
        (Chrome PDF viewer disabled → forced download).

        Returns:
            (amount_str, amount_cents, statement_date_iso, pdf_path)
        """
        page = self.page
        ctx = self.context

        # --- Get first row ---
        rows = page.locator("table tbody tr")
        if await rows.count() == 0:
            self.log("[stmts] no rows in statements table")
            return None, None, None, None

        first_row = rows.first
        row_txt = await first_row.inner_text()
        amt_str, amt_cents = _money_to_cents(row_txt)
        self.log(f"[stmts] first row amount parsed: {amt_str}")

        # --- Extract statement date BEFORE clicking ---
        stmt_link = await self._first_present(
            [first_row.locator("a"), first_row.get_by_role("link")]
        )
        if not stmt_link:
            self.log("[stmts] could not find Statement Date link")
            return amt_str, amt_cents, None, None

        statement_date_iso = None
        try:
            link_text = await stmt_link.inner_text()
            m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", link_text)
            if m:
                statement_date_iso = self._parse_us_date(m.group(1))
            self.log("[stmts] extracted statement date:", statement_date_iso)
        except Exception:
            pass

        # --- Trigger download ---
        await stmt_link.scroll_into_view_if_needed()

        # Debug: inspect the link before clicking
        try:
            href = await stmt_link.get_attribute("href")
            target = await stmt_link.get_attribute("target")
            self.log(f"[stmts] link href={href}, target={target}")
        except Exception:
            pass

        self.log("[stmts] clicking statement link and expecting download or popup")

        # Strategy 0: if href is a direct URL, download it via context.request
        try:
            href = await stmt_link.get_attribute("href")
        except Exception:
            href = None

        if href and ("billfile" in href or href.lower().endswith(".pdf")):
            self.log("[stmts] direct PDF URL detected, downloading via context.request")
            try:
                full_url = href if href.startswith("http") else f"https://business.optimum.net{href}"
                resp = await self.context.request.get(full_url)
                pdf_bytes = await resp.body()
                if pdf_bytes[:4] == b"%PDF":
                    target_path = self.download_dir / f"optimum-biz-{statement_date_iso or 'stmt'}.pdf"
                    target_path.write_bytes(pdf_bytes)
                    self.log("[stmts] PDF saved via direct URL:", str(target_path))
                    return amt_str, amt_cents, statement_date_iso, str(target_path)
            except Exception as e:
                self.log("[stmts] direct URL download failed:", repr(e))

        # href=# means JS-driven — set up network listener BEFORE clicking
        pdf_url_holder = {"url": None}
        pdf_response_holder = {"bytes": None}
        context = self.context

        # Log ALL requests after click for debugging
        def handle_request_debug(request):
            url = request.url
            if not any(skip in url for skip in ["google", "analytics", "omniture", "rlcdn", "pub.network", "cloudflare"]):
                self.log(f"[stmts][req] {request.method} {url}")

        async def handle_response(response):
            try:
                ct = response.headers.get("content-type", "") or ""
                url = response.url
                if "pdf" in ct.lower() or "billfile" in url or "bill" in url.lower():
                    self.log(f"[stmts] captured PDF response: {url} (content-type: {ct})")
                    pdf_url_holder["url"] = url
                    try:
                        pdf_response_holder["bytes"] = await response.body()
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("request", handle_request_debug)
        page.on("response", handle_response)

        # Also check for new pages (popups)
        new_pages = []
        context.on("page", lambda p: new_pages.append(p))

        # Click and wait for network activity
        try:
            await stmt_link.click()
        except Exception:
            await stmt_link.click(force=True)

        self.log("[stmts] clicked, waiting for page to settle...")

        # Wait for page to load after click
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        self.log(f"[stmts] after click: url={page.url}")

        # The click may have loaded a statement viewer page — look for PDF links/buttons
        # Debug: dump all links and buttons on the page
        try:
            all_links = await page.evaluate("""() => {
                const items = [];
                document.querySelectorAll('a, button, input[type="submit"]').forEach(el => {
                    const text = (el.innerText || el.value || '').trim().substring(0, 80);
                    const href = el.href || el.getAttribute('href') || '';
                    const onclick = el.getAttribute('onclick') || '';
                    if (text || href || onclick) {
                        items.push({tag: el.tagName, text, href: href.substring(0, 200), onclick: onclick.substring(0, 200)});
                    }
                });
                return items;
            }""")
            for item in all_links:
                if any(kw in (item.get('text','') + item.get('href','') + item.get('onclick','')).lower()
                       for kw in ['pdf', 'download', 'print', 'save', 'bill', 'statement', 'view']):
                    self.log(f"[stmts][link] {item}")
        except Exception as e:
            self.log(f"[stmts] link scan failed: {e}")

        # The first click navigated to "Statements Available" page with real PDF links.
        # Find and click the first implPreLoadBillTypePDFSwitchAction link.
        pdf_action_link = page.locator('a[href*="implPreLoadBillTypePDFSwitchAction"]').first
        try:
            pdf_action_count = await pdf_action_link.count()
        except Exception:
            pdf_action_count = 0

        if pdf_action_count > 0:
            pdf_href = await pdf_action_link.get_attribute("href")
            self.log("[stmts] found PDF action link:", pdf_href)

            # Link has target="_blank" but may be hidden — use JS to navigate
            # Build full URL from relative href
            full_url = pdf_href if pdf_href.startswith("http") else await page.evaluate(
                "(href) => new URL(href, document.baseURI).href", pdf_href
            )
            self.log("[stmts] navigating to PDF URL in new page:", full_url)

            # Open in a new page to preserve session cookies
            pdf_page = await context.new_page()
            try:
                # Listen for download on the new page
                download = None
                try:
                    async with pdf_page.expect_download(timeout=30000) as dl_info:
                        await pdf_page.goto(full_url, wait_until="commit", timeout=30000)
                    download = await dl_info.value
                    pdf_path = await self._save_download(download)
                    if pdf_path and Path(pdf_path).read_bytes()[:4] == b"%PDF":
                        self.log("[stmts] PDF saved via new page download:", pdf_path)
                        return amt_str, amt_cents, statement_date_iso, pdf_path
                except Exception as e:
                    self.log("[stmts] new page download didn't trigger:", repr(e))

                # Maybe the page loaded the PDF directly — capture from network
                pdf_bytes = await self._capture_pdf_via_network(pdf_page)
                if pdf_bytes and pdf_bytes[:4] == b"%PDF":
                    target_path = self.download_dir / f"optimum-biz-{statement_date_iso or 'stmt'}.pdf"
                    target_path.write_bytes(pdf_bytes)
                    self.log("[stmts] PDF saved via network capture on new page:", str(target_path))
                    return amt_str, amt_cents, statement_date_iso, str(target_path)

                # Try reading the page content as PDF (in case goto loaded it inline)
                try:
                    resp = await context.request.get(full_url, timeout=60000)
                    body = await resp.body()
                    if body[:4] == b"%PDF":
                        target_path = self.download_dir / f"optimum-biz-{statement_date_iso or 'stmt'}.pdf"
                        target_path.write_bytes(body)
                        self.log("[stmts] PDF saved via context.request.get:", str(target_path))
                        return amt_str, amt_cents, statement_date_iso, str(target_path)
                    else:
                        self.log(f"[stmts] context.request returned non-PDF ({len(body)} bytes)")
                except Exception as e:
                    self.log("[stmts] context.request.get failed:", repr(e))
            finally:
                try:
                    await pdf_page.close()
                except Exception:
                    pass

        self.log("[stmts] all PDF download strategies failed")
        return amt_str, amt_cents, statement_date_iso, None

    # ----- PDF capture helpers / download logic -----

    async def _capture_pdf_via_network(self, pdf_page: Page) -> Optional[bytes]:
        """
        Listen for any network response on this page that returns application/pdf.
        Return the bytes if we catch them.
        """
        pdf_bytes_box = {"task": None}

        def _on_response(resp):
            try:
                ct = resp.headers.get("content-type", "") or resp.headers.get("Content-Type", "")
                if ct and "pdf" in ct.lower():
                    self.log(f"[pdf-net] captured PDF response: {resp.url}")
                    pdf_bytes_box["task"] = asyncio.create_task(resp.body())
            except Exception as e:
                self.log("[pdf-net] listener error:", repr(e))

        pdf_page.on("response", _on_response)

        # Wait a moment for network to settle
        try:
            await pdf_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)

        if pdf_bytes_box["task"]:
            try:
                data = await pdf_bytes_box["task"]
                if data and data[:4] == b"%PDF":
                    self.log("[pdf-net] got valid PDF bytes from network listener")
                    return data
            except Exception as e:
                self.log("[pdf-net] failed to resolve body task:", repr(e))

        return None

    async def _get_viewer_src(self, page: Page) -> Optional[str]:
        try:
            return await page.evaluate("""
                () => {
                const u = new URL(location.href);
                const s = u.searchParams.get('src') || u.searchParams.get('file');
                return s ? decodeURIComponent(s) :
                        (document.querySelector('embed[type="application/pdf"]')?.src || null);
                }
            """)
        except Exception:
            return None

    async def _download_pdf_from_statement(self, pdf_page: Page, pdf_initial_url: Optional[str]) -> Optional[str]:
        """
        Download the statement PDF.
        """

        # ----- PRE-HOP: if we're on the AMSS statement page (HTML), click the "view/print" control -----
        try:
            url_l = (pdf_page.url or "").lower()
            if "cbvprod.idahs.com" in url_l and "implinitinsertsnamespaceaction.do" in url_l:
                self.log("[pdf] on AMSS statement HTML page; trying to trigger viewer")

                # 1) First try obvious link/button text
                click_candidates = [
                    ':is(a,button,input)[href*="pdf" i]',
                    ':is(a,button,input)[onclick*="pdf" i]',
                    ':is(a,button,input):has-text("View")',
                    ':is(a,button,input):has-text("Print")',
                    ':is(a,button,input):has-text("Download")',
                    ':is(a,button,input):has-text("Statement")',
                ]

                target = None
                for sel in click_candidates:
                    loc = pdf_page.locator(sel).first
                    try:
                        if await loc.count() > 0 and await loc.is_visible():
                            target = loc
                            break
                    except Exception:
                        pass

                # 2) If nothing obvious, scan onclick/href attributes looking for window.open(...) or *pdf*
                if not target:
                    try:
                        candidate = await pdf_page.evaluate("""
                            () => {
                            const els = Array.from(document.querySelectorAll('a,button,input'));
                            for (const el of els) {
                                const txt = (el.textContent||'').toLowerCase();
                                const href = (el.getAttribute('href')||'').toLowerCase();
                                const on   = (el.getAttribute('onclick')||'').toLowerCase();
                                if (href.includes('pdf') || on.includes('pdf') || /window\\.open\\(.+pdf/i.test(on)) {
                                return { selector: el.tagName.toLowerCase() + (el.id ? '#'+el.id : ''), idx: els.indexOf(el) };
                                }
                            }
                            return null;
                            }
                        """)
                        if candidate:
                            # fall back to nth-of-type when idless
                            target = pdf_page.locator(f"{candidate['selector']}").nth(candidate["idx"])
                    except Exception:
                        pass

                if target:
                    self.log("[pdf] clicking to open viewer/download…")
                    popup = None
                    try:
                        async with self.context.expect_page() as ev:
                            await target.click()
                        popup = await ev.value
                    except Exception:
                        # some flows navigate same tab
                        pass

                    # If popup opened, use it; else we may have same-tab navigation
                    if popup:
                        try:
                            await popup.wait_for_load_state("domcontentloaded", timeout=20000)
                        except Exception:
                            pass
                        pdf_page = popup

                    # Seed initial URL after the click
                    try:
                        pdf_initial_url = pdf_page.url
                        self.log("[pdf] after click, page/url:", pdf_initial_url)
                    except Exception:
                        pass
        except Exception as e:
            self.log("[pdf] pre-hop viewer trigger failed (non-fatal):", repr(e))

        # -------- Strategy 0: Chrome built-in viewer preamble --------
        try:
            viewer_url = pdf_page.url or ""
            if viewer_url.startswith("chrome-extension://") and (
                "pdf_viewer" in viewer_url or "generated_pdf_viewer" in viewer_url
            ):
                self.log("[pdf] Chrome viewer detected; extracting src")
                src = await pdf_page.evaluate("""
                    () => {
                    const u = new URL(location.href);
                    const s = u.searchParams.get('src') || u.searchParams.get('file');
                    return s ? decodeURIComponent(s) : null;
                    }
                """)
                if src:
                    try:
                        b64 = await pdf_page.evaluate(
                            """async (u) => {
                                const r = await fetch(u, { credentials: 'include' });
                                if (!r.ok) throw new Error('status ' + r.status);
                                const buf = await r.arrayBuffer();
                                const bytes = new Uint8Array(buf);
                                let s = '';
                                for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
                                return btoa(s);
                            }""",
                            src,
                        )
                        if b64:
                            data0 = base64.b64decode(b64)
                            if data0 and data0[:4] == b"%PDF":
                                return self._write_pdf_bytes("optimum-statement.pdf", data0)
                    except Exception as e:
                        self.log("[pdf] in-page fetch failed; will fall back:", repr(e))
                    pdf_initial_url = src
        except Exception as e:
            self.log("[pdf] viewer preamble errored; proceeding with defaults:", repr(e))

        # -------- Strategy 1: network sniff for application/pdf --------
        data = await self._capture_pdf_via_network(pdf_page)
        if data and data[:4] == b"%PDF":
            return self._write_pdf_bytes("optimum-statement.pdf", data)

        # -------- Strategy 2: direct GET of the initial PDF URL --------
        if pdf_initial_url:
            try:
                self.log("[pdf] trying direct GET of initial URL:", pdf_initial_url)
                # 🔽 ADD THIS HEADER
                resp = await self.context.request.get(
                    pdf_initial_url,
                    headers={"Referer": "https://business.optimum.net/"},
                )
                if resp.ok:
                    data2 = await resp.body()
                    if data2 and data2[:4] == b"%PDF":
                        return self._write_pdf_bytes("optimum-statement.pdf", data2)
                    else:
                        self.log("[pdf] direct GET returned non-PDF bytes")
                else:
                    self.log("[pdf] direct GET not ok:", resp.status)
            except Exception as e:
                self.log("[pdf] direct GET via initial URL failed:", repr(e))

        # -------- Strategy 3: toolbar / DOM fallbacks --------
        toolbar_selectors = [
            'cr-icon-button#download',
            'cr-icon-button[aria-label*="Download" i]',
            '#download',
            'button#download',
            'a#download',
            'button[title*="Download" i]',
            'button[aria-label*="Download" i]',
            ':is(a,button)[aria-label*="Download" i]',
            ':is(a,button):has-text("Download")',
            ':is(a,button):has-text("Save")',
        ]

        for sel in toolbar_selectors:
            try:
                loc = pdf_page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    self.log(f"[pdf] trying toolbar selector: {sel}")
                    async with pdf_page.expect_download(timeout=25000) as dl_info:
                        await loc.first.click()
                    dl = await dl_info.value
                    path = await self._save_download(dl)
                    if path:
                        try:
                            if Path(path).read_bytes()[:4] == b"%PDF":
                                return path
                        except Exception:
                            pass
                    self.log("[pdf] toolbar download not valid PDF, continuing…")
            except Exception:
                continue

        # Scan DOM in pdf_page for blob:/data:/http(s) URLs
        try:
            srcs = await pdf_page.evaluate("""
                () => {
                const urls = new Set();
                const sels = ['a[href]', 'embed', 'object', 'iframe'];
                document.querySelectorAll(sels.join(',')).forEach(el => {
                    ['href','src','data'].forEach(attr => {
                    const v = el.getAttribute && el.getAttribute(attr);
                    if (v) urls.add(v);
                    });
                });
                return Array.from(urls);
                }
            """)
        except Exception:
            srcs = []

        for raw in srcs:
            if not raw:
                continue
            abs_url = raw
            if not raw.startswith(("http://", "https://", "blob:", "data:")):
                abs_url = urljoin(pdf_page.url, raw)

            self.log("[pdf] candidate src:", abs_url[:200])

            # blob:
            if abs_url.startswith("blob:"):
                try:
                    blob_bytes = await self._fetch_blob_via_page(pdf_page, abs_url)
                    if blob_bytes and blob_bytes[:4] == b"%PDF":
                        return self._write_pdf_bytes("optimum-statement.pdf", blob_bytes)
                except Exception as e:
                    self.log("[pdf] blob fetch failed:", repr(e))
                continue

            # data:
            if abs_url.startswith("data:"):
                data_bytes = self._decode_data_url(abs_url)
                if data_bytes and data_bytes[:4] == b"%PDF":
                    return self._write_pdf_bytes("optimum-statement.pdf", data_bytes)
                continue

            # direct GET of http(s)
            if abs_url.startswith(("http://", "https://")):
                try:
                    resp = await self.context.request.get(abs_url)
                    if resp.ok:
                        body = await resp.body()
                        if body[:4] == b"%PDF":
                            return self._write_pdf_bytes("optimum-statement.pdf", body)
                except Exception as e:
                    self.log("[pdf] direct GET failed:", repr(e))
                continue

        return None

    async def _fetch_blob_via_page(self, page: Page, blob_url: str) -> Optional[bytes]:
        """
        Fetch blob: URL bytes inside the page context by using page.evaluate.
        """
        try:
            b64 = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u);
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let binary = '';
                    for (let i = 0; i < bytes.length; i++) {
                        binary += String.fromCharCode(bytes[i]);
                    }
                    return btoa(binary);
                }""",
                blob_url,
            )
            return base64.b64decode(b64) if b64 else None
        except Exception as e:
            self.log("[pdf] blob fetch failed:", repr(e))
            return None

    def _decode_data_url(self, data_url: str) -> Optional[bytes]:
        """
        Decode a data: URL that is base64-encoded.
        """
        try:
            if ";base64," in data_url:
                return base64.b64decode(data_url.split(";base64,", 1)[1])
            return None
        except Exception:
            return None

    def _write_pdf_bytes(self, suggested: str, data: bytes) -> Optional[str]:
        """
        Write PDF bytes to disk and return the path.
        """
        if not data or data[:4] != b"%PDF":
            self.log("[pdf] invalid PDF bytes, rejecting")
            return None
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        target.write_bytes(data)
        self.log("[pdf] wrote PDF:", str(target))
        return str(target)

    async def _save_download(self, dl: Download) -> Optional[str]:
        """
        Save a Playwright download (from expect_download()) to disk.
        """
        suggested = dl.suggested_filename or "optimum-statement.pdf"
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        await dl.save_as(str(target))
        self.log("[pdf] saved:", str(target))
        return str(target)

    def _parse_dates_from_pdf(self, pdf_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Extract from the Optimum statement PDF:
        - due_date: from 'Payment Due Date:' or 'Due Date' line (Month DD, YYYY)
        - period_start / period_end: from 'Billing Period' range (MM/DD/YY - MM/DD/YY)
        Returns (due_date_iso, period_start_iso, period_end_iso).
        """
        due_date_iso = None
        period_start_iso = None
        period_end_iso = None

        # ---- read PDF text via pypdf ----
        try:
            reader = PdfReader(pdf_path)
            texts: List[str] = []
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t:
                    texts.append(t)
            text = "\n".join(texts)
        except Exception as e:
            self.log("[pdf] failed to read Optimum PDF:", repr(e))
            return due_date_iso, period_start_iso, period_end_iso

        if not text:
            self.log("[pdf] empty text when parsing Optimum dates")
            return due_date_iso, period_start_iso, period_end_iso

        # ---- 1) Due date (Month DD, YYYY) ----
        try:
            # Prefer the explicit "Payment Due Date:"
            m_due = re.search(
                r"Payment\s+Due\s+Date:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
                text,
                re.IGNORECASE,
            )
            if not m_due:
                # Fallback: any 'Due Date' label with a long date nearby
                m_due = re.search(
                    r"Due\s+Date[^A-Za-z0-9]{0,40}([A-Za-z]+\s+\d{1,2},\s*\d{4})",
                    text,
                    re.IGNORECASE,
                )

            if m_due:
                raw_due = m_due.group(1)
                due_date_iso = self._parse_long_us_date(raw_due)
                self.log("[pdf] Optimum due date raw:", raw_due, "→", due_date_iso)
        except Exception as e:
            self.log("[pdf] error parsing Optimum due date:", repr(e))

        # ---- 2) Billing period (MM/DD/YY - MM/DD/YY) ----
        try:
            # Focus first on the block after "Billing Period"
            m_block = re.search(
                r"Billing\s+Period(?P<after>.*?)(?:\n\s*\n|\Z)",
                text,
                re.IGNORECASE | re.DOTALL,
            )
            block = m_block.group("after") if m_block else text

            m_range = re.search(
                r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*[-–]\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                block,
            )
            if not m_range:
                # Fallback: whole doc
                m_range = re.search(
                    r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*[-–]\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                    text,
                )

            if m_range:
                raw_start, raw_end = m_range.groups()
                period_start_iso = self._parse_us_date(raw_start)
                period_end_iso = self._parse_us_date(raw_end)
                self.log(
                    "[pdf] Optimum billing period raw:",
                    raw_start,
                    "-",
                    raw_end,
                    "→",
                    period_start_iso,
                    period_end_iso,
                )
        except Exception as e:
            self.log("[pdf] error parsing Optimum billing period:", repr(e))

        return due_date_iso, period_start_iso, period_end_iso


    # ----- tiny locator utils -----

    async def _first_visible(self, locators: List, timeout: int = 7000):
        for loc in locators:
            try:
                await loc.first.wait_for(state="visible", timeout=timeout)
                return loc.first
            except Exception:
                continue
        return None

    async def _first_present(self, locators: List):
        for loc in locators:
            try:
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None


# ------------ CLI ------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Optimum Business scraper (amount due + latest statement PDF)"
    )
    p.add_argument("--username", default=os.getenv("OPTIMUM_USERNAME", ""), help="Optimum username / Optimum ID")
    p.add_argument("--password", default=os.getenv("OPTIMUM_PASSWORD", ""), help="Optimum password")
    p.add_argument("--headful", action="store_true", help="Run with browser window visible")
    p.add_argument("--slow-mo", type=int, default=int(os.getenv("OPTIMUM_SLOW_MO_MS", "0")),
                   help="Slow motion ms between Playwright actions")
    p.add_argument("--json", action="store_true", help="Print JSON result instead of just the amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    # Load env file for creds if present
    load_kv_env_file(ENV_FILE)

    args = _parse_args(argv)

    cfg = Config(
        username=args.username,
        password=args.password,
        headless=not args.headful,  # <-- headful flag maps to our real-Chrome path
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        timezone_id=os.getenv("OPTIMUM_TIMEZONE_ID", "America/New_York"),
    )

    async with OptimumScraper(cfg) as s:
        result = await s.run()

    if not result.ok:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "amount": result.amount,
            "amount_cents": result.amount_cents,
            "pdf_path": result.pdf_path,
            "final_url": result.final_url,
            "statement_date": result.statement_date,
            "due_date": result.due_date,
            "period_start": result.period_start,
            "period_end": result.period_end,
        }, indent=2))
    else:
        print(result.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
