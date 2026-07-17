#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Starlink Billing Scraper

Flow:
- Go to login page
- Enter email → Next
- Enter password → Sign In
- Wait for 2FA screen
- Pause for terminal input (email 2FA code)
- Submit code
- Land on dashboard
- Navigate to Billing
- Download most recent invoice (top row)

No parsing yet — PDF download only.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
import random
import time

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


LOGIN_URL = "https://www.starlink.com/auth/login"


# ----------------------------
# Data models
# ----------------------------

@dataclass
class StarlinkResult:
    ok: bool
    error: Optional[str] = None
    pdf_path: Optional[str] = None


@dataclass
class ScraperConfig:
    email: str
    password: str
    headless: bool
    debug: bool
    download_dir: Path = Path("./downloads/starlink")


async def human_pause(min_s=0.5, max_s=1.8):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_type(locator, text: str):
    for ch in text:
        await locator.type(ch, delay=random.randint(60, 140))

# ----------------------------
# Scraper
# ----------------------------

class StarlinkScraper:
    def __init__(self, cfg: ScraperConfig):
        self.cfg = cfg

    async def run(self) -> StarlinkResult:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.cfg.headless,
                slow_mo=0,  # we control timing manually
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = await browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )

            page = await context.new_page()

            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")

                # Wait for the email input to actually exist
                await page.wait_for_selector('input[type="email"]', timeout=20_000)

                # Human settle time
                await human_pause(2.0, 4.0)


                await self._login_email(page)
                await self._login_password(page)
                await self._handle_2fa(page)
                await self._go_to_billing(page)
                pdf_path = await self._download_latest_invoice(page)

                await browser.close()

                return StarlinkResult(
                    ok=True,
                    pdf_path=str(pdf_path),
                )

            except Exception as e:
                if self.cfg.debug:
                    print("[DEBUG] Error — leaving browser open")
                    input("Press ENTER to close browser")
                await browser.close()
                return StarlinkResult(ok=False, error=str(e))

    # ----------------------------
    # Steps
    # ----------------------------

    async def _login_email(self, page):
        if self.cfg.debug:
            print("[DEBUG] Entering email")

        email_input = page.locator(
            'input[type="email"], input[name="email"], input[autocomplete="email"]'
        ).first
        await email_input.wait_for(state="visible", timeout=15_000)
        await human_pause(1.2, 2.6)
        await email_input.click()
        await human_type(email_input, self.cfg.email)

        await human_pause(0.8, 1.6)

        next_btn = page.locator("button", has_text="Next")
        await next_btn.hover()
        await human_pause(0.3, 0.8)
        await next_btn.click()

    async def _login_password(self, page):
        if self.cfg.debug:
            print("[DEBUG] Entering password")

        password_input = page.locator('input[type="password"]')
        await password_input.wait_for(state="visible", timeout=15_000)
        await human_pause(1.0, 2.2)
        await password_input.click()
        await human_type(password_input, self.cfg.password)

        await human_pause(0.6, 1.4)

        sign_in_btn = page.locator("button", has_text="Sign In")
        await sign_in_btn.hover()
        await human_pause(0.3, 0.7)
        await sign_in_btn.click()

    async def _handle_2fa(self, page):
        if self.cfg.debug:
            print("[DEBUG] Waiting for 2FA screen")

        try:
            await page.wait_for_selector(
                "text=Two-Step Verification",
                timeout=15_000,
            )
        except PWTimeoutError:
            # Some accounts may not require 2FA
            if self.cfg.debug:
                print("[DEBUG] No 2FA required")
            return

        print("\n=== 2FA REQUIRED ===")
        code = input("Enter the Starlink verification code: ").strip()

        code_input = page.locator('input[name="code"], input[type="text"]')
        await code_input.first.wait_for(state="visible", timeout=10_000)
        await human_pause(1.0, 2.0)
        await code_input.click()
        await human_type(code_input, code)

        await human_pause(0.6, 1.2)

        verify_btn = page.locator("button", has_text="Verify")
        await verify_btn.hover()
        await human_pause(0.2, 0.6)
        await verify_btn.click()


        # Wait for dashboard
        await page.wait_for_url(
            lambda url: "/account" in url,
            timeout=30_000,
        )

    async def _go_to_billing(self, page):
        if self.cfg.debug:
            print("[DEBUG] Navigating to Billing")

        billing_link = page.locator("a", has_text="Billing")
        await billing_link.wait_for(state="visible", timeout=15_000)

        await human_pause(1.2, 2.4)
        await billing_link.hover()
        await human_pause(0.3, 0.8)

        async with page.expect_navigation():
            await billing_link.click()


        await page.wait_for_selector(
            "text=Invoices",
            timeout=20_000,
        )

    async def _download_latest_invoice(self, page) -> Path:
        if self.cfg.debug:
            print("[DEBUG] Downloading latest invoice")

        self.cfg.download_dir.mkdir(parents=True, exist_ok=True)

        # First row, download icon on the far right
        first_download_btn = page.locator(
            'table tbody tr:first-child button, table tbody tr:first-child a'
        ).filter(has_text="")

        await human_pause(1.0, 2.0)
        await first_download_btn.last.hover()
        await human_pause(0.3, 0.7)

        async with page.expect_download() as download_info:
            await first_download_btn.last.click()


        download = await download_info.value
        path = self.cfg.download_dir / download.suggested_filename
        await download.save_as(path)

        if self.cfg.debug:
            print(f"[DEBUG] Saved invoice to {path}")

        return path


# ----------------------------
# CLI
# ----------------------------

def parse_args():
    import argparse

    p = argparse.ArgumentParser("Starlink Billing Scraper")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
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
        email=args.email,
        password=args.password,
        headless=not args.headful,
        debug=args.debug,
    )

    scraper = StarlinkScraper(cfg)
    result = await scraper.run()

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
