#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frontier Internet scraper (new site) with Bright Data proxy (no storage_state reuse).

Upgrades in this build:
- Human-like behavior (mouse wiggle, hover, scroll, realistic typing with small mistakes)
- Stealth init script (webdriver/languages/chrome/runtime/platform/touchpoints)
- Randomized viewport & timezone (configurable)
- Login race vs. "trouble signing you in" modal + Bright Data session rotation
- Robust address switch & in-page Statements tab navigation

Inputs:
  --username, --password, --address
  --headful, --slow-mo, --debug, --json

Env (optional):
  BRIGHT_PROXY_SERVER / BRIGHT_PROXY_USER / BRIGHT_PROXY_PASS
  FRONTIER_DOWNLOAD_DIR  default: providers/frontier/downloads
  FRONTIER_TIMEZONE_ID   default: America/New_York
"""

import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from common.browser import open_real_chrome, close_all  

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
    Page,
    Browser,
    BrowserContext,
    Download,
)

# ---------------- Paths & constants ----------------

HERE = Path(__file__).resolve()
SCRAPERS_ROOT = HERE.parents[2] if len(HERE.parents) >= 2 else HERE.parent
ENV_FILE = SCRAPERS_ROOT / "env"  # scrapers/env if repo layout matches your other scrapers

DATA_ROOT = HERE.parent
LOGIN_URL = "https://frontier.com/pages/login"

DEFAULT_DL_DIR = Path(
    os.getenv("FRONTIER_DOWNLOAD_DIR", str(DATA_ROOT / "downloads"))
).expanduser().resolve()

AMOUNT_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2}))")


# ---------------- Models ----------------

@dataclass
class Config:
    username: str
    password: str
    address: str
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 35_000
    debug: bool = False
    use_bright_proxy: bool = False
    bright_proxy: Optional[dict] = None
    timezone_id: str = os.getenv("FRONTIER_TIMEZONE_ID", "America/New_York")


@dataclass
class Result:
    ok: bool
    error: Optional[str] = None
    amount_str: Optional[str] = None
    amount_cents: Optional[int] = None
    statement_date: Optional[str] = None
    selected_address: Optional[str] = None
    pdf_path: Optional[str] = None
    final_url: Optional[str] = None


# ---------------- Env loading ----------------

def load_kv_env_file(path: Path) -> None:
    try:
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    except Exception:
        pass


# ---------------- Utils ----------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _money_to_cents(txt: str) -> Tuple[Optional[str], Optional[int]]:
    if not txt:
        return None, None
    m = AMOUNT_RE.search(txt)
    if not m:
        return None, None
    raw = m.group(1)
    try:
        cents = int(round(float(raw.replace(",", "")) * 100))
    except Exception:
        cents = None
    return f"${raw}", cents


def _rand_delay_ms(lo=35, hi=110) -> int:
    return random.randint(lo, hi)


async def _pause(page: Page, lo=120, hi=280):
    await page.wait_for_timeout(random.randint(lo, hi))


def _norm_addr(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    rep = {
        " road": " rd",
        " street": " st",
        " avenue": " ave",
        " boulevard": " blvd",
        " lane": " ln",
        " drive": " dr",
        " court": " ct",
        " circle": " cir",
        " place": " pl",
        " terrace": " ter",
        " highway": " hwy",
    }
    for k, v in rep.items():
        s = s.replace(k, v)
    return s


def _addr_fuzzy_contains(a: str, b: str) -> bool:
    na, nb = _norm_addr(a), _norm_addr(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    toks = nb.split()
    return len(toks) >= 3 and all(tok in na for tok in toks[:3])


# ---------------- Scraper ----------------

class FrontierScraper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.download_dir: Path = DEFAULT_DL_DIR
        self._pw = None
        self._active_proxy = None  # dict used for this browser launch (with session)

        # optional real-Chrome integration (headful)
        self._using_real_chrome: bool = False
        self._close_fn = None

    def log(self, *args):
        if self.cfg.debug:
            print("DEBUG:", *args, file=sys.stderr, flush=True)

    # ---------- Bright Data helpers ----------

    def _proxy_username_with_session(self, base_username: Optional[str]) -> Optional[str]:
        if not base_username:
            return None
        if "session-" in base_username:
            return base_username
        sess = "".join(
            random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10)
        )
        return f"{base_username}-session-{sess}"

    # def _build_launch_kwargs(self):
    #     """
    #     Build Chromium launch kwargs.
    #     Headless mode tries to look like a normal desktop Chrome.
    #     """
    #     # random-ish window size for headless
    #     w, h = random.choice(
    #         [(1280, 720), (1366, 768), (1440, 900), (1536, 864), (1600, 900)]
    #     )

    #     args = [
    #         "--no-sandbox",
    #         "--disable-dev-shm-usage",
    #         "--disable-blink-features=AutomationControlled",
    #         "--disable-infobars",
    #         "--disable-web-security",
    #         f"--lang=en-US,en",
    #         f"--window-size={w},{h}",
    #         "--disable-features=IsolateOrigins,site-per-process",
    #     ]

    #     if self.cfg.headless:
    #         # explicit new headless mode
    #         args.append("--headless=new")
    #     else:
    #         args.append("--start-maximized")

    #     launch_kwargs = dict(
    #         headless=self.cfg.headless,
    #         slow_mo=self.cfg.slow_mo_ms,
    #         args=args,
    #     )

    #     bright = self.cfg.bright_proxy if self.cfg.use_bright_proxy else None
    #     if bright and bright.get("server"):
    #         proxy_username = self._proxy_username_with_session(bright.get("username"))
    #         self._active_proxy = {
    #             "server": bright["server"],
    #             "username": proxy_username,
    #             "password": bright.get("password"),
    #         }
    #         launch_kwargs["proxy"] = self._active_proxy
    #         self.log(
    #             "[pw] using proxy:",
    #             self._active_proxy["server"],
    #             "user:",
    #             proxy_username,
    #         )
    #     else:
    #         self._active_proxy = None
    #     return launch_kwargs

    # async def _relaunch_with_new_session_proxy(self):
    #     try:
    #         if self.context:
    #             await self.context.close()
    #     except Exception:
    #         pass
    #     try:
    #         if self.browser:
    #             await self.browser.close()
    #     except Exception:
    #         pass

    #     if (
    #         self.cfg.use_bright_proxy
    #         and self.cfg.bright_proxy
    #         and self.cfg.bright_proxy.get("server")
    #     ):
    #         if self.cfg.bright_proxy.get("username"):
    #             self.cfg.bright_proxy["username"] = self._proxy_username_with_session(
    #                 self.cfg.bright_proxy["username"]
    #             )

    #     if not self._pw:
    #         self._pw = await async_playwright().start()

    #     launch_kwargs = self._build_launch_kwargs()
    #     self.browser = await self._pw.chromium.launch(**launch_kwargs)

    #     context_kwargs = self._build_context_kwargs(storage_state_path=None)  # fresh login after rotation
    #     self.context = await self.browser.new_context(**context_kwargs)
    #     self.page = await self.context.new_page()
    #     await self._install_stealth()
    #     if self.cfg.debug:
    #         self.page.on(
    #             "console",
    #             lambda m: print(
    #                 f"DEBUG[console] {m.type}: {m.text}", file=sys.stderr
    #             ),
    #         )

    # ---------- Human/stealth helpers ----------

    async def _install_stealth(self):
        """
        Stealth-ish patches to make both headful + headless look more like a real Chrome.
        """
        script = """
        (() => {
          const patch = () => {
            try {
              // webdriver flag
              Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

              // chrome runtime presence
              if (!window.chrome) {
                window.chrome = { runtime: {} };
              }

              // languages
              Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });

              // platform / vendor
              Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
              Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });

              // touchpoints
              Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 1 });

              // userAgentData (Chromium hint) – fake desktop Chrome if present
              try {
                if ('userAgentData' in navigator) {
                  const brands = [
                    { brand: 'Chromium', version: '119' },
                    { brand: 'Google Chrome', version: '119' },
                  ];
                  const uaData = {
                    brands,
                    mobile: false,
                    platform: 'macOS',
                    toString: () => 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                    getHighEntropyValues: async (hints) => ({
                      platform: 'macOS',
                      platformVersion: '13.0.0',
                      architecture: 'x86',
                      model: '',
                      uaFullVersion: '119.0.0.0',
                      fullVersionList: brands,
                    }),
                  };
                  Object.defineProperty(navigator, 'userAgentData', {
                    get: () => uaData,
                  });
                }
              } catch (e) {}

              // permissions.query fix for notifications (classic headless giveaway)
              const origQuery = (navigator.permissions && navigator.permissions.query) || null;
              if (origQuery) {
                navigator.permissions.query = (parameters) => {
                  if (parameters && parameters.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission });
                  }
                  return origQuery(parameters);
                };
              }

              // plugins / mimeTypes length – some sites just check > 0
              try {
                const fakePlugins = [{ name: 'Chrome PDF Plugin' }];
                const fakeMimeTypes = [{ type: 'application/pdf', suffixes: 'pdf' }];
                Object.defineProperty(navigator, 'plugins', {
                  get: () => fakePlugins,
                });
                Object.defineProperty(navigator, 'mimeTypes', {
                  get: () => fakeMimeTypes,
                });
              } catch (e) {}

              // WebGL vendor / renderer – avoid "Google SwiftShader" / "ANGLE" giveaways
              try {
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function (param) {
                  const VENDOR = 0x1F00;
                  const RENDERER = 0x1F01;
                  if (param === VENDOR) {
                    return 'Apple Inc.';
                  }
                  if (param === RENDERER) {
                    return 'Apple GPU';
                  }
                  return getParameter.call(this, param);
                };
              } catch (e) {}

            } catch (e) {
              // swallow
            }
          };
          patch();
        })();
        """
        try:
            await self.context.add_init_script(script)
        except Exception:
            pass

    def _rand_viewport(self):
        # 13"–16" laptop-ish viewports
        widths = [1280, 1366, 1440, 1536, 1600]
        heights = [720, 768, 800, 900]
        return random.choice(widths), random.choice(heights)

    def _build_context_kwargs(self, storage_state_path: Optional[str]):
        w, h = self._rand_viewport()
        ctx = {
            "accept_downloads": True,
            "viewport": {"width": w, "height": h},
            "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/119.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": self.cfg.timezone_id,
        }
        # storage_state_path intentionally ignored (no reuse)
        return ctx

    async def _mouse_wiggle(self, page: Page, x: int, y: int, radius: int = 15, steps: int = 8):
        try:
            await page.mouse.move(x, y)
            for _ in range(steps):
                jitter_x = x + random.randint(-radius, radius)
                jitter_y = y + random.randint(-radius, radius)
                await page.mouse.move(
                    jitter_x, jitter_y, steps=random.randint(1, 3)
                )
                await page.wait_for_timeout(random.randint(20, 90))
        except Exception:
            pass

    async def _human_hover(self, locator):
        try:
            box = await locator.bounding_box()
            if box:
                x = int(box["x"] + box["width"] * random.uniform(0.25, 0.8))
                y = int(box["y"] + box["height"] * random.uniform(0.3, 0.7))
                await self._mouse_wiggle(self.page, x, y, radius=12, steps=6)
            await locator.hover()
        except Exception:
            try:
                await locator.hover()
            except Exception:
                pass

    async def _human_scroll_into_view(self, locator):
        try:
            await locator.scroll_into_view_if_needed()
            await self.page.wait_for_timeout(random.randint(60, 180))
        except Exception:
            pass

    async def _human_type(
        self, locator, text: str, base_delay: Tuple[int, int] = (40, 120)
    ):
        await locator.click()
        await self.page.wait_for_timeout(random.randint(60, 180))
        for ch in text:
            # occasional micro-pauses
            if random.random() < 0.08:
                await self.page.wait_for_timeout(random.randint(120, 220))
            # occasional tiny mistake + backspace
            if random.random() < 0.025 and ch.isalpha():
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                await locator.type(wrong, delay=random.randint(*base_delay))
                await self.page.wait_for_timeout(random.randint(40, 120))
                await locator.press("Backspace")
            await locator.type(ch, delay=random.randint(*base_delay))
        # small blur/refocus like a person
        await self.page.wait_for_timeout(random.randint(80, 160))
        await self.page.mouse.move(
            random.randint(20, 200), random.randint(60, 180)
        )

    # ---------- Lifecycle (Chrome-aware) ----------

    async def _launch_browser_and_context(self):

        # Ensure download dir exists
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Clean up old session if any
        await self._close()


        proxy = {
            "server": "http://161.77.52.166:12323",
            "username": "14ae15df3f919",
            "password": "6ef4856e7c",
        }

        # 🔑 FRONTIER MUST USE REAL CHROME
        if self.cfg.headless:
            raise RuntimeError(
                "Frontier scraper must run headful with real Chrome"
            )

        # 🔑 Launch REAL Chrome
        self._pw, self.context = await open_real_chrome(
            slow_mo=self.cfg.slow_mo_ms or 0,
            proxy=proxy,
        )
        self._using_real_chrome = True

        # Inject stealth early (open_real_chrome already does this,
        # but safe to double-guard)
        try:
            await self.context.add_init_script(self._stealth_js())
        except Exception:
            pass

        self.page = await self.context.new_page()

        if self.cfg.debug:
            self.page.on(
                "console",
                lambda m: print(
                    f"DEBUG[console] {m.type}: {m.text}",
                    file=sys.stderr,
                    flush=True,
                ),
            )

        async def _closer():
            await close_all(self._pw, self.context)

        self._close_fn = _closer

    async def _close(self):
        if self._close_fn:
            try:
                await self._close_fn()
            except Exception:
                pass

        self.context = None
        self.browser = None
        self.page = None
        self._pw = None
        self._close_fn = None
        self._using_real_chrome = False

    async def __aenter__(self):
        await self._launch_browser_and_context()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log("[lifecycle] closing")
        await self._close()

    # -------------- Main --------------

    async def run(self) -> Result:
        if not self.cfg.username or not self.cfg.password or not self.cfg.address:
            return Result(
                ok=False,
                error="username, password, and address are required",
            )

        # Always perform a fresh login (no session reuse)
        self.log("[run] starting login flow")
        if not await self._login_with_retry():
            return Result(ok=False, error="Login failed")

        if not await self._ensure_dashboard():
            return Result(ok=False, error="Could not reach account dashboard")

        selected_addr = await self._ensure_address(self.cfg.address)
        if selected_addr is None:
            return Result(
                ok=False, error="Desired address not found in dropdown"
            )

        if not await self._goto_billing_statements():
            return Result(
                ok=False,
                error="Could not open Billing history / Statements",
            )

        stmt_date, amt_str, amt_cents, pdf_path = (
            await self._download_latest_and_amount()
        )
        if not pdf_path:
            return Result(
                ok=False, error="Failed to download statement PDF"
            )

        return Result(
            ok=True,
            amount_str=amt_str,
            amount_cents=amt_cents,
            statement_date=stmt_date,
            selected_address=selected_addr,
            pdf_path=pdf_path,
            final_url=self.page.url,
        )

    # -------------- Session helpers --------------

    async def _is_logged_in(self) -> bool:
        try:
            has_menu = (
                await self.page.get_by_role(
                    "button",
                    name=re.compile(
                        r"^.*\b(My Account|Profile|[A-Z][a-z]+ LLC)\b", re.I
                    ),
                ).count()
                > 0
            )
            if has_menu:
                return True
        except Exception:
            pass
        try:
            await self.page.get_by_text(
                re.compile(r"^Hello, ", re.I)
            ).first.wait_for(timeout=4000)
            return True
        except Exception:
            return False

    async def _wait_for_login_or_trouble(
        self, timeout_ms: int = 22000
    ) -> str:
        page = self.page
        deadline = page._impl_obj._loop.time() + timeout_ms / 1000.0
        while page._impl_obj._loop.time() < deadline:
            try:
                if await page.get_by_role(
                    "button",
                    name=re.compile(
                        r"\b(My Account|Profile|[A-Z][a-z]+ LLC)\b", re.I
                    ),
                ).count() > 0:
                    return "ok"
            except Exception:
                pass
            try:
                if (
                    await page.get_by_text(
                        re.compile(r"^Hello, ", re.I)
                    ).count()
                    > 0
                ):
                    return "ok"
            except Exception:
                pass
            if await self._trouble_modal_present():
                return "trouble"
            await page.wait_for_timeout(200)
        return "timeout"

    async def _login_with_retry(self) -> bool:
        status = await self._login_once()
        if status == "ok":
            return True
        if status == "trouble":
            await self._dismiss_trouble_modal()
            self.log("[login] trouble modal -> retry same session")
            status = await self._login_once()
            if status == "ok":
                return True
        if (
            self.cfg.use_bright_proxy
            and self.cfg.bright_proxy
            and self.cfg.bright_proxy.get("server")
        ):
            self.log("[login] rotating Bright Data session and retrying...")
            await self._relaunch_with_new_session_proxy()
            status = await self._login_once()
            if status == "trouble":
                await self._dismiss_trouble_modal()
                status = await self._login_once()
            return status == "ok"
        return False

    async def _login_once(self) -> str:
        page = self.page
        try:
            await page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=self.cfg.nav_timeout_ms,
            )
        except PWTimeoutError:
            return "timeout"

        # Gentle human-like entrance: small scroll & idle
        try:
            await page.mouse.move(
                random.randint(40, 260), random.randint(80, 200)
            )
            await page.wait_for_timeout(random.randint(200, 400))
            await page.mouse.wheel(0, random.randint(100, 300))
            await page.wait_for_timeout(random.randint(200, 350))
        except Exception:
            pass

        email = await self._first_visible(
            [
                page.get_by_label("Email", exact=True),
                page.get_by_placeholder(re.compile(r"Email", re.I)),
                page.locator('input[type="email"]'),
            ]
        )
        pwd = await self._first_visible(
            [
                page.locator("input#password"),
                page.locator('input[name="password"][type="password"]'),
            ]
        )
        if not email or not pwd:
            self.log("[login] inputs not found")
            return "timeout"

        await self._human_scroll_into_view(email)
        await self._human_hover(email)
        await self._human_type(
            email, self.cfg.username, base_delay=(45, 130)
        )
        await _pause(page, 140, 260)

        await self._human_scroll_into_view(pwd)
        await self._human_hover(pwd)
        await self._human_type(
            pwd, self.cfg.password, base_delay=(48, 135)
        )
        await _pause(page, 140, 260)

        btn = await self._first_present(
            [
                page.get_by_role(
                    "button", name=re.compile(r"^sign in$", re.I)
                ),
                page.locator(
                    'button[type="submit"], input[type="submit"]'
                ),
            ]
        )
        if btn:
            await self._human_scroll_into_view(btn)
            await self._human_hover(btn)
            await _pause(page, 90, 180)
            try:
                await btn.click()
            except Exception:
                await pwd.press("Enter")
        else:
            await pwd.press("Enter")

        status = await self._wait_for_login_or_trouble(timeout_ms=26000)
        if status == "ok":
            self.log("[login] success")
        elif status == "trouble":
            self.log("[login] trouble modal detected")
        else:
            self.log("[login] timeout waiting for success or modal")
        return status

    async def _trouble_modal_present(self) -> bool:
        try:
            if (
                await self.page.get_by_text(
                    re.compile(
                        r"We['’]re having trouble signing you in", re.I
                    )
                ).count()
                > 0
            ):
                return True
        except Exception:
            pass
        try:
            dialog = self.page.locator('[role="dialog"]')
            if await dialog.count() > 0:
                if (
                    await dialog.get_by_role(
                        "button",
                        name=re.compile(r"^\s*close\s*$", re.I),
                    ).count()
                    > 0
                ):
                    return True
        except Exception:
            pass
        return False

    async def _dismiss_trouble_modal(self) -> bool:
        try:
            dialog = self.page.locator('[role="dialog"]').first
            await dialog.wait_for(state="visible", timeout=4000)
        except Exception:
            dialog = None

        for loc in [
            self.page.get_by_role(
                "button", name=re.compile(r"^\s*CLOSE\s*$", re.I)
            ).first,
            self.page.locator('button:has-text("CLOSE")').first,
            self.page.locator('[role="dialog"] button')
            .filter(has_text=re.compile(r"close", re.I))
            .first,
            self.page.locator(
                '[role="dialog"] [aria-label="Close"]'
            ).first,
        ]:
            try:
                await loc.click(timeout=1500)
                await self.page.wait_for_timeout(300)
                if not await self._trouble_modal_present():
                    return True
            except Exception:
                continue
        return not await self._trouble_modal_present()

    async def _ensure_dashboard(self) -> bool:
        if await self._is_logged_in():
            return True
        try:
            await self.page.goto(
                "https://frontier.com/myfrontier",
                wait_until="domcontentloaded",
                timeout=self.cfg.nav_timeout_ms,
            )
        except Exception:
            pass
        return await self._is_logged_in()

    # -------------- Address selection --------------

    async def _wait_for_addr_text(
        self, trigger, target_norm: str, timeout_ms: int = 12000
    ) -> Optional[str]:
        deadline = self.page._impl_obj._loop.time() + (
            timeout_ms / 1000.0
        )
        while self.page._impl_obj._loop.time() < deadline:
            try:
                txt = _norm_space(
                    (await trigger.text_content()) or ""
                )
                if _addr_fuzzy_contains(txt, target_norm):
                    return txt
            except Exception:
                pass
            await self.page.wait_for_timeout(200)
        return None

    async def _ensure_address(self, desired: str) -> Optional[str]:
        desired = _norm_space(desired)
        self.log("[addr] target:", desired)
        if not desired:
            return ""

        page = self.page
        addr_like = re.compile(
            r"\d{2,}.+\b(rd|st|ave|ct|dr|ln|blvd|cir|pl|ter|hwy)\b", re.I
        )

        # Find the address trigger (combobox/button showing the current address)
        addr_node = None
        for cand in [
            page.get_by_role("combobox")
            .filter(has_text=addr_like)
            .first,
            page.get_by_role("button")
            .filter(has_text=addr_like)
            .first,
            page.locator("button[aria-haspopup='listbox']")
            .filter(has_text=addr_like)
            .first,
            page.get_by_text(addr_like).first,
        ]:
            try:
                await cand.wait_for(timeout=4000)
                addr_node = cand
                break
            except Exception:
                continue

        current_text = ""
        if addr_node:
            try:
                current_text = (
                    await addr_node.text_content() or ""
                ).strip()
            except Exception:
                current_text = ""

        self.log("[addr] current:", current_text)

        if current_text and _addr_fuzzy_contains(current_text, desired):
            self.log("[addr] already correct")
            return current_text

        # Open dropdown (idempotent)
        try:
            await addr_node.click()
        except Exception:
            try:
                await addr_node.focus()
                await page.keyboard.press("Space")
            except Exception:
                pass

        # Locate dropdown/portal
        menu = page.locator(
            "[role='listbox'], [role='menu'], [data-radix-portal] div:visible"
        ).last
        try:
            await menu.wait_for(state="visible", timeout=8000)
        except Exception:
            self.log("[addr] menu did not appear; retrying click")
            try:
                await addr_node.click()
                await menu.wait_for(state="visible", timeout=8000)
            except Exception:
                self.log("[addr] menu still not visible")
                return None

        # Collect options and pick best fuzzy match
        options = menu.locator(
            ":is([role='option'],[role='menuitem'],button,a,div)"
        ).filter(has_text=addr_like)
        try:
            count = await options.count()
        except Exception:
            count = 0

        if count == 0:
            self.log("[addr] no options found in menu")
            return None

        best_text = None
        best_idx = None
        texts = []
        for i in range(count):
            t = _norm_space(await options.nth(i).inner_text())
            texts.append(t)
            if _addr_fuzzy_contains(t, desired) and best_text is None:
                best_text, best_idx = t, i

        if best_text is None:
            # permissive fallback: share >=2 of first 3 tokens
            desired_tokens = _norm_addr(desired).split()
            for i, t in enumerate(texts):
                toks = _norm_addr(t).split()
                if (
                    sum(tok in toks for tok in desired_tokens[:3]) >= 2
                ):
                    best_text, best_idx = t, i
                    break

        if best_text is None:
            self.log(
                "[addr] no dropdown match for",
                desired,
                " options:",
                texts,
            )
            return None

        self.log("[addr] selecting:", best_text)
        opt = options.nth(best_idx)

        # Robust click with fallbacks; DO NOT wait for navigation here
        clicked = False
        for attempt in range(4):
            try:
                if attempt == 0:
                    await opt.click(timeout=2500)
                elif attempt == 1:
                    await opt.click(force=True, timeout=2500)
                elif attempt == 2:
                    await opt.evaluate("el => el.click()")
                else:
                    await opt.dispatch_event("pointerdown")
                    await opt.dispatch_event("pointerup")
                clicked = True
                break
            except Exception:
                await page.wait_for_timeout(120)

        if not clicked:
            self.log("[addr] direct click failed; using keyboard fallback")
            try:
                await addr_node.click()
                await menu.wait_for(state="visible", timeout=5000)
                await page.keyboard.press("Enter")
                clicked = True
            except Exception:
                pass

        if not clicked:
            self.log("[addr] could not click address option")
            return None

        # Confirm by polling the trigger text; the page does NOT navigate
        try:
            await page.wait_for_timeout(250)
            if await menu.is_visible():
                await page.keyboard.press("Escape")
        except Exception:
            pass

        confirmed = await self._wait_for_addr_text(
            addr_node, best_text, timeout_ms=12000
        )
        if not confirmed:
            try:
                final_txt = _norm_space(
                    (await addr_node.text_content()) or ""
                )
            except Exception:
                final_txt = ""
            if not final_txt or not _addr_fuzzy_contains(
                final_txt, best_text
            ):
                self.log(
                    "[addr] selection not reflected in trigger text"
                )
                return None
            confirmed = final_txt

        self.log("[addr] selected:", confirmed)
        return confirmed

    # -------------- Navigate to statements --------------

    async def _click_with_fallbacks(self, locator) -> bool:
        try:
            await locator.scroll_into_view_if_needed()
        except Exception:
            pass
        for fn in (
            lambda: locator.click(),
            lambda: locator.click(force=True),
            lambda: locator.evaluate("el => el.click()"),
            lambda: locator.dispatch_event("pointerdown"),
        ):
            try:
                await fn()
                return True
            except Exception:
                await self.page.wait_for_timeout(120)
        return False

    async def _wait_for_statements_content(
        self, timeout_ms: int = 12000
    ) -> bool:
        """Wait until statements header or a 'Download bill' row appears."""
        page = self.page
        deadline = page._impl_obj._loop.time() + timeout_ms / 1000.0
        while page._impl_obj._loop.time() < deadline:
            try:
                if (
                    await page.locator(
                        "text=/\\bBilling statements\\b/i"
                    ).count()
                    > 0
                ):
                    return True
            except Exception:
                pass
            try:
                if (
                    await page.locator(
                        "li:has-text('Download bill'), div:has-text('Download bill')"
                    ).count()
                    > 0
                ):
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(200)
        return False

    async def _goto_billing_statements(self) -> bool:
        page = self.page
        self.log("[nav] go to Billing history / Statements")

        def url_is_billing() -> bool:
            try:
                return bool(
                    re.search(r"billing[-_/]history", page.url, re.I)
                )
            except Exception:
                return False

        # 1) Determine if we are already on Billing History
        already_on_billing = url_is_billing()
        if not already_on_billing:
            try:
                tabs_present = (
                    await page.locator(
                        ':is([role="button"],div,button):has(h6:has-text("Statements"))'
                    ).count()
                    > 0
                )
                already_on_billing = tabs_present
            except Exception:
                already_on_billing = False

        # 2) If not on Billing History, try to open it; otherwise continue
        if not already_on_billing:
            opened = await self._click_if_visible(
                "link", re.compile(r"\bBilling history\b", re.I), 5000
            )
            if not opened:
                try:
                    await page.get_by_role(
                        "button", name=re.compile(r"\bmy billing\b", re.I)
                    ).click()
                    await page.get_by_role(
                        "link", name=re.compile(r"\bbilling history\b", re.I)
                    ).click()
                except Exception:
                    pass
            await page.wait_for_timeout(800)

        # ---- 3) CLICK THE RIGHT CONTROL: div[role="button"] that contains h6 "Statements"
        statements_btn = None
        candidates = [
            page.locator(
                "div[role='button']:has(h6:has-text('Statements'))"
            ).first,
            page.locator(
                "//h6[normalize-space()='Statements']/ancestor::*[@role='button'][1]"
            ).first,
            page.locator(
                ":is([role='button'],div,button):has(h6:has-text('Statements'))"
            ).first,
        ]
        for cand in candidates:
            try:
                await cand.wait_for(
                    state="visible", timeout=7000
                )
                statements_btn = cand
                break
            except Exception:
                continue

        if not statements_btn:
            self.log("[nav] statements button not found")
            return False

        await self._human_scroll_into_view(statements_btn)
        await self._human_hover(statements_btn)
        if not await self._click_with_fallbacks(statements_btn):
            self.log("[nav] statements button click failed")
            return False

        # Confirm: selected class toggled OR statements content visible
        for _ in range(24):  # ~6s
            try:
                cls = await statements_btn.get_attribute("class")
                if cls and "selectedTab" in cls:
                    self.log(
                        "[nav] statements button has selectedTab class"
                    )
                    break
            except Exception:
                pass
            if await self._wait_for_statements_content(timeout_ms=2500):
                return True
            await page.wait_for_timeout(250)

        if await self._wait_for_statements_content(timeout_ms=4000):
            self.log("[nav] statements content visible")
            return True

        self.log("[nav] statements content did not appear")
        return False

    async def _click_if_visible(
        self, role: Optional[str], name_regex: re.Pattern, timeout_ms: int = 4000
    ) -> bool:
        try:
            el = (
                self.page.get_by_role(role, name=name_regex)
                if role
                else self.page.locator(
                    f"text=/^{name_regex.pattern}$/i"
                )
            )
            await el.first.wait_for(
                state="visible", timeout=timeout_ms
            )
            await el.first.click()
            return True
        except Exception:
            return False

    # -------------- Download latest + parse amount --------------

    async def _download_latest_and_amount(
        self,
    ) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
        """
        On the Statements tab:
        - Find the first 'Download bill' button within the 'Billing statements' section
        - From the same row/card, extract the amount ($xxx.xx) and statement date
        - Click download and save to disk
        """
        page = self.page

        # Anchor on the "Billing statements" section
        section = page.locator(
            "section:has-text('Billing statements'), "
            "div:has(h2:has-text('Billing statements'))"
        ).first
        try:
            await section.wait_for(state="visible", timeout=10000)
        except Exception:
            self.log("[statements] section not visible")
            return None, None, None, None

        # The first Download button in the list
        dl_btn = section.locator(
            ":is(a,button)[data-testid^='download-bill'], "
            ":is(a,button):has-text('Download bill')"
        ).first
        try:
            await dl_btn.wait_for(state="visible", timeout=10000)
        except Exception:
            self.log("[statements] download button not found")
            return None, None, None, None

        # Row/card that contains both amount and the button
        row = dl_btn.locator(
            "xpath=ancestor::*[contains(@class,'StatementCard_row') "
            "or contains(@class,'Card_container') "
            "or contains(@class,'HistoryContainer_container')][1]"
        )
        try:
            await row.wait_for(state="visible", timeout=5000)
        except Exception:
            row = section  # fallback to the section if ancestor heuristic fails

        # Extract amount from the same row
        amount_node = row.locator(
            "text=/\\$\\s*[0-9][0-9,]*\\.\\d{2}/"
        ).first
        amount_str = None
        amount_cents = None
        try:
            await amount_node.wait_for(timeout=5000)
            amount_str_raw = await amount_node.inner_text()
            amount_str = _norm_space(amount_str_raw)
            amount_str, amount_cents = _money_to_cents(amount_str)
        except Exception:
            # last resort: inner text of row
            try:
                row_text = _norm_space(await row.inner_text())
                amount_str, amount_cents = _money_to_cents(row_text)
            except Exception:
                pass

        # Extract statement date from the row
        statement_date = None
        try:
            date_node = row.locator(
                "text=/[A-Za-z]+\\s+\\d{1,2},\\s+\\d{4}/"
            ).first
            await date_node.wait_for(timeout=2000)
            statement_date = _norm_space(await date_node.inner_text())
        except Exception:
            try:
                row_text = _norm_space(await row.inner_text())
                m = re.search(
                    r"([A-Za-z]+ \d{1,2}, \d{4})", row_text
                )
                statement_date = m.group(1) if m else None
            except Exception:
                pass

        # Prepare filename
        filename_hint = f"frontier_{(statement_date or 'latest').replace(',', '').replace(' ', '_')}.pdf"
        target = (self.download_dir / filename_hint).resolve()

        # Download with expect_download
        async with page.expect_download(timeout=30000) as dl_info:
            try:
                await dl_btn.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                await dl_btn.click()
            except Exception:
                await dl_btn.click(force=True)
        dl: Download = await dl_info.value

        # Avoid clobbering
        i, stem, suf = 1, target.stem, target.suffix
        while target.exists():
            target = target.with_name(f"{stem}-{i}{suf}")
            i += 1
        await dl.save_as(str(target))

        try:
            head = target.read_bytes()[:4]
            if head != b"%PDF":
                self.log("[download] not a PDF header; got:", head)
        except Exception:
            pass

        self.log(f"[download] saved via expect_download to {target}")
        return statement_date, amount_str, amount_cents, str(target)

    # -------------- Generic helpers --------------

    async def _first_visible(self, locators: List, timeout: int = 7000):
        for loc in locators:
            try:
                await loc.first.wait_for(
                    state="visible", timeout=timeout
                )
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


# ---------------- CLI ----------------

def _parse_args(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="Frontier Internet latest statement downloader (Bright proxy, no session reuse)"
    )
    p.add_argument(
        "--username", default=os.getenv("FRONTIER_USERNAME", "")
    )
    p.add_argument(
        "--password", default=os.getenv("FRONTIER_PASSWORD", "")
    )
    p.add_argument(
        "--address",
        required=True,
        help="Service address to select (fuzzy match ok)",
    )
    p.add_argument(
        "--headful", action="store_true", help="Show browser window"
    )
    p.add_argument(
        "--slow-mo",
        type=int,
        default=int(os.getenv("FRONTIER_SLOW_MO_MS", "0")),
        help="Slow motion ms",
    )
    p.add_argument(
        "--debug", action="store_true", help="Verbose debug logging"
    )
    p.add_argument(
        "--json", action="store_true", help="Print JSON result"
    )
    return p.parse_args(argv)


async def main(argv=None):
    # Load scrapers/env first (without overriding env already set)
    load_kv_env_file(ENV_FILE)

    args = _parse_args(argv)

    # Bright proxy config
    bright_server = os.getenv("BRIGHT_PROXY_SERVER")
    bright_user = os.getenv("BRIGHT_PROXY_USER")
    bright_pass = os.getenv("BRIGHT_PROXY_PASS")

    cfg = Config(
        username=args.username,
        password=args.password,
        address=args.address,
        headless=not args.headful,  # headful -> real Chrome path
        slow_mo_ms=args.slow_mo,
        debug=args.debug,
        use_bright_proxy=bool(bright_server),
        bright_proxy={
            "server": bright_server,
            "username": bright_user,
            "password": bright_pass,
        }
        if bright_server
        else None,
    )

    async with FrontierScraper(cfg) as s:
        result = await s.run()

    if not result.ok:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "amount_str": result.amount_str,
                    "amount_cents": result.amount_cents,
                    "statement_date": result.statement_date,
                    "selected_address": result.selected_address,
                    "pdf_path": result.pdf_path,
                    "final_url": result.final_url,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(result.amount_str or "")


if __name__ == "__main__":
    asyncio.run(main())
