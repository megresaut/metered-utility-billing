#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WPCA (City of Bridgeport - WPCA via doxo) scraper

Flow:
- Go to biller page (public doxo profile)
- Click "Sign In"
- Fill login modal (username/password) -> Secure Login
- After login, click "Bills" in the top navbar
- On Bills page, find "City of Bridgeport, Connecticut - WPCA"
- Click "Pay"
- Scrape green amount on Pay page
- Open bill PDF viewer, download PDF
- Output amount (+ optional JSON bundle)
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple
from datetime import datetime
import pdfplumber

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
    APIResponse,
)

DOXO_WPCA_URL = "https://www.doxo.com/u/biller/city-of-bridgeport-17B2EB0"
CURRENCY_RE = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})")
DEFAULT_DL_DIR = Path(
    os.getenv("WPCA_DOWNLOAD_DIR", "/handoff/wpca")
).resolve()


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
    nav_timeout_ms: int = 35000
    debug: bool = False
    json_mode: bool = False


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None          # "3431.01"
    amount_cents: Optional[int] = None    # 343101
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None
    statement_date: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    due_date: Optional[str] = None

def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("WPCA_USERNAME") or "",
        password=args.password or os.getenv("WPCA_PASSWORD") or "",
        headless=not bool(args.headful) if args.headful is not None else _env_bool("WPCA_HEADLESS", True),
        slow_mo_ms=int(os.getenv("WPCA_SLOW_MO_MS", str(args.slow_mo or 0))),
        debug=bool(args.debug) or _env_bool("WPCA_DEBUG", False),
        json_mode=bool(args.json),
    )



# ---------------- Date helpers ----------------

def _to_date(date_str: str) -> str:
    return datetime.strptime(
        date_str.replace(" ", ""),
        "%m/%d/%Y"
    ).strftime("%Y-%m-%d")


# ---------------- Regex patterns ----------------

USAGE_PERIOD_RE = re.compile(
    r"\b(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\b"
)

INVOICE_DATE_RE = re.compile(
    r"INVOICE\s*DATE[\s\S]{0,40}?(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4})",
    re.I,
)

LIEN_DATE_RE = re.compile(
    r"LIEN\s*DATE[\s\S]{0,40}?(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4})",
    re.I,
)

# ---------------- Individual extractors ----------------

def extract_usage_period(pdf_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extracts:
      - period_start (USAGE PERIOD FROM)
      - period_end   (USAGE PERIOD TO)
    """
    m = USAGE_PERIOD_RE.search(pdf_text)
    if not m:
        return None, None

    start_raw, end_raw = m.groups()
    return _to_date(start_raw), _to_date(end_raw)


def extract_wpca_invoice_and_lien_dates(pdf_path: str):
    import pdfplumber
    from datetime import datetime

    def to_date(s):
        return datetime.strptime(s.replace(" ", ""), "%m/%d/%Y").strftime("%Y-%m-%d")

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]

        # Extract words WITH coordinates
        words = page.extract_words(use_text_flow=True)

    invoice_date = None
    lien_date = None

    for w in words:
        text = w["text"]

        # Match date-like text
        if not re.match(r"\d{1,2}/\d{1,2}/\d{4}", text):
            continue

        x = w["x0"]
        y = w["top"]

        # 🔹 THESE RANGES ARE BASED ON YOUR PDF LAYOUT
        # Invoice Date: left box
        if 50 < x < 300 and 50 < y < 200:
            invoice_date = to_date(text)

        # Lien Date: right box
        if 300 < x < 600 and 50 < y < 200:
            lien_date = to_date(text)

    return invoice_date, lien_date




# ---------------- Master extractor ----------------

def extract_wpca_dates(pdf_path: str) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
]:
    """
    Returns:
      (statement_date, period_start, period_end, due_date)
    """
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    period_start, period_end = extract_usage_period(text)
    statement_date, due_date = extract_wpca_invoice_and_lien_dates(pdf_path)

    return statement_date, period_start, period_end, due_date


class WPCAScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pdf_candidates: List[Tuple[str, str, int]] = []

    # ---------------- Utility ----------------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    async def __aenter__(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
        )
        self.context = await self.browser.new_context(accept_downloads=True)
        self.page = await self.context.new_page()

        if self.cfg.debug:
            self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))

        # grab pdf-ish responses so we have a direct-download fallback
        self.context.on("response", self._capture_pdf)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            await self._pw.stop()

    def _capture_pdf(self, response):
        try:
            url = response.url
            ct = (response.headers or {}).get("content-type", "")
            if ("application/pdf" in ct.lower()) or re.search(r"\.pdf(\?|$)", url, re.I):
                self._pdf_candidates.append((url, ct, response.status))
                self._pdf_candidates[:] = self._pdf_candidates[-10:]
                self.log("[net][pdf] captured:", url)
        except Exception:
            pass

    # ---------------- Main orchestration ----------------
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        page = self.page

        self.log("[step] goto biller page")
        try:
            await page.goto(DOXO_WPCA_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            return Result(ok=False, error="Timeout opening doxo WPCA page")

        self.log("[step] click Sign In")
        await self._click_sign_in_header(page)

        self.log("[step] login modal")
        ok = await self._login_modal(page, self.cfg.username, self.cfg.password)
        if not ok:
            return Result(ok=False, error="Failed to login via modal")

        self.log("[step] nav to Bills tab")
        bills_page = await self._goto_bills_tab()
        if not bills_page:
            return Result(ok=False, error="Could not open Bills tab after login")

        self.log("[step] open Pay page for WPCA")
        pay_page = await self._open_wpca_pay_page()
        if not pay_page:
            return Result(ok=False, error="Could not open WPCA Pay page")

        self.log("[step] scrape amount due")
        amount_str, amount_cents = await self._extract_green_amount(pay_page)
        self.log("[amount]", amount_str, amount_cents)
        if not amount_str:
            return Result(ok=False, error="Could not extract amount due")

        self.log("[step] open full PDF viewer tab")
        viewer_page = await self._open_pdf_viewer_from_inline(pay_page)
        final_url = viewer_page.url if viewer_page else pay_page.url
        self.log("[viewer-url]", final_url)

        pdf_path = None
        if viewer_page:
            self.log("[step] attempt viewer download")
            pdf_path = await self._download_from_viewer(viewer_page)
            if not pdf_path:
                pdf_path = await self._fetch_pdf_direct(viewer_page)

        statement_date = None
        period_start = None
        period_end = None
        due_date = None

        if pdf_path:
            self.log("[step] extract dates from PDF")
            (
                statement_date,
                period_start,
                period_end,
                due_date,
            ) = extract_wpca_dates(pdf_path)

            self.log(
                "[dates]",
                "statement_date=", statement_date,
                "period_start=", period_start,
                "period_end=", period_end,
                "due_date=", due_date,
            )

        return Result(
            ok=True,
            amount=amount_str,
            amount_cents=amount_cents,
            pdf_path=pdf_path,
            final_url=final_url,
            statement_date=statement_date,
            period_start=period_start,
            period_end=period_end,
            due_date=due_date,
        )

    # ---------------- Step helpers ----------------

    async def _click_sign_in_header(self, page: Page):
        """
        Clicks the "Sign In" button/link in the header of the public WPCA page.
        """
        for c in [
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.get_by_role("link", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('text="Sign In"'),
        ]:
            try:
                if await c.count() > 0:
                    await c.first.click()
                    return
            except Exception:
                continue

    async def _login_modal(self, page: Page, username: str, password: str) -> bool:
        """
        Updated version: detect that the login popup is actually in an <iframe>.
        We'll search all frames for a form that has input[name="username"] and input[type="password"].
        Then we fill/submit INSIDE THAT FRAME.

        After submit, we still watch the top-level page for the post-login navbar ("Bills", etc.).
        """

        async def _header_logged_in() -> bool:
            # Post-login navbar with "Bills" / "Wallet"
            if await page.get_by_role("link", name=re.compile(r"\bBills\b", re.I)).count() > 0:
                return True
            avatar_guess = page.locator("button,div").filter(
                has_text=re.compile(r"^[A-Z]$", re.I)
            ).filter(has_not_text=re.compile(r"Sign\s*In", re.I))
            if await avatar_guess.count() > 0 and await page.get_by_role(
                "link", name=re.compile(r"\bWallet\b", re.I)
            ).count() > 0:
                return True
            return False

        # maybe we're already logged in (cached session)
        if await _header_logged_in():
            self.log("[login_modal] already logged in before modal work")
            return True

        # 1. Find the frame that actually hosts the login form
        auth_frame = None
        for _ in range(40):  # retry a bit while iframe mounts
            for fr in page.frames:
                try:
                    # does this frame visibly contain username + password fields?
                    u_loc = fr.locator('input[name="username"], input#username, input[placeholder="Username"]')
                    p_loc = fr.locator('input[type="password"], input[name="password"]')
                    if await u_loc.count() > 0 and await p_loc.count() > 0:
                        # check they're at least attached/displayed
                        try:
                            await u_loc.first.wait_for(state="visible", timeout=200)
                            await p_loc.first.wait_for(state="visible", timeout=200)
                            auth_frame = fr
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
            if auth_frame:
                break
            await page.wait_for_timeout(250)

        if not auth_frame:
            # iframe never detected. maybe its not in an iframe after all,
            # or we raced it. as fallback, try top-level page (old logic).
            self.log("[login_modal] no auth iframe found, fallback to page-scoped form search")

            # --- fallback old logic ---
            modal_form = None
            for _ in range(20):
                candidate_forms = page.locator("form")
                count = await candidate_forms.count()
                for i in range(count):
                    f = candidate_forms.nth(i)
                    u_loc = f.locator('input[name="username"], input#username, input[placeholder="Username"]')
                    p_loc = f.locator('input[type="password"], input[name="password"]')
                    if await u_loc.count() > 0 and await p_loc.count() > 0:
                        try:
                            await u_loc.first.wait_for(state="visible", timeout=200)
                            await p_loc.first.wait_for(state="visible", timeout=200)
                            modal_form = f
                            break
                        except Exception:
                            continue
                if modal_form:
                    break
                await page.wait_for_timeout(250)

            if not modal_form:
                if await _header_logged_in():
                    self.log("[login_modal] header visible, assuming signed in")
                    return True
                self.log("[login_modal] login form not found in page or iframe")
                return False

            user_el = modal_form.locator('input[name="username"], input#username, input[placeholder="Username"]')
            pass_el = modal_form.locator('input[type="password"], input[name="password"]')

            try:
                await user_el.first.fill(username)
                await pass_el.first.fill(password)
            except Exception as e:
                self.log("[login_modal] fill error (fallback):", repr(e))
                return False

            submit_candidates = [
                modal_form.get_by_role("button", name=re.compile(r"Secure\s*Login", re.I)),
                modal_form.get_by_role("button", name=re.compile(r"Log\s*In", re.I)),
                modal_form.locator('button[type="submit"]'),
                modal_form.locator('input[type="submit"]'),
            ]

            clicked = False
            for cand in submit_candidates:
                if await cand.count() > 0:
                    try:
                        await cand.first.click()
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                try:
                    await pass_el.first.press("Enter")
                    clicked = True
                except Exception as e:
                    self.log("[login_modal] submit fallback err (fallback):", repr(e))
                    return False

        else:
            self.log("[login_modal] using auth iframe:", auth_frame.url)

            # 2. Within the iframe: fill username/password
            user_el = auth_frame.locator('input[name="username"], input#username, input[placeholder="Username"]')
            pass_el = auth_frame.locator('input[type="password"], input[name="password"]')

            try:
                await user_el.first.fill(username)
                await pass_el.first.fill(password)
            except Exception as e:
                self.log("[login_modal] iframe fill error:", repr(e))
                return False

            # 3. Click submit button INSIDE the iframe's form
            submit_candidates = [
                auth_frame.get_by_role("button", name=re.compile(r"Secure\s*Login", re.I)),
                auth_frame.get_by_role("button", name=re.compile(r"Log\s*In", re.I)),
                auth_frame.locator('button[type="submit"]'),
                auth_frame.locator('input[type="submit"]'),
            ]

            clicked = False
            for cand in submit_candidates:
                if await cand.count() > 0:
                    try:
                        await cand.first.click()
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                try:
                    await pass_el.first.press("Enter")
                    clicked = True
                except Exception as e:
                    self.log("[login_modal] iframe submit fallback err:", repr(e))
                    return False

        # 4. After submit, watch the TOP PAGE for logged-in navbar (Bills/etc.)
        for _ in range(60):
            if await _header_logged_in():
                self.log("[login_modal] login success (navbar visible)")
                return True
            await page.wait_for_timeout(500)

        self.log("[login_modal] navbar never appeared after submit")
        return False

    async def _goto_bills_tab(self) -> Optional[Page]:
        """
        Click "Bills" in the header navbar and wait for the Bills dashboard
        (the one with rows of billers + Mark Paid / Pay).
        """
        page = self.page
        bills_link = None
        for _ in range(20):
            cand = page.get_by_role("link", name=re.compile(r"^\s*Bills\s*$", re.I))
            if await cand.count() > 0:
                bills_link = cand.first
                break
            await page.wait_for_timeout(250)

        if not bills_link:
            self.log("[goto_bills] Bills link not found in header")
            return None

        await bills_link.click()
        # wait for WPCA row to load
        for _ in range(40):
            wpca_card = page.locator("css=div,section,article,li").filter(
                has_text=re.compile(r"City of Bridgeport,\s*Connecticut\s*-\s*WPCA", re.I)
            )
            if await wpca_card.count() > 0:
                self.log("[goto_bills] bills dashboard detected")
                return page
            await page.wait_for_timeout(250)

        self.log("[goto_bills] bills dashboard never fully rendered")
        return None

    async def _open_wpca_pay_page(self) -> Optional[Page]:
        """
        Bills page:
        - Lock onto the 'City of Bridgeport, Connecticut - WPCA' bill row
        - Click THAT row's 'Pay' button (not Eversource's)
        - Wait for the Pay page load
        """

        page = self.page

        # 1. Grab any container that mentions WPCA.
        wrapper = page.locator("div,section,article,li").filter(
            has_text=re.compile(r"City of Bridgeport,\s*Connecticut\s*-\s*WPCA", re.I)
        )

        if await wrapper.count() == 0:
            self.log("[wpca_pay] couldn't find any container with WPCA text")
            return None

        # 2. Drill down to the *smallest* node that still has that text.
        candidate = wrapper.first
        for _ in range(3):
            inner = candidate.locator("div,section,article,li").filter(
                has_text=re.compile(r"City of Bridgeport,\s*Connecticut\s*-\s*WPCA", re.I)
            )
            if await inner.count() > 0:
                # pick the one with shortest inner_text length (closest/tightest match)
                best_idx = 0
                best_len = None
                inner_count = await inner.count()
                for i in range(min(inner_count, 10)):
                    node = inner.nth(i)
                    try:
                        t = (await node.inner_text()) or ""
                    except Exception:
                        t = ""
                    l = len(t)
                    if best_len is None or l < best_len:
                        best_len = l
                        best_idx = i
                candidate = inner.nth(best_idx)
            else:
                break

        # At this point `candidate` might be *too* tight (left half of the card, without buttons).
        # So 3. Walk UPWARD to find the nearest ancestor that actually has a Pay button.
        async def find_row_with_pay(start_loc):
            cur = start_loc
            for _ in range(5):  # climb up a handful of times max
                pay_try = cur.get_by_role(
                    "button", name=re.compile(r"^\s*Pay\s*$", re.I)
                ).or_(
                    cur.get_by_role("link", name=re.compile(r"^\s*Pay\s*$", re.I))
                )
                if await pay_try.count() > 0:
                    return cur, pay_try
                # climb: parent() gives us the DOM parent of the first element in this locator
                cur = cur.locator("xpath=..")
            return None, None

        row_loc, pay_btn = await find_row_with_pay(candidate)
        if not row_loc or not pay_btn:
            self.log("[wpca_pay] couldn't climb to a row that has a Pay button")
            # dump some debug
            try:
                dbg_txt = await candidate.inner_text()
                self.log("[wpca_pay] innermost candidate text:\n", dbg_txt[:500])
            except Exception as e:
                self.log("[wpca_pay] couldn't dump candidate text:", repr(e))
            return None

        # 4. Click Pay.
        self.log("[wpca_pay] clicking Pay now")
        await pay_btn.first.click()

        # 5. Wait for the Pay page (green amount + inline pdf viewer).
        try:
            await page.wait_for_load_state("networkidle", timeout=self.cfg.nav_timeout_ms)
        except Exception:
            pass

        return page

    async def _extract_green_amount(self, pay_page: Page) -> Tuple[Optional[str], Optional[int]]:
        """
        On the Pay page, there's a card:
            Most recent amount due:
            $3,431.01
        We'll normalize to "3431.01" and also return cents.
        """
        try:
            body_text = await pay_page.inner_text("body")
        except Exception:
            body_text = ""
        m = CURRENCY_RE.search(body_text or "")
        if not m:
            return None, None
        raw_amt = m.group(0)  # "$3,431.01"
        clean_amt = raw_amt.replace("$", "").replace(",", "")  # "3431.01"
        try:
            cents = int(round(float(clean_amt) * 100))
        except Exception:
            cents = None
        return clean_amt, cents

    async def _open_pdf_viewer_from_inline(self, pay_page: Page) -> Optional[Page]:
        """
        On the WPCA Pay page:
        - There's a thumbnail/preview of the bill inside a <div class="document__preview">,
          wrapped in an <a ... target="_blank" title="View full document">.
        - Clicking that <a> opens a new tab with the full bill document.
        - Older assumptions about <iframe> don't hold here, so we prefer clicking that link.
        - If that fails, we fall back to trying iframe/embed/object clicks.

        Returns the Page that shows the full bill (new tab if opened),
        or pay_page if navigation happened in-place.
        """

        ctx = self.context
        before_pages = list(ctx.pages)

        # --- Preferred path: click the preview <a> around the bill image ---
        try:
            preview_link = pay_page.locator(
                ".document__preview a[title*='View full document'], "
                ".document__preview a[target='_blank']"
            )

            if await preview_link.count() > 0:
                self.log("[viewer] found preview link, clicking to open full doc tab")
                await preview_link.first.scroll_into_view_if_needed()
                await preview_link.first.click()

                # wait for a new tab to appear
                for _ in range(20):
                    current_pages = list(ctx.pages)
                    if len(current_pages) > len(before_pages):
                        newp = current_pages[-1]
                        try:
                            await newp.wait_for_load_state("domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                        except Exception:
                            pass
                        self.log("[viewer] new tab opened for viewer:", newp.url)
                        return newp
                    await pay_page.wait_for_timeout(250)

                # maybe same-tab nav instead
                self.log("[viewer] preview link clicked but no new tab detected, returning pay_page")
                return pay_page
        except Exception as e:
            self.log("[viewer] thumbnail link path failed:", repr(e))

        # --- Fallback path: try generic iframe/embed/object click ---
        before_pages = list(ctx.pages)
        for sel in [
            "iframe",
            "embed[type='application/pdf']",
            "object[type='application/pdf']",
        ]:
            loc = pay_page.locator(sel)
            if await loc.count() > 0:
                try:
                    self.log("[viewer] trying generic pdf iframe/embed/object click:", sel)
                    await loc.first.scroll_into_view_if_needed()
                    await loc.first.click(timeout=4000)
                    break
                except Exception as e:
                    self.log("[viewer] click failed for", sel, "err:", repr(e))

        # wait again for new tab after fallback click
        for _ in range(20):
            current_pages = list(ctx.pages)
            if len(current_pages) > len(before_pages):
                newp = current_pages[-1]
                try:
                    await newp.wait_for_load_state("domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                except Exception:
                    pass
                self.log("[viewer] new tab opened (fallback path):", newp.url)
                return newp
            await pay_page.wait_for_timeout(250)

        self.log("[viewer] no new tab; returning pay_page as fallback")
        return pay_page

    async def _download_from_viewer(self, viewer_page: Page) -> Optional[str]:
        """
        Try to actually download the PDF from a Chrome-style <pdf-viewer>.
        If there's no Chrome pdf-viewer toolbar, we'll just fall back to direct fetch.
        """

        chrome_viewer_script = """
        () => {
          const pv = document.querySelector('pdf-viewer');
          if (!pv) return false;
          const tb = pv.shadowRoot?.querySelector('#toolbar');
          if (!tb) return false;
          const end = tb.shadowRoot?.querySelector('#end') || tb.querySelector('#end');
          if (!end) return false;

          let dlBtn = null;
          const controls = end.querySelector('viewer-download-controls');
          if (controls && controls.shadowRoot) {
            dlBtn = controls.shadowRoot.querySelector('#download');
          }
          if (!dlBtn) {
            dlBtn = end.querySelector('cr-icon-button#download,[aria-label="Download"],[title="Download"]');
          }
          if (!dlBtn) return false;
          dlBtn.click();
          return true;
        }
        """

        try:
            async with viewer_page.expect_download(timeout=5000) as dl_info:
                clicked = await viewer_page.evaluate(chrome_viewer_script)
                if not clicked:
                    raise Exception("no clickable download button in viewer shadow DOM (not Chrome pdf-viewer)")
            dl: Download = await dl_info.value

            suggested = dl.suggested_filename or "wpca-bill.pdf"
            target = (self.download_dir / suggested).resolve()

            i, stem, suf = 1, target.stem, target.suffix
            while target.exists():
                target = target.with_name(f"{stem}-{i}{suf}")
                i += 1

            await dl.save_as(str(target))
            self.log("[download] saved:", target)
            return str(target)

        except Exception as e:
            self.log("[viewer][chrome] download via built-in viewer failed:", repr(e))
            return None

    async def _fetch_pdf_direct(self, viewer_page: Page) -> Optional[str]:
        """
        Fallback: try to GET the PDF bytes directly using either:
        - any URL in DOM that looks like a PDF/doc preview,
        - or a captured application/pdf response.
        """
        url = None

        # scrape the DOM for likely PDF URLs or doxo doc preview URLs
        try:
            urls = await viewer_page.evaluate(
                """() => {
                    const out = new Set();
                    const grab = el => {
                      for (const attr of ['href','src','data']) {
                        const v = el.getAttribute(attr);
                        if (v) out.add(v);
                      }
                    };
                    document.querySelectorAll('a[href], iframe[src], embed[src], object[data]').forEach(grab);
                    return Array.from(out);
                }"""
            )
        except Exception:
            urls = []

        base = viewer_page.url
        import urllib.parse
        for u in urls or []:
            absu = urllib.parse.urljoin(base, u)
            if re.search(r"\.pdf(\?|$)", absu, re.I) or re.search(r"/documents/\d+/.+inline=true", absu, re.I):
                url = absu
                break

        # if DOM scrape failed, fall back to sniffed network responses we captured
        if not url and self._pdf_candidates:
            best = sorted(
                self._pdf_candidates,
                key=lambda t: (("application/pdf" not in (t[1] or "").lower()), t[2] != 200),
            )[0]
            url = best[0]

        if not url:
            self.log("[pdf] fallback: no candidate pdf URL found in viewer_page or network capture")
            return None

        self.log("[pdf] attempting direct GET:", url)
        resp: APIResponse = await self.context.request.get(url)
        if not resp.ok:
            self.log("[pdf] fetch failed status:", resp.status)
            return None

        cd = resp.headers.get("content-disposition", "") or ""
        filename = None
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.I)
        if m:
            filename = os.path.basename(m.group(1))

        if not filename:
            parsed = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed.path) or "wpca-bill.pdf"
            if not filename.lower().endswith(".pdf"):
                filename += ".pdf"

        target = (self.download_dir / filename).resolve()
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1

        data = await resp.body()
        target.write_bytes(data)
        self.log("[pdf] saved direct to:", str(target))
        return str(target)


# ---------------- CLI ----------------
def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="WPCA (Bridgeport WPCA via doxo) scraper")
    p.add_argument("--username", help="doxo username/email")
    p.add_argument("--password", help="doxo password")
    p.add_argument("--headful", action="store_true", help="Show browser (headless=false)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion ms between steps")
    p.add_argument("--json", action="store_true", help="Print JSON result instead of just the amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)

    async with WPCAScraper(cfg) as s:
        r = await s.run()

    if not r.ok:
        print(f"ERROR: {r.error}", file=sys.stderr)
        sys.exit(1)

    if cfg.json_mode:
        print(json.dumps({
            "amount": r.amount,
            "amount_cents": r.amount_cents,
            "statement_date": r.statement_date,
            "period_start": r.period_start,
            "period_end": r.period_end,
            "due_date": r.due_date,
            "pdf_path": r.pdf_path,
            "final_url": r.final_url,
        }, indent=2))
    else:
        print(r.amount or "")


if __name__ == "__main__":
    asyncio.run(main())
