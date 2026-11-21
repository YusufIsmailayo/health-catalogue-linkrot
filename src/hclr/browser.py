"""Step 2b: real-browser re-check for links the HTTP checker couldn't judge.

This mirrors the NDL team's Playwright fallback. I only send a browser at URLs
that came back 403, timed out, or hit a WAF challenge. A browser can run the
JavaScript challenge some sites put in front of bots and then see the real status.

What I do NOT do: disguise the browser. It runs as stock headless Chromium with
its own user-agent string. If a site's bot protection still blocks it, I record
that and move on. Getting past bot detection on a government site would undo
the whole point of this exercise.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config


def _is_challenged(status, headers: dict, title: str) -> bool:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if "x-amzn-waf-action" in h or h.get("cf-mitigated"):
        return True
    return status in (202, 403, 503) and title.strip().lower() in ("just a moment...", "attention required!", "")


def _visit(page, url: str, settle_ms: int | None = None) -> dict:
    navs = []

    def on_resp(r):
        try:
            if r.request.is_navigation_request() and r.frame == page.main_frame:
                navs.append((r.status, r.url, r.headers))
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_resp)
    err = None
    t0 = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.BROWSER_NAV_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 - downloads and timeouts land here
        err = f"{type(exc).__name__}: {exc}"[:500]
    # If the first answer looks like a challenge, give its JavaScript time to resolve and reload.
    if navs and navs[-1][0] in (202, 403, 503):
        page.wait_for_timeout(settle_ms or config.BROWSER_SETTLE_MS)
    else:
        page.wait_for_timeout(500)
    page.remove_listener("response", on_resp)
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    status, final_url, headers = navs[-1] if navs else (None, None, {})
    is_download = bool(err and "Download is starting" in err)
    return {
        "browser_status": status, "browser_final_url": final_url,
        "browser_nav_chain": [n[0] for n in navs], "browser_title": title[:200],
        "browser_challenged": (not is_download) and _is_challenged(status, headers, title),
        "browser_download": is_download,
        "browser_error": None if is_download else err,
        "browser_elapsed_s": round(time.monotonic() - t0, 2),
    }


def run(urls, out_path: Path, settle_ms: int | None = None) -> Path:
    """Jupyter already runs an asyncio loop, and Playwright's sync API refuses to start inside
    one. So I do the browsing on a plain worker thread, which has no loop."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_run, list(urls), out_path, settle_ms).result()


def _run(urls, out_path: Path, settle_ms: int | None = None) -> Path:
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.exists():
        done = {json.loads(l)["url"] for l in out_path.read_text().splitlines() if l.strip()}
    by_host = defaultdict(list)
    for u in dict.fromkeys(urls):
        if u not in done:
            by_host[urlsplit(u).netloc.lower()].append(u)
    print(f"{len(done)} already re-checked; {sum(map(len, by_host.values()))} to go across {len(by_host)} hosts")

    with sync_playwright() as p:
        # The container ships one pre-installed Chromium; I point Playwright at it explicitly.
        browser = p.chromium.launch(executable_path=config.CHROMIUM_PATH)
        for host, host_urls in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
            # One context per host keeps any cookie a solved challenge sets, so
            # the site only has to challenge me once, not once per URL.
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            page.on("download", lambda d: d.cancel())
            challenged_in_a_row, attempts = 0, 0
            for i, url in enumerate(host_urls):
                if attempts >= config.BROWSER_HOST_PROBE and challenged_in_a_row == attempts:
                    rec = {"url": url, "host": host, "browser_skipped": "host_blocks_browser_after_probe"}
                else:
                    rec = {"url": url, "host": host, "browser_skipped": None, **_visit(page, url, settle_ms)}
                    attempts += 1
                    challenged_in_a_row = challenged_in_a_row + 1 if rec["browser_challenged"] else 0
                    time.sleep(config.BROWSER_DELAY_S)
                rec["checked_utc"] = datetime.now(timezone.utc).isoformat()
                with out_path.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            ctx.close()
            print(f"  {host}: {len(host_urls)} urls, {attempts} visited", flush=True)
        browser.close()
    return out_path
