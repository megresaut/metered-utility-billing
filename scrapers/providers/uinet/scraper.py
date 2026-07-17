#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI (United Illuminating) — "View Latest Bill" scraper with address filtering.
No proxy. Supports headful Real Chrome via common/browser.open_real_chrome, and
headless Playwright Chromium as a fallback.

Flow:
- Go to https://www.uinet.com/
- Click "Sign in / Register"
- Log in with username/password
- If there are multiple accounts, pick the one whose address best matches --address
- Land on that account dashboard
- Click the center-card "View latest bill" (not the nav link)
- Save the downloaded PDF
- Parse "Amount Due" from the PDF

Output:
- Default: prints ONLY the amount (e.g. 92.92)
- With --json:
  {
    "amount": "92.92",
    "amount_cents": 9292,
    "pdf_path": "/abs/path/ui-bill.pdf",
    "final_url": "https://..."
  }
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

# -------------------- Constants --------------------

HOME_URL = "https://www.uinet.com/"
DEFAULT_DL_DIR = Path(
    os.getenv("UI_DOWNLOAD_DIR", "/handoff/uinet")
).resolve()
# DEFAULT_DL_DIR = Path("./downloads/uinet").resolve()

# allow optional $, some extractors drop it
CURRENCY_RE_STRICT = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})")
CURRENCY_RE_LOOSE  = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})")


# ----------------- Models / Config -----------------

@dataclass
class Config:
    username: str
    password: str
    address_query: str
    account_number: Optional[str] = None
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    timezone_id: str = os.getenv("UI_TIMEZONE_ID", "America/New_York")


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
        username=args.username or os.getenv("UI_USERNAME", ""),
        password=args.password or os.getenv("UI_PASSWORD", ""),
        address_query=args.address or os.getenv("UI_ADDRESS", ""),
        account_number=args.account_number or os.getenv("UI_ACCOUNT_NUMBER"),
        headless=not args.headful,
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        timezone_id=os.getenv("UI_TIMEZONE_ID", "America/New_York"),
    )


# ----------------- Helpers -----------------

def _norm(s: str) -> str:
    """Uppercase, strip punctuation, collapse whitespace."""
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_street_only(s: str) -> str:
    """
    Normalize to: HOUSE_NUMBER + STREET NAME (+ suffix)
    Drops city, state, ZIP, unit, etc.

    Example:
      "230 LONG HILL CROSS RD, HM, SHELTON, CT, 06484"
      -> "230 LONG HILL CROSS RD"
    """
    if not s:
        return ""

    s = s.upper()

    # Cut off everything after first comma
    s = s.split(",", 1)[0]

    # Remove punctuation
    s = re.sub(r"[^\w\s]", " ", s)

    # Tokenize
    parts = s.split()

    if not parts:
        return ""

    # First token must be house number
    if not parts[0].isdigit():
        return ""

    house = parts[0]

    # Known street suffixes (expandable later)
    SUFFIXES = {
        "ST", "STREET",
        "RD", "ROAD",
        "AVE", "AVENUE",
        "BLVD", "BOULEVARD",
        "LN", "LANE",
        "DR", "DRIVE",
        "CT", "COURT",
        "PL", "PLACE",
        "WAY", "HWY", "HIGHWAY",
        "CIR", "CIRCLE",
        "PKWY", "PARKWAY",
    }

    street_tokens = []
    for t in parts[1:]:
        if t in SUFFIXES:
            street_tokens.append(t)
            break
        street_tokens.append(t)

    return " ".join([house] + street_tokens)


def _norm_account_number(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    # keep digits only
    digits = re.sub(r"\D", "", s)
    return digits or None


# ----------------- Scraper -----------------

class UIScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    # ---------- logging helper ----------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ---------- browser / stealth (CNG-style) ----------

    def _rand_viewport(self):
        # pick a laptop-y viewport (similar to CNG)
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
        await self._close()

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(**self._build_launch_kwargs())
        self.context = await self.browser.new_context(**self._build_context_kwargs())
        await self._install_stealth()

        self.page = await self.context.new_page()
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

    # -------------------- Main ---------------------

    async def run(self) -> Result:
        self.log("[run] starting")
        if not self.cfg.username or not self.cfg.password or not self.cfg.account_number:
            return Result(ok=False, error="username, password, and account_number are required")


        self.log("[step] open home → click Sign in / Register")
        if not await self._open_home_and_click_signin():
            return Result(ok=False, error="Could not reach login form from homepage")

        self.log("[step] login")
        if not await self._credential_login(self.cfg.username, self.cfg.password):
            return Result(ok=False, error="Login failed")

        self.log("[step] choose account for address")
        if not await self._pick_account_for_account_number(self.cfg.account_number):
            return Result(ok=False, error=f'No account matched account number: "{self.cfg.account_number}"')

        self.log('[step] click "View latest bill" → download')
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

    # -------------------- Steps (unchanged flow) --------------------

    async def _open_home_and_click_signin(self) -> bool:
        page = self.page
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            self.log("[entry] timeout hitting homepage")
            return False
        except Exception as e:
            self.log("[entry] page.goto exception:", repr(e))
            return False

        await self._dismiss_cookie_banner()

        # Header "Sign in / Register"
        loc = page.get_by_text(re.compile(r"^\s*Sign\s*in\s*/\s*Register\s*$", re.I))
        if await loc.count() == 0:
            loc = page.locator('header :is(a,button):has-text("Sign in / Register")')
        if await loc.count() == 0:
            self.log("[entry] sign-in button not found")
            return False

        self.log("[entry] clicking 'Sign in / Register'")
        await loc.first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await self._dismiss_cookie_banner()

        # Confirm presence of login inputs
        try:
            await page.locator('input[type="password"]').first.wait_for(timeout=9000)
            return True
        except Exception:
            self.log("[entry] password field not visible after Sign in / Register")
            return False

    async def _dismiss_cookie_banner(self):
        page = self.page
        for _ in range(3):
            dismissed = False
            for loc in [
                page.get_by_text(re.compile(r"^\s*Continue\s*$", re.I)),
                page.get_by_text(re.compile(r"^\s*Accept\s*$", re.I)),
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

        user_input = await self._first_visible([
            page.get_by_label(re.compile(r"User\s*ID|UserID|Username|Email", re.I)),
            page.get_by_placeholder(re.compile(r"User\s*ID|UserID|Username|Email", re.I)),
            page.locator('#EmailUsername, #username, input[name="username" i], input[id*="user" i]'),
        ])
        pass_input = await self._first_visible([
            page.get_by_label(re.compile(r"Password", re.I)),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.locator('#Password, input[type="password"]'),
        ])
        if not user_input or not pass_input:
            self.log("[login] inputs not found")
            return False

        self.log("[login] filling credentials")
        await user_input.fill(username)
        await pass_input.fill(password)

        # Try enter on password
        try:
            await pass_input.press("Enter")
        except Exception:
            pass

        # Also try explicit "Sign In" button
        btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"^\s*Sign\s*In\s*$", re.I)),
            page.get_by_text(re.compile(r"^\s*Sign\s*In\s*$", re.I)),
            page.locator('button[type="submit"], input[type="submit"]'),
        ])
        if btn:
            try:
                self.log("[login] clicking Sign In")
                await btn.click()
            except Exception:
                try:
                    await btn.click(force=True)
                except Exception:
                    pass

        try:
            await page.wait_for_load_state("networkidle", timeout=35000)
        except Exception:
            pass
        await self._dismiss_cookie_banner()

        return True

    # ---------- helper timing / stabilization utilities ----------

    def _now(self) -> float:
        loop = asyncio.get_running_loop()
        return loop.time()

    async def _wait_for_url_stable(self, settle_ms: int = 400, total_timeout: int = 20000) -> bool:
        """
        Wait until the page URL stops changing for settle_ms consecutively.
        Helps after login / account switch.
        """
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
        """
        After logging in, UI may either:
        - land on a dashboard (with "View latest bill")
        - land on an account list / Manage Accounts table
        We sniff which one we're on.
        """
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
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

    # async def _pick_account_for_address(self, address_query: str, account_number: Optional[str] = None) -> bool:
    #     """
    #     If we're dropped into "Manage Accounts", pick the row whose address best
    #     matches address_query. Otherwise, if we're already at dashboard, just continue.
    #     """
    #     page = self.page

    #     state = await self._wait_for_dashboard_or_accounts()
    #     self.log(f"[accounts] initial state={state} url={page.url}")
    #     if state == "dashboard":
    #         return True
    #     if state != "accounts":
    #         # maybe race, check again briefly
    #         state = await self._wait_for_dashboard_or_accounts(5000)
    #         if state == "dashboard":
    #             return True
    #         if state != "accounts":
    #             return False

    #     q_norm = _norm_street_only(address_query)
    #     acct_norm = _norm_account_number(account_number) if account_number else None
    #     self.log("[accounts] q_norm =", q_norm)

    #     # Search box on the account picker table
    #     lookup = await self._first_present([
    #         page.get_by_role("textbox", name=re.compile(r"Account\s*Lookup|Search|Lookup", re.I)),
    #         page.get_by_placeholder(re.compile(r"Search|Lookup", re.I)),
    #         page.locator('input[type="search"]'),
    #     ])
    #     if lookup:
    #         try:
    #             await lookup.fill("")
    #             await lookup.type(address_query, delay=20)
    #             await page.keyboard.press("Enter")
    #             await page.wait_for_timeout(450)
    #         except Exception:
    #             pass

    #     table = page.locator("table[role='grid'], table.p-datatable-table")
    #     row_nodes = table.locator("tbody tr")
    #     count = await row_nodes.count()
    #     self.log("[accounts] rows found:", count)

    #     matched_row = None
    #     for i in range(count):
    #         r = row_nodes.nth(i)

    #         # --- Account number (column 1, first line) ---
    #         try:
    #             acct_text = await r.locator("td:nth-child(1)").inner_text()
    #         except Exception:
    #             acct_text = ""

    #         acct_row_norm = _norm_account_number(acct_text)

    #         # --- Service address (column 2) ---
    #         try:
    #             addr_text = await r.locator("td:nth-child(2)").inner_text()
    #         except Exception:
    #             addr_text = ""

    #         addr_norm = _norm_street_only(addr_text)

    #         # --- Account number must match if provided ---
    #         if acct_norm:
    #             if acct_norm != acct_row_norm:
    #                 self.log(
    #                     f"[row {i}] acct mismatch row={acct_row_norm} expected={acct_norm}"
    #                 )
    #                 continue

    #         # --- Street address match ---
    #         is_match = q_norm and (q_norm in addr_norm)

    #         self.log(
    #             f"[row {i}] acct='{acct_row_norm}' addr='{addr_text}' match={is_match}"
    #         )

    #         if is_match:
    #             matched_row = r
    #             break

    #     if not matched_row:
    #         self.log("[accounts] no row matched")
    #         return False

    #     # First cell is usually clickable (account number link/button)
    #     first_cell = matched_row.locator("td").first
    #     link = first_cell.locator("a, button")
    #     if await link.count() == 0:
    #         self.log("[accounts] no clickable element in first cell")
    #         return False

    #     await link.first.scroll_into_view_if_needed()

    #     # Navigation can be SPA-y, so don't hard-rely on expect_navigation.
    #     try:
    #         await link.first.click()
    #     except Exception:
    #         try:
    #             await link.first.click(force=True)
    #         except Exception:
    #             return False

    #     # give it a chance to settle into dashboard
    #     try:
    #         await self.page.wait_for_url(re.compile(r"/dashboard|/account|/home"), timeout=20000)
    #     except Exception:
    #         pass
    #     await self._wait_for_url_stable(500, total_timeout=8000)

    #     try:
    #         await page.get_by_text(re.compile(r"View\s+latest\s+bill", re.I)).first.wait_for(timeout=20000)
    #         return True
    #     except Exception:
    #         await self._wait_for_url_stable(500, total_timeout=3000)
    #         if await self._safe_count(page.get_by_role("button", name=re.compile(r"View\s+latest\s+bill", re.I))) > 0:
    #             return True
    #         return False


    async def _pick_account_for_account_number(
        self,
        account_number: Optional[str],
    ) -> bool:
        """
        If we're dropped into "Manage Accounts", pick the row whose account number
        (if provided) AND street address match. Otherwise, if we're already at
        dashboard, just continue.
        """
        page = self.page

        state = await self._wait_for_dashboard_or_accounts()
        self.log(f"[accounts] initial state={state} url={page.url}")
        if state == "dashboard":
            return True
        if state != "accounts":
            # maybe race, check again briefly
            state = await self._wait_for_dashboard_or_accounts(5000)
            if state == "dashboard":
                return True
            if state != "accounts":
                return False

        acct_norm = _norm_account_number(account_number)
        self.log("[accounts] acct_norm =", acct_norm)

        # NOTE: we used to type into the PrimeNG search box, but that races
        # with the table filter re-render — by the time we scroll to the
        # matched row, the filter has narrowed the table and `nth(i)` is
        # stale, causing a 30s scroll_into_view timeout. We just iterate
        # all rows and match by account number text — much more robust.

        table = page.locator("table[role='grid'], table.p-datatable-table")
        row_nodes = table.locator("tbody tr")
        try:
            await row_nodes.first.wait_for(state="visible", timeout=15000)
        except Exception:
            self.log("[accounts] no rows ever rendered")
            return False
        count = await row_nodes.count()
        self.log("[accounts] rows found:", count)

        # Pin the matched row as an element_handle so subsequent DOM
        # updates (sorts/filters/re-renders) can't invalidate it.
        matched_handle = None
        for i in range(count):
            r = row_nodes.nth(i)
            try:
                acct_text = await r.locator("td:nth-child(1)").inner_text(timeout=4000)
            except Exception:
                acct_text = ""

            acct_first_line = acct_text.splitlines()[0].strip() if acct_text else ""
            acct_row_norm = _norm_account_number(acct_first_line)

            self.log(
                f"[accounts][row {i}] acct_first_line={acct_first_line!r} "
                f"norm={acct_row_norm!r} match={acct_norm == acct_row_norm}"
            )

            if acct_norm and acct_norm == acct_row_norm:
                try:
                    matched_handle = await r.element_handle(timeout=4000)
                except Exception:
                    matched_handle = None
                break

        if not matched_handle:
            # Fallback: type the account number into the Account Lookup search box.
            # Some accounts don't appear in the default table view and only show up
            # after filtering (e.g. accounts added via "Add Account" that aren't in
            # the primary list).
            self.log("[accounts] no row matched in default view — trying search box")
            try:
                search_input = page.locator(
                    'input[pInputText], input.p-inputtext, '
                    'input[placeholder*="lookup" i], input[placeholder*="account" i], '
                    'input[placeholder*="search" i]'
                ).first
                await search_input.wait_for(state="visible", timeout=5000)
                await search_input.fill(acct_norm or account_number or "")
                # Wait for the table to re-render with filtered results
                await page.wait_for_timeout(1500)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                # Read the (now-filtered) rows fresh
                count2 = await row_nodes.count()
                self.log("[accounts] rows after search:", count2)
                for j in range(count2):
                    r2 = row_nodes.nth(j)
                    try:
                        acct_text2 = await r2.locator("td:nth-child(1)").inner_text(timeout=4000)
                    except Exception:
                        acct_text2 = ""
                    acct_first_line2 = acct_text2.splitlines()[0].strip() if acct_text2 else ""
                    acct_row_norm2 = _norm_account_number(acct_first_line2)
                    self.log(
                        f"[accounts][search row {j}] acct_first_line={acct_first_line2!r} "
                        f"norm={acct_row_norm2!r} match={acct_norm == acct_row_norm2}"
                    )
                    if acct_norm and acct_norm == acct_row_norm2:
                        try:
                            matched_handle = await r2.element_handle(timeout=4000)
                        except Exception:
                            matched_handle = None
                        break
            except Exception as search_err:
                self.log("[accounts] search box fallback failed:", repr(search_err))

        if not matched_handle:
            self.log("[accounts] no row matched")
            return False

        # Find the clickable element inside the first cell of the pinned row.
        link_handle = None
        try:
            first_cell_h = await matched_handle.query_selector("td")
            if first_cell_h:
                link_handle = await first_cell_h.query_selector("a, button")
        except Exception:
            link_handle = None

        if not link_handle:
            self.log("[accounts] no clickable element in first cell; clicking row")
            try:
                await matched_handle.scroll_into_view_if_needed(timeout=8000)
                await matched_handle.click(timeout=5000)
            except Exception:
                try:
                    await matched_handle.click(force=True, timeout=5000)
                except Exception:
                    return False
        else:
            try:
                await link_handle.scroll_into_view_if_needed(timeout=8000)
            except Exception:
                pass
            try:
                await link_handle.click(timeout=5000)
            except Exception:
                try:
                    await link_handle.click(force=True, timeout=5000)
                except Exception:
                    return False

        # give it a chance to settle into dashboard
        try:
            await self.page.wait_for_url(
                re.compile(r"/dashboard|/account|/home"),
                timeout=20000,
            )
        except Exception:
            pass
        await self._wait_for_url_stable(500, total_timeout=8000)

        try:
            await page.get_by_text(
                re.compile(r"View\s+latest\s+bill", re.I)
            ).first.wait_for(timeout=20000)
            return True
        except Exception:
            await self._wait_for_url_stable(500, total_timeout=3000)
            if await self._safe_count(
                page.get_by_role("button", name=re.compile(r"View\s+latest\s+bill", re.I))
            ) > 0:
                return True
            return False


    # ========= CNG-style viewer tab + byte-extraction =========

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

    async def _download_pdf_from_ui_viewer(self, pdf_page: Page, initial_url: Optional[str] = None) -> Optional[str]:
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
                            path0 = self._write_pdf_bytes("ui-bill.pdf", data0)
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
            path = self._write_pdf_bytes("ui-bill.pdf", data)
            if path:
                return path

        # Strategy 2: direct GET of initial_url (if http(s))
        if initial_url and initial_url.startswith(("http://", "https://")):
            try:
                resp = await self.context.request.get(
                    initial_url,
                    headers={"Referer": "https://www.uinet.com/"},
                )
                if resp.ok:
                    b = await resp.body()
                    path = self._write_pdf_bytes("ui-bill.pdf", b)
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
                    path = self._write_pdf_bytes("ui-bill.pdf", b)
                    if path:
                        return path
                continue

            # data:
            if abs_url.startswith("data:") and ";base64," in abs_url:
                try:
                    b = base64.b64decode(abs_url.split(";base64,", 1)[1])
                    path = self._write_pdf_bytes("ui-bill.pdf", b)
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
                        headers={"Referer": "https://www.uinet.com/"},
                    )
                    if resp.ok:
                        b = await resp.body()
                        path = self._write_pdf_bytes("ui-bill.pdf", b)
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
                    async with pdf_page.expect_download(timeout=10000) as dl_ev:
                        await loc.click()
                    dl: Download = await dl_ev.value
                    suggested = dl.suggested_filename or "ui-bill.pdf"
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

    # async def _click_view_latest_bill_and_download(self) -> Optional[str]:
    #     """
    #     Click ONLY the center-card 'View latest bill' (not the side nav),
    #     then save PDF using multi-strategy viewer logic (CNG-style).
    #     """
    #     page = self.page
    #     ctx  = self.context
    #     self.log("[download] searching for 'View latest bill' button")

    #     # Prefer visible, non-nav instances
    #     handle = None
    #     candidates = [
    #         page.get_by_role("button", name=re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
    #         page.get_by_role("link",   name=re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
    #         page.locator(':is(a,button):has-text("View latest bill")'),
    #         page.get_by_text(re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
    #     ]

    #     for loc in candidates:
    #         try:
    #             if await loc.count() > 0:
    #                 # choose first one that's visible and *not* inside nav/aside/header
    #                 for el in await loc.element_handles():
    #                     vis = await el.is_visible()
    #                     in_nav = await el.evaluate(
    #                         """(el) => !!el.closest('nav,[role="navigation"],aside,header,footer,[aria-label*="menu" i]')"""
    #                     )
    #                     if vis and not in_nav:
    #                         handle = el
    #                         break
    #         except Exception:
    #             continue
    #         if handle:
    #             break

    #     if not handle:
    #         self.log("[download] 'View latest bill' control not found")
    #         return None

    #     try:
    #         await handle.scroll_into_view_if_needed()
    #         await handle.hover()
    #         await page.wait_for_timeout(200)
    #     except Exception:
    #         pass

    #     # Arm a context-wide sniffer BEFORE click
    #     sniff_task = asyncio.create_task(self._sniff_context_pdf_once(timeout_ms=15000))

    #     before_pages = list(ctx.pages)
    #     await handle.click()

    #     pdf_page: Optional[Page] = None
    #     for _ in range(40):
    #         now = list(ctx.pages)
    #         if len(now) > len(before_pages):
    #             pdf_page = now[-1]
    #             break
    #         if page.url != before_pages[0].url:  # same-tab nav
    #             pdf_page = page
    #             break
    #         await page.wait_for_timeout(250)

    #     # If sniffer already caught bytes, write & return immediately
    #     if sniff_task.done():
    #         data = await sniff_task
    #         if data:
    #             path = self._write_pdf_bytes("ui-bill.pdf", data)
    #             if path:
    #                 return path

    #     # Proceed with viewer strategies
    #     if not pdf_page:
    #         self.log("[download] statement viewer never opened")
    #         try:
    #             data = await asyncio.wait_for(sniff_task, 2)
    #             if data:
    #                 path = self._write_pdf_bytes("ui-bill.pdf", data)
    #                 if path:
    #                     return path
    #         except Exception:
    #             pass
    #         return None

    #     try:
    #         await pdf_page.wait_for_load_state("domcontentloaded", timeout=20000)
    #     except Exception:
    #         pass

    #     initial_url = None
    #     try:
    #         initial_url = pdf_page.url
    #     except Exception:
    #         pass

    #     pdf_path = await self._download_pdf_from_ui_viewer(pdf_page, initial_url)
    #     if pdf_path:
    #         return pdf_path

    #     # last chance: if context sniffer succeeded late
    #     try:
    #         data = await asyncio.wait_for(sniff_task, 2)
    #         if data:
    #             path = self._write_pdf_bytes("ui-bill.pdf", data)
    #             if path:
    #                 return path
    #     except Exception:
    #         pass

    #     self.log("[download] failed to download PDF from viewer")
    #     return None
    async def _dismiss_bill_ready_modal(self, max_wait_ms: int = 8000) -> bool:
        """
        On dashboard load, UI pops up a PrimeNG dialog titled
        'Your next bill is ready' whenever a new statement is available.
        The dialog (component: app-dashboard-bill-banner-dialog, host:
        div[role="dialog"].billPdfPanel) renders an overlay
        (.p-dialog-mask.p-component-overlay) on top of the dashboard, so
        any click on 'View latest bill' is intercepted until the dialog
        is dismissed.

        The dialog exposes two primary buttons inside
        app-dashboard-bill-banner-dialog:
          - 'My account' (primary, class agr-button-primary) → closes modal
          - 'Pay bill'   (secondary)                         → would start payment
        We click 'My account' to return to the underlying dashboard.

        Returns True if the modal was found AND dismissed, False otherwise
        (e.g. no new bill, so no modal in the first place).
        """
        page = self.page

        # Scope to the dialog host. Using `:has()` requires Playwright 1.27+,
        # but multiple selectors cover Angular + PrimeNG variations.
        dialog = page.locator(
            'app-dashboard-bill-banner-dialog, '
            'div[role="dialog"].billPdfPanel, '
            'div[role="dialog"][aria-modal="true"]:has(app-dashboard-bill-banner-dialog)'
        ).first

        try:
            await dialog.wait_for(state="visible", timeout=max_wait_ms)
        except Exception:
            self.log("[modal] no 'next bill is ready' modal appeared")
            return False

        self.log("[modal] 'next bill is ready' modal detected — clicking 'My account'")

        # All selectors are scoped to the dialog/overlay so we never match
        # the side-nav 'My account' link or any other lookalike.
        btn_selectors = [
            'app-dashboard-bill-banner-dialog button.agr-button-primary',
            'app-dashboard-bill-banner-dialog button:has-text("My account")',
            'div[role="dialog"][aria-modal="true"] button.agr-button-primary:has-text("My account")',
            'div[role="dialog"][aria-modal="true"] button:has-text("My account")',
            '.p-dialog.billPdfPanel button.agr-button-primary',
        ]

        clicked = False
        for sel in btn_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                await btn.wait_for(state="visible", timeout=3000)
                try:
                    await btn.click(timeout=4000)
                except Exception:
                    try:
                        await btn.click(force=True, timeout=4000)
                    except Exception:
                        continue
                clicked = True
                self.log(f"[modal] clicked 'My account' via selector: {sel}")
                break
            except Exception:
                continue

        if not clicked:
            self.log("[modal] could not click 'My account' — trying Escape as fallback")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        # Wait for the dialog to detach / fade out (PrimeNG ng-trigger-animation).
        try:
            await dialog.wait_for(state="hidden", timeout=10000)
        except Exception:
            await page.wait_for_timeout(1500)

        # Also wait for the masking overlay to leave the DOM.
        try:
            await page.locator(".p-dialog-mask.p-component-overlay").first.wait_for(
                state="hidden", timeout=5000
            )
        except Exception:
            pass

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        self.log("[modal] dismissed")
        return clicked

    async def _click_view_latest_bill_and_download(self) -> Optional[str]:
        """
        Click ONLY the center-card 'View latest bill' (not the side nav),
        then save PDF using multi-strategy viewer logic (CNG-style).

        If the 'Your next bill is ready' modal is up, dismiss it first via
        the 'My account' button — otherwise the modal overlay intercepts
        every click on the underlying dashboard.
        """
        page = self.page
        ctx  = self.context

        # ------------------------------------------------------------
        # 0️⃣ Dismiss the 'Your next bill is ready' modal if it appears.
        #    Wait up to ~8s for it to animate in. If no modal, returns fast.
        # ------------------------------------------------------------
        await self._dismiss_bill_ready_modal(max_wait_ms=8000)

        # ------------------------------------------------------------
        # 1️⃣ Wait for dashboard content BEFORE searching
        # ------------------------------------------------------------
        try:
            await page.wait_for_selector(
                ":has-text('View latest bill')",
                timeout=15000
            )
        except Exception:
            self.log("[download] dashboard did not render 'View latest bill' in time")

        self.log("[download] searching for 'View latest bill' button")

        # ------------------------------------------------------------
        # 2️⃣ Locate the CENTER-CARD "View latest bill" control
        # ------------------------------------------------------------
        handle = None
        candidates = [
            page.get_by_role("button", name=re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
            page.get_by_role("link",   name=re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
            page.locator(':is(a,button):has-text("View latest bill")'),
            page.get_by_text(re.compile(r"^\s*View\s+latest\s+bill\s*$", re.I)),
        ]

        for loc in candidates:
            try:
                if await loc.count() > 0:
                    for el in await loc.element_handles():
                        vis = await el.is_visible()
                        in_nav = await el.evaluate(
                            """(el) => !!el.closest('nav,[role="navigation"],aside,header,footer,[aria-label*="menu" i]')"""
                        )
                        if vis and not in_nav:
                            handle = el
                            break
            except Exception:
                continue
            if handle:
                break

        if not handle:
            self.log("[download] 'View latest bill' control not found")
            return None

        # ------------------------------------------------------------
        # 3️⃣ Click + sniff PDF
        # ------------------------------------------------------------
        try:
            await handle.scroll_into_view_if_needed()
            await handle.hover()
            await page.wait_for_timeout(200)
        except Exception:
            pass

        sniff_task = asyncio.create_task(
            self._sniff_context_pdf_once(timeout_ms=15000)
        )

        before_pages = list(ctx.pages)
        await handle.click()

        # ------------------------------------------------------------
        # 4️⃣ Detect viewer / new tab
        # ------------------------------------------------------------
        pdf_page: Optional[Page] = None
        for _ in range(40):
            now = list(ctx.pages)
            if len(now) > len(before_pages):
                pdf_page = now[-1]
                break
            if page.url != before_pages[0].url:
                pdf_page = page
                break
            await page.wait_for_timeout(250)

        if sniff_task.done():
            data = await sniff_task
            if data:
                return self._write_pdf_bytes("ui-bill.pdf", data)

        if not pdf_page:
            self.log("[download] statement viewer never opened")
            return None

        try:
            await pdf_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        initial_url = pdf_page.url if pdf_page else None

        pdf_path = await self._download_pdf_from_ui_viewer(pdf_page, initial_url)
        if pdf_path:
            return pdf_path

        try:
            data = await asyncio.wait_for(sniff_task, 2)
            if data:
                return self._write_pdf_bytes("ui-bill.pdf", data)
        except Exception:
            pass

        self.log("[download] failed to download PDF from viewer")
        return None


    # ---------------- locator sugar ----------------

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


# ----------------- PDF parsing -------------------

def extract_amount_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Extract the bill total from the saved PDF.

    Strategy:
      A) Look for labels like "Amount Now Due", "Amount Due", "Total Amount Due",
         "Please pay", etc. within ~120 chars of a money value.
      B) Fallback: pick the largest currency-like number in the file.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None

    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)
    money = r"(\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"

    direct_pats = [
        rf"Amount\s+Now\s+Due(?:\s+by[^\n]*?)?[\s\S]{{0,120}}?{money}",
        rf"Amount\s+Due[\s\S]{{0,120}}?{money}",
        rf"Total\s+Amount\s+Due[\s\S]{{0,120}}?{money}",
        rf"Total\s+Due[\s\S]{{0,120}}?{money}",
        rf"Please\s+pay[\s\S]{{0,120}}?{money}",
    ]
    for pat in direct_pats:
        m = re.search(pat, squashed, re.I)
        if m:
            raw = m.group(1)
            clean = raw.replace("$", "").replace(",", "").strip()
            try:
                cents = int(round(float(clean) * 100))
            except Exception:
                cents = None
            return clean, cents

    # fallback: choose largest-ish amount in doc
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


# --------------------- CLI ----------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="UI 'View Latest Bill' downloader + amount extractor (real Chrome/headless)"
    )
    p.add_argument("--username", help="UserID / Username (or env UI_USERNAME)")
    p.add_argument("--password", help="Password (or env UI_PASSWORD)")
    p.add_argument("--address", required=True, help="Substring to match the Service Address (or env UI_ADDRESS)")
    p.add_argument("--account_number", required=True, help="Account Number (or env UI_ACCOUNT_NUMBER)")
    p.add_argument("--headful", action="store_true", help="Run with real Chrome visible (uses common/browser)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion in ms between actions")
    p.add_argument("--json", action="store_true", help="Print JSON instead of bare amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with UIScraper(cfg) as s:
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
