#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
James R. Santaguida Sanitation (Soft-Pak) bill scraper

Flow:
- Go to Soft-Pak sign-in:
    https://secure.soft-pak.com/webpaksg/signin.jsp
- Login with e-mail + password.
- On the Billing page:
    * Wait for the Billing accordion: #billingAccordion.
    * Find the <h3> accordion header whose text includes the account number
      in parentheses (e.g. (01-1307)); the scraper receives the number
      without parentheses.
    * Click that header to expand it.
    * Parse the "Current Charges" amount from the Account / Activity panel.
    * In the expanded accordion content (the sibling <div> after the <h3>),
      find the detail table (id starts with "tblDetail").
    * Take the top-most invoice row and get the PDF link from the
      <td id="billingPDFLink00"><a href="https://pdf.soft-pak.com/...">...</a>
    * Use Playwright's API request client to GET that href and save the
      PDF bytes locally.
- Return:
    amount, amount_cents, pdf_path, final_url
  (Date fields are left as None for now.)

Output:
- Default: prints ONLY the amount (e.g., 360.00)
- With --json:
    {
      "amount": "360.00",
      "amount_cents": 36000,
      "pdf_path": ".../santaguida/invoice-496233.pdf",
      "final_url": "<PDF URL>",
      "statement_date": null,
      "invoice_number": "515020",
      "period_start": null,
      "period_end": null,
      "due_date": null
    }
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
    Browser,
    BrowserContext,
    Page,
)

LOGIN_URL = "https://secure.soft-pak.com/webpaksg/signin.jsp"
DEFAULT_DL_DIR = Path(
     os.getenv("SANTAGUIDA_DOWNLOAD_DIR", "/handoff/santaguida")
).resolve()
# DEFAULT_DL_DIR = Path("./santaguida").resolve()

CURRENCY_RE = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _normalize_account_number(s: str) -> str:
    """
    Normalize account number for matching: strip and remove internal spaces
    so "01-1307" and "01 - 1307" both match the page text "(01-1307)".
    """
    if not s:
        return ""
    return re.sub(r"\s+", "", s.strip())


def _normalize_address(s: str) -> str:
    """
    Very simple normalization so:
        "11 S LARGO DR, STAMFORD, CT 06907"
    and "11 S Largo Dr, Stamford CT 06907"
    both normalize to the same token soup.

    - upper-case
    - remove punctuation
    - collapse whitespace
    - drop common filler like "APT", "UNIT", "#"
    """
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[^\w\s]", " ", s)  # strip punctuation

    remove_tokens = {
        "APT",
        "APARTMENT",
        "UNIT",
        "STE",
        "SUITE",
        "FLOOR",
        "FL",
        "#",
    }
    parts = [p for p in s.split() if p not in remove_tokens]
    return " ".join(parts)


@dataclass
class Config:
    username: str
    password: str
    account_number: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    timezone_id: str = os.getenv("SANTAGUIDA_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    statement_date: Optional[str] = None
    invoice_number: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    due_date: Optional[str] = None


def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("SANTAGUIDA_USERNAME") or "",
        password=args.password or os.getenv("SANTAGUIDA_PASSWORD") or "",
        account_number=args.account_number or os.getenv("SANTAGUIDA_ACCOUNT_NUMBER") or "",
        headless=not bool(args.headful)
        if args.headful is not None
        else _env_bool("SANTAGUIDA_HEADLESS", True),
        slow_mo_ms=int(os.getenv("SANTAGUIDA_SLOW_MO_MS", str(args.slow_mo or 0))),
        debug=bool(args.debug) or _env_bool("SANTAGUIDA_DEBUG", False),
        timezone_id=os.getenv("SANTAGUIDA_TIMEZONE_ID", "America/New_York"),
    )


class SantaguidaScraper:
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
            self.page.on(
                "console",
                lambda m: print(
                    f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr
                ),
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

    async def _install_stealth(self):
        js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
        Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1});
        """
        try:
            await self.context.add_init_script(js)
        except Exception:
            pass

    # ----------------- main -----------------
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")
        if not self.cfg.account_number:
            return Result(ok=False, error="account_number is required")

        page = self.page

        # Step 1: login
        self.log("[step] goto login")
        try:
            await page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=self.cfg.nav_timeout_ms,
            )
        except PWTimeoutError:
            return Result(ok=False, error="Timeout opening login page")

        self.log("[step] credential login")
        await self._login_once(self.cfg.username, self.cfg.password)

        self.log("[step] wait for billing view")
        ok = await self._wait_for_billing_view()
        if not ok:
            return Result(ok=False, error="Post-login Billing view not detected")

        # Step 2: wait for accordion & choose account by account number
        self.log("[step] select account by number")
        account_header = await self._choose_account_by_number(
            page, self.cfg.account_number
        )
        if account_header is None:
            visible = getattr(self, "_last_visible_accounts", []) or []
            visible_str = ", ".join(visible) if visible else "<none on portal>"
            return Result(
                ok=False,
                error=(
                    f"No account row matched account number: {self.cfg.account_number!r}. "
                    f"Visible accounts for this login: [{visible_str}]"
                ),
            )

        # Expand the account accordion
        try:
            await account_header.scroll_into_view_if_needed()
            await account_header.click()
        except Exception as e:
            self.log("[account] click failed:", repr(e))

        # Wait for its content div (invoice table) to be visible
        content_div = account_header.locator(
            "xpath=following-sibling::div[1]"
        )
        try:
            await content_div.wait_for(state="visible", timeout=10000)
        except Exception:
            self.log("[account] accordion content did not become visible in time")

        await page.wait_for_timeout(1000)

        # ---------------- PDF first ----------------
        self.log("[step] download PDF for latest invoice")
        pdf_path, final_url = await self._download_latest_invoice_pdf(
            page, account_header
        )

        amount_str: Optional[str] = None
        amount_cents: Optional[int] = None

        # 3a. Try to get amount from the PDF itself
        if pdf_path:
            self.log("[step] extract amount from PDF")
            amount_str, amount_cents = extract_amount_from_pdf(pdf_path)
            self.log(f"[pdf-amount] amount from PDF: {amount_str}, {amount_cents}")

        # 3b. Fallback: summary panel
        if not amount_str:
            self.log("[amount] PDF parse failed; trying Account Activity summary")
            amount_str, amount_cents = await self._extract_amount_from_summary(page)

        # 3c. Fallback: invoice row in the table
        if not amount_str:
            self.log("[amount] summary parse failed; falling back to invoice row")
            amount_str, amount_cents = await self._extract_amount_from_invoice_row(
                page
            )

        # If we still don't have an amount, error out (but include pdf_path/final_url if we have them)
        if not amount_str:
            return Result(
                ok=False,
                error="Could not extract amount for selected account (PDF + page both failed)",
                amount=None,
                amount_cents=None,
                pdf_path=pdf_path,
                final_url=final_url,
            )

        # If we have an amount but no PDF, treat as failure but return amount for debugging
        if not pdf_path:
            return Result(
                ok=False,
                error="Matched account but failed to capture PDF download",
                amount=amount_str,
                amount_cents=amount_cents,
                pdf_path=None,
                final_url=final_url,
            )

        # ----- Date parsing from PDF -----
        stmt_iso: Optional[str] = None
        period_start_iso: Optional[str] = None
        period_end_iso: Optional[str] = None
        due_iso: Optional[str] = None

        try:
            stmt_iso = extract_invoice_date_from_pdf(pdf_path)
        except Exception as e:
            self.log("[pdf-date] error extracting invoice date:", repr(e))

        invoice_number: Optional[str] = None
        try:
            invoice_number = extract_invoice_number_from_pdf(pdf_path)
            if invoice_number:
                self.log("[pdf-invoice] invoice_number:", invoice_number)
        except Exception as e:
            self.log("[pdf-invoice] error extracting invoice number:", repr(e))

        # As requested: use Invoice Date for all four fields
        if stmt_iso:
            period_start_iso = stmt_iso
            period_end_iso = stmt_iso
            due_iso = stmt_iso

        # Success: amount + PDF + dates + invoice_number from PDF
        return Result(
            ok=True,
            amount=amount_str,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=final_url,
            statement_date=stmt_iso,
            invoice_number=invoice_number,
            period_start=period_start_iso,
            period_end=period_end_iso,
            due_date=due_iso,
        )

    # ----------------- login helpers -----------------
    async def _login_once(self, username: str, password: str):
        """
        Find the sign-in form by its "Sign In" button, then fill email/password
        and submit.
        """
        page = self.page

        # Locate the "Sign In" button first and derive the containing form
        login_btn = None
        for loc in [
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('input[type="submit"][value*="Sign In" i]'),
        ]:
            try:
                if await loc.count() > 0:
                    login_btn = loc.first
                    break
            except Exception:
                continue

        form = None
        if login_btn is not None:
            try:
                form = login_btn.locator("xpath=ancestor::form[1]")
            except Exception:
                form = None

        if form is None:
            self.log("[login] form not found; using global inputs fallback")
            form = page

        # Email field
        email_input = None
        for loc in [
            form.get_by_label(re.compile(r"E-?Mail", re.I)),
            form.locator('input[type="email"]'),
            form.locator('input[type="text"]'),
        ]:
            try:
                await loc.first.wait_for(state="visible", timeout=8000)
                email_input = loc.first
                break
            except Exception:
                continue

        # Password field
        pass_input = None
        for loc in [
            form.get_by_label(re.compile(r"Password", re.I)),
            form.locator('input[type="password"]'),
        ]:
            try:
                await loc.first.wait_for(state="visible", timeout=8000)
                pass_input = loc.first
                break
            except Exception:
                continue

        if not email_input or not pass_input:
            self.log("[login] inputs not found; maybe already signed in")
        else:
            await email_input.fill(username)
            await asyncio.sleep(random.uniform(0.15, 0.3))
            await pass_input.fill(password)

            if login_btn is not None:
                await login_btn.click()
            else:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        self.log("[login] done")

    async def _wait_for_billing_view(self) -> bool:
        """
        Heuristic: wait for a 'Billing' tab/link AND a 'Signed in as:' label,
        and for the billing accordion container to appear.
        """
        page = self.page
        for _ in range(40):  # up to ~20s
            try:
                if (
                    await page.get_by_text(re.compile(r"Signed in as", re.I)).count()
                    > 0
                ):
                    if (
                        await page.get_by_text(
                            re.compile(r"\bBilling\b", re.I)
                        ).count()
                        > 0
                    ):
                        # Also require the accordion container to exist
                        if await page.locator("#billingAccordion").count() > 0:
                            self.log("[post-login] Billing view detected")
                            return True
            except Exception:
                pass
            await page.wait_for_timeout(500)
        return False

    # ----------------- account selection -----------------
    async def _choose_account_by_number(self, page: Page, account_number: str):
        """
        Look at the billing accordion headers:

            #billingAccordion h3

        Each header text looks like:
            "(01-1307) ABSOLUTE VALUE MANAGEMENT: 11 S LARGO DR, STAMFORD, CT 06907"

        We match the account number (passed without parentheses, e.g. 01-1307)
        to the value in parentheses at the start of the header.
        """
        target_norm = _normalize_account_number(account_number)
        if not target_norm:
            return None

        # Wait for headers to appear
        headers = page.locator("#billingAccordion h3")
        for _ in range(40):  # up to ~20s
            if await headers.count() > 0:
                break
            await page.wait_for_timeout(500)

        count = await headers.count()
        self.log("[accounts] total accordion headers:", count)

        # Stash the list of visible accounts on the instance so the caller
        # can include them in the failure message. Makes "DB ↔ Santaguida"
        # mismatches (account on file but not granted to this login) easy
        # to diagnose without re-running with --debug.
        self._last_visible_accounts: List[str] = []

        if count == 0:
            return None

        # Match first parenthesized group in header (e.g. "(01-1307)") to our account number
        for i in range(count):
            hdr = headers.nth(i)
            try:
                txt = (await hdr.inner_text()).strip()
            except Exception:
                continue
            if not txt:
                continue
            match = re.search(r"\(([^)]+)\)", txt)
            if not match:
                continue
            header_account = match.group(1).strip()
            self._last_visible_accounts.append(header_account)
            header_number_norm = _normalize_account_number(header_account)
            if header_number_norm == target_norm:
                self.log("[accounts] matched header index", i, "text:", txt)
                return headers.nth(i)

        self.log(
            "[accounts] no header matched account number:",
            target_norm,
        )
        return None

    # ----------------- amount extraction -----------------
    async def _extract_amount_from_summary(
        self, page: Page
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Parse "Current Charges" from the Account Activity panel on the right.
        """
        candidates = page.locator("table").filter(
            has_text=re.compile(r"Account", re.I)
        ).filter(has_text=re.compile(r"Activity", re.I))

        if await candidates.count() == 0:
            self.log("[summary] Account Activity table not found")
            return None, None

        summary = candidates.first
        try:
            txt = (await summary.inner_text()).replace("\u00a0", " ")
        except Exception as e:
            self.log("[summary] inner_text error:", repr(e))
            return None, None

        m = re.search(
            r"Current\s+Charges\s+(\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2}))",
            txt,
            re.I,
        )
        if not m:
            m = re.search(
                r"Balance\s+Due\s+(\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2}))",
                txt,
                re.I,
            )

        if not m:
            self.log("[summary] no currency found in summary panel")
            return None, None

        raw = m.group(1)
        cleaned = raw.replace("$", "").replace(",", "").strip()
        self.log("[summary] amount from summary:", cleaned)

        try:
            cents = int(round(float(cleaned) * 100))
        except Exception:
            cents = None

        return cleaned, cents

    async def _extract_amount_from_invoice_row(
        self, page: Page
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        Fallback: parse amount from the first invoice row of the main invoice table.
        """
        table = page.locator("table").filter(
            has_text=re.compile(r"Invoice\s*#", re.I)
        )
        if await table.count() == 0:
            self.log("[invoice-amount] invoice table not found")
            return None, None

        inv_table = table.first
        rows = inv_table.locator("tbody tr")
        try:
            await rows.first.wait_for(state="visible", timeout=8000)
        except Exception:
            self.log("[invoice-amount] no visible invoice rows")
            return None, None

        first_row = rows.first
        try:
            row_text = (await first_row.inner_text()).replace("\u00a0", " ")
        except Exception:
            return None, None

        amounts = CURRENCY_RE.findall(row_text)
        if not amounts:
            self.log("[invoice-amount] no currency strings in first row")
            return None, None

        raw = amounts[-1]
        cleaned = raw.replace("$", "").replace(",", "").strip()
        self.log("[invoice-amount] amount from invoice row:", cleaned)

        try:
            cents = int(round(float(cleaned) * 100))
        except Exception:
            cents = None

        return cleaned, cents

    # ----------------- PDF capture -----------------
    async def _download_latest_invoice_pdf(
        self, page: Page, account_header
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        For the given account accordion header, look at its following sibling
        <div> (the expanded accordion content), find the billing detail table,
        take the first row, and download the PDF pointed to by the
        billingPDFLinkXX <a href="...">.

        Returns (pdf_path, final_url).
        """
        content_div = account_header.locator("xpath=following-sibling::div[1]")

        # The detail table has id="tblDetail0", "tblDetail1", etc.
        tables = content_div.locator("table[id^='tblDetail']")
        if await tables.count() == 0:
            self.log("[pdf] detail table not found under accordion content")
            return None, page.url

        inv_table = tables.first
        rows = inv_table.locator("tbody tr")
        try:
            await rows.first.wait_for(state="visible", timeout=8000)
        except Exception:
            self.log("[pdf] no visible invoice rows in detail table")
            return None, page.url

        first_row = rows.first

        # Invoice number for nicer filename
        invoice_no = None
        try:
            inv_cell = first_row.locator("td[id^='billingInvoiceNumber']")
            if await inv_cell.count() > 0:
                raw_inv = (await inv_cell.first.inner_text()).strip()
                invoice_no = re.sub(r"\s+", "", raw_inv)
        except Exception:
            pass

        # PDF link cell: td id="billingPDFLink00" <a href="...">
        pdf_cell = first_row.locator("td[id^='billingPDFLink']")
        if await pdf_cell.count() == 0:
            self.log("[pdf] billingPDFLink cell not found")
            return None, page.url

        link = pdf_cell.locator("a")
        if await link.count() == 0:
            self.log("[pdf] no <a> inside billingPDFLink cell")
            return None, page.url

        href = await link.first.get_attribute("href")
        if not href:
            self.log("[pdf] PDF link has no href")
            return None, page.url

        self.log("[pdf] fetching PDF via API request:", href)

        # Use Playwright's APIRequestContext bound to this browser context.
        request_ctx = self.context.request
        resp = await request_ctx.get(href)
        status = resp.status
        if status != 200:
            self.log("[pdf] GET failed with status", status)
            return None, href

        pdf_bytes = await resp.body()

        # Write to disk
        stem = f"invoice-{invoice_no}" if invoice_no else "invoice"
        filename = stem + ".pdf"
        target_path = (self.download_dir / filename).resolve()

        i = 1
        base_stem = stem
        while target_path.exists():
            target_path = self.download_dir / f"{base_stem}-{i}.pdf"
            i += 1

        try:
            with open(target_path, "wb") as f:
                f.write(pdf_bytes)
        except Exception as e:
            self.log("[pdf] failed writing PDF:", repr(e))
            return None, href

        self.log("[pdf] saved:", str(target_path))
        return str(target_path), href

# --------------------- CLI ---------------------


# ----------------- PDF text + amount parsing -----------------

def _extract_text_pdfminer(pdf_path: str) -> Optional[str]:
    """
    Try pdfminer first. If it isn't installed or returns junk, return None.
    """
    try:
        from pdfminer.high_level import extract_text  # type: ignore

        text = extract_text(pdf_path)
    except Exception:
        return None

    cleaned = text.replace("\x0c", "").strip()
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
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(pdf_path)
    except Exception:
        try:
            from PyPDF2 import PdfReader as R2  # type: ignore

            reader = R2(pdf_path)
        except Exception:
            return None

    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return text.replace("\x00", "")


def _parse_us_date_to_iso(s: str) -> Optional[str]:
    """
    Parse US-style dates like 11/01/2025 or 11/01/25 into 'YYYY-MM-DD'.
    """
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_invoice_date_from_pdf(pdf_path: str) -> Optional[str]:
    """
    Extract the 'Invoice Date' from the Santaguida PDF.

    Strategy:
      - Grab text from the PDF.
      - Look only at the top ~20 lines (the header block where the invoice meta lives).
      - First try to find a date near 'Invoice Date'.
      - If that fails, fall back to the first date-looking token in that header block.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path)
    if not text:
        return None

    text = text.replace("\u00a0", " ")

    lines = text.splitlines()
    header_block = "\n".join(lines[:20])  # top section with Invoice Date, Bill Period, etc.

    # 1) Prefer a date specifically near 'Invoice Date'
    m = re.search(
        r"Invoice\s*Date[^0-9]{0,40}([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
        header_block,
        re.I,
    )

    # 2) Fallback: first date-looking token in the header block
    if not m:
        m = re.search(r"([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", header_block)
        if not m:
            return None

    return _parse_us_date_to_iso(m.group(1))


def _find_invoice_number_in_text(text: str) -> Optional[str]:
    """
    Try to locate the invoice number inside already-extracted PDF text.

    Handles two known layouts:
      A) pypdf-style – label and value on one line:
             "Invoice #: 515020"
      B) pdfminer-style – labels listed first, values in a separate block:
             Line 5: "Invoice Date:"
             Line 6: "Invoice #:"
             Line 7: "Customer #:"
             ...
             Line 13: "02/01/2026"
             Line 14: "515020"         <- value for Invoice #
             Line 15: "01-7422 8"
    """
    text = text.replace("\u00a0", " ").replace("\x0c", " ")
    lines = text.splitlines()
    header_lines = lines[:30]

    # --- Strategy 1: value on the SAME line as "Invoice #:" ---
    for line in header_lines:
        m = re.search(r"Invoice\s*#\s*:\s*(\d+)", line, re.I)
        if m:
            return m.group(1)

    # --- Strategy 2: value on the NEXT line after "Invoice #:" ---
    for i, line in enumerate(header_lines):
        if re.match(r"^\s*Invoice\s*#\s*:\s*$", line, re.I):
            if i + 1 < len(header_lines):
                nxt = header_lines[i + 1].strip()
                if re.match(r"^\d+$", nxt):
                    return nxt

    # --- Strategy 3: pdfminer columnar layout ---
    # Labels appear in a block (ending with ":"), then an empty line,
    # then values appear in the same order.
    # Find where "Invoice #:" sits among the labels, then count the
    # same offset into the values block.
    label_indices: list[int] = []          # line indices of label lines
    invoice_label_pos: Optional[int] = None  # position of Invoice # within labels
    for i, line in enumerate(header_lines):
        stripped = line.strip()
        if not stripped:
            continue
        # A label line ends with ":" and has no digits after the colon
        if re.match(r"^[A-Za-z #.]+:\s*$", stripped):
            if re.search(r"Invoice\s*#\s*:", stripped, re.I):
                invoice_label_pos = len(label_indices)
            label_indices.append(i)
        elif label_indices and invoice_label_pos is not None:
            # We've passed the label block; now we're in the values.
            break

    if invoice_label_pos is not None and label_indices:
        # Values start after the last label line (skip blanks)
        val_start = label_indices[-1] + 1
        val_idx = 0
        for i in range(val_start, min(val_start + 20, len(lines))):
            stripped = lines[i].strip()
            if not stripped:
                continue
            if val_idx == invoice_label_pos:
                m = re.match(r"^(\d+)", stripped)
                if m:
                    return m.group(1)
                break
            val_idx += 1

    # --- Strategy 4: "INVOICE #" in the payment-stub section ---
    for i, line in enumerate(lines):
        m = re.search(r"INVOICE\s*#\s*$", line)
        if m:
            # Value is on a subsequent non-empty line
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = lines[j].strip()
                if nxt and re.match(r"^\d+$", nxt):
                    return nxt

    return None


def extract_invoice_number_from_pdf(pdf_path: str) -> Optional[str]:
    """
    Extract the 'Invoice #' value from the Santaguida PDF.
    Tries both pdfminer and pypdf text extraction independently, because
    they produce different layouts (pdfminer splits labels/values into
    separate column blocks; pypdf keeps them on one line).
    Returns the invoice number string (e.g. "515020") or None.
    """
    for extract_fn in (_extract_text_pdfminer, _extract_text_pypdf):
        text = extract_fn(pdf_path)
        if not text:
            continue
        result = _find_invoice_number_in_text(text)
        if result:
            return result
    return None


def extract_amount_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[int]]:
    """
    For Santaguida PDFs, we try:

      1) A line containing 'ACCOUNT BALANCE' and grab the last $xxx.xx on that line.
      2) Otherwise, take the largest $xxx.xx amount in the whole document.

    Returns (amount_str, amount_cents) or (None, None) on failure.
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path)
    if not text:
        return None, None

    # Normalize whitespace a bit
    text = text.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]

    # 1) Look for ACCOUNT BALANCE line
    for ln in lines:
        if re.search(r"ACCOUNT\s+BALANCE", ln, re.I):
            matches = CURRENCY_RE.findall(ln)
            if matches:
                raw = matches[-1]
                cleaned = raw.replace("$", "").replace(",", "").strip()
                try:
                    cents = int(round(float(cleaned) * 100))
                except Exception:
                    cents = None
                return cleaned, cents

    # 2) Fallback: all currency amounts, pick the largest numeric value
    all_matches = CURRENCY_RE.findall(text)
    if not all_matches:
        return None, None

    best_val = None
    best_str = None
    for raw in all_matches:
        cleaned = raw.replace("$", "").replace(",", "").strip()
        try:
            val = float(cleaned)
        except Exception:
            continue
        if best_val is None or val > best_val:
            best_val = val
            best_str = cleaned

    if best_str is None:
        return None, None

    try:
        cents = int(round(best_val * 100))
    except Exception:
        cents = None

    return best_str, cents


def _parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="Santaguida Sanitation (Soft-Pak) bill scraper"
    )
    p.add_argument(
        "--username", help="Soft-Pak portal username / email", required=False
    )
    p.add_argument(
        "--password", help="Soft-Pak portal password", required=False
    )
    p.add_argument(
        "--account-number",
        dest="account_number",
        help="Account number (e.g. 01-1307) used to select the correct account",
        required=False,
    )
    p.add_argument(
        "--headful",
        action="store_true",
        help="Run with visible browser (headless = false)",
    )
    p.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow motion ms between actions",
    )
    p.add_argument(
        "--json", action="store_true", help="Print JSON instead of just amount"
    )
    p.add_argument(
        "--debug", action="store_true", help="Verbose debug logging"
    )
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with SantaguidaScraper(cfg) as s:
        result = await s.run()

    if not result.ok:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(
            json.dumps(
                {
                    "amount": result.amount,
                    "amount_cents": result.amount_cents,
                    "pdf_path": result.pdf_path,
                    "final_url": result.final_url,
                    "statement_date": result.statement_date,
                    "invoice_number": result.invoice_number,
                    "period_start": result.period_start,
                    "period_end": result.period_end,
                    "due_date": result.due_date,
                },
                indent=2,
            )
        )
    else:
        print(result.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
