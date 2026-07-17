#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Optimum PERSONAL portal scraper (simple navigation phase).

Flow:
- Launch browser (same login logic as business scraper)
- Go to optimum.net
- Sign in
- Detect successful login by:
    * Greeting text "Hi <username>"
    * Presence of "Sign out"
- Navigate:
    * Hover "Pay bill"
    * Click "View my bill"
    * On bill page click: "Save or print this bill (pdf)"
"""

import asyncio
import os
import random
import re
import sys
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from common.stealth import get_playwright_module, install_stealth, apply_stealth_to_page, LAUNCH_ARGS

_async_playwright, _ENGINE = get_playwright_module()

try:
    from patchright.async_api import TimeoutError as PWTimeoutError
except ImportError:
    from playwright.async_api import TimeoutError as PWTimeoutError
import requests
from datetime import datetime, timedelta
from pdfminer.high_level import extract_text
from dateutil.relativedelta import relativedelta
from io import BytesIO
import json

# ------------ constants / paths ------------
HERE = Path(__file__).resolve()

SNAP_DIR = Path(
    os.getenv("OPTIMUM_SNAP_DIR", str(HERE.parent / "snaps"))
).expanduser().resolve()
SNAP_DIR.mkdir(parents=True, exist_ok=True)

SCRAPERS_ROOT = HERE.parents[2] if len(HERE.parents) > 2 else HERE.parent
ENV_FILE = SCRAPERS_ROOT / "env"  # optional creds store

OPTIMUM_HOME_URL = "https://www.optimum.net/"

_dl_env = os.getenv("OPTIMUM_DOWNLOAD_DIR", "").strip()
if _dl_env:
    DEFAULT_DL_DIR = Path(_dl_env).expanduser().resolve()
elif Path("/handoff").exists():
    DEFAULT_DL_DIR = Path("/handoff/optimum").resolve()
else:
    DEFAULT_DL_DIR = Path("./downloads/optimum").resolve()

DEFAULT_DL_DIR.mkdir(parents=True, exist_ok=True)




def extract_optimum_fields(pdf_bytes):
    # Extract text from PDF
    text = extract_text(BytesIO(pdf_bytes))
    text = " ".join(text.split())   # normalize whitespace

    # Billing Period — type A: "Billing Period MM/DD/YY - MM/DD/YY"
    m_period = re.search(
        r"Billing Period\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        text
    )
    start_raw = m_period.group(1) if m_period else None
    end_raw = m_period.group(2) if m_period else None

    def parse_mmddyy(x):
        return datetime.strptime(x, "%m/%d/%y").date().isoformat()

    period_start = parse_mmddyy(start_raw) if start_raw else None
    period_end   = parse_mmddyy(end_raw) if end_raw else None

    # Billing Period — type B fallback: "Bill period Mon DD - Mon DD" (no year in text)
    if not period_start:
        m_bp = re.search(
            r"Bill period\s+([A-Za-z]+\s+\d{1,2})\s*-\s*([A-Za-z]+\s+\d{1,2})",
            text, re.IGNORECASE
        )
        if m_bp:
            yr_m = re.search(r"\b(20\d{2})\b", text)
            yr = yr_m.group(1) if yr_m else str(datetime.now().year)
            try:
                period_start = datetime.strptime(f"{m_bp.group(1)} {yr}", "%b %d %Y").date().isoformat()
                period_end   = datetime.strptime(f"{m_bp.group(2)} {yr}", "%b %d %Y").date().isoformat()
            except Exception:
                pass

    # Due Date — type A: "Due Date Month DD, YYYY"
    m_due = re.search(
        r"Due Date\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        text
    )
    # Due Date — type B fallback: "Due date Month DD, YYYY" or "Payment due date: ..."
    if not m_due:
        m_due = re.search(
            r"(?:Payment\s+)?[Dd]ue\s+[Dd]ate\s*:?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
            text, re.IGNORECASE
        )
    if m_due:
        raw_due = m_due.group(1)
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                due_date = datetime.strptime(raw_due, fmt).date().isoformat()
                break
            except ValueError:
                due_date = None
    else:
        due_date = None

    # Amount — type A: "Total Amount Due $X.XX"
    m_amt = re.search(
        r"Total Amount Due\s*\$?\s*([0-9][0-9,]*\s*\.\s*[0-9]{2})",
        text,
        re.IGNORECASE
    )
    # Amount — type B fallback: "Total due $X.XX" or "Total amount due: $X.XX"
    if not m_amt:
        m_amt = re.search(
            r"Total\s+(?:amount\s+)?due\s*:?\s*\$([0-9][0-9,]*\.[0-9]{2})",
            text, re.IGNORECASE
        )
    amount_str = m_amt.group(1).replace(",", "").strip() if m_amt else None
    amount_cents = int(round(float(amount_str) * 100)) if amount_str else None

    # Account Number — type A: "Account Number: XXXX"
    m_acct = re.search(
        r"Account Number:\s*([0-9\-]+)",
        text
    )
    # Account Number — type B fallback: "Account no. XX XX XX" or "Account number: XX XX XX"
    if not m_acct:
        m_acct = re.search(
            r"Account\s+(?:no\.?|number)\s*:?\s*([0-9][\d\s]{5,})",
            text, re.IGNORECASE
        )
    if m_acct:
        invoice_number = re.sub(r"\s+", "-", m_acct.group(1).strip())
    else:
        invoice_number = None

    # Statement Date = one month before due date (or period_start as fallback)
    if due_date:
        statement_date = (datetime.strptime(due_date, "%Y-%m-%d") - relativedelta(months=1)).date().isoformat()
    else:
        statement_date = period_start


    return {
        "amount": amount_str,
        "amount_cents": amount_cents,
        "statement_date": statement_date,
        "period_start": period_start,
        "period_end": period_end,
        "due_date": due_date,
        "invoice_number": invoice_number
    }



def _rand_delay_ms(lo=35, hi=110) -> int:
    return random.randint(lo, hi)


async def _pause(page, lo=120, hi=280):
    await page.wait_for_timeout(random.randint(lo, hi))


@dataclass
class Config:
    username: str
    password: str
    headless: bool = True
    slow_mo_ms: int = 0
    debug: bool = False
    timezone_id: str = "America/New_York"
    login_retries: int = 2
    retry_delay_s: float = 5.0
# ---------------- SCRAPER ----------------

class PersonalOptimumScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser = None
        self.context = None
        self.page = None
        self.pw = None
        self.download_dir: Path = DEFAULT_DL_DIR

    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr)

    # ---------------- LAUNCH ----------------

    async def _launch(self):
        """
        Light-footprint launch: bundled Chromium + ephemeral BrowserContext.

        We previously used `launch_persistent_context` with a real-Chrome
        channel and a tempdir-backed user-data-dir. That combo was the main
        driver of container OOMs (each scrape kept a Chrome profile on disk
        + the heavier real-Chrome process tree). Cloudflare Turnstile is
        still handled — CapSolver submits the token directly via
        `_try_solve_turnstile`, so we don't need real Chrome for stealth.
        """
        self.pw = await _async_playwright().start()
        self.log(f"[launch] engine={_ENGINE}")

        self.browser = await self.pw.chromium.launch(
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo_ms,
            args=LAUNCH_ARGS,
        )

        _no_proxy2 = os.getenv("OPTIMUM_NO_PROXY", "").strip().lower() in ("1", "true", "yes")
        _ctx2 = dict(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            device_scale_factor=1.2,
            timezone_id=self.cfg.timezone_id,
        )
        if not _no_proxy2:
            res_server = os.getenv("IPROYAL_RES_SERVER", "http://geo.iproyal.com:12321").strip()
            res_user   = os.getenv("IPROYAL_RES_USER", "m7InJNYS4b6PkjwT").strip()
            res_pass   = os.getenv("IPROYAL_RES_PASS", "4x2Vx50o7ijTMI3d").strip()
            _ctx2["proxy"] = {"server": res_server, "username": res_user, "password": res_pass}

        self.context = await self.browser.new_context(**_ctx2)

        await install_stealth(self.context)
        self.page = await self.context.new_page()
        await apply_stealth_to_page(self.page)

    async def _close(self):
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
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass

    # ---------------- CAPTCHA / LOGIN ----------------

    async def _captcha_present(self) -> bool:
        """
        Detect reCAPTCHA, Cloudflare Turnstile, or generic robot checks.
        Matches the business scraper's detection logic.
        """
        page = self.page
        # reCAPTCHA iframe
        try:
            iframe_cap = page.locator('iframe[src*="recaptcha" i]')
            if await iframe_cap.count() > 0 and await iframe_cap.first.is_visible():
                return True
        except Exception:
            pass
        # Cloudflare Turnstile iframe
        try:
            turnstile = page.locator('iframe[src*="challenges.cloudflare.com"]')
            if await turnstile.count() > 0 and await turnstile.first.is_visible():
                return True
        except Exception:
            pass
        # Turnstile widget container
        try:
            turnstile_div = page.locator('[class*="cf-turnstile"]')
            if await turnstile_div.count() > 0 and await turnstile_div.first.is_visible():
                return True
        except Exception:
            pass
        # "I'm not a robot" text
        try:
            robot_txt = page.get_by_text(re.compile(r"I['\u2019]m not a robot", re.I))
            if await robot_txt.count() > 0 and await robot_txt.first.is_visible():
                return True
        except Exception:
            pass
        # "checkbox challenge" text
        try:
            chall_txt = page.get_by_text(re.compile(r"checkbox challenge", re.I))
            if await chall_txt.count() > 0 and await chall_txt.first.is_visible():
                return True
        except Exception:
            pass
        return False

    async def _try_solve_turnstile(self):
        """
        Solve Cloudflare Turnstile. Primary strategy: click the interactive checkbox
        in the browser so the token is issued for our own IP (no IP mismatch with
        the form submission). Falls back to CapSolver if the widget doesn't
        auto-resolve within 12 seconds.
        """
        page = self.page

        sitekey = None
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                for part in frame.url.split("/"):
                    if part.startswith("0x"):
                        sitekey = part
                        break
                break

        turnstile_container = page.locator('[class*="cf-turnstile"]')
        turnstile_iframe = page.locator('iframe[src*="challenges.cloudflare.com"]')

        has_container = await turnstile_container.count() > 0
        has_iframe = await turnstile_iframe.count() > 0

        if not sitekey and not has_container and not has_iframe:
            self.log("[turnstile] no Turnstile detected on page")
            return

        self.log(f"[turnstile] detected sitekey={sitekey}, trying browser click-solve first")

        clicked = False
        try:
            cf_frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            checkbox = cf_frame.locator('input[type="checkbox"]')
            if await checkbox.count() > 0:
                await checkbox.click(timeout=5000)
                clicked = True
                self.log("[turnstile] clicked checkbox inside iframe")
        except Exception as e:
            self.log(f"[turnstile] iframe checkbox click failed: {e}")

        if not clicked:
            try:
                if has_container:
                    await turnstile_container.first.click(timeout=5000)
                    clicked = True
                    self.log("[turnstile] clicked outer cf-turnstile container")
            except Exception as e:
                self.log(f"[turnstile] container click failed: {e}")

        solved_in_browser = False
        try:
            captcha_input = page.locator('input[name="captcha"], input[name="cf-turnstile-response"]')
            for _ in range(24):  # 24 x 500ms = 12s
                await page.wait_for_timeout(500)
                val = await captcha_input.first.input_value() if await captcha_input.count() > 0 else ""
                if val and len(val) > 10:
                    self.log(f"[turnstile] browser auto-solved! token length={len(val)}")
                    solved_in_browser = True
                    break
        except Exception as e:
            self.log(f"[turnstile] waiting for auto-solve failed: {e}")

        if solved_in_browser:
            return

        capsolver_key = os.getenv("CAPSOLVER_API_KEY", "").strip()
        if not sitekey or not capsolver_key:
            self.log("[turnstile] CapSolver fallback skipped (no sitekey or API key)")
            return

        self.log("[turnstile] browser auto-solve failed, falling back to CapSolver")
        try:
            token = await self._capsolver_solve_turnstile(capsolver_key, sitekey, page.url)
        except Exception as e:
            self.log(f"[turnstile] CapSolver solve failed: {e}")
            return

        if not token:
            self.log("[turnstile] CapSolver returned empty token")
            return

        self.log(f"[turnstile] CapSolver token ({len(token)} chars), injecting")

        injected = await page.evaluate(
            """(token) => {
                const selectors = [
                    'input[name="cf-turnstile-response"]',
                    'textarea[name="cf-turnstile-response"]',
                    'input[name="captcha"]',
                    'input[name="g-recaptcha-response"]',
                ];
                let found = false;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ) || Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        );
                        if (nativeInputValueSetter && nativeInputValueSetter.set) {
                            nativeInputValueSetter.set.call(el, token);
                        } else {
                            el.value = token;
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        found = true;
                    }
                }
                const widget = document.querySelector('[data-callback]');
                if (widget) {
                    const cbName = widget.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {
                        try { window[cbName](token); } catch(e) {}
                    }
                }
                if (window.__cfTurnstileCallback) {
                    try { window.__cfTurnstileCallback(token); } catch(e) {}
                }
                return found;
            }""",
            token,
        )

        if injected:
            self.log("[turnstile] CapSolver token injected into captcha field(s)")
        else:
            self.log("[turnstile] WARNING: could not find captcha input to inject token")

        await _pause(page, 300, 600)

    async def _capsolver_solve_turnstile(
        self, api_key: str, sitekey: str, page_url: str, timeout_s: int = 120
    ) -> Optional[str]:
        """
        Call CapSolver API to solve a Cloudflare Turnstile challenge.
        Returns the solved token string.
        """
        create_url = "https://api.capsolver.com/createTask"
        result_url = "https://api.capsolver.com/getTaskResult"

        # Build proxy params for AntiTurnstileTask
        import re as _re
        _res_server = os.getenv("IPROYAL_RES_SERVER", "http://geo.iproyal.com:12321").strip()
        _res_user   = os.getenv("IPROYAL_RES_USER", "m7InJNYS4b6PkjwT").strip()
        _res_pass   = os.getenv("IPROYAL_RES_PASS", "4x2Vx50o7ijTMI3d").strip()
        _m = _re.match(r"https?://([^:]+):(\d+)", _res_server)
        _proxy_host = _m.group(1) if _m else "geo.iproyal.com"
        _proxy_port = int(_m.group(2)) if _m else 12321

        # Create task
        payload = json.dumps({
            "clientKey": api_key,
            "task": {
                "type": "AntiTurnstileTask",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "metadata": {"type": "turnstile"},
                "proxyType": "http",
                "proxyAddress": _proxy_host,
                "proxyPort": _proxy_port,
                "proxyLogin": _res_user,
                "proxyPassword": _res_pass,
            },
        }).encode()

        req = urllib.request.Request(
            create_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())

        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"CapSolver createTask error: {data.get('errorDescription', data)}")

        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError(f"CapSolver returned no taskId: {data}")

        self.log(f"[capsolver] task created: {task_id}")

        # Poll for result
        poll_payload = json.dumps({
            "clientKey": api_key,
            "taskId": task_id,
        }).encode()

        elapsed = 0
        interval = 3
        while elapsed < timeout_s:
            await asyncio.sleep(interval)
            elapsed += interval

            req2 = urllib.request.Request(
                result_url,
                data=poll_payload,
                headers={"Content-Type": "application/json"},
            )
            resp2 = urllib.request.urlopen(req2, timeout=30)
            result = json.loads(resp2.read())

            status = result.get("status", "")
            if status == "ready":
                token = result.get("solution", {}).get("token", "")
                self.log(f"[capsolver] solved in ~{elapsed}s")
                return token
            elif result.get("errorId", 0) != 0:
                raise RuntimeError(f"CapSolver error: {result.get('errorDescription', result)}")

            self.log(f"[capsolver] polling... {elapsed}s")

        raise RuntimeError(f"CapSolver timeout after {timeout_s}s")

    async def _is_logged_in(self):
        """
        Detect PERSONAL portal login success by:
        - Greeting "Hi <username>"
        - "Sign out" visible
        """
        page = self.page
        txt = await page.content()

        if re.search(r"Sign\s*out", txt, re.I):
            return True
        if re.search(r"Hi\s+[A-Za-z0-9_]+", txt, re.I):
            return True

        return False

    async def _login(self) -> bool:
        """Login with retry. Uses CapSolver to solve Turnstile if present."""
        for attempt in range(1, self.cfg.login_retries + 1):
            self.log(f"[login] attempt {attempt}/{self.cfg.login_retries}")

            # Relaunch browser for a clean slate on retries
            if attempt > 1:
                self.log(f"[login] waiting {self.cfg.retry_delay_s}s before retry")
                await asyncio.sleep(self.cfg.retry_delay_s)
                await self._close()
                await self._launch()

            status = await self._attempt_login()
            if status == "ok":
                return True

            self.log(f"[login] attempt {attempt} result: {status}")

        return False

    async def _attempt_login(self) -> str:
        """
        Returns:
        "ok"      -> logged in
        "captcha" -> captcha detected and unsolvable
        "fail"    -> creds rejected / can't reach portal
        """
        page = self.page
        self.log("[login] navigating to optimum.net")

        try:
            await page.goto(OPTIMUM_HOME_URL, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            self.log("[login] home goto failed", e)
            return "fail"

        # Click Sign in
        sign_in = page.get_by_role("link", name=re.compile(r"Sign\s*in", re.I)).first
        try:
            await sign_in.hover()
            await _pause(page, 100, 220)
            await sign_in.click(timeout=5000)
        except Exception:
            try:
                await page.locator('a:has-text("Sign in")').first.click()
            except:
                self.log("[login] cannot find Sign in")
                return "fail"

        # wait for login form
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

        # detect captcha early — try to solve, then continue regardless
        if await self._captcha_present():
            self.log("[login] captcha visible pre-typing, attempting to solve")
            await self._try_solve_turnstile()
            await page.wait_for_timeout(2000)
            self.log("[login] continuing with login after solve attempt")

        # username field
        username_field = page.locator('input[type="text"], input[name*="user" i]').first
        password_field = page.locator('input[type="password"]').first

        # human-ish typing (character by character with random delays)
        try:
            await username_field.click()
            for ch in self.cfg.username:
                await username_field.type(ch, delay=_rand_delay_ms())
            await _pause(page)

            await password_field.click()
            for ch in self.cfg.password:
                await password_field.type(ch, delay=_rand_delay_ms())
            await _pause(page)
        except Exception:
            self.log("[login] unable to fill credentials")
            return "fail"

        # Try to solve Turnstile before submit
        await self._try_solve_turnstile()

        # submit
        try:
            await password_field.press("Enter")
        except:
            self.log("[login] pressing Enter failed")
            return "fail"

        # wait for redirect
        try:
            await page.wait_for_load_state("networkidle", timeout=25000)
        except:
            pass
        await page.wait_for_timeout(1000)

        # Wait for Turnstile to auto-resolve (if present)
        try:
            turnstile = page.locator(
                'iframe[src*="challenges.cloudflare.com"], [class*="cf-turnstile"]'
            )
            if await turnstile.count() > 0:
                self.log("[login] Turnstile detected, waiting up to 30s for auto-resolve…")
                try:
                    await turnstile.first.wait_for(state="hidden", timeout=30000)
                    self.log("[login] Turnstile resolved")
                except Exception:
                    self.log("[login] Turnstile did not auto-resolve within 30s")
        except Exception:
            pass

        # captcha after submit?
        if await self._captcha_present():
            self.log("[login] captcha visible after submit")
            return "captcha"

        await page.wait_for_timeout(1200)

        # final check
        ok = await self._is_logged_in()
        self.log("[login] logged in?", ok, "url=", page.url)
        if not ok:
            await self._snap("login-fail")
        return "ok" if ok else "fail"

    # ---------------- NAVIGATION ----------------
    async def _goto_bill_page(self) -> bool:
        page = self.page

        self.log("[nav] going directly to /pay-bill/my-bill/")

        try:
            await page.goto("https://www.optimum.net/pay-bill/my-bill/",
                            timeout=30000,
                            wait_until="domcontentloaded")
        except Exception as e:
            self.log("[nav] direct navigation failed:", e)
            return False

        # Give Angular time to render the bill page components
        await page.wait_for_timeout(2000)

        html = await page.content()

        # Look for keywords that actually appear on the bill page
        if not re.search(r"Statement|Billing|Amount Due|PDF|Your Bill", html, re.I):
            self.log("[nav] bill page not detected after direct navigation")
            return False

        self.log("[nav] reached bill page (direct navigation)")
        return True


    async def _click_pdf_button(self) -> bool:
        page = self.page
        context = self.context

        self.log("[pdf] starting network listener...")

        pdf_url_holder = {"url": None}

        def handle_request(request):
            url = request.url
            if "billfile" in url and "billID" in url:
                pdf_url_holder["url"] = url
                self.log("[pdf] CAPTURED PDF URL:", url)

        context.on("request", handle_request)

        btn = page.locator('a.font-cta-link', has_text="Save").first

        try:
            async with context.expect_page() as popup_info:
                await btn.click()
        except Exception as e:
            self.log("[pdf] ERROR clicking PDF button:", e)
            return False

        pdf_page = await popup_info.value
        self.log("[pdf] popup opened!")

        # Wait briefly for request interception
        await page.wait_for_timeout(2000)

        if not pdf_url_holder["url"]:
            self.log("[pdf] ERROR — failed to capture billfile request!")
            return False

        pdf_url = pdf_url_holder["url"]
        self.log("[pdf] SUCCESS — PDF URL:", pdf_url)


        # self.log("[pdf] attempting to download PDF", pdf_bytes)
        # 1. Get session cookies from Playwright
        try:
            resp = await context.request.get(pdf_url)
            pdf_bytes = await resp.body()

            # Extract fields from the PDF
            fields = extract_optimum_fields(pdf_bytes)

            # Save PDF to default download dir
            target = (self.download_dir / "optimum-statement.pdf").resolve()
            i = 1
            while target.exists():
                target = target.with_name(f"optimum-statement-{i}.pdf")
                i += 1

            target.write_bytes(pdf_bytes)
            pdf_path = str(target)

            self.log("[pdf] extracted fields:", fields)

            return {
                "ok": True,
                "pdf_path": pdf_path,
                "final_url": self.page.url,
                **fields
            }
        except Exception as e:
            self.log("[pdf] ERROR downloading PDF:", e)
            return False

    async def run(self):
        await self._launch()

        if not await self._login():
            self.log("[run] login failed")
            await self._close()
            return {"ok": False, "error": "login_failed"}

        if not await self._goto_bill_page():
            self.log("[run] could not reach bill page")
            await self._close()
            return {"ok": False, "error": "bill_page_unreachable"}

        result = await self._click_pdf_button()
        if not result:
            self.log("[run] could not click pdf button")
            await self._close()
            return {"ok": False, "error": "pdf_download_failed"}

        # result is the dictionary returned by _click_pdf_button()
        self.log("[run] navigation successful")

        # IMPORTANT: do not sleep forever anymore
        await self._close()

        return result


# ---------------- MAIN ----------------

async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--headful", action="store_true", default=False)
    p.add_argument("--slow-mo", type=int, default=0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--json", action="store_true", help="Print JSON result")
    args = p.parse_args()

    cfg = Config(
        username=args.username,
        password=args.password,
        headless=not args.headful,
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
    )

    scraper = PersonalOptimumScraper(cfg)
    result = await scraper.run()

    if not result or not result.get("ok"):
        print(
            f"ERROR: {result.get('error', 'navigation failed')}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("amount") or "")


if __name__ == "__main__":
    asyncio.run(main())
