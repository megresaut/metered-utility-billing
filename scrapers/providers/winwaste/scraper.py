#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Win Waste Innovations scraper

Args:
  --username
  --password
  --account-number
  --headful
  --debug

Flow:
- Login
- Email 2FA (Send email → enter code)
- Dashboard → Payments
- Select account by matching account number suffix
- Invoice History
- Click invoice → auto-download PDF
- Parse amount
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from calendar import monthrange
import os
import uuid

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ----------------------------
# Constants
# ----------------------------

STEP_TIMEOUT = 60  # seconds\\

HANDOFF_ROOT = Path(os.getenv("RA_HANDOFF_DIR", "/handoff"))
DEFAULT_DL_DIR = (HANDOFF_ROOT / "winwaste").resolve()
# DEFAULT_DL_DIR = Path("./downloads/winwaste").resolve()
WINWASTE_HANDOFF_DIR = DEFAULT_DL_DIR / "winwaste"

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
class WinWasteResult:
    ok: bool
    error: Optional[str] = None
    amount_cents: Optional[int] = None
    account_number: Optional[str] = None
    statement_date: Optional[str] = None
    due_date: Optional[str] = None
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
    download_dir: Path = WINWASTE_HANDOFF_DIR
    login_url: str = "https://www.win-waste.com/my-account/"

# ----------------------------
# Utilities
# ----------------------------


def add_one_month(dt: datetime) -> datetime:
    year = dt.year + (dt.month // 12)
    month = dt.month % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day) 

def money_to_cents(val: str) -> int:
    return int(round(float(val.replace(",", "")) * 100))

def extract_amount_from_pdf(pdf_path: Path) -> Optional[int]:
    if extract_text is None:
        return None

    text = extract_text(str(pdf_path)) or ""
    text = re.sub(r"\s+", " ", text)

    # Example: "$179.54"
    m = re.search(r"\$([0-9,]+\.\d{2})", text)
    if m:
        return money_to_cents(m.group(1))

    return None



def extract_statement_date(pdf_path: Path) -> Optional[datetime]:
    text = extract_pdf_text(pdf_path)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1️⃣ SAME-LINE MATCH (DATE Dec-01-25)
    for line in lines:
        m = re.search(
            r"\bDATE\b\s+([A-Za-z]{3}-\d{2}-(?:\d{2}|\d{4}))",
            line,
        )
        if m:
            fmt = "%b-%d-%y" if len(m.group(1).split("-")[-1]) == 2 else "%b-%d-%Y"
            return datetime.strptime(m.group(1), fmt)

    # 2️⃣ MULTI-LINE FALLBACK
    for i, line in enumerate(lines):
        if line.upper() == "DATE":
            for j in range(1, 4):
                if i + j < len(lines):
                    candidate = lines[i + j]
                    m = re.search(
                        r"([A-Za-z]{3}-\d{2}-(?:\d{2}|\d{4}))",
                        candidate,
                    )
                    if m:
                        fmt = "%b-%d-%y" if len(m.group(1).split("-")[-1]) == 2 else "%b-%d-%Y"
                        return datetime.strptime(m.group(1), fmt)

    return None


def extract_period_from_pdf(pdf_path: Path) -> tuple[Optional[datetime], Optional[datetime]]:
    text = extract_pdf_text(pdf_path)

    m = re.search(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–]\s*(\d{1,2}/\d{1,2}/\d{4})",
        text,
    )

    if not m:
        return None, None

    return (
        datetime.strptime(m.group(1), "%m/%d/%Y"),
        datetime.strptime(m.group(2), "%m/%d/%Y"),
    )



def extract_invoice_total_from_pdf(pdf_path: Path) -> Optional[int]:
    text = extract_pdf_text(pdf_path)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    for i, line in enumerate(lines):
        if "INVOICE TOTAL" in line.upper():
            # Look forward for the amount
            for j in range(1, 4):
                if i + j < len(lines):
                    m = re.search(r"\$([0-9,]+\.\d{2})", lines[i + j])
                    if m:
                        return money_to_cents(m.group(1))

    # Fallback: last dollar amount in document
    matches = re.findall(r"\$([0-9,]+\.\d{2})", text)
    if matches:
        return money_to_cents(matches[-1])

    return None



def extract_pdf_text(pdf_path: Path) -> str:
    if extract_text is None:
        return ""

    text = extract_text(str(pdf_path)) or ""

    # Aggressive normalization for layout-heavy PDFs
    text = text.replace("\u00a0", " ")      # non-breaking spaces
    text = re.sub(r"[ \t]+", " ", text)     # collapse spaces
    text = re.sub(r"\n+", "\n", text)       # collapse newlines

    return text.strip()
# ----------------------------
# Scraper
# ----------------------------

class WinWasteScraper:
    def __init__(self, cfg: ScraperConfig):
        self.cfg = cfg

    async def run(self) -> WinWasteResult:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.cfg.headless,
                slow_mo=100 if self.cfg.debug else 0,
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
            error = None

            try:
                await page.goto(self.cfg.login_url, wait_until="domcontentloaded")

                await self._login(page)
                await self._handle_2fa(page)
                await self._go_to_payments(page)

                pdf_path = await self._download_latest_invoice(page)

                period_start, period_end = extract_period_from_pdf(pdf_path)
                amount = extract_invoice_total_from_pdf(pdf_path)

                statement_date = period_start
                due_date = add_one_month(statement_date)

                await browser.close()

                return WinWasteResult(
                    ok=True,
                    amount_cents=amount,
                    account_number=self.cfg.account_number,
                    statement_date=statement_date.date().isoformat() if statement_date else None,
                    due_date=due_date.date().isoformat() if due_date else None,
                    period_start=period_start.date().isoformat() if period_start else None,
                    period_end=period_end.date().isoformat() if period_end else None,
                    pdf_path=str(pdf_path),
                )

            except Exception as e:
                error = str(e)
                return WinWasteResult(ok=False, error=error)

            finally:
                if self.cfg.debug and error:
                    print("[DEBUG] Error occurred — browser left open")
                    print("[DEBUG] Press ENTER to close browser")
                    input()
                    await browser.close()

    # ------------------------
    # Steps
    # ------------------------

    async def _login(self, page):
        await page.wait_for_selector('input[type="text"]', timeout=15_000)
        await page.fill('input[type="text"]', self.cfg.username)

        await page.wait_for_selector('input[type="password"]', timeout=15_000)
        await page.fill('input[type="password"]', self.cfg.password)

        sign_in = page.get_by_role("button", name=re.compile("sign in", re.I))
        await sign_in.scroll_into_view_if_needed()
        await sign_in.click(force=True)

        # Wait for page to respond after login attempt
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)

        # Check for login errors
        error_el = page.locator(
            'text=/invalid|incorrect|locked|disabled|not recognized|failed|unable to sign in/i'
        )
        if await error_el.count() > 0:
            error_text = (await error_el.first.inner_text()).strip()
            raise RuntimeError(f"Login failed: {error_text}")


    async def _handle_2fa(self, page):
        # Give the page a moment to settle after login
        await page.wait_for_timeout(3000)

        # Check if we're already on the dashboard (no 2FA needed)
        dashboard = page.locator("text=My Dashboard")
        payments = page.locator(
            'a.portal-nav__tab-link[href="/my-account/payments/"]'
        )
        if await dashboard.count() > 0 or await payments.count() > 0:
            return

        # Detect which 2FA variant is shown:
        # Variant A: method selection with "Send me an email" button
        # Variant B: direct "Verify with your email" (site remembers method)
        send_email_btn = page.get_by_text("Send me an email")
        verify_with_email = page.locator("text=/Verify with your email/i")

        triggered_email = False

        if await send_email_btn.count() > 0:
            # Variant A: click "Send me an email" to trigger verification
            await send_email_btn.click(force=True)
            triggered_email = True
        elif await verify_with_email.count() > 0:
            # Variant B: click "Verify with your email" to trigger it
            await verify_with_email.click(force=True)
            triggered_email = True

        if not triggered_email:
            # Neither 2FA variant found — capture page state for diagnostics
            url = page.url
            title = await page.title()
            body_text = await page.locator("body").inner_text()
            snippet = body_text[:300].strip()
            raise RuntimeError(
                f"After login, not on dashboard and no 2FA prompt found. "
                f"Page: {url} | Title: {title} | Content: {snippet}"
            )

        # Wait for code input to appear
        try:
            await page.wait_for_selector("input", timeout=20_000)
        except PWTimeoutError:
            url = page.url
            body_text = await page.locator("body").inner_text()
            snippet = body_text[:300].strip()
            raise RuntimeError(
                f"2FA triggered but code input never appeared. "
                f"Page: {url} | Content: {snippet}"
            )

        # Poll handoff file for the 2FA code
        handoff_dir = Path(
            os.getenv("WINWASTE_HANDOFF_DIR", "/handoff/winwaste")
        )
        handoff_file = handoff_dir / "2fa_code.txt"

        code = None
        for _ in range(180):  # ~3 minutes
            if handoff_file.exists():
                code = handoff_file.read_text().strip()
                if code:
                    try:
                        handoff_file.unlink()  # consume-once
                    except FileNotFoundError:
                        pass
                    break
            await asyncio.sleep(1)

        if not code:
            raise RuntimeError(
                "2FA verification code required but not delivered. "
                f"Expected at: {handoff_file}"
            )

        if self.cfg.debug:
            print("[DEBUG][2FA] Code loaded from handoff")

        # Fill code
        code_input = page.locator("input").first
        await code_input.wait_for(state="visible", timeout=10_000)
        await code_input.fill(code)

        verify_btn = page.get_by_role(
            "button", name=re.compile("verify", re.I)
        )
        await verify_btn.scroll_into_view_if_needed()
        await verify_btn.click(force=True)

        # Wait for dashboard after verification.
        #
        # The WP portal lazy-hydrates after the post-2FA redirect, so the
        # "My Dashboard" heading text may not be visible inside the original
        # 30s window even though the user is logged in. We now race several
        # signals: the dashboard text, the Payments nav tab (what we click
        # next anyway), and the My Profile nav tab. We also let the network
        # settle first as a hydration hint.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        dashboard_text = page.locator("text=My Dashboard").first
        payments_link = page.locator(
            'a.portal-nav__tab-link[href="/my-account/payments/"]'
        ).first
        profile_link = page.locator(
            'a.portal-nav__tab-link[href="/my-account/my-profile/"]'
        ).first

        ready = False
        for _ in range(120):  # 60s at 500ms cadence
            for loc in (payments_link, dashboard_text, profile_link):
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        ready = True
                        break
                except Exception:
                    continue
            if ready:
                break
            await asyncio.sleep(0.5)

        if not ready:
            url = page.url
            body_text = await page.locator("body").inner_text()
            snippet = body_text[:300].strip()
            raise RuntimeError(
                f"2FA code submitted but did not reach dashboard. "
                f"Page: {url} | Content: {snippet}"
            )


    async def _go_to_payments(self, page):
        """
        Robust navigation to Payments → select account → Invoice History.
        Handles hydration delays, lazy rendering, and dynamic dropdowns.
        """

        acct = self.cfg.account_number.strip()

        # ----------------------------
        # Click Payments tab (top nav)
        # ----------------------------
        payments_link = page.locator(
            'a.portal-nav__tab-link[href="/my-account/payments/"]'
        )

        try:
            await payments_link.wait_for(state="visible", timeout=20_000)
        except PWTimeoutError:
            # Capture current page state for debugging
            url = page.url
            title = await page.title()
            raise RuntimeError(
                f"Payments tab not found — login may have failed. "
                f"Current page: {url} (title: {title})"
            )
        await payments_link.scroll_into_view_if_needed()

        async with page.expect_navigation(wait_until="domcontentloaded"):
            await payments_link.click(force=True)

        # ----------------------------
        # Allow React hydration
        # ----------------------------
        await page.wait_for_timeout(1500)

        acct = self.cfg.account_number.strip()

        if self.cfg.debug:
            print(f"[DEBUG][acct] forcing account via API: {acct}")

        resp = await page.request.get(
            "https://www.win-waste.com/api/Customer/GetAccountPaymentData",
            params={
                "accountNumber": acct,
                "cacheNoStore": str(uuid.uuid4()),
            },
        )

        if not resp.ok:
            raise RuntimeError(
                f"Account switch API failed ({resp.status}) for account {acct}"
            )

        if self.cfg.debug:
            try:
                data = await resp.json()
                print(f"[DEBUG][acct] API response keys = {list(data.keys())}")
            except Exception:
                print("[DEBUG][acct] API response not JSON")

        # ----------------------------
        # Wait for Payments table to refresh
        # ----------------------------
        rows = page.locator(
            'div.ag-center-cols-container div[role="row"]:visible'
        )

        await rows.first.wait_for(state="visible", timeout=20_000)

        if self.cfg.debug:
            print("[DEBUG][acct] payments table loaded for account")



        # Allow page to reload table
        await page.wait_for_timeout(1500)

        # ----------------------------
        # Force scroll to trigger lazy content
        # ----------------------------
        await page.evaluate(
            """
            () => {
                window.scrollTo({
                    top: document.body.scrollHeight,
                    behavior: 'instant'
                });
            }
            """
        )

        await page.wait_for_timeout(1000)

        # ----------------------------
        # Click Invoice History tab
        # ----------------------------
        tabs = page.locator("ul.nav-tabs")
        await tabs.wait_for(state="visible", timeout=20_000)

        invoice_tab = tabs.locator(
            'a.nav-link:has-text("Invoice History")'
        )

        await invoice_tab.wait_for(state="visible", timeout=20_000)
        await invoice_tab.scroll_into_view_if_needed()
        await invoice_tab.click(force=True)

        # ----------------------------
        # Confirm invoice table loaded
        # ----------------------------
        await page.wait_for_selector(
            "text=Invoice",
            timeout=20_000,
        )

    async def _select_account(self, page):
        dropdown = page.locator("div[role='combobox'], input[role='combobox']").first
        await dropdown.scroll_into_view_if_needed()
        await dropdown.click(force=True)

        await page.wait_for_selector("text=(", timeout=10_000)

        options = page.locator("div[role='option'], li")
        count = await options.count()

        for i in range(count):
            opt = options.nth(i)
            text = (await opt.inner_text()).strip()

            # Match account number inside parentheses
            if f"({self.cfg.account_number})" in text:
                await opt.click(force=True)
                return

        raise RuntimeError("Account number not found in dropdown")

    async def _download_latest_invoice(self, page) -> Path:
        """
        Downloads the most recent invoice by calling the backend
        DownloadInvoice endpoint directly (no DOM clicking).
        """

        download_dir = self.cfg.download_dir
        download_dir.mkdir(parents=True, exist_ok=True)

        # ----------------------------
        # Ensure Invoice History tab
        # ----------------------------
        invoice_tab = page.locator('a.nav-link:has-text("Invoice History")')
        await invoice_tab.wait_for(state="visible", timeout=15_000)
        await invoice_tab.click(force=True)

        # ----------------------------
        # Wait for ag-Grid rows to exist
        # ----------------------------
        rows = page.locator(
            'div.ag-center-cols-container div[role="row"]:visible'
        )
        await rows.first.wait_for(state="visible", timeout=20_000)

        first_row = rows.first

        # ----------------------------
        # Extract invoice number text
        # ----------------------------
        invoice_number_el = first_row.locator(
            'div[col-id="invoiceNumber"] button.btn-link'
        )

        await invoice_number_el.wait_for(state="visible", timeout=10_000)

        invoice_number = (await invoice_number_el.inner_text()).strip()

        if self.cfg.debug:
            print(f"[DEBUG] Latest invoice number: {invoice_number}")

        # ----------------------------
        # Build download URL
        # ----------------------------
        download_url = (
            "https://www.win-waste.com/api/Customer/DownloadInvoice"
            f"?invoiceNumber={invoice_number}"
        )

        if self.cfg.debug:
            print(f"[DEBUG] Download URL: {download_url}")

        # ----------------------------
        # Fetch PDF using authenticated session
        # ----------------------------
        response = await page.request.get(download_url)

        if not response.ok:
            raise RuntimeError(
                f"Invoice download failed ({response.status})"
            )

        content_type = response.headers.get("content-type", "")

        print(f"[DEBUG] Invoice content-type: {content_type}")

        if response.status != 200:
            raise RuntimeError(f"Invoice download failed ({response.status})")

        pdf_bytes = await response.body()

        # ----------------------------
        # Save file
        # ----------------------------
        path = download_dir / f"{invoice_number}.pdf"
        path.write_bytes(pdf_bytes)

        return path

# ----------------------------
# CLI
# ----------------------------

def parse_args():
    import argparse

    p = argparse.ArgumentParser("Win Waste Innovations Scraper")
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

    scraper = WinWasteScraper(cfg)
    result = await scraper.run()

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
