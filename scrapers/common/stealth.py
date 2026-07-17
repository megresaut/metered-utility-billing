# common/stealth.py
"""
Shared stealth utilities for scrapers that need to bypass bot detection
(Cloudflare Turnstile, reCAPTCHA, etc.).

Provides:
  - get_playwright_module()  — tries patchright first, falls back to playwright
  - COMPREHENSIVE_STEALTH_JS — full anti-fingerprinting init script
  - install_stealth(context) — applies stealth JS to a browser context
"""

import sys

# Module-level engine name, set by get_playwright_module()
_ENGINE_NAME = "playwright"


def get_playwright_module():
    """
    Try to import patchright (patched Playwright fork that fixes CDP
    Runtime.Enable leak).  Fall back to stock playwright if unavailable.

    Returns (async_playwright_fn, engine_name).
    """
    global _ENGINE_NAME
    try:
        from patchright.async_api import async_playwright
        _ENGINE_NAME = "patchright"
        return async_playwright, "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        _ENGINE_NAME = "playwright"
        print("WARN: patchright not installed, falling back to playwright", file=sys.stderr)
        return async_playwright, "playwright"


# ---------------------------------------------------------------------------
# Comprehensive stealth JS — adapted from Frontier scraper with improvements.
#
# Key fix vs. the old Optimum/Frontier versions: Chrome version is derived
# dynamically from navigator.userAgent instead of being hardcoded to '119'.
# ---------------------------------------------------------------------------

COMPREHENSIVE_STEALTH_JS = r"""
(() => {
  const patch = () => {
    try {
      // ---- webdriver flag ----
      Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

      // ---- chrome runtime presence ----
      if (!window.chrome) {
        window.chrome = { runtime: {} };
      }

      // ---- languages ----
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });

      // ---- platform / vendor ----
      Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
      Object.defineProperty(navigator, 'vendor',   { get: () => 'Google Inc.' });

      // ---- touchpoints ----
      Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 1 });

      // ---- userAgentData (Chromium hint) ----
      try {
        if ('userAgentData' in navigator) {
          // Derive version from real UA string so it stays current
          const m = navigator.userAgent.match(/Chrome\/(\d+)/);
          const ver = m ? m[1] : '131';
          const brands = [
            { brand: 'Chromium',      version: ver },
            { brand: 'Google Chrome', version: ver },
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
              uaFullVersion: ver + '.0.0.0',
              fullVersionList: brands,
            }),
          };
          Object.defineProperty(navigator, 'userAgentData', {
            get: () => uaData,
          });
        }
      } catch (e) {}

      // ---- permissions.query fix (classic headless giveaway) ----
      const origQuery = (navigator.permissions && navigator.permissions.query) || null;
      if (origQuery) {
        navigator.permissions.query = (parameters) => {
          if (parameters && parameters.name === 'notifications') {
            return Promise.resolve({ state: Notification.permission });
          }
          return origQuery(parameters);
        };
      }

      // ---- plugins / mimeTypes (some sites just check length > 0) ----
      try {
        const fakePlugins   = [{ name: 'Chrome PDF Plugin' }];
        const fakeMimeTypes = [{ type: 'application/pdf', suffixes: 'pdf' }];
        Object.defineProperty(navigator, 'plugins',   { get: () => fakePlugins });
        Object.defineProperty(navigator, 'mimeTypes', { get: () => fakeMimeTypes });
      } catch (e) {}

      // ---- WebGL vendor / renderer ----
      try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function (param) {
          const VENDOR   = 0x1F00;
          const RENDERER = 0x1F01;
          if (param === VENDOR)   return 'Apple Inc.';
          if (param === RENDERER) return 'Apple GPU';
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


LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
    "--disable-blink-features=AutomationControlled",
    "--force-color-profile=srgb",
    "--lang=en-US,en",
    "--ignore-gpu-blocklist",
    "--enable-webgl",
    "--password-store=basic",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-features=NetworkServiceSandbox",  # fix DNS in patchright persistent contexts
]


async def install_stealth(context, engine=None):
    """
    Apply comprehensive stealth JS to a browser context.

    IMPORTANT: patchright + add_init_script on persistent contexts is broken
    (causes DNS resolution failure).  When engine == "patchright", we skip
    add_init_script entirely — patchright already patches the main detection
    vector (CDP Runtime.Enable).  Stealth JS can still be applied per-page
    via apply_stealth_to_page() after navigation.
    """
    if engine is None:
        engine = _ENGINE_NAME

    if engine == "patchright":
        # patchright handles the critical CDP leak; skip add_init_script
        # which is incompatible with patchright persistent contexts.
        return

    try:
        await context.add_init_script(COMPREHENSIVE_STEALTH_JS)
    except Exception:
        pass


async def apply_stealth_to_page(page):
    """
    Apply stealth patches to an already-loaded page via evaluate().
    Use this with patchright where add_init_script is not available.
    """
    try:
        await page.evaluate(COMPREHENSIVE_STEALTH_JS)
    except Exception:
        pass
