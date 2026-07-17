#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SCG (Southern Connecticut Gas) — "View Latest Bill" scraper

Flow:
- Go to homepage: https://www.soconngas.com/
- Click the header "Sign in / Register"
- Fill UserID + Password and submit
- After login, click "My Account" to reach the account portal
- Click "View Latest Bill" which opens a PDF viewer / triggers a PDF download
- Parse the downloaded PDF to extract the bill amount

Output:
- Default: prints ONLY the amount (e.g., 123.45)
- With --json:
  {
    "amount":"123.45",
    "amount_cents":12345,
    "pdf_path":"/abs/path/file.pdf",
    "final_url":"..."
  }

This build matches the CNG implementation for:
- Browser lifecycle: headful Real Chrome via common/browser, or headless Chromium
- Stealth init script, realistic UA, randomized viewport
- Robust PDF capture: context sniff → viewer src/blob → toolbar fallback
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
from pdfminer.high_level import extract_text
from io import BytesIO
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
)

# ---- URLs / Constants --------------------------------------------------------

HOME_URL = "https://www.soconngas.com/"
DEFAULT_DL_DIR = Path(
    os.getenv("SCG_DOWNLOAD_DIR", "/handoff/scg")
).resolve()

CURRENCY_RE_STRICT = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})")
CURRENCY_RE_LOOSE  = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})")


# ---- Data models -------------------------------------------------------------

@dataclass
class Config:
    username: str
    password: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    timezone_id: str = os.getenv("SCG_TIMEZONE_ID", "America/New_York")


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



def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("SCG_USERNAME") or "",
        password=args.password or os.getenv("SCG_PASSWORD") or "",
        headless=(not args.headful),
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        timezone_id=os.getenv("SCG_TIMEZONE_ID", "America/New_York"),
    )


# ---- Scraper ----------------------------------------------------------------

class SCGScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    # --------------- logging helpers ---------------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # --------------- misc helpers ---------------
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

    # --------------- context lifecycle --------------
    async def _launch(self):
        await self._close()  # clean any previous

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
        self.log("[lifecycle] closing")
        await self._close()

    # ---------------- main ----------------
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        # Entry → Homepage → click 'Sign in / Register'
        self.log("[step] enter via homepage and click Sign in / Register")
        ok = await self._enter_via_home_and_click_signin()
        if not ok:
            return Result(ok=False, error="Could not reach login form from homepage")

        # Credentials
        self.log("[step] credential login")
        if not await self._credential_login(self.cfg.username, self.cfg.password):
            return Result(ok=False, error="Login submit failed (selectors may need update)")

        # Navigate to portal
        self.log("[step] post-login → account portal")
        ok = await self._post_login_navigate_to_portal()
        if not ok:
            return Result(ok=False, error="Could not reach account landing after login")

        # Download latest bill
        self.log("[step] click 'View Latest Bill' → download")
        pdf_path = await self._click_view_latest_bill_and_download()
        if not pdf_path:
            return Result(ok=False, error="Could not download latest bill PDF")

        # Parse amount
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
    # ---------------- page flow helpers ----------------

    async def _enter_via_home_and_click_signin(self) -> bool:
        page = self.page
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            self.log("[entry] timeout hitting homepage")
            return False
        except Exception as e:
            self.log("[entry] page.goto blew up:", repr(e))
            return False

        # Dismiss cookie/footer if it's nagging
        await self._dismiss_cookie_banner()

        # Click header "Sign in / Register"
        clicked = False
        for loc in [
            page.get_by_role("button", name=re.compile(r"Sign\s*in\s*/\s*Register", re.I)),
            page.get_by_role("link",   name=re.compile(r"Sign\s*in\s*/\s*Register", re.I)),
            page.locator('a:has-text("Sign in / Register"), button:has-text("Sign in / Register")'),
        ]:
            try:
                if await loc.count() > 0:
                    await loc.first.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            self.log("[entry] 'Sign in / Register' control not found")
            return False

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await self._dismiss_cookie_banner()

        # Confirm presence of login inputs
        try:
            await page.locator('input[type="password"]').first.wait_for(timeout=8000)
            return True
        except Exception:
            self.log("[entry] password input did not show, maybe interstitial?")
            return False

    async def _dismiss_cookie_banner(self):
        """Click common cookie/consent buttons. Safe and opportunistic."""
        page = self.page
        for _ in range(3):
            dismissed = False
            for loc in [
                page.get_by_role("button", name=re.compile(r"Continue|Accept|Got it|I Agree", re.I)),
                page.locator('button:has-text("Continue"), button:has-text("Accept")'),
                page.locator('[aria-label="Close"]'),
            ]:
                try:
                    if await loc.count() > 0:
                        await loc.first.click(timeout=800)
                        dismissed = True
                        break
                except Exception:
                    continue
            if not dismissed:
                break
            await page.wait_for_timeout(200)

    async def _credential_login(self, username: str, password: str) -> bool:
        page = self.page

        # Username field (often labeled "UserID")
        user_candidates = [
            page.get_by_label(re.compile(r"User\s*ID|UserID|Username|Email", re.I)),
            page.get_by_placeholder(re.compile(r"User\s*ID|UserID|Username|Email", re.I)),
            page.locator('#EmailUsername, #username, input[name="username" i], input[id*="user" i]'),
            page.locator('input[type="email"]'),
        ]
        user_input = await self._first_visible(user_candidates, 6000)
        if not user_input:
            self.log("[login] user input not found")
            return False

        pass_candidates = [
            page.get_by_label(re.compile(r"Password", re.I)),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.locator('#Password, input[type="password"]'),
        ]
        pass_input = await self._first_visible(pass_candidates, 6000)
        if not pass_input:
            self.log("[login] password input not found")
            return False

        await user_input.fill(username)
        await pass_input.fill(password)

        # Submit
        sign_in_candidates = [
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('button[type="submit"], input[type="submit"]'),
        ]
        btn = await self._first_present(sign_in_candidates)
        if btn:
            try:
                await btn.click()
            except Exception:
                pass
        else:
            try:
                await page.keyboard.press("Enter")
            except Exception:
                return False

        try:
            await page.wait_for_load_state("networkidle", timeout=35000)
        except Exception:
            pass
        await self._dismiss_cookie_banner()
        return True

    async def _post_login_navigate_to_portal(self) -> bool:
        """
        After credentials:
        - click 'My Account' in the header to get into the portal.
        - If we hit a SAML hiccup (Unable to process SAML request), bounce through
          'Sign in / Register' once then try 'My Account' again.
        """
        page = self.page

        async def click_my_account() -> bool:
            for loc in [
                page.get_by_role("button", name=re.compile(r"My\s*Account", re.I)),
                page.get_by_role("link", name=re.compile(r"My\s*Account", re.I)),
                page.locator('a:has-text("My Account"), button:has-text("My Account")'),
            ]:
                if await loc.count() > 0:
                    await loc.first.click()
                    return True
            return False

        async def click_signin_register() -> bool:
            for loc in [
                page.get_by_role("link", name=re.compile(r"Sign\s*in\s*/\s*Register", re.I)),
                page.get_by_role("button", name=re.compile(r"Sign\s*in\s*/\s*Register", re.I)),
                page.locator('a:has-text("Sign in / Register"), button:has-text("Sign in / Register")'),
            ]:
                if await loc.count() > 0:
                    await loc.first.click()
                    return True
            return False

        # Try My Account
        await click_my_account()
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await self._dismiss_cookie_banner()

        # If we see a SAML error banner, recover
        if await page.get_by_text(re.compile(r"Unable to process SAML request", re.I)).count() > 0:
            await click_signin_register()
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await self._dismiss_cookie_banner()

            await click_my_account()
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await self._dismiss_cookie_banner()

        # Success heuristics
        if await page.get_by_role("button", name=re.compile(r"View\s+Latest\s+Bill", re.I)).count() > 0:
            return True
        if await page.get_by_role("link", name=re.compile(r"View\s+Latest\s+Bill", re.I)).count() > 0:
            return True
        if await page.get_by_text(re.compile(r"Billing|Latest Bill|Account", re.I)).count() > 0:
            return True
        return False

    # ======== PDF capture utilities (same approach as CNG) ========

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

    async def _download_pdf_from_scg_viewer(self, pdf_page: Page, initial_url: Optional[str] = None) -> Optional[str]:
        """
        Strategy order:
          0) Chrome viewer ?src/?file param (headful real Chrome)
          1) Network sniff for application/pdf
          2) Direct GET of http(s) src (with Referer)
          3) DOM scan (embed/object/iframe/a[data|href])
          4) blob: fetch inside the page context
          5) Toolbar 'Download' fallback
        """
        # 0) Chrome viewer param
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
                            path0 = self._write_pdf_bytes("scg-bill.pdf", data0)
                            if path0:
                                return path0
                    except Exception:
                        pass
                    initial_url = src
        except Exception:
            pass

        # 1) network sniff
        data = await self._capture_pdf_via_network(pdf_page)
        if data:
            path = self._write_pdf_bytes("scg-bill.pdf", data)
            if path:
                return path

        # 2) direct GET of initial_url (if http(s))
        if initial_url and initial_url.startswith(("http://", "https://")):
            try:
                resp = await self.context.request.get(initial_url, headers={"Referer": HOME_URL})
                if resp.ok:
                    b = await resp.body()
                    path = self._write_pdf_bytes("scg-bill.pdf", b)
                    if path:
                        return path
            except Exception:
                pass

        # 3/4) DOM scan & blob/data fetch
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
                    path = self._write_pdf_bytes("scg-bill.pdf", b)
                    if path:
                        return path
                continue

            # data:
            if abs_url.startswith("data:") and ";base64," in abs_url:
                try:
                    b = base64.b64decode(abs_url.split(";base64,", 1)[1])
                    path = self._write_pdf_bytes("scg-bill.pdf", b)
                    if path:
                        return path
                except Exception:
                    pass
                continue

            # http(s):
            if abs_url.startswith(("http://", "https://")):
                try:
                    resp = await self.context.request.get(abs_url, headers={"Referer": HOME_URL})
                    if resp.ok:
                        b = await resp.body()
                        path = self._write_pdf_bytes("scg-bill.pdf", b)
                        if path:
                            return path
                except Exception:
                    pass

        # 5) toolbar fallback
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
                    async with pdf_page.expect_download(timeout=10000) as dl_ev:
                        await loc.click()
                    dl: Download = await dl_ev.value
                    suggested = dl.suggested_filename or "scg-bill.pdf"
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
        """
        Find and click "View Latest Bill", capture viewer/new tab,
        and save the PDF locally using multi-strategy extraction.
        """
        page = self.page
        ctx  = self.context

        # Locate control
        control = None
        for loc in [
            page.get_by_role("button", name=re.compile(r"View\s+Latest\s+Bill", re.I)),
            page.get_by_role("link",   name=re.compile(r"View\s+Latest\s+Bill", re.I)),
            page.locator('button:has-text("View Latest Bill"), a:has-text("View Latest Bill")'),
        ]:
            try:
                if await loc.count() > 0:
                    control = loc.first
                    break
            except Exception:
                continue

        if not control:
            self.log("[landing] 'View Latest Bill' control not found")
            return None

        await control.scroll_into_view_if_needed()
        await control.hover()
        await page.wait_for_timeout(150)

        # Arm a context-wide PDF sniffer BEFORE the click
        sniff_task = asyncio.create_task(self._sniff_context_pdf_once(timeout_ms=15000))

        before_pages = list(ctx.pages)
        await control.click()

        pdf_page: Optional[Page] = None
        for _ in range(40):
            now = list(ctx.pages)
            if len(now) > len(before_pages):
                pdf_page = now[-1]  # new tab
                break
            if page.url != before_pages[0].url:  # same-tab nav
                pdf_page = page
                break
            await page.wait_for_timeout(250)

        # If the sniffer already caught bytes, write & return
        if sniff_task.done():
            data = await sniff_task
            if data:
                path = self._write_pdf_bytes("scg-bill.pdf", data)
                if path:
                    return path

        if not pdf_page:
            self.log("[download] statement viewer never opened")
            try:
                data = await asyncio.wait_for(sniff_task, 2)
                if data:
                    path = self._write_pdf_bytes("scg-bill.pdf", data)
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

        # Multi-strategy viewer downloader
        pdf_path = await self._download_pdf_from_scg_viewer(pdf_page, initial_url)
        if pdf_path:
            return pdf_path

        # Final fallback: if viewer failed, check if sniffer succeeded late
        try:
            data = await asyncio.wait_for(sniff_task, 2)
            if data:
                path = self._write_pdf_bytes("scg-bill.pdf", data)
                if path:
                    return path
        except Exception:
            pass

        self.log("[download] failed to download PDF from viewer")
        return None

    # -------------- small locator utils --------------

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


# ---- PDF parsing -------------------------------------------------------------

def extract_amount_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Extract bill total from SCG PDFs.

    Strategy:
      A) Regex around 'Amount Now Due' / 'Amount Due' / etc. (same or nearby lines)
      B) Line-wise proximity search
      C) Fallback: choose the largest $xx.xx in the doc
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None

    # Normalize whitespace; keep single newlines for locality
    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    money_pat = r"(\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
    direct_pats = [
        rf"Amount\s+Now\s+Due(?:\s+by[^\n]*?)?[\s\S]{{0,120}}?{money_pat}",
        rf"Amount\s+Due[\s\S]{{0,120}}?{money_pat}",
        rf"Total\s+Amount\s+Due[\s\S]{{0,120}}?{money_pat}",
        rf"Total\s+Due[\s\S]{{0,120}}?{money_pat}",
        rf"Please\s+pay[\s\S]{{0,120}}?{money_pat}",
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

    # line-wise proximity
    lines = [ln.strip() for ln in squashed.splitlines() if ln.strip()]
    kw = re.compile(r"(amount\s+now\s+due|amount\s+due|total\s+amount\s+due|total\s+due|please\s+pay)", re.I)
    num_re = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})")

    for i, ln in enumerate(lines):
        if kw.search(ln):
            m = num_re.search(ln)
            if not m:
                for j in range(1, 5):
                    if i + j < len(lines):
                        m = num_re.search(lines[i + j])
                        if m:
                            break
            if m:
                clean = m.group(0).replace("$", "").replace(",", "").strip()
                try:
                    cents = int(round(float(clean) * 100))
                except Exception:
                    cents = None
                return clean, cents

    # fallback: biggest number
    candidates = CURRENCY_RE_STRICT.findall(squashed) or CURRENCY_RE_LOOSE.findall(squashed)
    nums: List[float] = []
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
        from pdfminer_high_level import extract_text  # wrong import name guarded below
        return extract_text(pdf_path)
    except Exception:
        try:
            from pdfminer.high_level import extract_text  # type: ignore
            return extract_text(pdf_path)
        except Exception:
            return None


def _extract_text_pypdf(pdf_path: str) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(pdf_path)
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks)
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
# ---- CLI --------------------------------------------------------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="SCG 'View Latest Bill' downloader + amount extractor (Real Chrome/headless)")
    p.add_argument("--username", help="UserID / Username (or set SCG_USERNAME)")
    p.add_argument("--password", help="Password (or set SCG_PASSWORD)")
    p.add_argument("--headful", action="store_true", help="Show browser window (uses common/browser Real Chrome)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion in ms between actions")
    p.add_argument("--json", action="store_true", help="Print JSON instead of bare amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with SCGScraper(cfg) as s:
        result = await s.run()

    if not result.ok:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "amount":         result.amount,
            "amount_cents":   result.amount_cents,
            "pdf_path":       result.pdf_path,
            "final_url":      result.final_url,
            "statement_date": result.statement_date,
            "period_start":   result.period_start,
            "period_end":     result.period_end,
            "due_date":       result.due_date,
        }, indent=2))
    else:
        print(result.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
