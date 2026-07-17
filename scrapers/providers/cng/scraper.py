#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CNG (Connecticut Natural Gas) — "View Latest Bill" scraper with address filtering.
No proxy. Supports headful Real Chrome via common/browser.open_real_chrome, and
headless Playwright Chromium as a fallback.

Flow:
- Open CNG site (homepage → click "Sign in / Register"), with fallback straight to portal.cngcorp.com
- Log in with username/password
- If multiple accounts, pick the one whose address best matches --address
- Click "View latest bill" and capture the PDF viewer tab
- Pull PDF bytes via network sniff / viewer src / blob fetch
- Save the PDF locally
- Parse total due from the PDF

CLI:
  --username, --password, --address  (or env CNG_USERNAME/CNG_PASSWORD/CNG_ADDRESS)
  --headful (use Real Chrome persistent context from common/browser)
  --slow-mo, --json, --debug
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
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
)

# ---------- constants ----------
HOME_URL = "https://www.cngcorp.com/"
PORTAL_URL = "https://portal.cngcorp.com/"
# DEFAULT_DL_DIR = Path(os.getenv("CNG_DOWNLOAD_DIR", "./downloads/cng")).expanduser().resolve()
# HERE = Path(__file__).resolve()
# SCRAPERS_ROOT = HERE.parents[2]          # scrapers/
# DATA_ROOT = HERE.parent                 # providers/cng
# DEFAULT_DL_DIR = Path(
#    os.getenv("CNG_DOWNLOAD_DIR", str(Path(__file__).resolve().parents[2] / "downloads"))
# ).resolve()
HANDOFF_ROOT = Path(os.getenv("RA_HANDOFF_DIR", "/handoff"))
DEFAULT_DL_DIR = (HANDOFF_ROOT / "cng").resolve()

CURRENCY_RE_STRICT = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})")
CURRENCY_RE_LOOSE = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})")


# ---------- data ----------
@dataclass
class Config:
    username: str
    password: str
    address_query: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35000
    debug: bool = False
    timezone_id: str = os.getenv("CNG_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    statement_date: Optional[str] = None   # e.g. "2025-10-13T00:00:00Z"
    period_start: Optional[str] = None     # e.g. "2025-09-11T00:00:00Z"
    period_end: Optional[str] = None       # e.g. "2025-10-08T00:00:00Z"
    due_date: Optional[str] = None         # e.g. "2025-11-10T00:00:00Z"



# ---------- helpers ----------
def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------- scraper ----------
class CNGScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ----- browser/context setup -----
    def _rand_viewport(self):
        widths = [1280, 1366, 1440, 1536, 1600]
        heights = [720, 768, 800, 900]
        return random.choice(widths), random.choice(heights)

    def _build_launch_kwargs(self):
        return dict(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--headless=new" if self.cfg.headless else "",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
            ],
        )

    def _build_context_kwargs(self):
        w, h = self._rand_viewport()
        return {
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

    async def _install_stealth(self):
        script = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1});
        """
        try:
            await self.context.add_init_script(script)
        except Exception:
            pass

    async def _launch(self):
        # Clean any previous
        await self._close()

        if not self.cfg.headless:
            # Use your common headful Chrome helper
            from common.browser import open_real_chrome, close_all
            self._pw, self.context = await open_real_chrome(slow_mo=self.cfg.slow_mo_ms or 80)
            self.browser = None
            self._using_real_chrome = True

            async def _closer():
                await close_all(self._pw, self.context)

            self._close_fn = _closer

            self.page = await self.context.new_page()
            if self.cfg.debug:
                self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))
            await self._install_stealth()
            return

        # Headless Playwright Chromium
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(**self._build_launch_kwargs())
        self.context = await self.browser.new_context(**self._build_context_kwargs())
        await self._install_stealth()
        self.page = await self.context.new_page()
        if self.cfg.debug:
            self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))

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
        if self._close_fn:
            try:
                await self._close_fn()
            except Exception:
                pass
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    async def __aenter__(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        await self._launch()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] __aexit__ (closing)")
        await self._close()

    # ----- run -----
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password or not self.cfg.address_query:
            return Result(ok=False, error="username, password, and address are required")

        self.log("[step] open login surface")
        ok = await self._open_login_surface()
        if not ok:
            return Result(ok=False, error="Could not reach login form")

        self.log("[step] login")
        if not await self._credential_login(self.cfg.username, self.cfg.password):
            return Result(ok=False, error="Login failed (selectors / captcha?)")

        self.log("[step] choose account for address")
        if not await self._pick_account_for_address(self.cfg.address_query):
            return Result(ok=False, error=f'No account matched address query: "{self.cfg.address_query}"')

        self.log("[step] click 'View latest bill' → download")
        pdf_path = await self._click_view_latest_bill_and_download()
        if not pdf_path:
            return Result(ok=False, error="Could not download latest bill PDF")

        self.log("[step] parse amount from PDF")
        amount_str, amount_cents = extract_amount_from_pdf(pdf_path)
        if not amount_str:
            return Result(
                ok=False,
                error="Failed to detect bill amount in the PDF",
                pdf_path=pdf_path,
                final_url=self.page.url,
            )

        self.log("[step] parse dates from PDF")
        stmt_iso, start_iso, end_iso, due_iso = extract_dates_from_pdf(pdf_path)


        return Result(
            ok=True,
            amount=amount_str,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=self.page.url,
            statement_date=stmt_iso,
            period_start=start_iso,
            period_end=end_iso,
            due_date=due_iso,
        )

    # ----- entry / resilience -----
    async def _http_probe(self, url: str) -> tuple[int, str]:
        """Lightweight reachability probe using Playwright's request context."""
        try:
            resp = await self.context.request.get(url, timeout=8000)
            return (resp.status if not resp.ok else 200, resp.url)
        except Exception as e:
            self.log("[probe] error:", repr(e))
            return (0, url)

    async def _open_login_surface(self) -> bool:
        page = self.page

        # First try the portal directly (often less marketing fluff / popups)
        # st, _ = await self._http_probe(PORTAL_URL)
        # try_direct = st == 200

        # targets = [PORTAL_URL, HOME_URL] if try_direct else [HOME_URL, PORTAL_URL]
        targets = [HOME_URL, PORTAL_URL]
        for target in targets:
            self.log(f"[entry] goto {target}")
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
            except Exception as e:
                self.log("[entry] goto failed:", repr(e))
                continue

            await self._dismiss_cookie_banner()

            # If we're already on the login page (portal), look for password field
            try:
                pw = page.locator('input[type="password"]').first
                await pw.wait_for(timeout=4000)
                return True
            except Exception:
                pass

            # Else from HOME: click "Sign in / Register"
            loc = page.get_by_text(re.compile(r"Sign\s*in\s*/\s*Register", re.I))
            if await loc.count() == 0:
                loc = page.locator('header :is(a,button):has-text("Sign in / Register")')

            if await loc.count() > 0:
                self.log("[entry] clicking 'Sign in / Register'")
                try:
                    await loc.first.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await self._dismiss_cookie_banner()
                    # confirm password appears
                    await page.locator('input[type="password"]').first.wait_for(timeout=9000)
                    return True
                except Exception as e:
                    self.log("[entry] click sign-in failed:", repr(e))
                    continue

        return False

    async def _dismiss_cookie_banner(self):
        page = self.page
        for _ in range(3):
            dismissed = False
            for loc in [
                page.get_by_text(re.compile(r"Continue", re.I)),
                page.get_by_text(re.compile(r"Accept", re.I)),
                page.locator('[aria-label="Close"]'),
            ]:
                try:
                    if await loc.count() > 0 and await loc.first.is_visible():
                        await loc.first.click(timeout=800)
                        dismissed = True
                        break
                except Exception:
                    continue
            if not dismissed:
                break
            await page.wait_for_timeout(200)

    # ----- login + navigation helpers -----
    async def _credential_login(self, username: str, password: str) -> bool:
        page = self.page

        user_input = await self._first_visible(
            [
                page.get_by_label(re.compile(r"User", re.I)),
                page.get_by_placeholder(re.compile(r"User", re.I)),
                page.locator('#EmailUsername, #username, input[name="username" i]'),
            ]
        )
        pass_input = await self._first_visible(
            [
                page.get_by_label(re.compile(r"Password", re.I)),
                page.locator('input[type="password"]'),
            ]
        )
        if not user_input or not pass_input:
            self.log("[login] username/password fields not found")
            return False

        await user_input.fill(username)
        await pass_input.fill(password)
        await pass_input.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=35000)
        except Exception:
            pass

        try:
            await self.page.wait_for_url(re.compile(r"cngcorp\.com|portal\.cngcorp\.com"), timeout=15000)
        except Exception:
            pass
        await self._wait_for_url_stable(400, total_timeout=8000)
        await self._dismiss_cookie_banner()
        return True

    def _now(self) -> float:
        loop = asyncio.get_running_loop()
        return loop.time()

    async def _wait_for_url_stable(self, settle_ms: int = 400, total_timeout: int = 20000) -> bool:
        page = self.page
        deadline = self._now() + (total_timeout / 1000)
        last_url = page.url
        stable_ms = 0
        step_ms = 50
        while self._now() < deadline:
            await page.wait_for_timeout(step_ms)
            cur = page.url
            if cur == last_url:
                stable_ms += step_ms
                if stable_ms >= settle_ms:
                    return True
            else:
                last_url = cur
                stable_ms = 0
        return False

    async def _safe_count(self, loc) -> int:
        try:
            return await loc.count()
        except Exception:
            return -1

    async def _wait_for_dashboard_or_accounts(self, timeout=20000) -> str:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass

        try:
            await page.wait_for_url(re.compile(r"portal\.cngcorp\.com"), timeout=timeout)
        except Exception:
            pass
        await self._wait_for_url_stable(400, total_timeout=timeout)

        dash_sel = [
            page.get_by_role("button", name=re.compile(r"View\s+latest\s+bill", re.I)),
            page.get_by_text(re.compile(r"Account Summary", re.I)),
            page.get_by_role("button", name=re.compile(r"Switch Account", re.I)),
        ]
        acct_sel = [
            page.get_by_text(re.compile(r"Manage Accounts", re.I)),
            page.get_by_role("textbox", name=re.compile(r"Account\s*Lookup|Search|Lookup", re.I)),
            page.locator("table[role='grid'] tbody tr"),
        ]

        deadline = self._now() + (timeout / 1000)
        while self._now() < deadline:
            for s in dash_sel:
                cnt = await self._safe_count(s)
                if cnt > 0:
                    return "dashboard"
            for s in acct_sel:
                cnt = await self._safe_count(s)
                if cnt > 0:
                    return "accounts"
            await page.wait_for_timeout(100)
        return "unknown"

    async def _pick_account_for_address(self, address_query: str) -> bool:
        page = self.page
        state = await self._wait_for_dashboard_or_accounts()
        self.log(f"[accounts] initial state={state} url={page.url}")
        if state == "dashboard":
            return True
        if state != "accounts":
            state = await self._wait_for_dashboard_or_accounts(5000)
            if state == "dashboard":
                return True
            if state != "accounts":
                return False

        q_norm = _norm(address_query)
        self.log("[accounts] q_norm =", q_norm)

        lookup = await self._first_present(
            [
                page.get_by_role("textbox", name=re.compile(r"Account\s*Lookup|Search|Lookup", re.I)),
                page.get_by_placeholder(re.compile(r"Search|Lookup", re.I)),
                page.locator('input[type="search"]'),
            ]
        )
        if lookup:
            await lookup.fill("")
            await lookup.type(address_query, delay=20)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)

        table = page.locator("table[role='grid'], table.p-datatable-table")
        row_nodes = table.locator("tbody tr")
        count = await row_nodes.count()
        self.log("[accounts] rows found:", count)

        matched_row = None
        for i in range(count):
            r = row_nodes.nth(i)
            try:
                addr_text = await r.locator("td:nth-child(2)").inner_text()
            except Exception:
                addr_text = ""
            addr_norm = _norm(addr_text)

            # --- Improved matching logic ---
            is_match = q_norm in addr_norm or addr_norm in q_norm
            if not is_match:
                def _tok(s: str) -> list[str]:
                    return [t for t in re.findall(r"[A-Za-z0-9]+", s)]

                q_tokens = _tok(_norm(address_query))
                a_tokens = _tok(addr_norm)

                q_set = set(q_tokens)
                a_set = set(a_tokens)

                q_nums = [t for t in q_tokens if t.isdigit()]
                house_num = q_nums[0] if q_nums else None
                zip5 = next((t for t in q_tokens if t.isdigit() and len(t) == 5), None)

                num_ok = (house_num is None) or (house_num in a_set)
                zip_ok = (zip5 is None) or (zip5 in a_set)

                overlap = len(q_set & a_set) / max(1, len(q_set))

                # optional: enforce street bigram overlap
                def bigrams(ts): 
                    return {f"{ts[i]} {ts[i+1]}" for i in range(len(ts)-1)}
                q_bigrams = bigrams(q_tokens[:min(len(q_tokens), 6)])
                a_str = " ".join(a_tokens)
                street_ok = any(bg in a_str for bg in q_bigrams)

                is_match = num_ok and zip_ok and overlap >= 0.6 and street_ok
            # --- End matching logic ---

            self.log(f"[row {i}] addr='{addr_text}' match={is_match}")

            if is_match:
                matched_row = r
                break

        if not matched_row:
            self.log("[accounts] no row matched")
            return False

        first_cell = matched_row.locator("td").first
        link = first_cell.locator("a, button")
        if await link.count() == 0:
            self.log("[accounts] no clickable element in first cell")
            return False

        await link.first.scroll_into_view_if_needed()
        await link.first.click()

        try:
            await self.page.wait_for_url(re.compile(r"/dashboard|/account|/home"), timeout=20000)
        except Exception:
            pass
        await self._wait_for_url_stable(500, total_timeout=8000)

        try:
            await page.get_by_text(re.compile(r"View\s+latest\s+bill", re.I)).first.wait_for(timeout=20000)
            return True
        except Exception:
            await self._wait_for_url_stable(500, total_timeout=3000)
            if await self._safe_count(page.get_by_role("button", name=re.compile(r"View\s+latest\s+bill", re.I))) > 0:
                return True
            return False

    # ========= UPDATED: viewer tab + byte-extraction =========
    async def _capture_pdf_via_network(self, pdf_page: Page) -> Optional[bytes]:
        box = {"task": None}

        def on_resp(resp):
            try:
                ct = resp.headers.get("content-type", "") or resp.headers.get("Content-Type", "")
                if "pdf" in ct.lower():
                    self.log(f"[pdf-net] {resp.url}")
                    box["task"] = asyncio.create_task(resp.body())
            except Exception:
                pass

        pdf_page.on("response", on_resp)
        try:
            await pdf_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)
        if box["task"]:
            try:
                data = await box["task"]
                if data and data[:4] == b"%PDF":
                    return data
            except Exception:
                pass
        return None

    async def _fetch_blob_via_page(self, page: Page, blob_url: str) -> Optional[bytes]:
        try:
            b64 = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u);
                    if (!r.ok) throw new Error('status ' + r.status);
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let s=''; for (let i=0;i<bytes.length;i++) s += String.fromCharCode(bytes[i]);
                    return btoa(s);
                }""",
                blob_url,
            )
            return base64.b64decode(b64) if b64 else None
        except Exception:
            return None

    def _write_pdf_bytes(self, suggested: str, data: bytes) -> Optional[str]:
        if not data or data[:4] != b"%PDF":
            return None
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        target.write_bytes(data)
        self.log("[pdf] wrote:", str(target))
        return str(target)

    async def _sniff_context_pdf_once(self, timeout_ms: int = 15000) -> Optional[bytes]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def on_resp(resp):
            if fut.done():
                return
            try:
                ct = resp.headers.get("content-type", "") or resp.headers.get("Content-Type", "")
                if "pdf" in ct.lower():
                    # capture body async and resolve future
                    async def _grab():
                        try:
                            b = await resp.body()
                            if b and b[:4] == b"%PDF" and not fut.done():
                                fut.set_result(b)
                        except Exception:
                            if not fut.done():
                                fut.set_result(None)
                    asyncio.create_task(_grab())
            except Exception:
                pass

        self.context.on("response", on_resp)
        try:
            return await asyncio.wait_for(fut, timeout_ms / 1000)
        except asyncio.TimeoutError:
            return None
        finally:
            try:
                self.context.off("response", on_resp)
            except Exception:
                pass


    async def _download_pdf_from_cng_viewer(self, pdf_page: Page, initial_url: Optional[str] = None) -> Optional[str]:
        """
        Strategy order:
          0) Chrome viewer src/file param
          1) Network sniff for application/pdf
          2) Direct GET of an http(s) src (with Referer)
          3) DOM scan (embed/object/iframe/a[data|href])
          4) blob: fetch inside the page context
        """
        # Strategy 0: Chrome's built-in viewer with ?src= or ?file=
        try:
            url_now = pdf_page.url or ""
            if url_now.startswith("chrome-extension://") and ("pdf_viewer" in url_now or "generated_pdf_viewer" in url_now):
                self.log("[pdf] chrome viewer detected; pulling src")
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
                                let s=''; for (let i=0;i<bytes.length;i++) s += String.fromCharCode(bytes[i]);
                                return btoa(s);
                            }""",
                            src,
                        )
                        if b64:
                            data0 = base64.b64decode(b64)
                            path0 = self._write_pdf_bytes("cng-statement.pdf", data0)
                            if path0:
                                return path0
                    except Exception:
                        pass
                    initial_url = src
        except Exception:
            pass

        # Strategy 1: network sniff
        data = await self._capture_pdf_via_network(pdf_page)
        if data:
            path = self._write_pdf_bytes("cng-statement.pdf", data)
            if path:
                return path

        # Strategy 2: direct GET of initial_url (if http(s))
        if initial_url and initial_url.startswith(("http://", "https://")):
            try:
                resp = await self.context.request.get(
                    initial_url,
                    headers={"Referer": "https://portal.cngcorp.com/"},
                )
                if resp.ok:
                    b = await resp.body()
                    path = self._write_pdf_bytes("cng-statement.pdf", b)
                    if path:
                        return path
            except Exception:
                pass

        # Strategy 3/4: DOM scan & blob/data fetch
        try:
            srcs = await pdf_page.evaluate("""
                () => {
                  const out = new Set();
                  const sels = ['embed[type="application/pdf"]','object[type="application/pdf"]','iframe','a[href]','a[data]'];
                  document.querySelectorAll(sels.join(',')).forEach(el => {
                    ['src','data','href'].forEach(k => {
                      const v = el.getAttribute && el.getAttribute(k);
                      if (v) out.add(v);
                    });
                  });
                  return Array.from(out);
                }
            """)
        except Exception:
            srcs = []

        for raw in srcs or []:
            if not raw:
                continue
            abs_url = raw if raw.startswith(("http://", "https://", "blob:", "data:")) else urljoin(pdf_page.url, raw)

            # blob:
            if abs_url.startswith("blob:"):
                b = await self._fetch_blob_via_page(pdf_page, abs_url)
                if b:
                    path = self._write_pdf_bytes("cng-statement.pdf", b)
                    if path:
                        return path
                continue

            # data:
            if abs_url.startswith("data:") and ";base64," in abs_url:
                try:
                    b = base64.b64decode(abs_url.split(";base64,", 1)[1])
                    path = self._write_pdf_bytes("cng-statement.pdf", b)
                    if path:
                        return path
                except Exception:
                    pass
                continue

            # http(s):
            if abs_url.startswith(("http://", "https://")):
                try:
                    resp = await self.context.request.get(
                        abs_url,
                        headers={"Referer": "https://portal.cngcorp.com/"},
                    )
                    if resp.ok:
                        b = await resp.body()
                        path = self._write_pdf_bytes("cng-statement.pdf", b)
                        if path:
                            return path
                except Exception:
                    pass

        # Optional: toolbar fallback inside viewer (works occasionally)
        toolbar = [
            'cr-icon-button#download',
            'cr-icon-button[aria-label*="Download" i]',
            ':is(a,button)[aria-label*="Download" i]',
            ':is(a,button):has-text("Download")',
        ]
        for sel in toolbar:
            try:
                loc = pdf_page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    # Playwright's download event is unreliable in the native viewer,
                    # but try it anyway — if it fires, we can save it via API.
                    async with pdf_page.expect_download(timeout=10000) as dl_ev:
                        await loc.click()
                    dl: Download = await dl_ev.value
                    suggested = dl.suggested_filename or "cng-statement.pdf"
                    target = (self.download_dir / suggested).resolve()
                    i, stem, suf = 1, target.stem, target.suffix
                    while target.exists():
                        target = target.with_name(f"{stem}-{i}{suf}")
                        i += 1
                    await dl.save_as(str(target))
                    self.log("[pdf] saved via toolbar:", str(target))
                    return str(target)
            except Exception:
                continue

        return None

    async def _click_view_latest_bill_and_download(self) -> Optional[str]:
        page = self.page
        ctx  = self.context

        # --- Strategy A: New billing page flow (2026+) ---
        # Navigate to /billing via "View bills and payments" sidebar button,
        # then click the "Download bill" icon on the first table row.
        self.log("[download] trying new billing-page flow")
        billing_btn = page.locator('button:has-text("View bills and payments")')
        if await billing_btn.count() > 0:
            await billing_btn.first.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            dl_link = page.locator('a[title="Download bill"]')
            if await dl_link.count() > 0:
                self.log(f"[download] found {await dl_link.count()} 'Download bill' links on billing page")

                # Arm context-wide PDF sniffer before clicking
                sniff_task = asyncio.create_task(self._sniff_context_pdf_once(timeout_ms=15000))

                before_pages = list(ctx.pages)
                await dl_link.first.click()

                # Wait for new tab or network PDF response
                pdf_page: Optional[Page] = None
                for _ in range(40):
                    now = list(ctx.pages)
                    if len(now) > len(before_pages):
                        pdf_page = now[-1]
                        break
                    await page.wait_for_timeout(250)

                # Check if sniffer caught the PDF bytes
                if sniff_task.done():
                    data = await sniff_task
                    if data:
                        path = self._write_pdf_bytes("cng-statement.pdf", data)
                        if path:
                            return path

                # Give the sniffer more time
                try:
                    data = await asyncio.wait_for(sniff_task, 8)
                    if data:
                        path = self._write_pdf_bytes("cng-statement.pdf", data)
                        if path:
                            return path
                except Exception:
                    pass

                # If a new tab opened, try multi-strategy viewer download
                if pdf_page:
                    try:
                        await pdf_page.wait_for_load_state("domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    pdf_path = await self._download_pdf_from_cng_viewer(pdf_page, pdf_page.url)
                    if pdf_path:
                        return pdf_path

            # Navigate back to dashboard for fallback attempt
            try:
                await page.goto(PORTAL_URL + "dashboard", wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
            except Exception:
                pass

        # --- Strategy B: Legacy "View latest bill" button on dashboard ---
        self.log("[download] trying legacy 'View latest bill' flow")
        candidates = [
            page.get_by_role("button", name=re.compile(r"View\s+latest\s+bill", re.I)),
            page.get_by_role("link",   name=re.compile(r"View\s+latest\s+bill", re.I)),
            page.locator(':is(a,button):has-text("View latest bill")'),
        ]
        handle = None
        for loc in candidates:
            if await loc.count() > 0:
                handle = loc.first
                break
        if not handle:
            self.log("[download] no download method found")
            return None

        await handle.scroll_into_view_if_needed()
        await handle.hover()
        await page.wait_for_timeout(150)

        sniff_task = asyncio.create_task(self._sniff_context_pdf_once(timeout_ms=15000))

        before_pages = list(ctx.pages)
        await handle.click()

        pdf_page: Optional[Page] = None
        for _ in range(40):
            now = list(ctx.pages)
            if len(now) > len(before_pages):
                pdf_page = now[-1]
                break
            if page.url != before_pages[0].url:  # same-tab nav
                pdf_page = page
                break
            await page.wait_for_timeout(250)

        if sniff_task.done():
            data = await sniff_task
            if data:
                path = self._write_pdf_bytes("cng-statement.pdf", data)
                if path:
                    return path

        if not pdf_page:
            self.log("[download] statement viewer never opened")
            try:
                data = await asyncio.wait_for(sniff_task, 2)
                if data:
                    path = self._write_pdf_bytes("cng-statement.pdf", data)
                    if path:
                        return path
            except Exception:
                pass
            return None

        try:
            await pdf_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        initial_url = None
        try:
            initial_url = pdf_page.url
        except Exception:
            pass

        pdf_path = await self._download_pdf_from_cng_viewer(pdf_page, initial_url)
        if pdf_path:
            return pdf_path

        try:
            data = await asyncio.wait_for(sniff_task, 2)
            if data:
                path = self._write_pdf_bytes("cng-statement.pdf", data)
                if path:
                    return path
        except Exception:
            pass

        self.log("[download] failed to download PDF from viewer")
        return None

    # ----- small locator utils -----
    async def _first_visible(self, locators: List, timeout: int = 4000):
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


# ---------- PDF amount extraction ----------
def extract_amount_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Parse amount due from the downloaded PDF.
    Strategy:
    - Prefer phrases near amounts: "Amount Now Due", "Amount Due", "Total Due"
    - Fallback to max $xxx.xx in document
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None

    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    money_pat = r"(\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
    direct_pats = [
        rf"Amount\s+Now\s+Due[\s\S]{{0,120}}?{money_pat}",
        rf"Amount\s+Due[\s\S]{{0,120}}?{money_pat}",
        rf"Total\s+Due[\s\S]{{0,120}}?{money_pat}",
    ]
    for pat in direct_pats:
        m = re.search(pat, squashed, re.I)
        if m:
            clean = m.group(1).replace("$", "").replace(",", "").strip()
            try:
                cents = int(round(float(clean) * 100))
            except Exception:
                cents = None
            return clean, cents

    nums: List[float] = []
    candidates = CURRENCY_RE_STRICT.findall(squashed) or CURRENCY_RE_LOOSE.findall(squashed)
    for m in candidates:
        try:
            nums.append(float(str(m).replace("$", "").replace(",", "").strip()))
        except Exception:
            pass
    if nums:
        best = f"{max(nums):.2f}"
        return best, int(round(float(best) * 100))

    return None, None


def _extract_text_pdfminer(pdf_path: str) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(pdf_path)
    except Exception:
        return None


def _extract_text_pypdf(pdf_path: str) -> Optional[str]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None

def _parse_us_date_to_iso(s: str) -> Optional[str]:
    """
    Parse dates like 09/11/25 or 10/13/2025 into ISO-8601 'YYYY-MM-DDT00:00:00Z'.
    """
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_dates_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extract statement date, period start, period end, and due date from the CNG PDF.

    Returns:
        (statement_date_iso, period_start_iso, period_end_iso, due_date_iso)
        where each value is either an ISO string 'YYYY-MM-DDT00:00:00Z' or None.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None, None, None

    # Normalize whitespace to make regex easier
    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    stmt_iso = None
    start_iso = None
    end_iso = None
    due_iso = None

    # ----- Statement Date: 10/13/2025 -----
    m = re.search(r"Statement\s+Date:\s*([0-9/]{6,10})", squashed, re.I)
    if m:
        stmt_iso = _parse_us_date_to_iso(m.group(1))

    # ----- Bill Period: 09/11/25 to 10/08/25 -----
    m = re.search(
        r"Bill\s+Period[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*(?:to|-|–)\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        squashed,
        re.I,
    )
    if m:
        start_iso = _parse_us_date_to_iso(m.group(1))
        end_iso   = _parse_us_date_to_iso(m.group(2))

    # ----- Due Date from green bar: "Amount Now Due by 11/10/25" -----
    m = re.search(
        r"Amount\s+Now\s+Due(?:\s+by)?[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        squashed,
        re.I,
    )
    if not m:
        # 2) Fallback: find "Amount Now Due" and then grab the first date shortly after
        m2 = re.search(r"Amount\s+Now\s+Due", squashed, re.I)
        if m2:
            tail = squashed[m2.end() : m2.end() + 100]  # window after the label
            m3 = re.search(r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", tail)
            if m3:
                due_iso = _parse_us_date_to_iso(m3.group(1))
    else:
        due_iso = _parse_us_date_to_iso(m.group(1))

    return stmt_iso, start_iso, end_iso, due_iso



# ---------- CLI ----------
def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="CNG 'View Latest Bill' downloader + amount extractor (real Chrome/headless)")
    p.add_argument("--username", help="UserID / Username (or env CNG_USERNAME)")
    p.add_argument("--password", help="Password (or env CNG_PASSWORD)")
    p.add_argument("--address", required=True, help="Substring to match the Service Address (or env CNG_ADDRESS)")
    p.add_argument("--headful", action="store_true", help="Run with real Chrome visible (uses common/browser)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion in ms between actions")
    p.add_argument("--json", action="store_true", help="Print JSON instead of bare amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("CNG_USERNAME", ""),
        password=args.password or os.getenv("CNG_PASSWORD", ""),
        address_query=args.address or os.getenv("CNG_ADDRESS", ""),
        headless=(not args.headful),
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        timezone_id=os.getenv("CNG_TIMEZONE_ID", "America/New_York"),
    )


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with CNGScraper(cfg) as s:
        result = await s.run()

    if not result.ok:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result.__dict__, indent=2))
    else:
        print(result.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
