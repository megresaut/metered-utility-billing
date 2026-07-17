#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regional Water Authority (rwater.com) bill scraper

Flow:
- Go to https://myaccount.rwater.com/
- Login with username/password
- After login, open bills page:
    https://myaccount.rwater.com/bills/AP0117898/211660881
- Find the first bill row that has a PDF icon (id starts with "openbill_")
  * Extract:
      - billId from that element's id ("openbill_<billId>")
      - amount due from the row text
- Click the PDF icon, sniff the actual network request to:
    https://myaccount-api.rwater.com/api/account/getbillattachmentdata?billId=<billId>
  capture the binary response, and save it as <billId>.pdf locally.
- Return amount, amount_cents, pdf_path, final_url

Output:
- Default: prints ONLY the amount (e.g., 169.24)
- With --json: {"amount":"169.24","amount_cents":16924,"pdf_path":"/abs/path/B003663726.pdf","final_url":"..."}
"""

import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
)

LOGIN_URL = "https://myaccount.rwater.com/"
BILLS_URL = "https://myaccount.rwater.com/bills/AP0117898/211660881"
DEFAULT_DL_DIR = Path(
    os.getenv("RWATER_DOWNLOAD_DIR", "/handoff/rwa")
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
    timezone_id: str = os.getenv("RWATER_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # New: dates parsed from the PDF
    statement_date: Optional[str] = None   # Bill Date → "YYYY-MM-DD"
    period_start: Optional[str] = None     # "YYYY-MM-DD"
    period_end: Optional[str] = None       # "YYYY-MM-DD"
    due_date: Optional[str] = None         # "YYYY-MM-DD"


def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("RWATER_USERNAME") or "",
        password=args.password or os.getenv("RWATER_PASSWORD") or "",
        headless=not bool(args.headful) if args.headful is not None else _env_bool("RWATER_HEADLESS", True),
        slow_mo_ms=int(os.getenv("RWATER_SLOW_MO_MS", str(args.slow_mo or 0))),
        debug=bool(args.debug) or _env_bool("RWATER_DEBUG", False),
        timezone_id=os.getenv("RWATER_TIMEZONE_ID", "America/New_York"),
    )


class RWaterScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None

    # --------------- logging helpers ---------------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # --------------- lifecycle ---------------
    async def __aenter__(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self.context = await self.browser.new_context(
            accept_downloads=True,
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id=self.cfg.timezone_id,
        )
        await self._install_stealth()

        self.page = await self.context.new_page()
        if self.cfg.debug:
            self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))
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

    async def _install_stealth(self):
        """
        Light stealth to look less like automation.
        """
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

    # ----------------- main -----------------
    async def run(self) -> Result:
        # sanity
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        page = self.page

        # Step 1: login
        self.log("[step] goto login")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            return Result(ok=False, error="Timeout opening login page")

        self.log("[step] credential login")
        await self._login_once_rwater(self.cfg.username, self.cfg.password)

        self.log("[step] wait for post-login view")
        await self._wait_for_post_login_view()

        # Step 2: go directly to bills page
        self.log("[step] open bills page")
        bills_page = await self._open_bills_tab()
        if not bills_page:
            return Result(ok=False, error="could not open bills page tab")

        # Step 3: scrape 1st bill row -> get amount + billId -> capture PDF
        amount_str, amount_cents, pdf_path = await self._scrape_first_bill_row_and_download_pdf(bills_page)

        if not amount_str:
            return Result(ok=False, error="Could not extract amount / bill row")

        # Parse dates from the downloaded PDF
        self.log("[step] parse dates from PDF")
        stmt_iso, start_iso, end_iso, due_iso = (None, None, None, None)
        if pdf_path:
            stmt_iso, start_iso, end_iso, due_iso = extract_dates_from_pdf(pdf_path)
        self.log(f"[bill] parsed dates: statement={stmt_iso}, period={start_iso} → {end_iso}, due={due_iso}")

        return Result(
            ok=True,
            amount=amount_str,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=bills_page.url,
            statement_date=stmt_iso,
            period_start=start_iso,
            period_end=end_iso,
            due_date=due_iso,
        )
    # ----------------- login helpers -----------------
    async def _login_once_rwater(self, username: str, password: str):
        """
        Fill in username/password, click Sign in, wait for network idle.
        """
        page = self.page

        # Find email field
        email_input = None
        for loc in [
            page.get_by_label(re.compile(r"Email Address", re.I)),
            page.locator('input[type="email"]'),
        ]:
            try:
                await loc.first.wait_for(state="visible", timeout=8000)
                email_input = loc.first
                break
            except Exception:
                continue

        # Find password field
        pass_input = None
        for loc in [
            page.get_by_label(re.compile(r"Password", re.I)),
            page.locator('input[type="password"]'),
        ]:
            try:
                await loc.first.wait_for(state="visible", timeout=8000)
                pass_input = loc.first
                break
            except Exception:
                continue

        if not email_input or not pass_input:
            self.log("[login] inputs not found (maybe already logged in)")
        else:
            await email_input.fill(username)
            await asyncio.sleep(random.uniform(0.15, 0.35))
            await pass_input.fill(password)

            # Click Sign in
            login_btn = None
            for loc in [
                page.get_by_role("button", name=re.compile(r"Sign\\s*in", re.I)),
                page.locator('button:has-text("Sign in")'),
            ]:
                try:
                    if await loc.count() > 0:
                        login_btn = loc.first
                        break
                except Exception:
                    continue

            if login_btn:
                await login_btn.click()
            else:
                # fallback to Enter submit
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        self.log("[login] done")

    async def _wait_for_post_login_view(self):
        """
        Heuristic: wait until we can see something like a "Billing" tab.
        Also tries to dismiss any doxoPLUS upgrade modal that might block the UI.
        """
        page = self.page
        for _ in range(10):
            # Try closing the doxoPLUS popup if it appears
            await self._wait_and_dismiss_doxo_plus(page, attempts=1, delay_ms=0)

            try:
                billing_tab = page.get_by_role("tab", name=re.compile(r"Billing", re.I))
                if await billing_tab.count() > 0:
                    self.log("[post-login] Billing tab present")
                    return
            except Exception:
                pass
            await page.wait_for_timeout(500)

    async def _maybe_close_doxo_plus_modal(self, page: Page):
        """
        If the 'Upgrade to doxoPLUS+' modal is present on the given page,
        click its X/Close button via its specific class. Safe to call repeatedly;
        it just no-ops if the modal isn't there.
        """
        if not page:
            return

        try:
            modal_root = None

            # Prefer the modal card container, which is stable
            card = page.locator("div.x-doxo-modal-2__card")
            if await card.count() > 0:
                modal_root = card.first

            # Fallback: locate by title text, then climb to the card container
            if modal_root is None:
                title = page.get_by_text(re.compile(r"upgrade to doxo\\s*plus\\+?", re.I))
                if await title.count() == 0:
                    return  # no modal at all
                modal_root = title.locator(
                    "xpath=ancestor::div[contains(@class,'x-doxo-modal-2__card')]"
                ).first

            self.log("[doxo] Upgrade to doxoPLUS modal detected, attempting to close")

            # The actual close button (see your DOM: button.x-doxo-modal-2__close-x)
            close_btn = modal_root.locator("button.x-doxo-modal-2__close-x").first

            # Keep your older generic fallbacks as backups in case the class changes
            fallback_candidates = [
                page.get_by_role("button", name=re.compile(r"close", re.I)),
                page.locator("button[aria-label*='close' i]"),
                page.locator("button:has-text('×')"),
                page.locator("button:has-text('✕')"),
            ]

            try:
                await close_btn.scroll_into_view_if_needed()
                await close_btn.click()
                await page.wait_for_timeout(500)
                self.log("[doxo] closed doxoPLUS modal via x-doxo-modal-2__close-x")
                return
            except Exception as e:
                self.log("[doxo] class-based close click failed:", repr(e))

            # Fallbacks if the class-based selector ever breaks
            for loc in fallback_candidates:
                try:
                    if await loc.count() > 0:
                        btn = loc.first
                        await btn.scroll_into_view_if_needed()
                        await btn.click()
                        await page.wait_for_timeout(500)
                        self.log("[doxo] closed doxoPLUS modal via fallback selector")
                        return
                except Exception as e:
                    self.log("[doxo] fallback close attempt failed:", repr(e))

            self.log("[doxo] modal detected but no close button found")
        except Exception as e:
            self.log("[doxo] modal probe error:", repr(e))

    async def _wait_and_dismiss_doxo_plus(self, page: Page, attempts: int = 15, delay_ms: int = 300):
        """
        For a short period, repeatedly check for the doxoPLUS popup and close it
        if it appears. Stops early when it's gone.
        """
        for _ in range(attempts):
            await self._maybe_close_doxo_plus_modal(page)
            try:
                if await page.get_by_text(re.compile(r"upgrade to doxo\\s*plus\\+?", re.I)).count() == 0:
                    return
            except Exception:
                return
            if delay_ms > 0:
                await page.wait_for_timeout(delay_ms)

    async def _open_bills_tab(self) -> Optional[Page]:
        """
        Open bills URL in a *new* page in the same context to avoid losing session.
        """
        try:
            new_page = await self.context.new_page()
            await new_page.goto(BILLS_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
            try:
                await new_page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            self.log("[bills] loaded:", new_page.url)
            return new_page
        except Exception as e:
            self.log("[bills] navigation error:", repr(e))
            return None

    # ----------------- bills scraping -----------------
    async def _scrape_first_bill_row_and_download_pdf(
        self, bills_page: Page
    ) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        self.log("[bill] locating bill rows")

        try:
            await bills_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        all_rows = bills_page.locator("div.row.no-gutters")

        # wait until at least one row is visible
        for _ in range(6):
            if await all_rows.count() > 0:
                break
            await bills_page.wait_for_timeout(500)

        count_rows = await all_rows.count()
        if count_rows == 0:
            self.log("[bill] no rows found")
            return None, None, None

        # pick first row that has a PDF icon/button
        target_row = None
        for idx in range(count_rows):
            candidate = all_rows.nth(idx)
            try:
                if await candidate.locator("a.iti-icon-button[id^='openbill_']").count() > 0:
                    target_row = candidate
                    break
            except Exception:
                continue

        if target_row is None:
            self.log("[bill] no row had openbill_ link")
            return None, None, None

        # extract row text and pull amount like "$169.24"
        try:
            row_text = (await target_row.inner_text()).strip()
        except Exception:
            row_text = ""

        amounts = CURRENCY_RE.findall(row_text)
        amount_clean = None
        amount_cents = None
        if amounts:
            raw_amt = amounts[-1]
            cleaned = raw_amt.replace("$", "").replace(",", "").strip()
            amount_clean = cleaned
            try:
                amount_cents = int(round(float(cleaned) * 100))
            except Exception:
                amount_cents = None

        # extract billId from <a id="openbill_<billId>">
        pdf_button = target_row.locator("a.iti-icon-button[id^='openbill_']").first
        bill_id_attr = None
        try:
            bill_id_attr = await pdf_button.get_attribute("id")
        except Exception:
            pass

        bill_id = None
        if bill_id_attr and bill_id_attr.startswith("openbill_"):
            bill_id = bill_id_attr.split("_", 1)[1].strip()

        if not bill_id:
            self.log("[bill] could not parse billId from PDF icon")
            return amount_clean, amount_cents, None

        self.log("[bill] bill_id:", bill_id)

        # Capture real authenticated network request triggered by click
        pdf_path = await self._capture_bill_pdf_via_click(bills_page, bill_id)
        return amount_clean, amount_cents, pdf_path

    # ----------------- PDF capture via sniffing -----------------
    async def _capture_bill_pdf_via_click(self, bills_page: Page, bill_id: str) -> Optional[str]:
        """
        Clicks the bill's PDF icon to trigger the portal's authenticated API call,
        then captures that network response and saves it as a decoded PDF.
        """
        import base64
        import json

        api_endpoint_substr = f"/api/account/getbillattachmentdata?billId={bill_id}"
        pdf_bytes_holder = {"data": None}

        async def _maybe_capture(response):
            try:
                url = response.url
                if api_endpoint_substr in url and response.status == 200:
                    body = await response.body()
                    # Try JSON decode first (since the API returns {"base64data":"..."}).
                    try:
                        text = body.decode("utf-8")
                        if text.strip().startswith("{"):
                            parsed = json.loads(text)
                            if "base64data" in parsed:
                                pdf_bytes_holder["data"] = base64.b64decode(parsed["base64data"])
                                self.log("[net-sniff] captured base64 PDF, decoded OK")
                                return
                        # else fallback to binary
                    except Exception:
                        pass
                    pdf_bytes_holder["data"] = body
                    self.log("[net-sniff] captured raw PDF bytes from", url)
            except Exception as e:
                self.log("[net-sniff] capture error:", repr(e))

        self.context.on("response", _maybe_capture)

        # click the icon to trigger API
        pdf_selector = f"a.iti-icon-button#openbill_{bill_id}"
        self.log("[net-sniff] clicking", pdf_selector, "to trigger real API call")
        try:
            btn = bills_page.locator(pdf_selector)
            await btn.first.scroll_into_view_if_needed()
            await btn.first.click(timeout=5000, force=True)
        except Exception as e:
            self.log("[net-sniff] click failed:", repr(e))

        # wait for response capture
        for _ in range(20):
            if pdf_bytes_holder["data"] is not None:
                break
            await bills_page.wait_for_timeout(250)

        try:
            self.context.off("response", _maybe_capture)
        except Exception:
            pass

        if pdf_bytes_holder["data"] is None:
            self.log("[net-sniff] did not capture PDF bytes")
            return None

        # write decoded bytes to file
        filename = f"{bill_id}.pdf"
        target_path = (self.download_dir / filename).resolve()
        i = 1
        stem, suf = target_path.stem, target_path.suffix
        while target_path.exists():
            target_path = target_path.with_name(f"{stem}-{i}{suf}")
            i += 1

        try:
            with open(target_path, "wb") as f:
                f.write(pdf_bytes_holder["data"])
        except Exception as e:
            self.log("[net-sniff] failed writing PDF file:", repr(e))
            return None

        self.log("[net-sniff] saved decoded PDF:", str(target_path))
        return str(target_path)
# ----------------- PDF text + date parsing -----------------

def _extract_text_pdfminer(pdf_path: str) -> Optional[str]:
    """
    Try pdfminer first, but return None if the result is effectively empty /
    useless so that we fall back to PyPDF2/pypdf.
    """
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(pdf_path)
    except Exception:
        return None

    # RWA bills often give just form-feed chars; strip those
    cleaned = text.replace("\x0c", "").strip()

    # If there's no real content (or no alphanumeric chars), treat as failure
    if not cleaned or not re.search(r"[A-Za-z0-9]", cleaned):
        return None

    return cleaned


def _extract_text_pypdf(pdf_path: str) -> Optional[str]:
    """
    Fallback if pdfminer doesn't give usable text.
    Uses pypdf if available, otherwise PyPDF2.
    Returns None if neither library is installed.
    """
    reader = None

    # First try pypdf
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(pdf_path)
    except Exception:
        # Then try PyPDF2
        try:
            from PyPDF2 import PdfReader as R2  # type: ignore
            reader = R2(pdf_path)
        except Exception:
            # Neither library is available
            return None

    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    # PDFs often have NULs sprinkled in; strip those so regex works
    return text.replace("\x00", "")

def _parse_us_date_full(s: str) -> Optional[datetime]:
    """Parse 10/09/2025 or 10/09/25 → datetime."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _build_period_from_mmdd_range(start_mmdd: str, end_mmdd: str, base_year: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Given something like '09/04-10/05' and a base_year from the bill:

        - Normally use base_year for both dates.
        - If end_month < start_month, assume New Year crossover:
            start_year = base_year - 1
            end_year   = base_year
    """
    try:
        sm, sd = [int(x) for x in start_mmdd.split("/")]
        em, ed = [int(x) for x in end_mmdd.split("/")]
    except Exception:
        return None, None

    if em < sm:
        start_year = base_year - 1
        end_year = base_year
    else:
        start_year = base_year
        end_year = base_year

    try:
        start_dt = datetime(start_year, sm, sd)
        end_dt = datetime(end_year, em, ed)
    except ValueError:
        return None, None

    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def _parse_us_date_to_iso(s: str) -> Optional[str]:
    """
    Parse dates like 10/09/2025 or 10/09/25 into 'YYYY-MM-DD'.
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
    Extract statement date (Bill Date), usage period, and due date from the RWA PDF.

        Bill Date line:       "Bill Date: 10/09/2025"
        Due date line:        "Total Amount Due by 11/06/2025"
        Usage period lines:   "Water Consumption 09/04-10/05 28 CCF x $5.229 $146.41"
                              "Service Charge 09/04-10/05  $22.83"

    Returns:
        (statement_date_iso, period_start_iso, period_end_iso, due_date_iso)
        where each is 'YYYY-MM-DD' or None.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None, None, None

    # Normalize weird whitespace:
    # - remove NULs
    # - convert non-breaking spaces (U+00A0) to regular spaces
    text = text.replace("\x00", "").replace("\u00A0", " ")

    # Normalize spaces/newlines for easier regex
    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    stmt_iso: Optional[str] = None
    start_iso: Optional[str] = None
    end_iso: Optional[str] = None
    due_iso: Optional[str] = None
    bill_year: Optional[int] = None

    # --- Bill Date: "Bill Date: 10/09/2025" ---
    m = re.search(
        r"Bill\s+Date:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        squashed,
        re.I,
    )
    if m:
        stmt_iso = _parse_us_date_to_iso(m.group(1))
        if stmt_iso:
            bill_year = int(stmt_iso[:4])

    # --- Due Date: "Total Amount Due by 11/06/2025" ---
    m = re.search(
        r"Total\s+Amount\s+Due\s+by\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})",
        squashed,
        re.I,
    )
    if m:
        due_iso = _parse_us_date_to_iso(m.group(1))
        if not bill_year and due_iso:
            bill_year = int(due_iso[:4])

    # --- Period from the detail lines ---
    # First try the Water Consumption line:
    #   "Water Consumption 09/04-10/05 28 CCF x $5.229 $146.41"
    period_match = re.search(
        r"Water\W*Consumption\W*([0-9]{2}/[0-9]{2})-([0-9]{2}/[0-9]{2})",
        squashed,
        re.I,
    )

    # If that somehow doesn't match, try a slightly more generic pattern
    # just anchored on "Consumption":
    if not period_match:
        period_match = re.search(
            r"Consumption[^0-9]{0,20}([0-9]{2}/[0-9]{2})-([0-9]{2}/[0-9]{2})",
            squashed,
            re.I,
        )

    # Final fallback: the Service Charge line
    #   "Service Charge 09/04-10/05  $22.83"
    if not period_match:
        period_match = re.search(
            r"Service\s+Charge\W*([0-9]{2}/[0-9]{2})-([0-9]{2}/[0-9]{2})",
            squashed,
            re.I,
        )

    if period_match and bill_year:
        start_md, end_md = period_match.group(1), period_match.group(2)
        sm, sd = map(int, start_md.split("/"))
        em, ed = map(int, end_md.split("/"))

        y1 = bill_year
        y2 = bill_year

        # December → January edge case: 12/15-01/15 means end-year = bill_year + 1
        if sm == 12 and em == 1:
            y2 = bill_year + 1

        start_iso = f"{y1:04d}-{sm:02d}-{sd:02d}"
        end_iso  = f"{y2:04d}-{em:02d}-{ed:02d}"

    return stmt_iso, start_iso, end_iso, due_iso

# --------------------- CLI ---------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="RWater bill scraper (amount + PDF)")
    p.add_argument("--username", help="RWater portal username / email", required=False)
    p.add_argument("--password", help="RWater portal password", required=False)
    p.add_argument("--headful", action="store_true", help="Run with visible browser (headless = false)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion ms between actions")
    p.add_argument("--json", action="store_true", help="Print JSON instead of just amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with RWaterScraper(cfg) as s:
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
            "period_start": result.period_start,
            "period_end": result.period_end,
            "due_date": result.due_date,
        }, indent=2))
    else:
        print(result.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
