#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SNEW (South Norwalk Electric and Water) — latest bill scraper.

Flow:
1. Go straight to billingHistory
2. Check:
   - If we see the SmartHub "ARE YOU READY..." modal OR we see the billing table rows,
     assume we are logged in.
   - Otherwise (no modal + no rows), try one login attempt.
3. Dismiss the modal using the header X or the "No" button (not the backdrop).
4. Scrape first row:
   - extract $amount
   - click View Bill (data-cy="viewBillLink")
   - capture new tab
   - download PDF
5. Output amount and pdf_path (JSON optional)

Key differences:
- Seeing that marketing modal = we're in. Don't try to log in again.
- We no longer click the fullscreen backdrop because it was timing out.
- We removed all re-check login loops after we're clearly in.
"""

import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlsplit, unquote
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
)

BILLING_HISTORY_URL = "https://snew.smarthub.coop/ui/#/billingHistory"
DEFAULT_DL_DIR = Path(
    os.getenv("SNEW_DOWNLOAD_DIR", "/handoff/snew")
).resolve()
CURRENCY_RE = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    username: str
    password: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    use_bright_proxy: bool = False
    bright_proxy: Optional[dict] = None
    timezone_id: str = os.getenv("SNEW_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None          # "202.41"
    amount_cents: Optional[int] = None    # 20241
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # NEW: parsed from PDF
    statement_date: Optional[str] = None  # "2025-10-31"
    due_date: Optional[str] = None        # "2025-11-30"
    period_start: Optional[str] = None    # "2025-09-21"
    period_end: Optional[str] = None      # "2025-10-25"


def build_config(args) -> Config:
    bright_server = os.getenv("BRIGHT_PROXY_SERVER")
    bright_user = os.getenv("BRIGHT_PROXY_USER")
    bright_pass = os.getenv("BRIGHT_PROXY_PASS")

    return Config(
        username=args.username or os.getenv("SNEW_USERNAME") or "",
        password=args.password or os.getenv("SNEW_PASSWORD") or "",
        headless=not bool(args.headful) if args.headful is not None else _env_bool("SNEW_HEADLESS", True),
        slow_mo_ms=int(os.getenv("SNEW_SLOW_MO_MS", str(args.slow_mo or 0))),
        debug=bool(args.debug) or _env_bool("SNEW_DEBUG", False),
        use_bright_proxy=bool(bright_server),
        bright_proxy={"server": bright_server, "username": bright_user, "password": bright_pass} if bright_server else None,
        timezone_id=os.getenv("SNEW_TIMEZONE_ID", "America/New_York"),
    )


class SNEWScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None
        self._active_proxy = None

    # ---------- logging ----------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ---------- browser plumbing ----------
    def _proxy_username_with_session(self, base_username: Optional[str]) -> Optional[str]:
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

    def _build_launch_kwargs(self):
        launch_kwargs = dict(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        if self.cfg.headless:
            launch_kwargs["args"].append("--headless=new")

        bright = self.cfg.bright_proxy if self.cfg.use_bright_proxy else None
        if bright and bright.get("server"):
            proxy_username = self._proxy_username_with_session(bright.get("username"))
            self._active_proxy = {
                "server": bright["server"],
                "username": proxy_username,
                "password": bright.get("password"),
            }
            launch_kwargs["proxy"] = self._active_proxy
            self.log("[pw] using Bright proxy:", self._active_proxy)

        return launch_kwargs

    def _build_context_kwargs(self):
        w, h = self._rand_viewport()
        return {
            "accept_downloads": False,
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
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1});
        """
        try:
            await self.context.add_init_script(stealth_js)
        except Exception:
            pass

    async def __aenter__(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)

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

        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] closing browser context")
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ---------- overall flow ----------
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        # 1. Go to billing history page directly
        self.log("[run] step 1: goto billingHistory")
        ok = await self._goto_billing_history()
        if not ok:
            return Result(ok=False, error="navigation failed")

        # Give the SPA a moment to render SOMETHING before we decide login state
        self.log("[run] step 2: initial wait_for_app_ready()")
        await self._wait_for_app_ready()

        # 2. Check: are we clearly logged in already?
        self.log("[run] step 3: check logged-in heuristics")
        already_in = await self._is_logged_in_view()
        self.log("[run] already_in?:", already_in)

        if not already_in:
            # not obviously in -> try logging in ONCE
            self.log("[run] not logged in -> login_once()")
            await self._login_once(self.cfg.username, self.cfg.password)

            # After login submit, wait again for SPA to actually finish routing
            self.log("[run] step 4: post-login wait_for_app_ready()")
            await self._wait_for_app_ready()

        # now we assume we're on either dashboard or billingHistory with the modal up

        # 3. Kill the modal if it's up
        self.log("[run] step 5: dismiss marketing modal if present")
        await self._dismiss_modal_once()

        # After dismiss, give page a sec to settle and show table
        self.log("[run] step 6: final wait_for_app_ready() before scrape")
        await self._wait_for_app_ready()

        # 4. Scrape: first bill row, click View Bill, download PDF
        self.log("[run] step 7: scrape_first_row_and_download_pdf()")
        amount_str, amount_cents, pdf_path = await self._scrape_first_row_and_download_pdf()

        self.log("[run] step 7: _debug_print_first_row()")
        await self._debug_print_first_row()

        if not amount_str:
            return Result(ok=False, error="Could not extract amount / download PDF")

        statement_date, due_date, period_start, period_end = extract_snew_dates_from_pdf(pdf_path)

        return Result(
            ok=True,
            amount=amount_str,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=self.page.url,
            statement_date=statement_date,
            due_date=due_date,
            period_start=period_start,
            period_end=period_end,
        )

    # ---------- helpers: nav / login ----------
    async def _goto_billing_history(self) -> bool:
        try:
            await self.page.goto(
                BILLING_HISTORY_URL,
                wait_until="domcontentloaded",
                timeout=self.cfg.nav_timeout_ms,
            )
            return True
        except PWTimeoutError:
            self.log("[nav] timeout loading billingHistory")
            return False
        except Exception as e:
            self.log("[nav] other nav err:", repr(e))
            return False

    async def _wait_for_app_ready(self) -> None:
        """
        After navigation or login, SmartHub is an SPA that needs a second to render.
        We don't want to touch anything until either:
          - the marketing modal header shows up, OR
          - the billing history table appears, OR
          - we clearly landed on a dashboard card layout (like the screenshot).

        We'll poll gently for a few seconds instead of instantly poking.
        """
        page = self.page

        # we'll try up to ~4 seconds total
        for _ in range(8):
            try:
                # 1) modal header?
                hdr = page.get_by_text(
                    re.compile(r"ARE YOU READY FOR A NEW LOOK AND FEEL OF SMARTHUB", re.I)
                )
                if await hdr.count() > 0:
                    self.log("[wait_ready] modal header detected")
                    return
            except Exception:
                pass

            try:
                # 2) billing table rows?
                rows = page.locator("table tbody tr")
                if await rows.count() > 0:
                    self.log("[wait_ready] billing table rows detected")
                    return
            except Exception:
                pass

            try:
                # 3) dashboard card layout (like the big white modal over a dimmed dashboard):
                # we'll look for that big modal container with the blue header bar
                modal_frame = page.locator("div[role='dialog'], div[aria-modal='true']")
                if await modal_frame.count() > 0:
                    self.log("[wait_ready] generic modal/dialog detected")
                    return
            except Exception:
                pass

            # small delay before polling again
            await page.wait_for_timeout(500)

        self.log("[wait_ready] finished polling; proceeding anyway")


    async def _is_logged_in_view(self) -> bool:
        """
        Heuristic for "we're already logged in":
        - If the SmartHub marketing modal is visible, that's only shown AFTER login.
        - OR if billing table rows are present (table tbody tr > 0).
        """
        if await self._marketing_modal_present():
            self.log("[is_logged_in_view] modal detected => logged in")
            return True

        if await self._has_rows():
            self.log("[is_logged_in_view] billing rows detected => logged in")
            return True

        self.log("[is_logged_in_view] neither modal nor rows visible yet")
        return False

    async def _login_once(self, username: str, password: str) -> None:
        """
        Try to log in exactly one time.
        If login form isn't visible, we just return.
        We do NOT loop or retry. We do NOT try again later.
        """
        page = self.page
        self.log("[login_once] looking for email/password inputs")

        email_input = await self._first_visible([
            page.get_by_label(re.compile("Email", re.I)),
            page.locator('input[type="email"]'),
            page.locator('input[name="email" i]'),
        ], timeout=6000)

        pass_input = await self._first_visible([
            page.get_by_label(re.compile("Password", re.I)),
            page.locator('input[type="password"]'),
        ], timeout=6000)

        if not email_input or not pass_input:
            self.log("[login_once] no login form => skipping")
            return

        self.log("[login_once] filling creds")
        await email_input.fill(username)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await pass_input.fill(password)

        submit_btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('button:has-text("Sign In")'),
            page.locator('input[type="submit"]'),
        ])
        if submit_btn:
            self.log("[login_once] clicking submit_btn")
            await submit_btn.click()
        else:
            self.log("[login_once] pressing Enter fallback")
            await page.keyboard.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        # done. we don't verify here. run() will handle modal/scrape next.

    # ---------- helpers: modal ----------
    async def _marketing_modal_present(self) -> bool:
        """
        Returns True if the SmartHub marketing popup ('ARE YOU READY FOR A NEW LOOK...')
        is currently visible on the page.
        """
        page = self.page
        hdr = page.get_by_text(
            re.compile(r"ARE YOU READY FOR A NEW LOOK AND FEEL OF SMARTHUB", re.I)
        )
        try:
            # count() > 0 is enough; we don't need wait_for here
            if await hdr.count() > 0:
                return True
        except Exception:
            pass
        return False

    async def _dismiss_modal_once(self) -> None:
        """
        Dismiss the SmartHub modal ONE TIME, if present.
        Order of attempts:
          1. Click the "No" button in the footer (that's the desired behavior in screenshot)
          2. Click the header X
          3. Click "Yes" as absolute last resort
        After any click, pause briefly and return.
        """
        page = self.page

        # quick check if modal is even there
        if not await self._marketing_modal_present():
            self.log("[dismiss_modal_once] modal not present, nothing to do")
            return

        self.log("[dismiss_modal_once] modal present -> try 'No' first")

        # 1. Try the explicit "No" button in modal footer
        try:
            no_btn = page.get_by_role("button", name=re.compile(r"^no$", re.I))
            if await no_btn.count() > 0:
                self.log("[dismiss_modal_once] clicking 'No' button")
                await no_btn.first.click(timeout=2000)
                await page.wait_for_timeout(400)
                return
        except Exception as e:
            self.log("[dismiss_modal_once] 'No' click err:", repr(e))

        # 2. fallback: header close / X button
        try:
            close_btns = page.locator(
                '[aria-label="Close"], button[aria-label*="Close" i], button:has-text("×"), button:has-text("✕"), button:has-text("X")'
            )
            if await close_btns.count() > 0:
                self.log("[dismiss_modal_once] clicking header X/Close fallback")
                await close_btns.first.click(timeout=2000)
                await page.wait_for_timeout(400)
                return
        except Exception as e:
            self.log("[dismiss_modal_once] close_btn err:", repr(e))

        # 3. absolute last fallback: "Yes"
        try:
            yes_btn = page.get_by_role("button", name=re.compile(r"^yes$", re.I))
            if await yes_btn.count() > 0:
                self.log("[dismiss_modal_once] clicking 'Yes' fallback")
                await yes_btn.first.click(timeout=2000)
                await page.wait_for_timeout(400)
                return
        except Exception as e:
            self.log("[dismiss_modal_once] yes_btn err:", repr(e))

        self.log("[dismiss_modal_once] modal still present after attempts (continuing anyway)")

    # ---------- helpers: table rows ----------
    async def _has_rows(self) -> bool:
        """
        Do we have at least one billing row in the billing history table?
        """
        rows = self.page.locator("table tbody tr")
        try:
            cnt = await rows.count()
            self.log("[_has_rows] row count:", cnt)
            return cnt > 0
        except Exception as e:
            self.log("[_has_rows] error:", repr(e))
            return False


    async def _debug_print_first_row(self):
        """
        Debug helper for Angular Material billing table.

        We'll:
        - locate the billing table via 'mat-table'
        - locate the rows via 'mat-row.cdk-row'
        - grab the first row
        - print its text + each cell's text
        """
        page = self.page
        self.log("[debug] ENTER _debug_print_first_row (mat-table mode)")

        # 1. locate the mat-table
        table = page.locator("mat-table.cdk-table")
        try:
            await table.wait_for(state="visible", timeout=5000)
            self.log("[debug] mat-table is visible")
        except Exception as e:
            self.log("[debug] mat-table not visible yet:", repr(e))

        # 2. locate all rows inside (mat-row, not tr)
        rows = table.locator("mat-row.cdk-row, mat-row.mat-row")
        # (both classes show up in your screenshot so we match either)

        # poll up to ~3s for rows to show
        for i in range(6):
            try:
                cnt = await rows.count()
                self.log(f"[debug] poll {i}: mat-row count()={cnt}")
                if cnt > 0:
                    break
            except Exception as e:
                self.log("[debug] poll error:", repr(e))
            await page.wait_for_timeout(500)

        try:
            row_cnt = await rows.count()
        except Exception as e:
            self.log("[debug] final mat-row count() threw:", repr(e))
            row_cnt = 0

        self.log(f"[debug] row_cnt after wait (mat-row): {row_cnt}")

        if row_cnt == 0:
            print("NO ROWS FOUND (mat-row)")
            self.log("[debug] no rows found in mat-table")
            return

        # 3. get first row
        first_row = rows.nth(0)
        self.log("[debug] got first_row locator (mat-row)")

        # highlight first row so you can visually confirm in headful mode
        try:
            await first_row.evaluate(
                "(el)=>{el.style.outline='3px solid cyan';el.style.outlineOffset='2px';}"
            )
            self.log("[debug] highlighted first_row with cyan outline")
        except Exception as e:
            self.log("[debug] could not highlight first_row:", repr(e))

        # scroll row into view before reading text
        try:
            await first_row.scroll_into_view_if_needed(timeout=2000)
        except Exception as e:
            self.log("[debug] scroll_into_view_if_needed non-fatal:", repr(e))

        # dump the entire row's text
        try:
            row_text = (await first_row.inner_text()).strip()
        except Exception as e:
            row_text = ""
            self.log("[debug] inner_text() failed:", repr(e))

        print("\n----------- FULL ROW TEXT (mat-row) -----------\n")
        print(row_text)
        print("\n-----------------------------------------------\n")

        # 4. now get cells in that row
        # Angular Material cells are <mat-cell ... class="mat-cell cdk-cell ...">
        cells = first_row.locator("mat-cell.cdk-cell, mat-cell.mat-cell, cml-basic-table-cell")

        try:
            cell_cnt = await cells.count()
        except Exception as e:
            self.log("[debug] cells.count() threw:", repr(e))
            cell_cnt = 0

        print(f"cell_cnt={cell_cnt}")
        for idx in range(cell_cnt):
            try:
                cell = cells.nth(idx)
                # add a colored outline to each cell in turn (nice visual debug)
                await cell.evaluate(
                    "(el)=>{el.style.outline='2px solid magenta';el.style.outlineOffset='1px';}"
                )
                cell_text = (await cell.inner_text()).strip()
            except Exception as e:
                cell_text = f"(unreadable: {e!r})"
            print(f"[cell {idx}] {cell_text}")

        print("\n----------- END ROW DEBUG -----------\n")

        self.log("[debug] EXIT _debug_print_first_row")

    async def _scrape_first_row_and_download_pdf(self) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """
        Production scrape:
          - target Angular Material billing table
          - read amount + click View Bill from first row's "Paperless" cell
          - download the PDF in the new tab
        """

        page = self.page
        self.log("[scrape] === ENTER _scrape_first_row_and_download_pdf (mat-table) ===")

        # Make sure modal isn't blocking things
        await self._dismiss_modal_once()

        # 1. locate the mat-table
        table = page.locator("mat-table.cdk-table")
        try:
            await table.wait_for(state="visible", timeout=5000)
            self.log("[scrape] mat-table is visible")
        except Exception as e:
            self.log("[scrape] FATAL: mat-table not visible:", repr(e))
            return None, None, None

        # 2. locate all data rows
        rows = table.locator("mat-row.cdk-row, mat-row.mat-row")

        # wait up to ~3s for rows
        for i in range(6):
            try:
                cnt = await rows.count()
                self.log(f"[scrape] poll {i}: mat-row count()={cnt}")
                if cnt > 0:
                    break
            except Exception as e:
                self.log("[scrape] poll error:", repr(e))
            await page.wait_for_timeout(500)

        try:
            row_cnt = await rows.count()
        except Exception as e:
            self.log("[scrape] final mat-row count threw:", repr(e))
            row_cnt = 0

        self.log(f"[scrape] row_cnt after wait: {row_cnt}")
        if row_cnt == 0:
            self.log("[scrape] FATAL: still no rows, bailing")
            return None, None, None

        # 3. grab the first row
        first_row = rows.nth(0)
        self.log("[scrape] got first_row locator")

        # scroll and (try to) highlight row
        try:
            await first_row.scroll_into_view_if_needed(timeout=2000)
        except Exception as e:
            self.log("[scrape] scroll_into_view_if_needed non-fatal:", repr(e))
        try:
            await first_row.evaluate(
                "(el)=>{el.style.outline='3px solid cyan';el.style.outlineOffset='2px';}"
            )
        except Exception as e:
            self.log("[scrape] could not highlight first_row:", repr(e))

        # 4. get all cells in this row
        cells = first_row.locator("mat-cell.cdk-cell, mat-cell.mat-cell, cml-basic-table-cell")
        try:
            cell_cnt = await cells.count()
        except Exception as e:
            self.log("[scrape] cells.count() threw:", repr(e))
            cell_cnt = 0

        self.log(f"[scrape] first_row cell_cnt={cell_cnt}")

        if cell_cnt == 0:
            self.log("[scrape] FATAL: row has no cells")
            return None, None, None

        # we discovered from debug:
        #   cell 5 is the one with "$202.41\nView Bill"
        paperless_cell_index = 5
        if paperless_cell_index >= cell_cnt:
            # fallback: try 4 if 5 doesn't exist for some reason
            paperless_cell_index = min(5, cell_cnt - 1)

        paperless_cell = cells.nth(paperless_cell_index)
        self.log(f"[scrape] using cell index {paperless_cell_index} as paperless_cell")

        # visually highlight that cell so we can confirm in PWDEBUG headful mode
        try:
            await paperless_cell.evaluate(
                "(el)=>{el.style.backgroundColor='rgba(255,0,0,0.15)';el.style.outline='2px solid magenta';}"
            )
        except Exception as e:
            self.log("[scrape] could not highlight paperless_cell:", repr(e))

        # 5. extract amount text from that cell
        try:
            cell_text = (await paperless_cell.inner_text()).strip()
        except Exception as e:
            self.log("[scrape] paperless_cell.inner_text() threw:", repr(e))
            cell_text = ""

        self.log(f"[scrape] paperless_cell text: {cell_text!r}")

        # parse amount from that cell ("$202.41\nView Bill")
        amt_match = CURRENCY_RE.search(cell_text)
        if amt_match:
            amount_raw = amt_match.group(0)  # "$202.41"
            amount_clean = amount_raw.replace("$", "").replace(",", "").strip()  # "202.41"
            try:
                amount_cents = int(round(float(amount_clean) * 100))
            except Exception:
                amount_cents = None
        else:
            self.log("[scrape] WARNING: couldn't find $amount in paperless_cell text")
            amount_clean = None
            amount_cents = None

        self.log(f"[scrape] parsed amount_clean={amount_clean} amount_cents={amount_cents}")

        # 6. find the 'View Bill' link inside that same cell
        view_link = paperless_cell.locator('[data-cy="viewBillLink"], a.view-bill-pdf')

        try:
            link_count = await view_link.count()
        except Exception as e:
            self.log("[scrape] view_link.count() threw:", repr(e))
            link_count = 0

        self.log(f"[scrape] view_link count={link_count}")

        if link_count == 0:
            self.log("[scrape] FATAL: no View Bill link in paperless_cell")
            return amount_clean, amount_cents, None

        # highlight and move mouse to it so we can SEE it
        try:
            await view_link.first.evaluate(
                "(el)=>{el.style.outline='3px solid lime'; el.style.backgroundColor='rgba(0,255,0,0.15)';}"
            )
        except Exception as e:
            self.log("[scrape] could not highlight view_link:", repr(e))

        try:
            box = await view_link.first.bounding_box()
            if box:
                await page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                self.log(f"[scrape] moved mouse to view_link center at {box}")
        except Exception as e:
            self.log("[scrape] could not move mouse to view_link:", repr(e))

        # 7. click "View Bill" and capture the new tab
        self.log("[scrape] clicking 'View Bill' now, expecting new tab")
        pdf_page = None
        try:
            async with self.context.expect_page(timeout=8000) as new_page_info:
                await view_link.first.click(timeout=4000, force=True)
            pdf_page = await new_page_info.value
            self.log("[scrape] got pdf_page via expect_page ✅")
        except Exception as e:
            self.log("[scrape] expect_page failed:", repr(e))
            # fallback: maybe it reused same page or tab is already there
            try:
                pages = self.context.pages
                self.log(f"[scrape] context has {len(pages)} pages total")
                if len(pages) > 1:
                    pdf_page = pages[-1]
                    self.log("[scrape] fallback using last page in context as pdf_page")
                else:
                    pdf_page = page
                    self.log("[scrape] fallback using same current page as pdf_page")
            except Exception as ee:
                self.log("[scrape] could not fallback to any page:", repr(ee))
                pdf_page = None

        if not pdf_page:
            self.log("[scrape] FATAL: no pdf_page after clicking View Bill")
            return amount_clean, amount_cents, None

        # 8. wait for the bill view tab
        try:
            await pdf_page.wait_for_load_state("networkidle", timeout=20000)
            self.log("[scrape] pdf_page reached networkidle")
        except Exception as e:
            self.log("[scrape] wait_for_load_state(networkidle) non-fatal:", repr(e))

        # 9. attempt to click a "Download" button on that new page
        pdf_path = None
        download_clicked = False

        possible_dl_buttons = [
            pdf_page.get_by_role("button", name=re.compile(r"download", re.I)),
            pdf_page.locator('button:has-text("Download")'),
            pdf_page.locator('a:has-text("Download")'),
            pdf_page.locator('[aria-label*="Download" i]'),
        ]

        for idx, cand in enumerate(possible_dl_buttons):
            try:
                cand_count = await cand.count()
                self.log(f"[scrape] dl_button[{idx}] count={cand_count}")
                if cand_count > 0:
                    # highlight the download button, visible in headful mode
                    try:
                        await cand.first.evaluate(
                            "(el)=>{el.style.outline='3px solid orange'; el.style.backgroundColor='rgba(255,165,0,0.2)';}"
                        )
                    except Exception as ee:
                        self.log(f"[scrape] could not highlight dl_button[{idx}]:", repr(ee))

                    self.log("[scrape] attempting to click viewer Download button")
                    async with self.context.expect_page(timeout=5000) as dl_page_info:
                        await cand.first.click(timeout=3000)
                    dl_page = await dl_page_info.value
                    self.log("[scrape] got dl_page after Download click ✅")

                    try:
                        await dl_page.wait_for_load_state("networkidle", timeout=10000)
                        self.log("[scrape] dl_page reached networkidle")
                    except Exception as ee:
                        self.log("[scrape] dl_page wait_for_load_state err non-fatal:", repr(ee))

                    dl_url = dl_page.url
                    self.log("[scrape] dl_page url:", dl_url)

                    pdf_path = await self._download_pdf_url(dl_page, dl_url)
                    self.log("[scrape] _download_pdf_url returned:", pdf_path)

                    download_clicked = True
                    break
            except Exception as e:
                self.log(f"[scrape] viewer Download button attempt err[{idx}]:", repr(e))

        # 10. fallback: just grab whatever URL we ended up on in pdf_page
        if not download_clicked:
            pdf_url = pdf_page.url
            self.log("[scrape] fallback: using pdf_page.url as PDF source:", pdf_url)
            pdf_path = await self._download_pdf_url(pdf_page, pdf_url)
            self.log("[scrape] fallback download path:", pdf_path)

        self.log("[scrape] === EXIT _scrape_first_row_and_download_pdf ===")

        return amount_clean, amount_cents, pdf_path

    async def _download_pdf_url(self, pdf_page: Page, pdf_url: str) -> Optional[str]:
        """
        GET the PDF bytes using the same authenticated context and save to disk.
        """
        if not pdf_url.lower().endswith(".pdf"):
            parsed = urlsplit(pdf_url)
            if not parsed.path.lower().endswith(".pdf"):
                self.log("[pdf] URL may not be direct .pdf:", pdf_url)

        try:
            resp = await pdf_page.request.get(pdf_url)
        except Exception as e:
            self.log("[pdf] request.get failed:", repr(e))
            return None

        if not resp.ok:
            self.log("[pdf] non-OK HTTP status:", resp.status)
            return None

        try:
            data = await resp.body()
        except Exception as e:
            self.log("[pdf] body() failed:", repr(e))
            return None

        # choose filename from URL last segment
        try:
            path_part = urlparse(pdf_url).path
        except Exception:
            path_part = ""
        filename = os.path.basename(path_part) or "snew_bill.pdf"
        filename = unquote(filename)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        # sanitize and avoid collisions
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        target = (self.download_dir / filename).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1

        with open(target, "wb") as f:
            f.write(data)
        self.log("[pdf] saved:", str(target))
        return str(target)

    # ---------- locator utils ----------
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



# ---------- PDF text + date extraction ----------

def _extract_text_pdfminer(pdf_path: str) -> str:
    """
    Try pdfminer first; it works best on the SNEW layout.
    """
    try:
        from pdfminer.high_level import extract_text
        return extract_text(pdf_path)
    except Exception:
        return ""


def _extract_text_pypdf(pdf_path: str) -> str:
    """
    Simple fallback using pypdf if pdfminer fails.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _normalize_mmddyyyy(date_str: str) -> Optional[str]:
    """
    Convert 'MM/DD/YYYY' or 'MM/DD/YY' -> 'YYYY-MM-DD'.
    """
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_snew_dates_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Parse statement_date, due_date, period_start, period_end from a SNEW bill PDF.

    - statement_date: near the "Statement Date" label; we take the *last* date
      in that block (because Payment Due and Payment Received come first).
    - due_date: prefer "Payment Due", then "Total Amount Due by", then "Total Due".
    - period_start: first 'From' date in the Service Dates table.
    - period_end: last 'To' date in the Service Dates table.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None, None, None

    # --- statement date ---
    statement_date = None
    lowered = text.lower()
    pos = lowered.find("statement date")
    if pos != -1:
        # Look in a window after the "Statement Date" label and grab all dates;
        # the *last* one in that window is the statement date (e.g. 10/31/2025)
        window = text[pos : pos + 300]
        date_candidates = re.findall(r"\d{1,2}/\d{1,2}/\d{2,4}", window)
        if date_candidates:
            statement_date = _normalize_mmddyyyy(date_candidates[-1])

    # --- due date ---
    due_date = None
    for pat in [
        r"Payment\s+Due\s+(\d{1,2}/\d{1,2}/\d{2,4})",
        r"Total\s+Amount\s+Due\s+by\s+(\d{1,2}/\d{1,2}/\d{2,4})",
        r"Total\s+Due\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            due_date = _normalize_mmddyyyy(m.group(1))
            break

    # --- service date range (period_start / period_end) ---
    period_start = period_end = None
    m = re.search(
        r"Service\s+Dates(.*?)(?:Water\s+Usage\s+History|Current\s+Charges|$)",
        text,
        re.I | re.S,
    )
    if m:
        seg = m.group(1)
        # Each meter row has 'FROM TO' like '09/21/2025 10/24/2025'
        pairs = re.findall(r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})", seg)
        if pairs:
            # Top line "From" => overall period_start
            period_start = _normalize_mmddyyyy(pairs[0][0])
            # Bottom line "To" => overall period_end
            period_end = _normalize_mmddyyyy(pairs[-1][1])

    return statement_date, due_date, period_start, period_end


# ---------- CLI ----------
def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="SNEW SmartHub bill scraper (modal=logged-in)")
    p.add_argument("--username", required=False)
    p.add_argument("--password", required=False)
    p.add_argument("--headful", action="store_true")
    p.add_argument("--slow-mo", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with SNEWScraper(cfg) as s:
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
