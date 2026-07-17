"""
Eversource property amount extractor + PDF downloader (Account History path)

Flow:
- Go to https://www.eversource.com/cg/customer/Account#/select
- Login with Email/Username + Password
- If "Two-step verification" prompt appears, click "Ask Me Again Later"
- Wait for the Account Overview list
- Find the property whose address contains --address (case-insensitive)
- Extract the amount from THAT row
- Click "Acct Details" → "Past Bills & Payments" (Account History)
- In the table, pick the most recent row with Type = "Bill" and click VIEW
- In the full-page viewer, click Download (PDF.js first, then Chrome-viewer shadow fallback)
- If that fails, try network/DOM discovery and download bytes directly

Output:
- Default: prints ONLY the amount (e.g., 751.54)
- With --json: {"amount":"751.54","amount_cents":75154,"pdf_path":"/abs/path/file.pdf","final_url":"..."}
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from pypdf import PdfReader

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
    APIResponse,
)

from common.stealth import COMPREHENSIVE_STEALTH_JS, LAUNCH_ARGS

LOGIN_URL = "https://www.eversource.com/cg/customer/Account#/select"
CURRENCY_RE = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})")
HANDOFF_ROOT = Path(os.getenv("RA_HANDOFF_DIR", "/handoff"))
DEFAULT_DL_DIR = (HANDOFF_ROOT / "eversource").resolve()

# DEFAULT_DL_DIR = Path("./downloads/eversource").resolve()

def parse_eversource_pdf(pdf_path: str) -> dict:
    """
    Parse a downloaded Eversource bill PDF and extract:
    - amount
    - amount_cents
    - due_date
    - statement_date
    - period_start
    - period_end
    """
    info = {
        "amount": None,
        "amount_cents": None,
        "due_date": None,
        "statement_date": None,
        "period_start": None,
        "period_end": None,
    }

    reader = PdfReader(str(pdf_path))

    # ---- Extract text from all pages ----
    texts = []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
    raw = "\n".join(texts)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", raw)
    text = re.sub(r"\s*\n\s*", " ", text)

    # --------------------------------------------------
    # Statement Date
    # --------------------------------------------------
    m = re.search(
        r"Sta\s*tement\s+Da\s*te:.*?(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        info["statement_date"] = m.group(1)

    # --------------------------------------------------
    # Service Period
    # --------------------------------------------------
    m = re.search(
        r"Ser\s*vice\s+from\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        info["period_start"] = m.group(1)
        info["period_end"] = m.group(2)

    # --------------------------------------------------
    # Amount + Due Date
    #
    # pypdf splits words across lines in Eversource PDFs (kerning artifact),
    # so after our whitespace collapse we see things like:
    #   "Amount no w due b y 06/05/26"  (i.e. "now" → "no w", "by" → "b y")
    #
    # Layouts we've seen:
    #   A) Autopay bill: "$49.72 Payment will be sent to bank ... on 02/01/26"
    #   B) Autopay reversed: "for processing on 03/21/26 ... bank $2,259.61"
    #   C) Manual-pay header: "$130.88 Amount now due by 06/05/26"
    #   D) Manual-pay stub:   "by 06/05/26 Amount now due $130.88"
    #
    # All regexes below tolerate split "no\s*w" and "b\s*y".
    # --------------------------------------------------
    amount_raw = None
    due_raw = None

    # A) Autopay forward
    m = re.search(
        r"\$([0-9,]+\.[0-9]{2})\s+Payment will be sent to bank.*?on\s+(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        amount_raw, due_raw = m.group(1), m.group(2)

    # B) Autopay reversed
    if not amount_raw:
        m = re.search(
            r"for processing on\s+(\d{2}/\d{2}/\d{2})\s+Payment will be sent to bank\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(2), m.group(1)

    # C) Manual-pay: "$AMT Amount now due by DATE" (most common, appears on each page header)
    if not amount_raw:
        m = re.search(
            r"\$([0-9,]+\.[0-9]{2})\s+Amount\s+no\s*w\s+due\s+b\s*y\s+(\d{2}/\d{2}/\d{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(1), m.group(2)

    # D) Manual-pay reversed (mailing stub): "by DATE ... Amount now due $AMT"
    if not amount_raw:
        m = re.search(
            r"b\s*y\s+(\d{2}/\d{2}/\d{2})\s+Amount\s+no\s*w\s+due\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(2), m.group(1)

    # E) "Amount now due by DATE <up to 150 chars> $AMT" (date precedes amount in text flow)
    if not amount_raw:
        m = re.search(
            r"Amount\s+no\s*w\s+due\s+b\s*y\s+(\d{2}/\d{2}/\d{2})[\s\S]{0,150}?\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(2), m.group(1)

    # Standalone fallbacks
    if not amount_raw:
        amt_m = re.search(
            r"\$([0-9,]+\.[0-9]{2})\s+T\s*otal\s+Amount\s+Due",
            text,
            re.IGNORECASE,
        )
        if amt_m:
            amount_raw = amt_m.group(1)
    if not amount_raw:
        # "Total Current Charges $130.88" or "$130.88 Total Current Charges"
        amt_m = re.search(
            r"T\s*otal\s+Current\s+Charges\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if not amt_m:
            amt_m = re.search(
                r"\$([0-9,]+\.[0-9]{2})\s+T\s*otal\s+Current\s+Charges",
                text,
                re.IGNORECASE,
            )
        if amt_m:
            amount_raw = amt_m.group(1)

    if not due_raw:
        due_m = re.search(
            r"for processing on\s+(\d{2}/\d{2}/\d{2})",
            text,
            re.IGNORECASE,
        )
        if not due_m:
            # "Amount now due by DATE" — scope due-date lookup to that phrase
            # so we don't accidentally grab some other "by DATE" string.
            due_m = re.search(
                r"Amount\s+no\s*w\s+due\s+b\s*y\s+(\d{2}/\d{2}/\d{2})",
                text,
                re.IGNORECASE,
            )
        if due_m:
            due_raw = due_m.group(1)

    if amount_raw:
        clean = amount_raw.replace(",", "")
        info["amount"] = clean
        info["due_date"] = due_raw
        try:
            info["amount_cents"] = int(round(float(clean) * 100))
        except Exception:
            info["amount_cents"] = None

    return info


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    username: str
    password: str
    address_query: str
    account_number: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount: Optional[str] = None          # final amount (we'll override from PDF if we can)
    amount_cents: Optional[int] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None
    # new fields parsed from the PDF
    statement_date: Optional[str] = None
    due_date: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None


def build_config(args) -> Config:
    return Config(
        username=args.username or os.getenv("EVERSOURCE_USERNAME") or "",
        password=args.password or os.getenv("EVERSOURCE_PASSWORD") or "",
        address_query=args.address,
        account_number=args.account_number or os.getenv("EVERSOURCE_ACCOUNT_NUMBER"),
        headless=not bool(args.headful) if args.headful is not None else _env_bool("EVERSOURCE_HEADLESS", True),
        slow_mo_ms=int(os.getenv("EVERSOURCE_SLOW_MO_MS", str(args.slow_mo or 0))),
        debug=bool(args.debug) or _env_bool("EVERSOURCE_DEBUG", False),
    )


class EversourceScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pdf_candidates: List[Tuple[str, str, int]] = []

    # --------------- logging helpers ---------------
    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    def log_block(self, tag: str, lines: List[str]):
        if self.cfg.debug:
            print(f"DEBUG[{tag}]", file=sys.stderr)
            for ln in lines:
                print("  " + ln, file=sys.stderr)
            sys.stderr.flush()

    async def __aenter__(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.cfg.headless, slow_mo=self.cfg.slow_mo_ms,
            args=LAUNCH_ARGS,
        )
        # IPRoyal residential proxies (avoid .8. IP — it's unreliable)
        IPROYAL_PROXIES = [
            "http://161.77.201.71:12323",
            "http://161.77.10.71:12323",
            "http://161.77.52.166:12323",
        ]
        proxy = {
            "server": IPROYAL_PROXIES[0],
            "username": "14ae15df3f919",
            "password": "6ef4856e7c",
        }

        self.context = await self.browser.new_context(accept_downloads=True, proxy=proxy)
        await self.context.add_init_script(COMPREHENSIVE_STEALTH_JS)
        self.page = await self.context.new_page()

        # Console → stderr when debugging
        if self.cfg.debug:
            self.page.on("console", lambda m: print(f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr))

        # Capture pdf-ish responses across the context
        def _capture_pdf(response):
            try:
                url = response.url
                ct = (response.headers or {}).get("content-type", "")
                if ("application/pdf" in ct.lower()) or re.search(r"\.pdf(\?|$)", url, re.I):
                    self._pdf_candidates.append((url, ct, response.status))
                    self._pdf_candidates[:] = self._pdf_candidates[-10:]
                    self.log("[net][pdf] captured:", url, "| ct=", ct, "| status=", response.status)
            except Exception:
                pass

        self.context.on("response", _capture_pdf)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        finally:
            await self._pw.stop()

    # ----------------- main -----------------
    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password:
            return Result(ok=False, error="username and password are required")

        if not self.cfg.account_number and not self.cfg.address_query:
            return Result(
                ok=False,
                error="Either --account-number or --address must be provided",
            )

        page = self.page

        self.log("[step] goto login")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        except PWTimeoutError:
            return Result(ok=False, error="Timeout opening login page")

        await self._dismiss_cookie_banner()

        self.log("[step] credential login")
        if not await self._credential_login(self.cfg.username, self.cfg.password):
            return Result(ok=False, error="Failed to submit login form (selectors may need adjustment)")

        self.log("[step] handle two-step prompt if present")
        await self._handle_two_step_prompt()
        await self._dismiss_cookie_banner()

        self.log("[step] wait for account list")
        if not await self._wait_for_account_list(self.cfg.nav_timeout_ms):
            try:
                await page.reload(wait_until="domcontentloaded", timeout=self.cfg.nav_timeout_ms)
                await self._dismiss_cookie_banner()
            except Exception:
                pass
            if not await self._wait_for_account_list(20_000):
                return Result(ok=False, error="Account list did not appear after login (relaxed check)")


        self.log("[step] navigate via Account & Billing → View Bills & Payments")
        if not await self._nav_to_view_bills_and_payments():
            return Result(
                ok=False,
                error="Failed to navigate via Account & Billing → View Bills & Payments",
            )

        # TEMP: hard-coded account number (will become CLI flag next)
        # HARD_CODED_ACCOUNT = "51644459091"

        acct = self.cfg.account_number

        if acct:
            self.log("[step] apply account filter:", acct)
            filter_result = await self._apply_account_filter(acct)

            if filter_result == "NO_RECORDS":
                return Result(
                    ok=True,
                    error=f"No records found for account {acct}",
                )

            if not filter_result:
                return Result(
                    ok=False,
                    error=f"Failed to apply account filter for {acct}",
                )
        else:
            self.log("[step] no account-number provided; address-based flow will run")

        # # Go to details
        # try:
        #     self.log("[step] click Acct Details")
        #     acct_details = card.get_by_role("link", name=re.compile(r"Acct\s*Details|Account\s*Details", re.I))
        #     if await acct_details.count() == 0:
        #         acct_details = card.get_by_role("button", name=re.compile(r"Acct\s*Details|Account\s*Details", re.I))
        #     if await acct_details.count() > 0:
        #         await acct_details.first.scroll_into_view_if_needed()
        #         await acct_details.first.click()
        #         await page.wait_for_load_state("domcontentloaded", timeout=self.cfg.nav_timeout_ms)
        #         await self._dismiss_cookie_banner()
        #         self.log("[nav] details url:", page.url)
        # except Exception as e:
        #     self.log("[warn] Acct Details click failed:", repr(e))

        # # → Past Bills & Payments (Account History)
        # self.log("[step] go to 'Past Bills & Payments' / Account History")
        # ok = await self._go_to_account_history(page)
        # if not ok:
        #     return Result(ok=False, error="Could not navigate to Account History (Past Bills & Payments)")

        # On history page: open newest 'Bill' row (VIEW) into a new page
        # self.log("[step] open newest Bill → VIEW")
        # viewer_page = await self._open_latest_bill_view()
        # final_url = viewer_page.url if viewer_page else page.url
        # self.log("[nav] viewer page url:", final_url)

        # pdf_path = None
        # parsed = {}
        # if viewer_page:
        #     self.log("[step] download from full-page viewer (PDF.js → Chrome viewer → network fallback)")
        #     pdf_path = await self._download_from_full_viewer(viewer_page)
        #     if not pdf_path:
        #         # Last resort: direct fetch from captured/DOM URL
        #         pdf_path = await self._fetch_pdf_direct(viewer_page)

        #     # If we have a PDF, parse fields from it
        #     if pdf_path:
        #         self.log("[step] parsing fields from downloaded PDF")
        #         parsed = self._parse_bill_pdf(pdf_path) or {}

        #         # If the PDF gave us an amount, trust that as authoritative
        #         if parsed.get("amount"):
        #             amount_str = parsed["amount"]
        #             amount_cents = parsed.get("amount_cents", amount_cents)

        # --- replace the existing VIEW + download block in run() with this ---
        self.log("[step] open newest Bill → VIEW (network-trigger)")
        ok = await self._open_latest_bill_view()
        if not ok:
            return Result(ok=False, error="Failed to trigger Bill VIEW")

        pdf_path, final_url = await self._wait_for_and_save_pdf()

        if not pdf_path:
            return Result(
                ok=False,
                error="Failed to capture bill PDF from Eversource"
            )

        self.log("[step] parsing fields from downloaded PDF")
        parsed = parse_eversource_pdf(pdf_path) or {}
        self.log_block(
            "pdf.parsed",
            [f"{k}: {v}" for k, v in parsed.items()]
        )


        return Result(
            ok=True,
            amount=parsed.get("amount"),
            amount_cents=parsed.get("amount_cents"),
            pdf_path=pdf_path,
            final_url=final_url,
            statement_date=parsed.get("statement_date"),
            due_date=parsed.get("due_date"),
            period_start=parsed.get("period_start"),
            period_end=parsed.get("period_end"),
        )



    # --------------------- helpers ---------------------

    async def _wait_for_and_save_pdf(self, timeout_ms: int = 15_000) -> Tuple[Optional[str], Optional[str]]:
        """
        Wait for an application/pdf response to appear in the network,
        then fetch its bytes and persist to disk.

        Returns: (pdf_path, pdf_url)
        """
        self.log("[pdf] waiting for PDF response bytes")

        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        pdf_url = None

        while asyncio.get_event_loop().time() < deadline:
            for url, ct, status in reversed(self._pdf_candidates):
                if "application/pdf" in (ct or "").lower() and status == 200:
                    pdf_url = url
                    break

            if pdf_url:
                break

            await asyncio.sleep(0.25)

        if not pdf_url:
            self.log("[pdf] no PDF response captured within timeout")
            return None, None

        self.log("[pdf] captured PDF url:", pdf_url)

        try:
            req = self.context.request  # type: ignore[attr-defined]
            resp: APIResponse = await req.get(pdf_url)
            if not resp.ok:
                self.log("[pdf] fetch failed with status", resp.status)
                return None, None

            data = await resp.body()
            out_path = self.download_dir / "eversource-bill.pdf"
            out_path.write_bytes(data)

            self.log("[pdf] saved PDF bytes →", out_path)
            return str(out_path), pdf_url

        except Exception as e:
            self.log("[pdf] exception saving PDF:", repr(e))
            return None, None


    async def _dismiss_cookie_banner(self):
        page = self.page
        try:
            for _ in range(3):
                dismissed = False
                for fr in [page, *page.frames]:
                    for loc in [
                        fr.get_by_role("button", name=re.compile(r"Accept Cookies|Accept|Continue|Got it", re.I)),
                        fr.locator('button:has-text("Accept")'),
                        fr.locator('button[aria-label="Close"], button:has-text("×")'),
                    ]:
                        if await loc.count() > 0:
                            try:
                                await loc.first.click(timeout=1000)
                                dismissed = True
                                self.log("[cookies] dismissed in frame:", getattr(fr, "url", "page"))
                                break
                            except Exception:
                                continue
                    if dismissed:
                        break
                if not dismissed:
                    break
                await page.wait_for_timeout(200)
        except Exception as e:
            self.log("[cookies] dismiss error:", repr(e))

    async def _credential_login(self, username: str, password: str) -> bool:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        await self._dismiss_cookie_banner()

        user_candidates = [
            page.locator("#EmailUsername"),
            page.locator('input[name="email" i]'),
            page.get_by_placeholder(re.compile(r"Email|Username", re.I)),
            page.get_by_label(re.compile(r"Email|Username", re.I)),
            page.locator('input[type="email"]'),
            page.locator('input[id*="Email" i], input[id*="Username" i]'),
        ]
        user_input = None
        for c in user_candidates:
            try:
                await c.wait_for(state="visible", timeout=4000)
                user_input = c.first
                break
            except Exception:
                continue
        if not user_input:
            self.log("[login] no username input found")
            return False

        pass_candidates = [
            page.locator("#Password"),
            page.locator('input[type="password"]'),
            page.get_by_placeholder(re.compile(r"Password", re.I)),
            page.get_by_label(re.compile(r"Password", re.I)),
        ]
        pass_input = None
        for c in pass_candidates:
            try:
                await c.wait_for(state="visible", timeout=4000)
                pass_input = c.first
                break
            except Exception:
                continue
        if not pass_input:
            self.log("[login] no password input found")
            return False

        await user_input.fill(username)
        await pass_input.fill(password)

        sign_in_candidates = [
            page.get_by_role("button", name=re.compile(r"Sign\s*In", re.I)),
            page.locator('button[type="submit"]'),
            page.locator('input[type="submit"]'),
        ]
        clicked = False
        for c in sign_in_candidates:
            try:
                if await c.count() > 0:
                    await c.first.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            try:
                await page.keyboard.press("Enter")
                clicked = True
            except Exception:
                self.log("[login] failed to trigger submit")
                return False

        try:
            await page.wait_for_load_state("networkidle", timeout=35000)
            await self._dismiss_cookie_banner()
        except Exception:
            pass

        self.log("[login] submit complete")
        return True

    async def _handle_two_step_prompt(self):
        page = self.page
        try:
            prompt_heading = page.get_by_role("heading", name=re.compile(r"Two[- ]step verification", re.I))
            ask_later = page.get_by_role("button", name=re.compile(r"Ask\s*Me\s*Again\s*Later|Maybe\s*Later|Not\s*Now", re.I))
            if await prompt_heading.count() > 0 or await ask_later.count() > 0:
                self.log("[2FA] prompt detected")
                if await ask_later.count() > 0:
                    await ask_later.first.click(timeout=5000)
                else:
                    fallback = prompt_heading.locator("..").get_by_role("button", name=re.compile(r"Later|Not\s*Now", re.I))
                    if await fallback.count() > 0:
                        await fallback.first.click(timeout=5000)
                await page.wait_for_load_state("networkidle", timeout=15000)
                await self._dismiss_cookie_banner()
                self.log("[2FA] dismissed")
        except Exception as e:
            self.log("[2FA] handler error:", repr(e))

    async def _wait_for_account_list(self, timeout_ms: int = 35000) -> bool:
        page = self.page
        end = page._impl_obj._loop.time() + (timeout_ms / 1000.0)  # type: ignore[attr-defined]
        while page._impl_obj._loop.time() < end:  # type: ignore[attr-defined]
            try:
                await self._dismiss_cookie_banner()
            except Exception:
                pass

            if "/customer/Account#/" in page.url:
                try:
                    await page.wait_for_load_state("networkidle", timeout=1500)
                except Exception:
                    pass

            try:
                if await page.get_by_role("heading", name=re.compile(r"Account\s*&?\s*Billing|Account\s*Overview", re.I)).count() > 0:
                    self.log("[account] heading found")
                    return True
            except Exception:
                pass

            for loc in [
                page.get_by_role("link", name=re.compile(r"Acct\s*Details|Account\s*Details", re.I)),
                page.get_by_role("button", name=re.compile(r"View\s*&\s*Pay\s*Bill", re.I)),
            ]:
                try:
                    if await loc.count() > 0:
                        self.log("[account] action control found")
                        return True
                except Exception:
                    pass

            try:
                body_txt = await page.inner_text("body")
                if re.search(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})", body_txt):
                    self.log("[account] currency detected in body")
                    return True
            except Exception:
                pass

            await page.wait_for_timeout(400)

        return False


    async def _nav_to_view_bills_and_payments(self) -> bool:
        """
        Click:
          Account & Billing (top nav button)
          → View Bills & Payments (submenu link)

        Returns True if navigation succeeds.
        """
        page = self.page

        self.log("[nav] opening Account & Billing menu")

        try:
            acct_btn = page.get_by_role(
                "button",
                name=re.compile(r"Account\s*&\s*Billing", re.I),
            )
            await acct_btn.first.wait_for(state="visible", timeout=self.cfg.nav_timeout_ms)
            await acct_btn.first.click()
        except Exception as e:
            self.log("[nav] failed to click Account & Billing button:", repr(e))
            return False

        # Wait for submenu to appear
        try:
            submenu_link = page.get_by_role(
                "link",
                name=re.compile(r"View\s*Bills\s*&\s*Payments", re.I),
            )
            await submenu_link.first.wait_for(state="visible", timeout=self.cfg.nav_timeout_ms)
        except Exception as e:
            self.log("[nav] View Bills & Payments submenu not visible:", repr(e))
            return False

        self.log("[nav] clicking View Bills & Payments")

        try:
            async with page.expect_navigation(wait_until="load", timeout=self.cfg.nav_timeout_ms):
                await submenu_link.first.click()
        except Exception as e:
            self.log("[nav] navigation after View Bills & Payments click failed:", repr(e))
            return False

        try:
            await page.wait_for_load_state("networkidle", timeout=self.cfg.nav_timeout_ms)
        except Exception:
            pass

        await self._dismiss_cookie_banner()

        self.log("[nav] landed on View Bills & Payments:", page.url)
        return True



    async def _apply_account_filter(self, account_number: str) -> bool:
        """
        On Account History page:
        - Select account number from dropdown
        - Click Apply Filters
        """
        page = self.page
        self.log("[filter] applying account filter:", account_number)

        try:
            select = page.locator("select#main_ddlAccount")
            await select.wait_for(state="attached", timeout=15_000)

            # Ensure option exists
            opt = select.locator(f"option[value='{account_number}']")
            if await opt.count() == 0:
                self.log("[filter] account number not found in dropdown:", account_number)
                return False

            await select.select_option(value=account_number)
            self.log("[filter] selected account option")
        except Exception as e:
            self.log("[filter] failed selecting account:", repr(e))
            return False

        # Click Apply Filters (postback)
        try:
            apply_btn = page.locator("#main_lnkApply")
            await apply_btn.wait_for(state="visible", timeout=10_000)

            await apply_btn.click()

            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                # WebForms sometimes doesn't fully idle; table wait below is authoritative
                pass

            self.log("[filter] Apply Filters clicked")
        except Exception as e:
            self.log("[filter] failed clicking Apply Filters:", repr(e))
            return False

        # Wait for table to refresh
        # Wait for table to refresh
        try:
            await page.wait_for_selector("#payHistList tbody tr", timeout=20_000)
        except Exception:
            self.log("[filter] table did not reload after applying filter")
            return False

        # ---- NEW: detect empty result set ----
        try:
            first_row = page.locator("#payHistList tbody tr").first
            row_text = (await first_row.inner_text()).strip().lower()

            if "no records found" in row_text:
                self.log("[filter] no records found for account:", account_number)
                return "NO_RECORDS"
        except Exception:
            pass

        self.log("[filter] account filter applied successfully")
        return True


    async def _find_card_for_address_across_pages(self, address_query: str):
        """
        Look for the address on the current page; if not found, scroll down to the
        pagination bar and click page 2, 3, ... until we either find the card or
        run out of numbered pages (up to a sane limit).
        """
        page = self.page
        max_pages = 10  # hard safety cap so we don't loop forever

        for page_index in range(1, max_pages + 1):
            # 1) Search on the current page
            self.log(f"[addr] searching page {page_index} for {address_query!r}")
            card = await self._locate_row_for_address(address_query)
            if card:
                self.log(f"[addr] found address on page {page_index}")
                return card

            # 2) Prepare to go to the next page
            next_page = page_index + 1

            # Scroll to bottom so the pagination bar is definitely in view
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(500)

            # 3) Try to find the button for the next page
            btn = page.locator(f"nav[aria-label='pagination'] button[data-page='{next_page}']")
            if await btn.count() == 0:
                btn = page.locator(f"button[data-page='{next_page}']")
            if await btn.count() == 0:
                # Fallback: rely on the visible page number as the button name
                btn = page.get_by_role("button", name=str(next_page))

            count = await btn.count()
            self.log(f"[addr] pagination button for page {next_page} count = {count}")
            if count == 0:
                self.log(f"[addr] no button for page {next_page}; stopping pagination search")
                break

            # 4) Click the next page button and wait for things to settle
            self.log(f"[addr] clicking pagination button for page {next_page}")
            await btn.first.scroll_into_view_if_needed()
            await btn.first.click()

            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_timeout(500)

        self.log("[addr] address not found after paginating across pages")
        return None

    async def _locate_row_for_address(self, address_query: str):
        page = self.page
        addr = address_query.strip()
        if not addr:
            return None

        try:
            addr_el = page.get_by_text(addr, exact=False)
            await addr_el.first.wait_for(timeout=6000)

            li_row = addr_el.first.locator(
                "xpath=ancestor::li[.//a[contains(., 'Acct Details')] or .//button[contains(., 'Acct Details')] "
                "or .//a[contains(., 'View & Pay Bill')] or .//button[contains(., 'View & Pay Bill')]][1]"
            )
            if await li_row.count() > 0:
                self.log("[match] row located via <li>")
                return li_row.first

            div_row = addr_el.first.locator(
                "xpath=ancestor::div[.//a[contains(., 'Acct Details')] or .//button[contains(., 'Acct Details')] "
                "or .//a[contains(., 'View & Pay Bill')] or .//button[contains(., 'View & Pay Bill')]][1]"
            )
            if await div_row.count() > 0:
                self.log("[match] row located via <div>")
                return div_row.first
        except Exception:
            pass

        try:
            per_card = page.locator("css=li,div,article,section").filter(has_text=addr).filter(
                has=page.get_by_role("link", name=re.compile(r"Acct\s*Details|Account\s*Details|View\s*&\s*Pay\s*Bill", re.I)).or_(
                    page.get_by_role("button", name=re.compile(r"Acct\s*Details|Account\s*Details|View\s*&\s*Pay\s*Bill", re.I))
                )
            )
            if await per_card.count() > 0:
                self.log("[match] row located via generic card filter")
                return per_card.first
        except Exception:
            pass

        self.log("[match] row NOT found for", addr)
        return None


    async def _extract_amount_from_history_row(self, row) -> Tuple[Optional[str], Optional[int]]:
        """
        Extract dollar amount from Account History table row.

        Primary: td.tdAmount (cell text)
        Fallback: span[id^="main_lvAccountHistory_lblAmount_"]
        Fallback: scan entire row text for $X.XX
        """
        try:
            # ---- Primary: td.tdAmount ----
            cell = row.locator("td.tdAmount")
            if await cell.count() > 0:
                try:
                    await cell.first.wait_for(state="visible", timeout=5000)
                except Exception:
                    # visible can fail if off-screen; try scrolling
                    await cell.first.scroll_into_view_if_needed()
                    await cell.first.wait_for(state="attached", timeout=2000)

                cell_text = (await cell.first.inner_text()).strip()
                self.log("[amount] td.tdAmount text =", repr(cell_text))

                m = CURRENCY_RE.search(cell_text)
                if m:
                    clean = m.group(0).replace("$", "").replace(",", "")
                    return clean, int(round(float(clean) * 100))

            # ---- Fallback: span id pattern from your inspect ----
            span = row.locator("span[id^='main_lvAccountHistory_lblAmount_']")
            if await span.count() > 0:
                await span.first.scroll_into_view_if_needed()
                span_text = (await span.first.inner_text()).strip()
                self.log("[amount] span[id^=main_lvAccountHistory_lblAmount_] text =", repr(span_text))

                m = CURRENCY_RE.search(span_text)
                if m:
                    clean = m.group(0).replace("$", "").replace(",", "")
                    return clean, int(round(float(clean) * 100))

            # ---- Last fallback: scan the whole row ----
            row_text = (await row.inner_text()).strip()
            self.log("[amount] row.inner_text =", repr(row_text))

            m = CURRENCY_RE.search(row_text)
            if m:
                clean = m.group(0).replace("$", "").replace(",", "")
                return clean, int(round(float(clean) * 100))

            return None, None

        except Exception as e:
            self.log("[amount] failed extracting from history row:", repr(e))
            return None, None


    async def _extract_amount_from_card(self, card) -> Tuple[Optional[str], Optional[int]]:
        try:
            txt = (await card.inner_text()).strip()
        except Exception:
            return None, None
        m = CURRENCY_RE.search(txt)
        if not m:
            return None, None
        raw = m.group(0)
        clean = raw.replace("$", "").replace(",", "")
        try:
            cents = int(round(float(clean) * 100))
        except Exception:
            cents = None
        return clean, cents

    # ---------- Account History navigation ----------
    async def _go_to_account_history(self, page: Page) -> bool:
        """Ensure we're on the Account History / Past Bills page."""
        # Helper heuristic: are we already on the history page?
        async def is_history(p: Page) -> bool:
            try:
                if re.search(r"AccountHistory\.aspx", p.url, re.I):
                    return True
                if await p.get_by_role("heading", name=re.compile(r"Account\s*History", re.I)).count() > 0:
                    return True
                if await p.get_by_text(re.compile(r"Past\s*bills\s*and\s*payments", re.I)).count() > 0:
                    return True
            except Exception:
                pass
            return False

        # Fast path: if the details page already dumped us onto history, don't click anything.
        if await is_history(page):
            self.log("[history] already on Account History; skipping nav")
            return True

        self.log("[history] trying to navigate to Account History from details page")

        # Try obvious links/buttons on Details page
        candidates = [
            page.get_by_role("link", name=re.compile(r"Past\s*Bills\s*&\s*Payments", re.I)),
            page.get_by_role("button", name=re.compile(r"Past\s*Bills\s*&\s*Payments", re.I)),
            page.get_by_role("link", name=re.compile(r"Account\s*History", re.I)),
            page.get_by_role("button", name=re.compile(r"Account\s*History", re.I)),
        ]

        for idx, loc in enumerate(candidates):
            try:
                cnt = await loc.count()
                self.log(f"[history] candidate {idx} count = {cnt}")
                if cnt == 0:
                    continue

                target = loc.first
                await target.scroll_into_view_if_needed()
                self.log(f"[history] clicking candidate {idx}")

                # Expect a navigation when we click into history
                async with page.expect_navigation(wait_until="load", timeout=self.cfg.nav_timeout_ms):
                    await target.click()

                # Let any ASP.NET postbacks settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                await self._dismiss_cookie_banner()

                if await is_history(page):
                    self.log("[history] reached Account History after clicking candidate", idx)
                    return True
            except Exception as e:
                self.log(f"[history] candidate {idx} click error:", repr(e))
                continue

        # Fallback: maybe there's a Past Bills link somewhere else on the page
        try:
            alt = page.get_by_role("link", name=re.compile(r"Past\s*Bills\s*&\s*Payments", re.I))
            cnt = await alt.count()
            self.log(f"[history] fallback Past Bills link count = {cnt}")
            if cnt > 0:
                async with page.expect_navigation(wait_until="load", timeout=self.cfg.nav_timeout_ms):
                    await alt.first.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                if await is_history(page):
                    self.log("[history] reached Account History via fallback link")
                    return True
        except Exception as e:
            self.log("[history] fallback click error:", repr(e))

        # Last resort: if heuristics still say we're on Account History, treat it as success
        if await is_history(page):
            self.log("[history] heuristics say we are on Account History despite failed clicks")
            return True

        self.log("[history] failed to navigate to Account History")
        return False


    async def _open_latest_bill_view(self) -> bool:
        """
        Find the most recent Bill row and click VIEW.
        Amount extraction is handled exclusively via PDF.
        """
        page = self.page

        self.log("[history] waiting for Account History table")
        await page.wait_for_selector("#payHistList tbody tr", timeout=25_000)

        rows = page.locator("#payHistList tbody tr")
        count = await rows.count()
        self.log(f"[history] detected {count} rows")

        target_row = None
        for i in range(min(count, 30)):
            r = rows.nth(i)
            try:
                type_txt = (await r.locator("td.tdType").inner_text()).strip().lower()
            except Exception:
                continue

            if "bill" in type_txt and "payment" not in type_txt:
                target_row = r
                self.log(f"[history] selected row {i} as Bill")
                break

        if not target_row:
            self.log("[history] no Bill row found")
            return False

        view = target_row.locator("a[id*='viewBtn'], a:has-text('View'), button:has-text('View')")
        if await view.count() == 0:
            self.log("[history] VIEW button not found")
            return False

        await view.first.scroll_into_view_if_needed()
        self.log("[history] clicking VIEW (network-trigger only)")
        await view.first.click(force=True)

        return True

    async def _download_from_full_viewer(self, viewer_page: Page) -> Optional[str]:
        """
        Try to use the viewer's own Download button with a short expect_download.
        If nothing obvious is found or no download happens quickly, return None.
        """
        self.log("[download] attempting in-viewer download via toolbar")

        # Short helper to try a set of locators with a small timeout
        candidates = [
            viewer_page.get_by_role("button", name=re.compile(r"download", re.I)),
            viewer_page.get_by_role("link", name=re.compile(r"download", re.I)),
            viewer_page.locator("[aria-label*='Download' i]"),
            viewer_page.locator("[title*='Download' i]"),
        ]

        for idx, loc in enumerate(candidates):
            try:
                if await loc.count() == 0:
                    continue

                btn = loc.first
                await btn.scroll_into_view_if_needed()
                self.log(f"[download] trying viewer toolbar candidate {idx}")

                # Use a *short* expect_download so we never hang forever
                async with viewer_page.expect_download(timeout=6_000) as dl_info:
                    await btn.click()

                download: Download = await dl_info.value
                suggested = download.suggested_filename or "eversource-bill.pdf"
                out_path = self.download_dir / suggested
                await download.save_as(out_path)
                self.log(f"[download] saved via viewer toolbar → {out_path}")
                return str(out_path)
            except Exception as e:
                self.log(f"[download] viewer candidate {idx} failed:", repr(e))
                continue

        self.log("[download] no viewer toolbar download succeeded")
        return None

    async def _fetch_pdf_direct(self, viewer_page: Page) -> Optional[str]:
        """
        Last-resort: use captured network responses or direct GET to fetch PDF bytes.
        """
        self.log("[download] attempting direct fetch from captured PDF candidates")

        # Prefer the most recent captured PDF-ish response
        candidate_url: Optional[str] = None
        for url, ct, status in reversed(self._pdf_candidates):
            if "application/pdf" in (ct or "").lower():
                candidate_url = url
                break
            if re.search(r"\.pdf(\?|$)", url, re.I):
                candidate_url = url
                break

        # As a fallback, if the current viewer URL looks like a PDF, try that
        if not candidate_url and re.search(r"\.pdf(\?|$)", viewer_page.url, re.I):
            candidate_url = viewer_page.url

        if not candidate_url:
            self.log("[download] no suitable PDF candidate URL found")
            return None

        self.log("[download] fetching PDF directly from", candidate_url)
        try:
            # Playwright's APIRequestContext via the browser context
            req = self.context.request  # type: ignore[attr-defined]
            resp: APIResponse = await req.get(candidate_url)
            if not resp.ok:
                self.log("[download] direct fetch failed with status", resp.status)
                return None

            data = await resp.body()
            filename = "eversource-bill-direct.pdf"
            out_path = self.download_dir / filename
            out_path.write_bytes(data)
            self.log(f"[download] saved direct PDF → {out_path}")
            return str(out_path)
        except Exception as e:
            self.log("[download] direct fetch exception:", repr(e))
            return None


# --------------------- CLI ---------------------

def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Eversource property amount extractor + PDF downloader (Account History path)")
    p.add_argument("--username", help="Email or Username")
    p.add_argument("--password", help="Password")
    p.add_argument("--address", required=True, help="Substring to match the service address (case-insensitive)")
    p.add_argument("--account-number", required=True, help="Account number")
    p.add_argument("--headful", action="store_true", help="Show browser window (headless=false)")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow motion in ms between actions")
    p.add_argument("--json", action="store_true", help="Print JSON instead of bare amount")
    p.add_argument("--debug", action="store_true", help="Verbose debug logging")
    return p.parse_args(argv)


async def main(argv=None):
    args = _parse_args(argv)
    cfg = build_config(args)
    async with EversourceScraper(cfg) as s:
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
    