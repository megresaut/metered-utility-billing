#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
First District Water Department (InvoiceCloud) — latest invoice downloader
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
)

BASE_URL = "https://cw2.firstdistrictwater.org/"
DEFAULT_DL_DIR = Path(
    os.getenv("FDWD_DOWNLOAD_DIR", "/handoff/fdwd")
).resolve()
# DEFAULT_DL_DIR = Path(os.getenv("RA_HANDOFF_DIR", "/Users/megkrish/Desktop/ra-avm/dev/avm-backend/ra-handoff")) / "fdwd"
CURRENCY_RE_STRICT = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})")
CURRENCY_RE_LOOSE  = re.compile(r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})")


# ---------------- Models ----------------

@dataclass
class Config:
    username: str
    password: str
    address_query: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # NEW: parsed from PDF
    statement_date: Optional[str] = None  # Bill Date, e.g. "2025-09-18"
    due_date: Optional[str] = None        # "2025-10-18"
    period_start: Optional[str] = None    # "2025-06-05"
    period_end: Optional[str] = None      # "2025-09-05"



# ---------------- Utils ----------------

def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _money_to_cents(txt: str) -> Tuple[Optional[str], Optional[int]]:
    m = CURRENCY_RE_LOOSE.search(txt or "")
    if not m:
        return None, None
    clean = m.group(0).replace("$", "").replace(",", "").strip()
    try:
        cents = int(round(float(clean) * 100))
    except Exception:
        cents = None
    return clean, cents


# ---------------- Scraper ----------------

class FDWDScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None

        # NEW: track last seen PDF URL from any network response
        self._last_pdf_url: Optional[str] = None

        # NEW: currently-selected FDWD account number (e.g. "501714")
        self._account_number: Optional[str] = None


    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

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
        self.context = await self.browser.new_context(accept_downloads=True)
        self.page = await self.context.new_page()

        if self.cfg.debug:
            # console log passthrough
            self.page.on(
                "console",
                lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr),
            )

            # response sniff: remember the last PDF URL we saw
            def _resp_logger(r):
                ctype = r.headers.get("content-type", "").lower()
                if "application/pdf" in ctype:
                    self._last_pdf_url = r.url
                    self.log("[resp][pdf]", r.url)
            self.context.on("response", _resp_logger)

        else:
            # still track _last_pdf_url in non-debug mode, just quietly
            def _resp_tracker(r):
                ctype = r.headers.get("content-type", "").lower()
                if "application/pdf" in ctype:
                    self._last_pdf_url = r.url
            self.context.on("response", _resp_tracker)

        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] closing")
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # -------------- Main --------------

    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password or not self.cfg.address_query:
            return Result(ok=False, error="username, password, and address are required")

        if not await self._login():
            return Result(ok=False, error="Login failed")

        if not await self._open_account_and_goto_bills(self.cfg.address_query):
            return Result(ok=False, error="Could not navigate to Bills list for the given address")

        amount, amount_cents, pdf_path = await self._open_latest_invoice_and_download()
        if not pdf_path:
            return Result(ok=False, error="Failed to download invoice PDF")

        statement_date, due_date, period_start, period_end = extract_fdwd_dates_from_pdf(pdf_path)

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
    # -------------- Steps --------------

    async def _login(self) -> bool:
        page = self.page
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            return False

        user_input = await self._first_visible([
            page.get_by_label(re.compile(r"Username", re.I)),
            page.get_by_placeholder(re.compile(r"Username", re.I)),
            page.locator('input[name="username" i], input[id*="user" i]'),
        ])
        pass_input = await self._first_visible([
            page.get_by_label(re.compile(r"Password", re.I)),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.locator('input[type="password"]'),
        ])
        if not user_input or not pass_input:
            self.log("[login] inputs not found")
            return False

        await user_input.fill(self.cfg.username)
        await pass_input.fill(self.cfg.password)

        login_btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"^\s*Login\s*$", re.I)),
            page.locator('button[type="submit"], input[type="submit"]'),
        ])
        if login_btn:
            await login_btn.click()
        else:
            await pass_input.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass

        try:
            await page.get_by_text(re.compile(r"^\s*Account\s+", re.I)).first.wait_for(timeout=20000)
            return True
        except Exception:
            return False

    async def _open_account_and_goto_bills(self, address_query: str) -> bool:
        page = self.page
        q_norm = _norm(address_query)
        self.log("[address] query:", address_query, " → norm:", q_norm)

        # Filter box
        search = await self._first_present([
            page.get_by_placeholder(re.compile(r"Enter\s+partial\s+Address", re.I)),
            page.locator('input[type="text"][placeholder*="Address" i]'),
        ])
        if search:
            await search.fill("")
            await search.type(address_query)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)

        # Row matching
        addr = q_norm
        street_tokens = [t for t in addr.split() if t.isalnum()]
        first_num = next((t for t in street_tokens if t.isdigit()), None)
        first_word_ix = street_tokens.index(first_num) + 1 if first_num and first_num in street_tokens else 0
        street_anchor = " ".join(street_tokens[first_word_ix:first_word_ix+2]) if first_word_ix < len(street_tokens) else ""
        city = None
        if "," in address_query:
            parts = [p.strip() for p in address_query.split(",")]
            if len(parts) >= 2:
                city = parts[1]

        patterns = []
        if first_num and street_anchor:
            patterns.append(re.compile(rf"\(\d+\)\s*{re.escape(first_num)}\s+{re.escape(street_anchor)}", re.I))
        if city:
            patterns.append(re.compile(rf"\(\d+\).*\b{re.escape(city)}\b", re.I))
        patterns.append(re.compile(r"\(\d+\)\s+.*", re.I))

        row_loc = None
        for pat in patterns:
            try:
                loc = page.get_by_text(pat)
                if await loc.count() > 0:
                    row_loc = loc.first
                    self.log("[address] matched row via pattern:", pat.pattern)
                    break
            except Exception:
                continue

        if not row_loc:
            self.log("[address] no row matched after filtering")
            return False

        # --- NEW: capture the account number from "(501714) 49 Stuart Ave, Norwalk 06850"
        try:
            row_text = (await row_loc.inner_text()).strip()
            m_acct = re.search(r"\((\d+)\)", row_text)
            if m_acct:
                self._account_number = m_acct.group(1)
                self.log("[address] captured account number:", self._account_number)
            else:
                self.log("[address] could not parse account number from row text:", row_text)
        except Exception as e:
            self.log("[address] failed to read row text for account number:", repr(e))

        # Click the row to select the account
        try:
            await row_loc.scroll_into_view_if_needed()
            await row_loc.click()
            self.log("[address] clicked row to select account")
            await page.wait_for_timeout(500)
        except Exception as e:
            self.log("[address] failed to click row:", repr(e))
            return False

        # FDWD: use the big "$ Pay Now" button instead of "Go to Bills list"
        pay_btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"\$\s*Pay\s+Now", re.I)),
            page.get_by_role("button", name=re.compile(r"Pay\s+Now", re.I)),
            page.locator(':is(a,button):has-text("Pay Now")'),
        ])
        if not pay_btn:
            self.log("[address] 'Pay Now' button not found after selecting account")
            return False

        self.log("[address] clicking Pay Now to go to InvoiceCloud")
        # Clicking Pay Now typically opens InvoiceCloud in a new tab/window
        page_event = self.context.wait_for_event("page", timeout=15000)
        await pay_btn.click()

        new_page: Optional[Page] = None
        try:
            new_page = await page_event
        except Exception:
            new_page = None

        target_page = new_page or page
        try:
            await target_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        # Now we should be on InvoiceCloud — wait for the bills/history marker
        try:
            await target_page.get_by_text(
                re.compile(r"Recent Invoice and Payment History", re.I)
            ).first.wait_for(timeout=20000)
            self.page = target_page
            self.log("[address] reached InvoiceCloud bills/history page")
            return True
        except Exception as e:
            self.log("[address] did not find 'Recent Invoice and Payment History':", repr(e))
            return False



        page = self.page
        q_norm = _norm(address_query)
        self.log("[address] query:", address_query, " → norm:", q_norm)

        # Filter box
        search = await self._first_present([
            page.get_by_placeholder(re.compile(r"Enter\s+partial\s+Address", re.I)),
            page.locator('input[type="text"][placeholder*="Address" i]'),
        ])
        if search:
            await search.fill("")
            await search.type(address_query)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)

        # Row matching
        addr = q_norm
        street_tokens = [t for t in addr.split() if t.isalnum()]
        first_num = next((t for t in street_tokens if t.isdigit()), None)
        first_word_ix = street_tokens.index(first_num) + 1 if first_num and first_num in street_tokens else 0
        street_anchor = " ".join(street_tokens[first_word_ix:first_word_ix+2]) if first_word_ix < len(street_tokens) else ""
        city = None
        if "," in address_query:
            parts = [p.strip() for p in address_query.split(",")]
            if len(parts) >= 2:
                city = parts[1]

        patterns = []
        if first_num and street_anchor:
            patterns.append(re.compile(rf"\(\d+\)\s*{re.escape(first_num)}\s+{re.escape(street_anchor)}", re.I))
        if city:
            patterns.append(re.compile(rf"\(\d+\).*\b{re.escape(city)}\b", re.I))
        patterns.append(re.compile(r"\(\d+\)\s+.*", re.I))

        row_loc = None
        for pat in patterns:
            try:
                loc = page.get_by_text(pat)
                if await loc.count() > 0:
                    row_loc = loc.first
                    self.log("[address] matched row via pattern:", pat.pattern)
                    break
            except Exception:
                continue

        if not row_loc:
            self.log("[address] no row matched after filtering")
            return False

        # Click the row to select the account
        try:
            await row_loc.scroll_into_view_if_needed()
            await row_loc.click()
            self.log("[address] clicked row to select account")
            await page.wait_for_timeout(500)
        except Exception as e:
            self.log("[address] failed to click row:", repr(e))
            return False

        # FDWD: use the big "$ Pay Now" button instead of "Go to Bills list"
        pay_btn = await self._first_present([
            page.get_by_role("button", name=re.compile(r"\$\s*Pay\s+Now", re.I)),
            page.get_by_role("button", name=re.compile(r"Pay\s+Now", re.I)),
            page.locator(':is(a,button):has-text("Pay Now")'),
        ])
        if not pay_btn:
            self.log("[address] 'Pay Now' button not found after selecting account")
            return False

        self.log("[address] clicking Pay Now to go to InvoiceCloud")
        # Clicking Pay Now typically opens InvoiceCloud in a new tab/window
        page_event = self.context.wait_for_event("page", timeout=15000)
        await pay_btn.click()

        new_page: Optional[Page] = None
        try:
            new_page = await page_event
        except Exception:
            new_page = None

        target_page = new_page or page
        try:
            await target_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        # Now we should be on InvoiceCloud — wait for the bills/history marker
        try:
            await target_page.get_by_text(
                re.compile(r"Recent Invoice and Payment History", re.I)
            ).first.wait_for(timeout=20000)
            self.page = target_page
            self.log("[address] reached InvoiceCloud bills/history page")
            return True
        except Exception as e:
            self.log("[address] did not find 'Recent Invoice and Payment History':", repr(e))
            return False
    async def _open_latest_invoice_and_download(self) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        page = self.page

        # All invoice rows
        rows = page.locator("table tbody tr")
        try:
            await rows.first.wait_for(timeout=15000)
        except Exception:
            self.log("[invoice] no rows in invoice table")
            return None, None, None

        target_row = rows.first

        # NEW: if we have an account number, find the matching row
        if self._account_number:
            acct_pat = re.compile(
                rf"Account\s*#\s*{re.escape(self._account_number)}\b",
                re.I,
            )
            try:
                row_count = await rows.count()
                self.log("[invoice] searching", row_count, "rows for account #", self._account_number)
                for i in range(row_count):
                    row = rows.nth(i)
                    try:
                        txt = (await row.inner_text()).strip()
                    except Exception:
                        continue

                    if acct_pat.search(txt):
                        self.log("[invoice] matched account row index", i, "text:", txt.replace("\n", " | "))
                        target_row = row
                        break
                else:
                    self.log("[invoice] no row contained Account #", self._account_number, "- using first row fallback")
            except Exception as e:
                self.log("[invoice] error while searching rows for account:", repr(e))

        # (optional) highlight the chosen row in headful mode
        try:
            await target_row.evaluate(
                "(el)=>{el.style.outline='3px solid cyan';el.style.outlineOffset='2px';}"
            )
        except Exception:
            pass

        # Find the View Invoice link *inside that row* (instead of always first row)
        view = await self._first_present([
            target_row.get_by_role("link", name=re.compile(r"View\s+Invoice", re.I)),
            target_row.get_by_role("button", name=re.compile(r"View\s+Invoice", re.I)),
            target_row.locator(':is(a,button):has-text("View Invoice")'),
        ])
        if not view:
            self.log("[invoice] View Invoice not found in target row")
            return None, None, None

        page_event = self.context.wait_for_event("page", timeout=7000)
        await view.click()
        new_tab: Optional[Page] = None
        try:
            new_tab = await page_event
        except Exception:
            new_tab = None

        target_page = new_tab or page
        try:
            await target_page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        pdf_path = await self._download_pdf_from_viewer(target_page)

        if not pdf_path:
            # last resort: sniff a direct pdf network response and dump it
            try:
                resp = await target_page.wait_for_response(
                    lambda r: "application/pdf" in r.headers.get("content-type", "").lower(),
                    timeout=6000,
                )
                data = await resp.body()
                pdf_path = self._write_bytes("invoice.pdf", data)
                self.log("[download] response-fallback saved via wait_for_response")
            except Exception:
                pass

        if not pdf_path:
            return None, None, None

        amount_str, amount_cents = extract_amount_from_pdf(pdf_path)
        return amount_str, amount_cents, pdf_path

    # -------------- PDF Download Helpers --------------

    async def _save_pdf_direct(self, pdf_url: str) -> Optional[str]:
        """
        Download the PDF bytes directly using this browser context's auth/cookies.
        Works in both headful and headless, bypasses Chromium's flaky download UI.
        """
        # ensure download dir
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # unique filename
        target = (self.download_dir / "invoice.pdf").resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1

        try:
            resp = await self.context.request.get(pdf_url)
        except Exception as e:
            self.log("[download] direct GET failed:", repr(e))
            return None

        if not resp.ok:
            self.log("[download] direct GET not ok:", resp.status)
            return None

        ct = (resp.headers or {}).get("content-type", "").lower()
        data = await resp.body()

        # sanity check: should look like a PDF
        if not data or not data.startswith(b"%PDF"):
            self.log("[download] direct GET got non-PDF or empty body; ct=", ct)
            # we'll still dump for debugging, but we won't claim success
            target.write_bytes(data)
            return None

        target.write_bytes(data)
        self.log("[download] wrote direct pdf to", str(target))
        return str(target)

    async def _download_pdf_from_viewer(self, page: Page) -> Optional[str]:
        """
        Priority order:
        1. If we already saw a PDF URL in any response (self._last_pdf_url), just download that.
        2. Try to construct a `...pdf=1` URL from the viewer URL and GET it.
        3. Try clicking the Chrome viewer download button and sniff the network.
        4. Last: scan embeds/iframes inside the viewer and GET that URL.
        """
        self.log("[download] entering viewer")

        # 1) use the exact PDF URL we sniffed earlier from the response hook
        if self._last_pdf_url:
            self.log("[download] attempting cached last_pdf_url:", self._last_pdf_url)
            saved = await self._save_pdf_direct(self._last_pdf_url)
            if saved:
                return saved

        # 2) construct direct InvoiceCloud pdf=1 URL from the page URL itself
        direct_pdf_guess = self._invoicecloud_direct_pdf_url(page.url)
        if direct_pdf_guess:
            self.log("[download] attempting direct pdf=1 guess:", direct_pdf_guess)
            saved = await self._save_pdf_direct(direct_pdf_guess)
            if saved:
                return saved

        # 3) try viewer's shadow-root download button (mainly helps in headful)
        try:
            clicked = await page.evaluate_handle("""
                () => {
                    const el = document.querySelector('cr-icon-button#download');
                    if (!el) return false;
                    try {
                        if (el.shadowRoot) {
                            (el.shadowRoot.querySelector('div#icon, #maskedImage, cr-icon') || el).click();
                        } else {
                            el.click();
                        }
                        return true;
                    } catch (e) { return false; }
                }
            """)
            if clicked:
                self.log("[download] clicked shadow-root download button")
                # see if that click immediately triggered a networked pdf response
                try:
                    resp = await page.wait_for_response(
                        lambda r: "application/pdf" in r.headers.get("content-type", "").lower(),
                        timeout=3000,
                    )
                    data = await resp.body()
                    return self._write_bytes("invoice.pdf", data)
                except Exception:
                    pass
        except Exception as e:
            self.log("[download] shadow click failed:", repr(e))

        # 4) fallback: scan embeds/iframes for a PDF URL and GET that directly
        pdf_url = await self._find_pdf_url_candidates(page)
        if pdf_url:
            self.log("[download] attempting iframe/embed url:", pdf_url)
            saved = await self._save_pdf_direct(pdf_url)
            if saved:
                return saved

        self.log("[download] viewer click + direct fetch failed")
        return None

    def _invoicecloud_direct_pdf_url(self, viewer_url: str) -> Optional[str]:
        """
        InvoiceCloud PDF viewer URLs look like:
          /templates/cogsdale/cogsdalepdfviewer.aspx?InvoiceGUID=...&...&pdf=1
        Force pdf=1 and hit that URL directly.
        """
        try:
            u = urlparse(viewer_url)
            if not u.netloc.lower().endswith("invoicecloud.com"):
                return None
            if "pdfviewer.aspx" not in u.path.lower():
                return None
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            q["pdf"] = "1"
            new_q = urlencode(q, doseq=True)
            return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
        except Exception:
            return None

    # -------------- Locator helpers --------------

    async def _find_pdf_url_candidates(self, page: Page) -> Optional[str]:
        try:
            urls = await page.evaluate("""
                () => {
                  const out = [];
                  const els = Array.from(document.querySelectorAll('embed, object, iframe'));
                  for (const el of els) {
                    const src = el.src || el.getAttribute('data') || el.getAttribute('src') || '';
                    if (!src) continue;
                    if (/\\.pdf(\\?|$)/i.test(src) || /invoice/i.test(src) || /document/i.test(src))
                      out.push(src);
                  }
                  return out;
                }
            """)
            for u in urls or []:
                return urljoin(page.url, u)
        except Exception:
            pass
        return None

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

    def _write_bytes(self, suggested: str, data: bytes) -> str:
        target = (self.download_dir / suggested).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        target.write_bytes(data)
        self.log("[download] wrote bytes:", str(target))
        return str(target)


# ---------------- PDF parsing ----------------

def extract_amount_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[int]]:
    # Try both extractors; keep raw newlines for line-based heuristics
    text_a = _extract_text_pdfminer(pdf_path)
    text_b = _extract_text_pypdf(pdf_path)
    text = (text_a or "") if (text_a and len(text_a) >= len(text_b or "")) else (text_b or "")
    if not text.strip():
        return None, None

    # Normalized versions for regex
    # Keep newlines; also build a "squashed" version where multiple spaces collapse
    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    money_loose = r"(\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
    money_strict = r"(\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"  # must have $
    labels = [
        r"TOTAL\s+DUE",
        r"AMOUNT\s+DUE",
        r"TOTAL\s+AMOUNT\s+DUE",
        r"CURRENT\s+CHARGES",
    ]

    # 1) Targeted regex: label then amount (or amount then label), allow generous distance
    for lbl in labels:
        for pat in [
            rf"{lbl}[\s:\-]*{money_loose}",
            rf"{lbl}[\s\S]{{0,500}}?{money_loose}",
            rf"{money_loose}[\s\S]{{0,120}}?{lbl}",
        ]:
            m = re.search(pat, squashed, re.I)
            if m:
                raw = m.group(1)
                clean = raw.replace("$", "").replace(",", "").strip()
                try:
                    cents = int(round(float(clean) * 100))
                except Exception:
                    cents = None
                return clean, cents

    # 2) Line-based heuristic: find a label line, then grab the nearest $ amount
    lines = [ln.strip() for ln in text.splitlines()]
    def find_dollar(s: str) -> Optional[str]:
        m = re.search(money_strict, s)
        return m.group(1) if m else None

    for i, ln in enumerate(lines):
        u = ln.upper()
        if any(re.search(lbl, u, re.I) for lbl in labels):
            # same line
            hit = find_dollar(ln)
            if not hit:
                # or next few lines (tables often wrap)
                for j in range(1, 5):
                    if i + j < len(lines):
                        hit = find_dollar(lines[i + j])
                        if hit:
                            break
            if hit:
                clean = hit.replace("$", "").replace(",", "").strip()
                try:
                    cents = int(round(float(clean) * 100))
                except Exception:
                    cents = None
                return clean, cents

    # 3) Fallback: last $X.XX in doc
    dollars = [m.group(0) for m in re.finditer(r"\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})", squashed)]
    if dollars:
        last = dollars[-1]
        clean = last.replace("$", "").replace(",", "").strip()
        try:
            cents = int(round(float(clean) * 100))
        except Exception:
            cents = None
        return clean, cents

    # 4) nothing found
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


def extract_fdwd_dates_from_pdf(
    pdf_path: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extract Bill Date, Due Date, and service period start/end from an FDWD invoice PDF.

    Mapping:
      - statement_date = Bill Date
      - due_date       = Due Date
      - period_start   = first 'from' date in SERVICE DATES section
      - period_end     = last 'to' date in SERVICE DATES section
                         (if only one row, both are from that line)
    """
    # Get full text using same strategy as amount extractor
    text_a = _extract_text_pdfminer(pdf_path) or ""
    text_b = _extract_text_pypdf(pdf_path) or ""
    text = text_a if len(text_a) >= len(text_b) else text_b
    if not text.strip():
        return None, None, None, None

    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")

    # ---------- Header section (for Bill / Due) ----------
    # Take everything before the first "SERVICE DATES" as the header block
    header = text
    idx = text.upper().find("SERVICE DATES")
    if idx != -1:
        header = text[:idx]

    header_dates: List[str] = [m.group(0) for m in date_re.finditer(header)]

    statement_date: Optional[str] = None
    due_date: Optional[str] = None

    if header_dates:
        statement_date = _normalize_mmddyyyy(header_dates[0])
    if len(header_dates) >= 2:
        due_date = _normalize_mmddyyyy(header_dates[1])

    # ---------- Service period: SERVICE DATES table ----------
    period_start = period_end = None

    # Scope to the section after "SERVICE TYPE SERVICE DATES" and before "ACCOUNT ACTIVITY" etc.
    m_seg = re.search(
        r"SERVICE\s+TYPE\s+SERVICE\s+DATES(.*?)(?:ACCOUNT\s+ACTIVITY|ACCOUNT\s+ACTIVITY\s+INFORMATION|KEEP\s+THIS\s+PORTION|TOTAL\s+DUE)",
        text,
        re.I | re.S,
    )
    segment = m_seg.group(1) if m_seg else text

    # Find all ranges like "06/05/2025 - 09/05/2025"
    pairs = re.findall(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
        segment,
    )

    # Fallback: if none found in the scoped segment, try the whole text
    if not pairs:
        pairs = re.findall(
            r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
            text,
        )

    if pairs:
        # Top line "from" => overall period_start
        period_start = _normalize_mmddyyyy(pairs[0][0])
        # Bottom line "to" => overall period_end
        period_end = _normalize_mmddyyyy(pairs[-1][1])

    return statement_date, due_date, period_start, period_end

# ---------------- CLI ----------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="FDWD latest invoice downloader (+amount extractor)")
    p.add_argument("--username", default=os.getenv("FDWD_USERNAME", ""), help="FDWD username")
    p.add_argument("--password", default=os.getenv("FDWD_PASSWORD", ""), help="FDWD password")
    p.add_argument("--address", required=True, help="Address to match on the Account page")
    p.add_argument("--headful", action="store_true", help="Show browser window (headless=false)")
    p.add_argument("--slow-mo", type=int, default=int(os.getenv("FDWD_SLOW_MO_MS", "0")), help="Slow motion (ms)")
    p.add_argument("--json", action="store_true", help="Print JSON result")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = Config(
        username=args.username,
        password=args.password,
        address_query=args.address,
        headless=not args.headful,
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
    )
    async with FDWDScraper(cfg) as s:
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
