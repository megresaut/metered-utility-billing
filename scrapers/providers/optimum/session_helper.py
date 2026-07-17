#!/usr/bin/env python3
# optimum_session_helpers.py
"""
Helpers to export Playwright storage_state -> requests-friendly cookies +
dump localStorage keys. Intended to be called from inside your Playwright
flow once you have an authenticated context/page.
"""

import json
from pathlib import Path
from typing import Any, Dict, List


def dump_cookies_from_storage_state(storage_state_path: Path, out_cookies_path: Path):
    """
    Read Playwright storage_state JSON and write simplified cookies JSON used by requests.
    storage_state_path: path to Playwright storage_state JSON (contains 'cookies' key)
    out_cookies_path: file to write list-of-cookie objects
    """
    st = json.loads(storage_state_path.read_text())
    cookies = st.get("cookies", [])
    simple = []
    for c in cookies:
        simple.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "expires": c.get("expires"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite", None),
        })
    out_cookies_path.write_text(json.dumps(simple, indent=2))
    print("[session-helpers] wrote cookies to", out_cookies_path)


async def dump_local_storage(page, out_path: Path):
    """
    Dump window.localStorage keys/values from the given Playwright page.
    Call as: await dump_local_storage(page, Path("optimum_localstorage.json"))
    """
    try:
        data = await page.evaluate(
            "() => { const o = {}; for (let i=0;i<localStorage.length;i++){ const k = localStorage.key(i); o[k] = localStorage.getItem(k);} return o; }"
        )
        out_path.write_text(json.dumps(data, indent=2))
        print("[session-helpers] wrote localStorage to", out_path)
    except Exception as e:
        print("[session-helpers] failed to dump localStorage:", repr(e))
