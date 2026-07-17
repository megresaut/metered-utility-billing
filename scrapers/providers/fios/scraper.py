#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Verizon Fios bill scraper.

Flow:
- Go to Verizon login page
- Enter username, continue
- Enter password, sign in
- If a security question page appears, answer it (using --sec-answer)
- Land on account home page (Billing tile visible)
- Click "Bill overview"
- On bill page:
    - scrape Current Charges amount (e.g. "$299.89")
    - click "Download PDF" under Paper Free Billing
    - capture the downloaded PDF and save it locally

Output:
- amount (string, e.g. "$299.89")
- amount_cents (int, e.g. 29989)
- pdf_path (local path we wrote)
- final_url (url of bill page when we scraped)

CLI flags:
  --username
  --password
  --sec-answer
  --headful
  --slow-mo <ms>
  --json
  --debug
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
# from common.browser import open_real_chrome, close_all
from datetime import datetime, date
from typing import Optional, Tuple

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
)

LOGIN_URL = "https://secure.verizon.com/signin"
DEFAULT_DL_DIR = Path(
    os.getenv("FIOS_DOWNLOAD_DIR", "/handoff/fios")
).resolve()

CURRENCY_RE = re.compile(r"\$[0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})")


@dataclass
class Config:
    username: str
    password: str
    sec_answer: Optional[str]
    headful: bool
    slow_mo: int
    debug: bool
    json_out: bool


@dataclass
class Result:
    amount: Optional[str]
    amount_cents: Optional[int]
    pdf_path: Optional[str]
    final_url: Optional[str]

    # New fields (YYYY-MM-DD):
    statement_date: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    due_date: Optional[str] = None



def _log(cfg: Config, *args):
    if cfg.debug:
        print("DEBUG:", *args, file=sys.stderr)


def _money_to_cents(money: str) -> Optional[int]:
    """
    "$299.89" -> 29989
    "299.89"  -> 29989
    """
    if not money:
        return None
    m = re.search(r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2}))", money.replace("$", ""))
    if not m:
        return None
    cleaned = m.group(1).replace(",", "")
    # split dollars.cents
    try:
        dollars, cents = cleaned.split(".")
        return int(dollars) * 100 + int(cents)
    except ValueError:
        return None

async def _human_type(locator, text: str, delay_ms: int = 80):
    # Focus and ensure the element is editable/visible
    await locator.scroll_into_view_if_needed()
    await locator.wait_for(state="visible")
    await locator.click()  # focus

    # Hard clear (handles masked/controlled inputs better than just Backspace)
    await locator.press("ControlOrMeta+A")
    await locator.press("Backspace")

    # Put caret at end (in case the page re-populates the field)
    await locator.evaluate("el => { el.focus(); const L = el.value.length; el.setSelectionRange(L, L); }")

    # Type with a small delay, re-lock caret each char to defeat caret jumps
    for ch in text:
        await locator.type(ch, delay=delay_ms)
        await locator.evaluate("el => { const L = el.value.length; el.setSelectionRange(L, L); }")
        await locator.page.wait_for_timeout(30)

    # Tiny pause after typing
    await locator.page.wait_for_timeout(200)


async def _fill_username(page: Page, cfg: Config):
    _log(cfg, "[login] filling username")
    try:
        await page.wait_for_selector("text=Sign in", timeout=20000)

        username_input = page.locator('input[type="text"]').first
        await username_input.wait_for(state="visible", timeout=10000)
        await username_input.scroll_into_view_if_needed()
        await username_input.click()

        # Clear first (works with controlled inputs).
        await username_input.press("ControlOrMeta+A")
        await username_input.press("Backspace")

        # Preferred: atomic insert that still fires real input events.
        try:
            await page.keyboard.insert_text(cfg.username)  # <-- correct Python API
        except AttributeError:
            # Fallback for older Playwright: set value + dispatch input via JS.
            handle = await username_input.element_handle()
            await handle.evaluate(
                "(el, value) => {"
                "  el.focus();"
                "  el.value = '';"
                "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                "  el.value = value;"
                "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                "}",
                cfg.username,
            )

        # Verify and harden: if the field doesn't match, force-fill and nudge validators.
        val = await username_input.input_value()
        if val != cfg.username:
            _log(cfg, f"[login] insert mismatch ({val!r}), falling back to fill()")
            await username_input.fill(cfg.username)

        # Nudge validators like a human
        await page.keyboard.press("Tab")
        await page.keyboard.press("Shift+Tab")
        await page.wait_for_timeout(250)

        # Wait for Continue to become enabled before click
        continue_btn = page.get_by_role("button", name=re.compile(r"continue", re.I)).first
        await continue_btn.wait_for(state="visible", timeout=8000)

        await page.wait_for_function(
            "el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'",
            arg=await continue_btn.element_handle(),
            timeout=8000,
        )

        await continue_btn.click()
        _log(cfg, "[login] clicked Continue")
        await page.wait_for_timeout(600)

    except Exception as e:
        raise RuntimeError(f"Failed to fill username: {e}")


async def _fill_password(page: Page, cfg: Config):
    _log(cfg, "[login] filling password")

    # wait for the password screen to load
    # we expect an <input type="password"> to appear
    pw_input = page.locator('input[type="password"]').first
    await pw_input.wait_for(state="visible", timeout=10000)

    # type password human-style
    await _human_type(pw_input, cfg.password, delay_ms=80)

    _log(cfg, "[login] clicking Sign in")

    # Now click ONLY the Sign in button in the auth card, not the global nav one.
    # Strategy:
    #   1. grab all buttons with role=button and name ~ /sign in/i
    #   2. pick the one that is NOT the nav dropdown (shouldn't have "dropdown" in aria-label)
    #   3. fallback to .first if only one remains.

    sign_in_buttons = page.get_by_role("button", name=re.compile(r"sign\s*in", re.I))

    # Filter to visible buttons whose textContent is basically "Sign in"
    # and not "Sign in dropdown menu"
    matching_handles = []
    count = await sign_in_buttons.count()
    for i in range(count):
        btn = sign_in_buttons.nth(i)
        # grab accessible name / inner_text
        name = await btn.inner_text()
        aria = await btn.get_attribute("aria-label")
        # heuristics:
        bad_nav = aria and "dropdown" in aria.lower()
        looks_right = "sign in" in (name or "").lower()
        if looks_right and not bad_nav:
            matching_handles.append(btn)

    # tiny pause before clicking sign in (looks human)
    await page.wait_for_timeout(400)

    if not matching_handles:
        # fallback: just click the first one (should still work in most cases)
        await sign_in_buttons.first.click()
    else:
        await matching_handles[0].click()

    # give it a moment for nav / possible security question
    await page.wait_for_timeout(1000)


async def _maybe_answer_security_question(page: Page, cfg: Config):
    """
    Handle Verizon's post-login hurdles.

    Possible flows after clicking "Sign in":
      A) "Verify your information." interstitial with a big black "Continue" button.
      B) Secret question page with answer field (#sqa-answer) and a Continue button.
      C) Neither (go straight to account home / billing).
      D) A followed by B.

    We'll loop a few times and try to clear A and B, stopping early if we detect
    we're already in the account dashboard/billing area.
    """
    _log(cfg, "[login] checking post-login hurdles")

    # helper: are we already basically in the account?
    async def in_account_page() -> bool:
        # heuristics:
        #   - billing nav pill row with "Billing"
        #   - billhome app text like "You are Enrolled in Auto Pay"
        #   - top tabs row with "Take action", "Profile & Settings", etc.
        try:
            if await page.locator("text=Billing").count() > 0:
                return True
            if await page.locator("text=You are Enrolled in Auto Pay").count() > 0:
                return True
            if await page.locator("text=Take action").count() > 0:
                return True
        except Exception:
            pass
        return False

    # click "Verify your information" → Continue if present
    # async def handle_verify_interstitial() -> bool:
    #     """
    #     Try to clear the 'Verify your information' checkpoint.

    #     Behavior right now:
    #     - We find the 'Verify your information' card
    #     - We try to click the 'Continue' button

    #     Extra logging:
    #     - whether that button is disabled or not
    #     """
    #     try:
    #         verify_card = page.locator("text=Verify your information").first
    #         card_count = await verify_card.count()

    #         if card_count == 0:
    #             _log(cfg, "[login][verify-checkpoint] no 'Verify your information' card detected")
    #             return False

    #         _log(cfg, "[login][verify-checkpoint] found 'Verify your information' card")

    #         # grab the Continue button that lives in that card
    #         cont_btn = page.get_by_role("button", name=re.compile(r"continue", re.I)).first

    #         try:
    #             await cont_btn.wait_for(state="visible", timeout=5000)
    #         except Exception as e:
    #             _log(cfg, f"[login][verify-checkpoint] Continue button not visible yet: {e}")
    #             return False

    #         # inspect disabled state BEFORE we click
    #         disabled_attr = await cont_btn.get_attribute("disabled")
    #         is_disabled = disabled_attr is not None

    #         _log(cfg, f"[login][verify-checkpoint] Continue button disabled? {is_disabled}")

    #         # now try to click like we did before
    #         _log(cfg, "[login][verify-checkpoint] attempting click on Continue")
    #         await cont_btn.click()
    #         _log(cfg, "[login][verify-checkpoint] clicked checkpoint Continue (Playwright reported success)")

    #         # short pause to simulate real navigation lag
    #         await page.wait_for_timeout(1500)
    #         return True

    #     except Exception as e:
    #         _log(cfg, f"[login][verify-checkpoint] could not click checkpoint Continue: {e}")
    #         return False


# --- REPLACE your _maybe_answer_security_question with this ---
async def _maybe_answer_security_question(page: Page, cfg: Config):
    import re
    from playwright.async_api import TimeoutError as PWTimeoutError

    _log(cfg, "[login] checking post-login hurdles")

    async def _on_signin() -> bool:
        return "/signin" in page.url

    async def _in_billing_ui() -> bool:
        try:
            if await page.get_by_role("button", name=re.compile(r"^bill\\s*overview$", re.I)).first.is_visible():
                return True
        except Exception:
            pass
        for sel in ("text=Billing", "text=You are Enrolled in Auto Pay", "text=Take action"):
            try:
                if await page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return False

    # 1) Verify interstitial
    try:
        if await page.get_by_text(re.compile(r"verify your information", re.I)).first.count() > 0:
            _log(cfg, "[login] verify-interstitial detected → handling")
            await handle_verify_interstitial(page, cfg)
    except Exception as e:
        _log(cfg, f"[login] verify-interstitial probe error: {e}")

    if not await _on_signin() or await _in_billing_ui():
        return

    # 2) Secret question
    try:
        sq_input = page.locator('input#sqa-answer, input[name="sqaAnswer"], input[type="password"], input[type="text"]').first
        sq_visible = False
        try:
            await sq_input.wait_for(state="visible", timeout=3000)
            sq_visible = True
        except Exception:
            pass

        if sq_visible:
            if not cfg.sec_answer:
                raise RuntimeError("Secret question shown but no --sec-answer provided")
            _log(cfg, "[login] secret-question detected → answering")
            await _human_type(sq_input, cfg.sec_answer, delay_ms=80)

            cont = page.get_by_role("button", name=re.compile(r"continue", re.I)).first
            await cont.wait_for(state="visible", timeout=8000)
            try:
                await page.wait_for_function(
                    "el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'",
                    arg=await cont.element_handle(),
                    timeout=6000,
                )
            except PWTimeoutError:
                pass
            await cont.click()
            await page.wait_for_timeout(800)
    except Exception as e:
        _log(cfg, f"[login] secret-question handling error: {e}")

    # 3) Occasionally A then B; re-check verify once
    if await _on_signin():
        try:
            if await page.get_by_text(re.compile(r"verify your information", re.I)).first.count() > 0:
                _log(cfg, "[login] verify-interstitial re-check → handling")
                await handle_verify_interstitial(page, cfg)
        except Exception:
            pass

    await page.wait_for_timeout(600)

async def handle_verify_interstitial(page, cfg) -> bool:
    """
    Verizon 'Verify your information.' interstitial handler.
    - If radios present: select one (prefer 'I'll verify again next time'), then click Continue.
    - If no radios: wait for Continue to enable and click it.
    Returns True on success, False otherwise.
    """
    import re, random
    from playwright.async_api import TimeoutError as PWTimeoutError

    def _logp(msg: str):
        _log(cfg, f"[login][verify-interstitial] {msg}")

    async def _close_cookie_banner():
        # Bottom privacy bar with a "Close" button (seen in screenshot)
        try:
            btn = page.get_by_role("button", name=re.compile(r"close", re.I)).last
            if await btn.is_visible():
                await btn.click(timeout=1500)
                _logp("cookie banner closed")
        except Exception:
            pass

    async def _human_nudge():
        # light human-ish activity to let SPA hydrate
        await page.bring_to_front()
        x0, y0 = random.randint(220, 440), random.randint(220, 320)
        await page.mouse.move(x0, y0, steps=10)
        await page.wait_for_timeout(120)
        await page.mouse.wheel(0, 200)
        await page.wait_for_timeout(100)
        await page.mouse.wheel(0, -120)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(120)

    async def _continue_button():
        # Prefer role, but also matches the class shown in your HTML
        btn = page.get_by_role("button", name=re.compile(r"continue", re.I)).first
        if not await btn.count():
            btn = page.locator('button.mvo-main-button:has-text("Continue")').first
        return btn

    async def _enable_and_click_continue(max_attempts=8) -> bool:
        btn = await _continue_button()
        for attempt in range(1, max_attempts + 1):
            try:
                visible = await btn.is_visible()
                # Some pages keep a 'disabled' attribute — check both states
                enabled = await btn.is_enabled()
                disabled_attr = await btn.get_attribute("disabled")
                _logp(f"Continue state attempt {attempt}: visible={visible} enabled={enabled} disabled_attr={disabled_attr}")
                if visible and enabled and disabled_attr is None:
                    await btn.click()
                    _logp("clicked Continue")
                    await page.wait_for_timeout(900)
                    return True
            except Exception as e:
                _logp(f"continue read/click error: {e}")
            await _human_nudge()
            await page.wait_for_timeout(220 + random.randint(40, 240))
        return False

    async def _pick_radio_if_present() -> bool:
        # Use concrete ids/names from your DOM, but fall back to generic radios
        try:
            radios = page.locator('input[type="radio"]')
            count = await radios.count()
            if count == 0:
                return False

            # Prefer "I'll verify again next time"
            try:
                lbl_again = page.get_by_label(re.compile(r"i['` ]?ll verify again next time", re.I))
                if await lbl_again.is_visible():
                    await lbl_again.check()  # label->input mapping works via Playwright
                    _logp("selected: I'll verify again next time (label)")
                    return True
            except Exception:
                pass
            try:
                # some pages wire by id/for
                again_by_id = page.locator('#remember-verification-next-time-id')
                if await again_by_id.is_visible():
                    await again_by_id.check()
                    _logp("selected: I'll verify again next time (by id)")
                    return True
            except Exception:
                pass

            # Fallback to "Remember my verification next time"
            try:
                lbl_rem = page.get_by_label(re.compile(r"remember my verification next time", re.I))
                if await lbl_rem.is_visible():
                    await lbl_rem.check()
                    _logp("selected: Remember my verification next time (label)")
                    return True
            except Exception:
                pass
            try:
                rem_by_name = page.locator('input[type="radio"][name*="rememberVerification" i]')
                if await rem_by_name.first.is_visible():
                    await rem_by_name.first.check()
                    _logp("selected: rememberVerification (by name)")
                    return True
            except Exception:
                pass

            # Last resort: check the first visible radio
            try:
                first = radios.first
                if await first.is_visible():
                    await first.check()
                    _logp("selected: first visible radio (fallback)")
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    # --- main flow ---
    # Wait for the interstitial text somewhere on the card
    try:
        await page.get_by_text(re.compile(r"verify your information", re.I)).first.wait_for(timeout=15000)
        _logp("card found")
    except PWTimeoutError:
        _logp("card not present")
        return False

    await _close_cookie_banner()

    # If radios exist, select one; otherwise we’re in the “just Continue” variant
    radios_present = False
    try:
        radios_present = (await page.locator('input[type="radio"]').count()) > 0
    except Exception:
        radios_present = False

    if radios_present:
        _logp("radios detected → selecting one")
        picked = await _pick_radio_if_present()
        if not picked:
            _logp("radio select failed; nudging and retrying once")
            await _human_nudge()
            picked = await _pick_radio_if_present()
        if not picked:
            _logp("radio selection still failed; proceeding anyway")

    # Try to enable/click Continue in both cases
    if await _enable_and_click_continue():
        return True

    # Soft reload and one more try (hydration sometimes races)
    _logp("reload + retry once")
    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(700)
        await page.get_by_text(re.compile(r"verify your information", re.I)).first.wait_for(timeout=8000)
        await _close_cookie_banner()

        # If radios exist after reload, try again
        try:
            radios_present = (await page.locator('input[type="radio"]').count()) > 0
        except Exception:
            radios_present = False
        if radios_present:
            await _pick_radio_if_present()

        if await _enable_and_click_continue(max_attempts=5):
            return True
    except Exception as e:
        _logp(f"post-reload retry failed: {e}")

    _logp("still gated after retries")
    return False


# --- ADD this helper ---
async def _wait_for_account_landing(page: Page, cfg: Config):
    import re
    _log(cfg, "[nav] waiting for account landing (real, visible signals)")

    def on_signin(u: str) -> bool: return re.search(r"/signin\\b", u) is not None

    async def bill_overview_visible() -> bool:
        btn = page.get_by_role("button", name=re.compile(r"^bill\\s*overview$", re.I)).first
        link = page.get_by_role("link",   name=re.compile(r"^bill\\s*overview$", re.I)).first
        try:
            if await btn.is_visible(): return True
        except Exception: pass
        try:
            if await link.is_visible(): return True
        except Exception: pass
        return False

    async def heuristic_visible() -> bool:
        root = page.locator('#root, [id^="vz-"], [data-testid="bill-home-root"]').first
        try:
            if not await root.count(): return False
        except Exception: return False
        for sel in ("text=Billing", "text=You are Enrolled in Auto Pay", "text=Take action"):
            try:
                if await root.locator(sel).first.is_visible(): return True
            except Exception: pass
        return False

    for _ in range(30):  # ~15s
        if on_signin(page.url):
            await page.wait_for_timeout(500); continue
        if await bill_overview_visible(): return
        if await heuristic_visible():     return
        await page.wait_for_timeout(500)

    # Debug snapshot on timeout
    try:
        short = (await page.content())[:2000].replace("\n","\\n")
        _log(cfg, f"[nav] landing timeout at {page.url}")
        _log(cfg, f"[nav] HTML(2k): {short}")
    except Exception as e:
        _log(cfg, f"[nav] snapshot failed: {e}")


async def _wait_for_account_landing(page: Page, cfg: Config):
    """
    Wait until we're truly on the account/billing UI.

    Signals (in order):
      A) URL no longer contains /signin
      B) 'Bill overview' button/link is VISIBLE
      C) Fallback heuristics (VISIBLE 'Billing'/'Auto Pay'/'Take action' under app root)

    While we're still on /signin, we ALSO re-run the post-login hurdle handler
    (_maybe_answer_security_question) so that late-appearing security questions
    or verify cards are handled.
    """
    import re
    _log(cfg, "[nav] waiting for account landing (real, visible signals)")

    def on_signin(u: str) -> bool:
        return re.search(r"/signin\b", u) is not None

    async def bill_overview_visible() -> bool:
        btn = page.get_by_role(
            "button", name=re.compile(r"^bill\s*overview$", re.I)
        ).first
        link = page.get_by_role(
            "link", name=re.compile(r"^bill\s*overview$", re.I)
        ).first
        try:
            if await btn.is_visible():
                return True
        except Exception:
            pass
        try:
            if await link.is_visible():
                return True
        except Exception:
            pass
        return False

    async def heuristic_visible() -> bool:
        # Scope to a root that only exists in the logged-in app (be flexible)
        app_root = page.locator(
            '#root, [id^="vz-"], [data-testid="bill-home-root"]'
        ).first
        try:
            if not await app_root.count():
                return False
        except Exception:
            return False

        for sel in (
            "text=Billing",
            "text=You are Enrolled in Auto Pay",
            "text=Take action",
        ):
            loc = app_root.locator(sel).first
            try:
                if await loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    # Poll up to ~15s total
    for attempt in range(30):
        url = page.url

        if on_signin(url):
            _log(
                cfg,
                f"[nav] still on /signin (attempt {attempt+1}/30); "
                f"checking hurdles… url={url}",
            )

            # NEW: while we're stuck on /signin, try to clear any late
            # verify / secret-question screens that appeared after password.
            try:
                await _maybe_answer_security_question(page, cfg)
            except Exception as e:
                _log(
                    cfg,
                    f"[nav] hurdle handler in landing loop error: {e}",
                )

            await page.wait_for_timeout(500)
            continue

        if await bill_overview_visible():
            _log(cfg, "[nav] found visible 'Bill overview' → landed")
            return

        if await heuristic_visible():
            _log(cfg, "[nav] visible billing heuristics found → landed")
            return

        await page.wait_for_timeout(500)

    # Timed out — dump a tiny snapshot for debugging
    _log(cfg, "[nav] account landing NOT detected (timeout); dumping snapshot")
    try:
        cur_url = page.url
        html_snapshot = await page.content()
        short_html = html_snapshot[:2000].replace("\n", "\\n")
        _log(cfg, f"[nav] URL: {cur_url}")
        _log(cfg, f"[nav] HTML (first 2k): {short_html}")
    except Exception as e:
        _log(cfg, f"[nav] snapshot failed: {e}")


async def _goto_bill_overview(page: Page, cfg: Config):
    """
    SPA-safe click of 'Bill overview'.
    Never waits on a fresh locator — only clicks what is visible NOW.
    """
    import re
    _log(cfg, "[nav] clicking Bill overview (SPA-safe)")

    for attempt in range(1, 7):
        _log(cfg, f"[nav] Bill overview click attempt {attempt}")

        # 1) Try button
        try:
            btn = page.get_by_role(
                "button", name=re.compile(r"bill\s*overview", re.I)
            ).first
            if await btn.is_visible():
                await btn.click()
                _log(cfg, "[nav] clicked Bill overview (button)")
                return
        except Exception:
            pass

        # 2) Try link
        try:
            link = page.get_by_role(
                "link", name=re.compile(r"bill\s*overview", re.I)
            ).first
            if await link.is_visible():
                await link.click()
                _log(cfg, "[nav] clicked Bill overview (link)")
                return
        except Exception:
            pass

        # 3) Try raw text (div/span/etc)
        try:
            text = page.locator("text=Bill overview").first
            if await text.is_visible():
                await text.click()
                _log(cfg, "[nav] clicked Bill overview (text)")
                return
        except Exception:
            pass

        await page.wait_for_timeout(400)

    # If we get here, it's a real failure
    try:
        snippet = (await page.content())[:2000].replace("\n", "\\n")
        _log(cfg, f"[nav] Bill overview click FAILED. HTML snippet: {snippet}")
    except Exception:
        pass

    raise RuntimeError("Could not click Bill overview after retries")


async def _scrape_amount_and_download_pdf(
    page: Page,
    ctx: BrowserContext,
    cfg: Config,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    On the bill page:
      - scrape Current Charges amount
      - download the bill PDF via "Download PDF"
      - parse statement date, period, and due date from the PDF

    Returns:
        (amount_str, pdf_path, statement_date, period_start, period_end, due_date)
        where dates are 'YYYY-MM-DD' or None.
    """
    _log(cfg, "[bill] waiting for bill data / Current Charges row")

    # Ensure these are always defined so we never hit NameError
    amount_str: Optional[str] = None
    pdf_path: Optional[str] = None

    # 1. Wait for "Current Charges" so we know content rendered
    try:
        await page.wait_for_selector("text=Current Charges", timeout=15000)
    except PWTimeoutError:
        _log(cfg, "[bill] 'Current Charges' didn't show in 15s, continuing anyway")

    # 2. Extract the amount next to "Current Charges"
    try:
        charges_block = page.get_by_role(
            "button",
            name=re.compile(r"current\s*charges", re.I),
        ).first
        await charges_block.wait_for(state="visible", timeout=5000)

        pii_span = charges_block.locator("span.contains-PII").first
        text_val = (await pii_span.text_content() or "").strip()
        _log(cfg, f"[bill] span.contains-PII text: {text_val!r}")

        m = CURRENCY_RE.search(text_val)
        if m:
            amount_str = m.group(0)
            _log(cfg, f"[bill] Parsed amount: {amount_str}")
        else:
            _log(cfg, "[bill] contains-PII span didn't match currency regex, trying fallback")
            fallback_txt = (await charges_block.inner_text() or "").strip()
            _log(cfg, f"[bill] fallback block text: {fallback_txt!r}")
            m2 = CURRENCY_RE.search(fallback_txt)
            if m2:
                amount_str = m2.group(0)
                _log(cfg, f"[bill] Parsed amount via fallback: {amount_str}")
            else:
                _log(cfg, "[bill] couldn't parse a $amount from fallback either")
    except Exception as e:
        _log(cfg, f"[bill] failed to extract amount: {e}")

    # 3. Download the PDF ("Download PDF" link)
    _log(cfg, "[bill] attempting Download PDF click")

    try:
        download_link = page.get_by_role("link", name=re.compile(r"download\s*pdf", re.I))
        if await download_link.count() == 0:
            download_link = page.get_by_role("button", name=re.compile(r"download\s*pdf", re.I))

        async with page.expect_download(timeout=15000) as dl_info:
            await download_link.first.click()

        download: Download = await dl_info.value

        out_dir = DEFAULT_DL_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        out_file = out_dir / "fios_bill.pdf"
        await download.save_as(str(out_file))

        pdf_path = str(out_file)
        _log(cfg, f"[bill] PDF saved to {pdf_path}")
    except Exception as e:
        _log(cfg, f"[bill] download failed: {e}")
        pdf_path = None

    # 4. Parse dates from the PDF we just downloaded
    stmt_iso = start_iso = end_iso = due_iso = None
    if pdf_path:
        stmt_iso, start_iso, end_iso, due_iso = _extract_fios_dates_from_pdf(pdf_path)
        _log(
            cfg,
            f"[bill] parsed dates: statement={stmt_iso}, "
            f"period={start_iso} → {end_iso}, due={due_iso}",
        )

    return amount_str, pdf_path, stmt_iso, start_iso, end_iso, due_iso


# -------- PDF text extraction --------

def _extract_text_pdfminer(pdf_path: str) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return extract_text(pdf_path)
    except Exception:
        return None


def _extract_text_pypdf(pdf_path: str) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None


# -------- Fios-specific date parsing --------

_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _month_name_to_int(s: str) -> Optional[int]:
    if not s:
        return None
    key = s.strip().lower()
    if key in _MONTH_MAP:
        return _MONTH_MAP[key]
    key = key[:3]
    return _MONTH_MAP.get(key)


def _extract_text_pdfminer(pdf_path: str) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        return extract_text(pdf_path)
    except Exception:
        return None


def _extract_text_pypdf(pdf_path: str) -> Optional[str]:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None


def _extract_fios_dates_from_pdf(pdf_path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Returns (statement_date, period_start, period_end, due_date) as 'YYYY-MM-DD' or None.

    - Bill Date:  "Bill Date: October 2, 2025"
    - Due Date:   "Total Due by October 27" (year may be omitted)
    - Period:     small "10/3 - 11/2" near Subtotal
    """
    text = _extract_text_pdfminer(pdf_path) or _extract_text_pypdf(pdf_path) or ""
    if not text.strip():
        return None, None, None, None

    squashed = re.sub(r"[ \t]+", " ", text)
    squashed = re.sub(r"\n{2,}", "\n", squashed)

    stmt_iso = None
    start_iso = None
    end_iso = None
    due_iso = None

    bill_year: Optional[int] = None
    bill_month: Optional[int] = None

    # ---- Bill Date: October 2, 2025 ----
    m = re.search(
        r"Bill\s+Date:\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
        squashed,
        re.I,
    )
    if m:
        mname, d_s, y_s = m.groups()
        bill_month = _month_name_to_int(mname)
        bill_year = int(y_s)
        day = int(d_s)
        if bill_month:
            bd = date(bill_year, bill_month, day)
            stmt_iso = bd.strftime("%Y-%m-%d")

    # ---- Total Due by October 27 (optional year) ----
    if bill_year and bill_month:
        m = re.search(
            r"Total\s+Due\s*by\s+([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?",
            squashed,
            re.I,
        )
        if m:
            mname, d_s, y_s = m.groups()
            due_month = _month_name_to_int(mname)
            due_day = int(d_s)
            if due_month:
                if y_s:
                    due_year = int(y_s)
                else:
                    due_year = bill_year
                    # If due month is "before" bill month (very rare), assume next year
                    if due_month < bill_month:
                        due_year += 1
                dd = date(due_year, due_month, due_day)
                due_iso = dd.strftime("%Y-%m-%d")

    # ---- Billing period: 10/3 - 11/2 ----
    # Don't overcomplicate: in Fios PDFs, the only mm/dd - mm/dd range is the period.
    if bill_year:
        m = re.search(r"(\d{1,2})/(\d{1,2})\s*[-–]\s*(\d{1,2})/(\d{1,2})", squashed)
        if m:
            sm, sd, em, ed = [int(x) for x in m.groups()]
            start_year = bill_year
            end_year = bill_year

            # Edge case: Dec → Jan rollover, e.g. 12/3 - 1/2 when bill date is in December.
            if em < sm:
                end_year = bill_year + 1

            try:
                start_dt = date(start_year, sm, sd)
                end_dt = date(end_year, em, ed)
                start_iso = start_dt.strftime("%Y-%m-%d")
                end_iso = end_dt.strftime("%Y-%m-%d")
            except ValueError:
                # If dates don't make sense, just leave them None
                pass

    return stmt_iso, start_iso, end_iso, due_iso


async def run_scraper(cfg: Config) -> Result:

    # Pre-set outputs to avoid UnboundLocalError on early exceptions
    amount_str: Optional[str] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None

    # Launch Chrome Stable (persistent profile, stealth flags, etc.)
    # pw, context = await open_real_chrome(slow_mo=cfg.slow_mo if cfg.slow_mo else 80)
    # page = await context.new_page()

    pw = await async_playwright().start()

    browser = await pw.chromium.launch(
        headless=not cfg.headful,
        slow_mo=cfg.slow_mo if cfg.slow_mo else 0,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context = await browser.new_context(
        accept_downloads=True,
    )

    page = await context.new_page()

    try:
        # Step 1: login page
        _log(cfg, "[nav] goto login page", LOGIN_URL)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        # Step 2: username
        await _fill_username(page, cfg)

        # Step 3: password
        await page.wait_for_load_state("networkidle")
        await _fill_password(page, cfg)

        # Step 4: post-login hurdles (verify interstitial -> secret question)
        await page.wait_for_load_state("networkidle")
        await _maybe_answer_security_question(page, cfg)

        # Guard: don’t proceed while still on /signin
        for _ in range(10):  # ~3s total
            if "/signin" not in page.url:
                break
            await page.wait_for_timeout(300)

        # DEBUG snapshot after hurdles
        try:
            post_hurdle_url = page.url
            post_hurdle_html = await page.content()
            short_post_hurdle_html = post_hurdle_html[:2000].replace("\n", "\\n")
            _log(cfg, f"[debug] after hurdles, current URL: {post_hurdle_url}")
            _log(cfg, f"[debug] after hurdles, DOM start: {short_post_hurdle_html}")
        except Exception as e:
            _log(cfg, f"[debug] failed to snapshot post-hurdle DOM: {e}")

        # Step 4.5: wait until account/billing UI is actually hydrated
        await _wait_for_account_landing(page, cfg)

        # Step 5: land on account and click Bill overview
        await page.wait_for_load_state("networkidle")
        await _goto_bill_overview(page, cfg)

        # Wait for bill page CONTENT, not navigation
        await page.wait_for_selector("text=Current Charges", timeout=20000)

        amount_str, pdf_path, stmt_iso, start_iso, end_iso, due_iso = \
            await _scrape_amount_and_download_pdf(page, context, cfg)


        # capture final url
        final_url = page.url

    # finally:
    #     await close_all(pw, context)

    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass

    return Result(
        amount=amount_str,
        amount_cents=_money_to_cents(amount_str or ""),
        pdf_path=pdf_path,
        final_url=final_url,
        statement_date=stmt_iso,
        period_start=start_iso,
        period_end=end_iso,
        due_date=due_iso,
    )

def parse_args(argv) -> Config:
    import argparse
    ap = argparse.ArgumentParser()

    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--sec-answer", dest="sec_answer", required=False, default=None,
                    help="Answer to the security question if it appears")

    ap.add_argument("--headful", action="store_true", help="Run with browser UI")
    ap.add_argument("--slow-mo", type=int, default=0, help="Playwright slowMo ms")
    ap.add_argument("--json", dest="json_out", action="store_true")
    ap.add_argument("--debug", action="store_true")

    args = ap.parse_args(argv)

    return Config(
        username=args.username,
        password=args.password,
        sec_answer=args.sec_answer,
        headful=args.headful,
        slow_mo=args.slow_mo,
        debug=args.debug,
        json_out=args.json_out,
    )


def main(argv=None):
    cfg = parse_args(argv or sys.argv[1:])
    res = asyncio.run(run_scraper(cfg))

    if cfg.json_out:
        print(json.dumps({
            "amount":         res.amount,
            "amount_cents":   res.amount_cents,
            "pdf_path":       res.pdf_path,
            "final_url":      res.final_url,
            "statement_date": res.statement_date,
            "period_start":   res.period_start,
            "period_end":     res.period_end,
            "due_date":       res.due_date,
        }, indent=2))
    else:
        print("Amount:", res.amount)
        print("Amount (cents):", res.amount_cents)
        print("PDF Path:", res.pdf_path)
        print("Final URL:", res.final_url)


if __name__ == "__main__":
    main()
