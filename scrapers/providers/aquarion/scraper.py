#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Aquarion Water (i-doxs) scraper

Args:
  --username
  --password
  --account-number
  --headful
  --debug

Flow:
- Login
- Optional email 2FA (terminal input)
- Bills → Account = All
- Find bill by account number
- Load more if needed
- Open bill
- Download PDF
- Parse amount + metadata
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
import os

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ----------------------------
# Constants
# ----------------------------

STEP_TIMEOUT = 60  # seconds


# HANDOFF_ROOT = Path(os.getenv("RA_HANDOFF_DIR", "/handoff"))
# DEFAULT_DL_DIR = (HANDOFF_ROOT / "aquarion").resolve()
# AQUARION_HANDOFF_DIR = DEFAULT_DL_DIR / "aquarion"

# HANDOFF_ROOT = Path(os.getenv("RA_HANDOFF_DIR", "/handoff"))
AQUARION_HANDOFF_DIR = (
    Path(os.getenv("AQUARION_HANDOFF_DIR")).resolve()
    if os.getenv("AQUARION_HANDOFF_DIR")
    else Path("/handoff/aquarion").resolve()
)

AQUARION_PROXY_SERVER = "http://161.77.10.71:12323"
AQUARION_PROXY_USER = "14ae15df3f919"
AQUARION_PROXY_PASS = "6ef4856e7c"

# ----------------------------
# Optional PDF parsing
# ----------------------------
try:
    from pdfminer.high_level import extract_text
except Exception:
    extract_text = None


# ----------------------------
# Data Models
# ----------------------------

@dataclass
class AquarionResult:
    ok: bool
    error: Optional[str] = None
    amount_cents: Optional[int] = None
    due_date: Optional[str] = None
    statement_date: Optional[str] = None
    account_number: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    pdf_path: Optional[str] = None
    fetched_at: str = datetime.utcnow().isoformat() + "Z"


@dataclass
class ScraperConfig:
    username: str
    password: str
    account_number: str
    headless: bool
    debug: bool
    proxy: Optional[str] = None
    download_dir: Path = AQUARION_HANDOFF_DIR
    login_url: str = "https://secure8.i-doxs.net/AquarionWater/SignIn.aspx"


# ----------------------------
# Utilities
# ----------------------------

def money_to_cents(val: str) -> int:
    return int(round(float(val.replace(",", "")) * 100))

def normalize_date_to_iso(val: Optional[str]) -> Optional[str]:
    """
    Convert MM/DD/YY or MM/DD/YYYY → YYYY-MM-DD
    """
    if not val:
        return None

    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(val, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    # If it doesn't match expected formats, return original (fail-safe)
    return val


def extract_pdf_fields(pdf_path: Path):
    if extract_text is None:
        return None, None, None

    text = extract_text(str(pdf_path)) or ""

    amount = None
    due_date = None
    stmt_date = None

    m = re.search(r"Total Amount Due.*?\$([0-9,]+\.\d{2})", text, re.I)
    if m:
        amount = money_to_cents(m.group(1))

    m = re.search(r"Due Date[:\s]+([0-9/]+)", text, re.I)
    if m:
        due_date = m.group(1)

    m = re.search(r"Statement Date[:\s]+([0-9/]+)", text, re.I)
    if m:
        stmt_date = m.group(1)

    return amount



def extract_pdf_dates_and_period(pdf_path: Path):
    """
    Extract:
      - statement_date
      - due_date
      - service period start
      - service period end
    """
    if extract_text is None:
        return None, None, None, None

    text = extract_text(str(pdf_path)) or ""
    text = re.sub(r"\s+", " ", text)  # normalize spacing

    statement_date = None
    due_date = None
    period_start = None
    period_end = None

    # ----------------------------
    # Statement Date
    # Example: "Statement Date: 12/12/25"
    # ----------------------------
    m = re.search(
        r"Statement Date:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        text,
        re.I,
    )
    if m:
        statement_date = m.group(1)

    # ----------------------------
    # Due Date
    # Example: "Total Amount Due by 01/07/2026"
    # ----------------------------
    m = re.search(
        r"Total Amount Due by\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        text,
        re.I,
    )
    if m:
        due_date = m.group(1)

    # ----------------------------
    # Service Period
    # Example:
    # "Service from 11/13/25 to 12/11/25 (29 days)"
    # ----------------------------
    m = re.search(
        r"Service from\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})\s*to\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        text,
        re.I,
    )
    if m:
        period_start = m.group(1)
        period_end = m.group(2)

    return statement_date, due_date, period_start, period_end


# ----------------------------
# Scraper
# ----------------------------

class AquarionScraper:
    def __init__(self, cfg: ScraperConfig):
        self.cfg = cfg


    async def run(self) -> AquarionResult:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.cfg.headless,
                slow_mo=100 if self.cfg.debug else 0,
                proxy={
                    "server": AQUARION_PROXY_SERVER,
                    "username": AQUARION_PROXY_USER,
                    "password": AQUARION_PROXY_PASS,
                },
                args=[
                    "--disable-features=PdfViewer",
                    "--disable-pdf-extension",
                ],
            )
            context = await browser.new_context(
                accept_downloads=True,
                java_script_enabled=True,
            )

            page = await context.new_page()

            if self.cfg.debug:
                print(f"[DEBUG] Aquarion running via ISP proxy: {AQUARION_PROXY_SERVER}")


            error = None

            try:
                await page.goto(self.cfg.login_url, wait_until="domcontentloaded")

                await self._login(page)
                await self._handle_2fa(page)
                await self._go_to_bills(page)
                await self._set_account_all(page)

                row = await self._find_bill_row(page)
                if not row:
                    raise RuntimeError("Account not found")

                await row.click()
                pdf_path = await self._download_pdf(page)
                await browser.close()

                # ---- PDF parsing ----
                amount = extract_pdf_fields(pdf_path)
                stmt_date, due_date, period_start, period_end = (
                    extract_pdf_dates_and_period(pdf_path)
                )
                # Normalize all dates to YYYY-MM-DD for backend
                stmt_date = normalize_date_to_iso(stmt_date)
                due_date = normalize_date_to_iso(due_date)
                period_start = normalize_date_to_iso(period_start)
                period_end = normalize_date_to_iso(period_end)

                return AquarionResult(
                    ok=True,
                    amount_cents=amount,
                    due_date=due_date,
                    statement_date=stmt_date,
                    period_start=period_start,
                    period_end=period_end,
                    account_number=self.cfg.account_number,
                    pdf_path=str(pdf_path),
                )
            except Exception as e:
                error = str(e)
                return AquarionResult(ok=False, error=error)

            finally:
                if self.cfg.debug and error:
                    print("[DEBUG] Error occurred — browser left open")
                    print("[DEBUG] Press ENTER to close browser")
                    input()
                    await browser.close()

    # ------------------------

    async def _login(self, page):
        # Fill username
        await page.wait_for_selector('input[type="text"]', timeout=10_000)
        await page.fill('input[type="text"]', self.cfg.username)

        # Fill password
        await page.wait_for_selector('input[type="password"]', timeout=10_000)
        await page.fill('input[type="password"]', self.cfg.password)

        # STRONG submit click (ASP.NET WebForms)
        submit = page.locator("#main_btnSubmit")

        await submit.wait_for(state="visible", timeout=10_000)

        # Scroll into view (important for WebForms + overlays)
        await submit.scroll_into_view_if_needed()

        # Force click to bypass JS/overlay weirdness
        await submit.click(force=True)

        # Optional: small wait to allow postback
        await page.wait_for_load_state("domcontentloaded")

    async def _handle_2fa(self, page):
        try:
            # Wait for 2FA page by URL (VerifyChange.aspx) so we don't skip 2FA on slow load
            await page.wait_for_url(
                re.compile(r"VerifyChange\.aspx", re.I),
                timeout=20_000,
            )
        except PWTimeoutError:
            # Not on 2FA page (e.g. already on Home/Default)
            return

        handoff_base = AQUARION_HANDOFF_DIR
        code_file = handoff_base / "2fa_code.txt"

        # IMPORTANT: do NOT preemptively unlink here. The worker.go already
        # removes any stale file before launching us, and the Go 2FA poller
        # may have already written this run's code in the window between
        # login submit and our arrival on VerifyChange.aspx. Unlinking would
        # wipe the legitimate code and we'd poll forever, since the Go
        # poller exits after a single successful write.

        code = None

        # wait up to ~3 minutes for Go to write the code
        for _ in range(180):
            if code_file.exists():
                code = code_file.read_text().strip()
                if code:
                    break
            await asyncio.sleep(1)

        if not code:
            try:
                code_file.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError("2FA required but no 2fa_code.txt was found")

        if self.cfg.debug:
            print(f"[DEBUG][2FA] Using code from {code_file}: {code}")

        try:
            # submit code
            code_input = page.locator("#main_txtAnswer")
            await code_input.wait_for(state="visible", timeout=10_000)
            await code_input.fill(code)

            # Use main Continue button by id (there are two Continue links on the page)
            await page.locator("#main_btnSubmit").click(force=True)

            await page.wait_for_load_state("domcontentloaded")
        finally:
            # Always remove file on exit (success or failure) so next run never sees stale code
            try:
                code_file.unlink(missing_ok=True)
            except Exception:
                pass


    async def _go_to_bills(self, page):
        try:
            # Wait until we are truly on the post-login landing page
            await page.wait_for_url(
                re.compile(r"/Secure/(Home|Default)\.aspx", re.I),
                timeout=30_000,
            )

            # Define both possible Bills links
            header_bills = page.locator("#lnkBills")
            content_bills = page.locator("#main_lnkBill")

            # Wait for either to be visible
            await page.wait_for_selector(
                "#lnkBills, #main_lnkBill",
                timeout=15_000,
            )

            # Pick the visible one (NO count())
            bills_link = header_bills if await header_bills.is_visible() else content_bills

            # Click and wait for navigation together
            async with page.expect_navigation():
                await bills_link.scroll_into_view_if_needed()
                await bills_link.click(force=True)

            # Ensure Bills page loaded
            await page.wait_for_url(
                re.compile(r"/Secure/Bills\.aspx", re.I),
                timeout=30_000,
            )

        except Exception as e:
            print(f"[ERROR] Failed navigating to Bills page: {e}")
            raise

    async def _set_account_all(self, page):
        try:
            # Target the correct Account dropdown explicitly
            account_dropdown = page.locator("#main_ddlAccount")

            await account_dropdown.wait_for(state="visible", timeout=10_000)

            # Selecting "All" triggers a postback → expect navigation
            async with page.expect_navigation():
                await account_dropdown.select_option(value="-1")

            # Ensure the page settles after filter change
            await page.wait_for_load_state("domcontentloaded")

        except Exception as e:
            print(f"[ERROR] Failed setting Account filter to All: {e}")
            raise


    async def _find_bill_row(self, page):
        while True:
            # Each bill row is a div, not a table row
            rows = page.locator('div[id^="main_rptBills_divRow_"]')
            count = await rows.count()

            for i in range(count):
                row = rows.nth(i)

                # Account number lives here
                acct_span = row.locator("span.jqAccountNo")
                if await acct_span.count() == 0:
                    continue

                acct_text = (await acct_span.inner_text()).strip()

                if acct_text == self.cfg.account_number:
                    # Clickable element is the <a> inside the row
                    link = row.locator("a[id^='main_rptBills_lnkBills_']")
                    await link.scroll_into_view_if_needed()
                    return link

            # Not found on current page → try Load more
            load_more = page.get_by_text("Load more", exact=True)
            if await load_more.count() == 0:
                return None

            await load_more.scroll_into_view_if_needed()
            await load_more.click()
            await page.wait_for_timeout(1500)

    async def _download_pdf(self, page) -> Path:
        download_dir = self.cfg.download_dir
        download_dir.mkdir(parents=True, exist_ok=True)

        # 1️⃣ Locate the PDF button
        pdf_btn = page.locator("img#PDF")
        await pdf_btn.wait_for(state="visible", timeout=10_000)

        # 2️⃣ Extract onclick JS
        onclick = await pdf_btn.get_attribute("onclick")
        if not onclick:
            raise RuntimeError("PDF button has no onclick attribute")

        # 3️⃣ Pull out the __ImageGrabber.axd URL
        m = re.search(r"document\.location\.href='([^']+)'", onclick)
        if not m:
            raise RuntimeError("Could not extract ImageGrabber URL")

        relative_url = m.group(1)

        # 4️⃣ Build absolute URL
        # page.url example:
        # https://secure8.i-doxs.net/AquarionWater/Secure/ViewBill.aspx
        base = page.url.split("/AquarionWater/")[0]
        pdf_url = f"{base}/AquarionWater/Secure/{relative_url}"

        if self.cfg.debug:
            print(f"[DEBUG] PDF URL: {pdf_url}")

        # 5️⃣ Fetch PDF bytes using authenticated session
        response = await page.request.get(pdf_url)

        if not response.ok:
            raise RuntimeError(f"PDF fetch failed: {response.status}")

        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower():
            raise RuntimeError(f"Unexpected content-type: {content_type}")

        pdf_bytes = await response.body()

        # 6️⃣ Save file
        path = download_dir / f"{self.cfg.account_number}.pdf"
        path.write_bytes(pdf_bytes)

        return path

# ----------------------------
# CLI
# ----------------------------

def parse_args():
    import argparse

    p = argparse.ArgumentParser("Aquarion Water Scraper")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--account-number", required=True)
    p.add_argument("--headful", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--json", action="store_true")
    return p.parse_args()


async def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = ScraperConfig(
        username=args.username,
        password=args.password,
        account_number=args.account_number,
        headless=not args.headful,
        debug=args.debug,
    )

    scraper = AquarionScraper(cfg)
    result = await scraper.run()

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
