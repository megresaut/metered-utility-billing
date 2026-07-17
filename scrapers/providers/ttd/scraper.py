#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTD (Third Taxing District / InvoiceCloud) scraper with:
- Bright Data rotating session proxy
- Stealth browser context
- storage_state reuse to skip login (and therefore skip CAPTCHA)

High-level flow:
1. Load env (for Bright proxy creds).
2. Launch Chromium with stealth + (optional) Bright proxy (rotating session username).
3. If we have storage_state.json and it's still valid (we can reach dashboard),
   skip login entirely.
4. Otherwise:
   - Try to log in with human-ish typing.
   - If we detect CAPTCHA or fail to reach dashboard:
       * If Bright proxy is configured, relaunch browser with a NEW session username
         and retry login once.
5. Go to Recent Payments, grab the first row, click "View Invoice", download PDF.
6. Parse amount from the row text.

Security note:
We are intentionally *not* solving reCAPTCHA.
We aim to avoid triggering it by looking like a normal user and by reusing session cookies.
"""

import asyncio
import base64
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urljoin
from datetime import datetime, timedelta
from pypdf import PdfReader

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
)

# ---------------- Paths & constants ----------------

HERE = Path(__file__).resolve()
SCRAPERS_ROOT = HERE.parents[2]          # .../scrapers
ENV_FILE = SCRAPERS_ROOT / "env"         # scrapers/env
DATA_ROOT = HERE.parent                  # .../scrapers/providers/ttd

LOGIN_URL = (
    "https://www.invoicecloud.com/portal/"
    "(S(mqvpew3lzbgqwamy0mvvdu0))/2/customerlogin.aspx"
    "?billerguid=9645deae-9003-4374-a520-dc28673495cb"
)

DEFAULT_DL_DIR = Path(
    os.getenv("TTD_DOWNLOAD_DIR", "/handoff/ttd")
).resolve()
STORAGE_STATE_PATH = Path(os.getenv("TTD_STORAGE_STATE", str(DATA_ROOT / "ttd_storage.json"))).expanduser().resolve()

AMOUNT_RE = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})", re.I)


# ---------------- Models ----------------

@dataclass
class Config:
    username: str
    password: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False

    # Bright Data
    use_bright_proxy: bool = False
    bright_proxy: Optional[dict] = None

    # Fingerprint-ish
    timezone_id: str = os.getenv("TTD_TIMEZONE_ID", "America/New_York")

@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # new date fields
    statement_date: Optional[str] = None  # ISO "YYYY-MM-DD"
    due_date: Optional[str] = None        # ISO "YYYY-MM-DD"
    period_start: Optional[str] = None    # ISO "YYYY-MM-DD"
    period_end: Optional[str] = None      # ISO "YYYY-MM-DD"


# ---------------- Env loading ----------------

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


# ---------------- Helpers ----------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _money_to_cents(txt: str) -> Tuple[Optional[str], Optional[int]]:
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


# ---------------- Scraper ----------------

class TTDScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg

        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self.download_dir: Path = DEFAULT_DL_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Active Bright proxy with rotated session username
        self._active_proxy = None

    # ---------- logging ----------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ---------- stealth / proxy helpers ----------

    def _proxy_username_with_session(self, base_username: Optional[str]) -> Optional[str]:
        """
        Bright Data best practice:
        rotate session so IP/fingerprint correlation breaks less often.
        example: br_user -> br_user-session-abc123xyz
        """
        if not base_username:
            return None
        if "session-" in base_username:
            return base_username
        sess = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10))
        return f"{base_username}-session-{sess}"

    def _rand_viewport(self):
        widths = [1280, 1366, 1440, 1536, 1600]
        heights = [720, 768, 800, 900]
        return random.choice(widths), random.choice(heights)

    def _build_launch_kwargs(self, rotate_session: bool = False):
        """
        Build Chromium launch kwargs with stealth args and optional Bright Data proxy.
        rotate_session=True will force-generate a new proxy username session-<rand>.
        """
        launch_kwargs = dict(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--headless=new" if self.cfg.headless else "",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        bright = self.cfg.bright_proxy if self.cfg.use_bright_proxy else None
        if bright and bright.get("server"):
            username = bright.get("username")
            if rotate_session or not self._active_proxy:
                username = self._proxy_username_with_session(username)
            self._active_proxy = {
                "server": bright["server"],
                "username": username,
                "password": bright.get("password"),
            }
            launch_kwargs["proxy"] = self._active_proxy
            self.log("[pw] using proxy:", self._active_proxy["server"], "user:", username)
        else:
            self._active_proxy = None

        return launch_kwargs

    def _build_context_kwargs(self, use_storage_state: bool):
        """
        Create a more realistic browser context: viewport, UA, tz, etc.
        """
        w, h = self._rand_viewport()
        ctx = {
            "accept_downloads": True,
            "viewport": {"width": w, "height": h},
            "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": self.cfg.timezone_id,
        }
        if use_storage_state and STORAGE_STATE_PATH.exists():
            ctx["storage_state"] = str(STORAGE_STATE_PATH)
            self.log("[context] loading storage_state from", STORAGE_STATE_PATH)
        return ctx

    async def _install_stealth(self):
        """
        Patch obvious automation fingerprints.
        """
        script = """
        // webdriver flag
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        // chrome runtime presence
        window.chrome = { runtime: {} };
        // languages
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        // platform
        Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        // touch points
        Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1});
        """
        try:
            await self.context.add_init_script(script)
        except Exception:
            pass

    # ---------- lifecycle: (re)launch browser ----------

    async def _launch_browser_and_context(self, *, use_storage_state: bool, rotate_session: bool):
        # Close old stuff if any
        await self._close()

        if not self._pw:
            self._pw = await async_playwright().start()

        launch_kwargs = self._build_launch_kwargs(rotate_session=rotate_session)
        self.browser = await self._pw.chromium.launch(**launch_kwargs)

        context_kwargs = self._build_context_kwargs(use_storage_state=use_storage_state)
        self.context = await self.browser.new_context(**context_kwargs)
        await self._install_stealth()

        self.page = await self.context.new_page()
        if self.cfg.debug:
            self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))

    async def _close(self):
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
        self.context = None
        self.browser = None
        self.page = None

    async def __aenter__(self):
        # first attempt: reuse storage state (no rotate yet, we want continuity)
        await self._launch_browser_and_context(use_storage_state=True, rotate_session=False)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] closing")
        await self._close()
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # ---------- public main ----------

    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        # 1. Try to use existing session cookies first
        self.log("[run] checking existing session / storage_state")
        if await self._has_valid_session():
            self.log("[run] session is valid without login")
        else:
            self.log("[run] session invalid, starting login workflow")
            if not await self._login_with_retry():
                return Result(ok=False, error="Login failed (captcha / block / bad creds)")

        # 2. We're authenticated now → go to Recent Payments
        if not await self._goto_recent_payments():
            return Result(ok=False, error="Could not open Recent Payments")

        # 3. Download most recent invoice
        amount, amount_cents, pdf_path = await self._click_latest_view_invoice_and_download()
        if not pdf_path:
            return Result(ok=False, error="Failed to download invoice PDF")

        statement_date, due_date, period_start, period_end = self._parse_dates_from_pdf(pdf_path)

        return Result(
            ok=True,
            amount=amount,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=self.page.url,
            statement_date=statement_date,
            due_date=due_date,
            period_start=period_start,
            period_end=period_end,
        )
    # ---------- session / login logic ----------

    async def _has_valid_session(self) -> bool:
        """
        Hit LOGIN_URL and check if we *already* land on a dashboard-ish page.
        If we see login form or captcha prompt, session is not valid.
        """
        page = self.page
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except Exception as e:
            self.log("[session-check] goto failed:", repr(e))
            return False

        if await self._captcha_present():
            self.log("[session-check] captcha visible -> not logged in")
            return False

        # dashboard cues
        if await page.get_by_text(re.compile(r"Your Account At A Glance|Recent Payments", re.I)).count() > 0:
            self.log("[session-check] dashboard content present")
            return True

        # still on login-ish form?
        if await page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)).count() > 0:
            self.log("[session-check] still seeing Sign In form")
            return False

        # mild fallback: if we don't *clearly* see dashboard, assume not logged in
        return False

    async def _login_with_retry(self) -> bool:
        """
        Try login in current browser/context.
        If:
          - login succeeds -> save storage_state
          - login fails due to captcha or no dashboard ->
              if Bright proxy is configured, rotate session and retry ONCE.
        """
        status = await self._attempt_login()
        if status == "ok":
            return True

        # If we failed due to CAPTCHA / block AND we have Bright proxy,
        # rotate session username and relaunch browser fresh (no storage_state yet),
        # then try again.
        if self.cfg.use_bright_proxy:
            self.log("[login] rotating Bright Data session and retrying login once")
            await self._launch_browser_and_context(use_storage_state=False, rotate_session=True)
            status2 = await self._attempt_login()
            return status2 == "ok"

        return False

    async def _attempt_login(self) -> str:
        """
        Returns:
          "ok"        -> dashboard reached and storage_state saved
          "captcha"   -> captcha detected on login form
          "fail"      -> other failure
        """
        page = self.page
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            self.log("[login] initial goto timed out")
            return "fail"
        except Exception as e:
            self.log("[login] initial goto failed:", repr(e))
            return "fail"

        # If captcha is already there before typing, we're blocked right away
        if await self._captcha_present():
            self.log("[login] captcha present before typing")
            return "captcha"

        email = await self._first_visible([
            page.get_by_label(re.compile(r"Email Address", re.I)),
            page.get_by_placeholder(re.compile(r"Email", re.I)),
            page.locator('input[type="email"]'),
        ])
        pwd = await self._first_visible([
            page.get_by_label(re.compile(r"Password", re.I)),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.locator('input[type="password"]'),
        ])

        if not email or not pwd:
            self.log("[login] could not locate email/password inputs")
            return "fail"

        # Human-ish typing to look less botty
        await email.click()
        for ch in self.cfg.username:
            await email.type(ch, delay=_rand_delay_ms())
        await _pause(page)

        await pwd.click()
        for ch in self.cfg.password:
            await pwd.type(ch, delay=_rand_delay_ms())
        await _pause(page)

        # Check again for captcha BEFORE attempting submit
        if await self._captcha_present():
            self.log("[login] captcha popped after typing creds (before submit)")
            return "captcha"

        # Click Sign In (or press Enter)
        btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('button[type="submit"], input[type="submit"]'),
        ])
        if btn:
            try:
                await btn.hover()
                await _pause(page, 100, 220)
                await btn.click()
            except Exception:
                await pwd.press("Enter")
        else:
            await pwd.press("Enter")

        # Let page settle
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

        # After submit: captcha visible?
        if await self._captcha_present():
            self.log("[login] captcha visible after submit")
            return "captcha"

        # Dashboard check
        try:
            await page.get_by_text(
                re.compile(r"Your Account At A Glance|Recent Payments", re.I)
            ).first.wait_for(timeout=25000)
            self.log("[login] dashboard detected, saving storage_state")
            # persist cookies so next run skips captcha entirely
            try:
                await self.context.storage_state(path=str(STORAGE_STATE_PATH))
                self.log("[login] saved storage_state to", STORAGE_STATE_PATH)
            except Exception as e:
                self.log("[login] failed saving storage_state:", repr(e))
            return "ok"
        except Exception:
            self.log("[login] dashboard not detected after submit")
            return "fail"

    async def _captcha_present(self) -> bool:
        """
        Detects "I'm not a robot" checkbox UI and/or the red error box you showed.
        We are not going to solve it; just treat as "blocked".
        """
        page = self.page
        try:
            # visible recaptcha iframe or checkbox label
            captcha_box = page.locator('iframe[src*="recaptcha"]')
            if await captcha_box.count() > 0 and await captcha_box.first.is_visible():
                return True
        except Exception:
            pass

        try:
            # the red banner that literally says "Please complete the checkbox challenge below."
            banner = page.get_by_text(re.compile(r"checkbox challenge", re.I))
            if await banner.count() > 0 and await banner.first.is_visible():
                return True
        except Exception:
            pass

        try:
            # the "I'm not a robot" label text
            robot = page.get_by_text(re.compile(r"I['’]m not a robot", re.I))
            if await robot.count() > 0 and await robot.first.is_visible():
                return True
        except Exception:
            pass

        return False

    # ---------- Recent Payments navigation ----------

    async def _goto_recent_payments(self) -> bool:
        page = self.page

        # --- 1) Inspect "Recent Open Invoices" tile on the dashboard ---
        use_open_invoices = False
        try:
            header_loc = page.get_by_text(re.compile(r"Recent Open Invoices", re.I))
            if await header_loc.count() > 0:
                header = header_loc.first
                # Walk up a few parents to get the tile container
                panel = header
                for _ in range(3):
                    panel = panel.locator("xpath=..")

                no_hist = panel.get_by_text(re.compile(r"No History Available", re.I))
                if await no_hist.count() > 0 and await no_hist.first.is_visible():
                    self.log("[nav] Open Invoices tile shows 'No History Available' → skip it")
                    use_open_invoices = False
                else:
                    self.log("[nav] Open Invoices tile appears to have history → will use it")
                    use_open_invoices = True
        except Exception as e:
            self.log("[nav] could not inspect Open Invoices tile:", repr(e))
            # If inspection fails, just fall back to Recent Payments
            use_open_invoices = False

        # --- 2) If there IS open-invoice history, click that first -----
        if use_open_invoices:
            open_loc = await self._first_present([
                page.get_by_role("link", name=re.compile(r"Recent Open Invoices", re.I)),
                page.get_by_role("button", name=re.compile(r"Recent Open Invoices", re.I)),
                page.get_by_text(re.compile(r"Recent Open Invoices", re.I)),
            ])
            if open_loc:
                try:
                    await open_loc.scroll_into_view_if_needed()
                except Exception:
                    pass

                try:
                    await open_loc.hover()
                    await _pause(page, 120, 240)
                except Exception:
                    pass

                try:
                    await open_loc.click()
                except Exception:
                    try:
                        await open_loc.click(force=True)
                    except Exception:
                        self.log("[nav] click on 'Recent Open Invoices' failed; falling back to 'Recent Payments'")
                else:
                    # Wait for a table with rows; if found, we use this page
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass

                    try:
                        await page.locator("table").locator("tbody tr").first.wait_for(timeout=20000)
                        self.log("[nav] using Open Invoices page")
                        return True
                    except Exception:
                        self.log("[nav] Open Invoices page has no table rows; falling back to 'Recent Payments'")
            else:
                self.log("[nav] 'Recent Open Invoices' control not found; falling back to 'Recent Payments'")

        # --- 3) Default / fallback: go to "Recent Payments" ------------
        loc = await self._first_present([
            page.get_by_role("link", name=re.compile(r"Recent Payments", re.I)),
            page.get_by_role("button", name=re.compile(r"Recent Payments", re.I)),
            page.get_by_text(re.compile(r"Recent Payments", re.I)),
        ])
        if not loc:
            self.log("[payments] Recent Payments control not found")
            return False

        try:
            await loc.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            await loc.hover()
            await _pause(page, 120, 240)
        except Exception:
            pass

        try:
            await loc.click()
        except Exception:
            try:
                await loc.click(force=True)
            except Exception:
                self.log("[payments] could not click 'Recent Payments'")
                return False

        # Wait for visible payment table
        try:
            await page.get_by_text(re.compile(r"Payment History", re.I)).first.wait_for(timeout=15000)
        except Exception:
            pass
        try:
            await page.locator("table").locator("tbody tr").first.wait_for(timeout=20000)
            return True
        except Exception:
            self.log("[payments] no payment rows after navigation")
            return False

    # ---------- Invoice download ----------

    async def _click_latest_view_invoice_and_download(self) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        page = self.page

        rows = page.locator("table tbody tr")
        count = await rows.count()
        if count == 0:
            self.log("[payments] no rows found in payments table")
            return None, None, None

        first_row = rows.first

        # Parse amount from row text
        try:
            amount_text = await first_row.inner_text()
        except Exception:
            amount_text = ""
        amount_str, amount_cents = _money_to_cents(amount_text)
        self.log(f"[payments] latest row amount parsed: {amount_str}")

        # Find "View Invoice" within that row
        view_btn = await self._first_present([
            first_row.get_by_role("link", name=re.compile(r"View Invoice", re.I)),
            first_row.get_by_role("button", name=re.compile(r"View Invoice", re.I)),
            first_row.locator(':is(a,button):has-text("View Invoice")'),
        ])
        if not view_btn:
            self.log("[payments] 'View Invoice' not found on first row")
            return amount_str, amount_cents, None

        # Click + wait for invoice view to load
        try:
            await view_btn.scroll_into_view_if_needed()
            await view_btn.click()
        except Exception:
            try:
                await view_btn.click(force=True)
            except Exception:
                self.log("[payments] could not click 'View Invoice'")
                return amount_str, amount_cents, None

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await _pause(page, 500, 800)

        pdf_path = await self._download_pdf_from_invoice_page(page)
        return amount_str, amount_cents, pdf_path

    # ---------- PDF fetching ----------

    async def _download_pdf_from_invoice_page(self, page: Page) -> Optional[str]:
        """
        Try 3 approaches:
        1. Chrome PDF viewer toolbar ("Download" button) via expect_download
        2. Grab blob:/data: URL from DOM and dump bytes
        3. Direct HTTP GET of embedded invoice link
        """
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
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    self.log(f"[download] trying toolbar selector: {sel}")
                    async with page.expect_download(timeout=25000) as dl_info:
                        await loc.first.click()
                    dl = await dl_info.value
                    path = await self._save_download(dl)
                    if path and Path(path).read_bytes()[:4] == b"%PDF":
                        return path
                    self.log("[download] toolbar file wasn't valid PDF, continuing…")
            except Exception:
                continue

        # Fallback: hunt for blob/data/src attributes
        try:
            srcs = await page.evaluate("""
                () => {
                  const urls = new Set();
                  const sels = ['a[href]', 'embed', 'object', 'iframe'];
                  document.querySelectorAll(sels.join(',')).forEach(el => {
                    ['href','src','data'].forEach(attr => {
                      const val = el.getAttribute && el.getAttribute(attr);
                      if (val) urls.add(val);
                    });
                  });
                  return Array.from(urls);
                }
            """)
        except Exception:
            srcs = []

        for href in srcs:
            if not href:
                continue
            abs_href = href
            if not href.startswith(("http://", "https://", "blob:", "data:")):
                abs_href = urljoin(page.url, href)

            self.log("[download] inspecting candidate src:", abs_href[:120])

            # blob: URL -> fetch via page context
            if abs_href.startswith("blob:"):
                data = await self._fetch_blob_via_page(page, abs_href)
                if data and data[:4] == b"%PDF":
                    return self._write_pdf_bytes("invoice.pdf", data)
                continue

            # data: URL -> base64 decode
            if abs_href.startswith("data:"):
                data = self._decode_data_url(abs_href)
                if data and data[:4] == b"%PDF":
                    return self._write_pdf_bytes("invoice.pdf", data)
                continue

            # direct GET through Playwright context
            try:
                resp = await self.context.request.get(abs_href)
                if resp.ok:
                    data = await resp.body()
                    if data[:4] == b"%PDF":
                        return self._write_pdf_bytes("invoice.pdf", data)
            except Exception as e:
                self.log("[download] direct GET failed:", repr(e))
                continue

        return None

    async def _fetch_blob_via_page(self, page: Page, blob_url: str) -> Optional[bytes]:
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
            self.log("[download] blob fetch failed:", repr(e))
            return None

    def _decode_data_url(self, data_url: str) -> Optional[bytes]:
        try:
            if ";base64," in data_url:
                return base64.b64decode(data_url.split(";base64,", 1)[1])
            return None
        except Exception:
            return None

    def _write_pdf_bytes(self, suggested: str, data: bytes) -> Optional[str]:
        if not data or data[:4] != b"%PDF":
            self.log("[download] invalid PDF bytes, rejecting")
            return None
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        target.write_bytes(data)
        self.log("[download] wrote PDF:", str(target))
        return str(target)

    async def _save_download(self, dl: Download) -> Optional[str]:
        suggested = dl.suggested_filename or "invoice.pdf"
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        await dl.save_as(str(target))
        self.log("[download] saved:", str(target))
        return str(target)

    # ---------- tiny locator utils ----------

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
        return dt.strftime("%Y-%m-%d")\


    def _parse_dates_from_pdf(self, pdf_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Extract:
        - statement_date: from 'Bill Date' in the account info box
                          (fallback: period_end if not found)
        - due_date: statement_date + 2 days
        - period_start / period_end: from the READING DATES range
        """
        statement_date_iso = None
        due_date_iso = None
        period_start_iso = None
        period_end_iso = None

        # --- 0) Read PDF text via pypdf --------------------------------
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
            self.log("[pdf] failed to read PDF for dates:", repr(e))
            return statement_date_iso, due_date_iso, period_start_iso, period_end_iso

        if not text:
            self.log("[pdf] empty text when parsing dates")
            return statement_date_iso, due_date_iso, period_start_iso, period_end_iso

        # --- 1) READING DATES range -> period_start / period_end --------
        try:
            # Focus on the block after "READING DATES" if possible
            block = None
            m_block = re.search(
                r"READING\s+DATES(?P<after>.*?)(?:\n\s*\n|\Z)",
                text,
                re.IGNORECASE | re.DOTALL,
            )
            if m_block:
                block = m_block.group("after")
            else:
                block = text

            m_range = re.search(
                r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*[-–]\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                block,
            )
            if not m_range:
                # Fallback: search whole doc
                m_range = re.search(
                    r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*[-–]\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                    text,
                )

            if m_range:
                raw_start, raw_end = m_range.groups()
                period_start_iso = self._parse_us_date(raw_start)
                period_end_iso = self._parse_us_date(raw_end)
                self.log(
                    "[pdf] Reading Dates raw:",
                    raw_start,
                    "-",
                    raw_end,
                    "→",
                    period_start_iso,
                    period_end_iso,
                )
        except Exception as e:
            self.log("[pdf] error parsing Reading Dates:", repr(e))

        # --- 2) Bill Date -> statement_date -----------------------------
        try:
            # Allow up to ~20 non-digit chars between 'Bill Date' and the date
            # to survive weird spacing / newlines / punctuation.
            m_bill = re.search(
                r"Bill\s*Date[^0-9]{0,20}([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                text,
                re.IGNORECASE,
            )
            if m_bill:
                raw_bill_date = m_bill.group(1)
                statement_date_iso = self._parse_us_date(raw_bill_date)
                self.log("[pdf] Bill Date raw:", raw_bill_date, "→", statement_date_iso)
            else:
                self.log("[pdf] Bill Date label not found via regex")
        except Exception as e:
            self.log("[pdf] error parsing Bill Date:", repr(e))

        # --- 3) Fallback: if no Bill Date, use period_end ----------------
        if not statement_date_iso and period_end_iso:
            statement_date_iso = period_end_iso
            self.log(
                "[pdf] falling back to period_end as statement_date:", statement_date_iso
            )

        # --- 4) Due Date = statement_date + 2 days -----------------------
        if statement_date_iso:
            try:
                dt = datetime.strptime(statement_date_iso, "%Y-%m-%d")
                due_date_iso = (dt + timedelta(days=2)).strftime("%Y-%m-%d")
            except Exception as e:
                self.log("[pdf] error computing due_date:", repr(e))

        return statement_date_iso, due_date_iso, period_start_iso, period_end_iso

# ---------------- CLI ----------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="TTD Electric (InvoiceCloud) latest invoice downloader (Bright proxy + stealth + storage_state)"
    )
    p.add_argument("--username", default=os.getenv("TTD_USERNAME", ""), help="Email address / login")
    p.add_argument("--password", default=os.getenv("TTD_PASSWORD", ""), help="InvoiceCloud password")
    p.add_argument("--headful", action="store_true", help="Show browser window instead of headless")
    p.add_argument("--slow-mo", type=int, default=int(os.getenv("TTD_SLOW_MO_MS", "0")), help="Slow motion ms for Playwright actions")
    p.add_argument("--json", action="store_true", help="Print JSON result instead of bare amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    # load scrapers/env first without clobbering already-set env vars
    load_kv_env_file(ENV_FILE)

    args = _parse_args(argv)

    # Bright Data creds from either OS env or scrapers/env
    bright_server = os.getenv("BRIGHT_PROXY_SERVER")
    bright_user = os.getenv("BRIGHT_PROXY_USER")
    bright_pass = os.getenv("BRIGHT_PROXY_PASS")

    cfg = Config(
        username=args.username,
        password=args.password,
        headless=not args.headful,
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        use_bright_proxy=bool(bright_server),
        bright_proxy={
            "server": bright_server,
            "username": bright_user,
            "password": bright_pass,
        } if bright_server else None,
        timezone_id=os.getenv("TTD_TIMEZONE_ID", "America/New_York"),
    )

    async with TTDScraper(cfg) as s:
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
