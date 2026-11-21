"""Step 2a: a deliberately slow, polite HTTP link checker.

My rules, in order of importance:
  * Never more than one request in flight per host, and at least
    PER_HOST_DELAY_S between requests to the same host.
  * HEAD first. Fall back to a GET (headers only, body never read) only when
    HEAD returned an error status, because some servers mishandle HEAD.
  * No retries. A timeout is recorded as a timeout. A 429 is recorded as a 429
    and I then leave that host alone for at least RATE_LIMIT_BACKOFF_S.
  * WAF challenge responses (AWS WAF's 202 + x-amzn-waf-action, Cloudflare's
    cf-mitigated) are flagged, never counted as OK. A challenge is the server
    saying "I won't tell a bot", not "this exists".
  * Results go to a JSONL file one line per URL, so a run can be resumed
    without re-requesting anything already checked.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from . import config

_write_lock = threading.Lock()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "*/*"})
    s.max_redirects = config.MAX_REDIRECTS
    no_retry = HTTPAdapter(max_retries=0)
    s.mount("http://", no_retry)
    s.mount("https://", no_retry)
    return s


def _challenge(resp: requests.Response) -> str | None:
    h = {k.lower(): v for k, v in resp.headers.items()}
    if "x-amzn-waf-action" in h:
        return f"aws-waf:{h['x-amzn-waf-action']}"
    if h.get("cf-mitigated"):
        return f"cloudflare:{h['cf-mitigated']}"
    return None


def _error_kind(exc: Exception) -> str:
    """Name the failure class. The verbatim message is stored alongside."""
    msg = str(exc)
    if isinstance(exc, requests.exceptions.ProxyError):
        # This container's egress proxy reports an upstream DNS/connect failure as a
        # 502 on the CONNECT tunnel, and an upstream timeout as a 504.
        m = re.search(r"Tunnel connection failed: (\d{3})", msg)
        code = m.group(1) if m else None
        return {"502": "tunnel_502_dns_or_connect", "504": "timeout",
                "403": "proxy_policy_403"}.get(code, "proxy_other")
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl_error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema)):
        return "invalid_url"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if re.search(r"NameResolution|Failed to resolve|Name or service not known|nodename nor servname", msg):
            return "dns_failure"
        if re.search(r"Connection refused|Errno 111", msg):
            return "connection_refused"
        if re.search(r"reset|RemoteDisconnected|aborted", msg, re.I):
            return "connection_reset"
        return "connection_error"
    return "other_error"


# --- Circuit breaker ----------------------------------------------------------
# If a host answers CHALLENGE_TRIP requests in a row with a WAF challenge, every
# further request there is wasted load: it will only ever say "prove you're
# human". From then on I don't go there, and I record where the redirect chain
# had got to, which is still evidence (a specific page, or just the homepage?).
CHALLENGE_TRIP = 10
_breaker_lock = threading.Lock()
_challenge_streak: dict[str, int] = defaultdict(int)
TRIPPED: set[str] = set()


def _note_challenge(host: str, challenged: bool):
    with _breaker_lock:
        if challenged:
            _challenge_streak[host] += 1
            if _challenge_streak[host] >= CHALLENGE_TRIP:
                TRIPPED.add(host)
        else:
            _challenge_streak[host] = 0


def _blank(method):
    return {"method": method, "status": None, "final_url": None, "redirect_chain": [], "redirect_urls": [],
            "challenge": None, "server": None, "content_type": None, "retry_after": None,
            "error_kind": None, "error_message": None}


def _one(s: requests.Session, method: str, url: str) -> dict:
    """One logical request, following redirects myself, hop by hop.

    Doing it by hand lets me stop before a hop that would land on a tripped host.
    """
    t0 = time.monotonic()
    rec = _blank(method)
    current = url
    try:
        for _hop in range(config.MAX_REDIRECTS + 1):
            hop_host = urlsplit(current).netloc.lower()
            if hop_host in TRIPPED:
                rec.update({"final_url": current, "error_kind": "stopped_at_challenging_host",
                            "error_message": f"not requested: {hop_host} returned {CHALLENGE_TRIP}+ WAF challenges in a row"})
                break
            r = s.request(method, current, allow_redirects=False, stream=True,
                          timeout=(config.CONNECT_TIMEOUT_S, config.READ_TIMEOUT_S))
            loc = r.headers.get("Location")
            if r.is_redirect and loc:
                rec["redirect_chain"].append(r.status_code)
                rec["redirect_urls"].append(loc)
                r.close()
                current = urljoin(current, loc)
                continue
            ch = _challenge(r)
            _note_challenge(hop_host, bool(ch))
            rec.update({"status": r.status_code, "final_url": current, "challenge": ch,
                        "server": r.headers.get("Server"), "content_type": r.headers.get("Content-Type"),
                        "retry_after": r.headers.get("Retry-After")})
            r.close()  # I never read the body
            break
        else:
            rec.update({"final_url": current, "error_kind": "too_many_redirects",
                        "error_message": f"more than {config.MAX_REDIRECTS} redirects"})
    except Exception as exc:  # noqa: BLE001 - I want every failure recorded, not raised
        rec.update({"final_url": current if current != url else None, "error_kind": _error_kind(exc),
                    "error_message": f"{type(exc).__name__}: {exc}"[:1000]})
    rec["elapsed_s"] = round(time.monotonic() - t0, 3)
    return rec


def check_url(s: requests.Session, url: str, pace) -> dict:
    """HEAD, then (only if HEAD came back as an error) a single GET."""
    out = {"url": url, "checked_utc": datetime.now(timezone.utc).isoformat()}
    if not url or urlsplit(url).scheme not in ("http", "https"):
        out.update({"head": None, "get": None, "error_kind": "invalid_url"})
        return out
    if urlsplit(url).netloc.lower() in TRIPPED:
        out.update({"head": {**_blank("HEAD"), "final_url": url, "error_kind": "stopped_at_challenging_host",
                             "error_message": "not requested: host already returning WAF challenges to every request"},
                    "get": None})
        return out  # no pace(): nothing was sent
    pace()
    head = _one(s, "HEAD", url)
    out["head"] = head
    out["get"] = None
    needs_get = (
        head["status"] is not None and head["status"] >= 400
        and head["status"] != 429          # it asked me to slow down; I comply
        and not head["challenge"]          # a challenge will challenge GET too
    )
    if needs_get:
        pace()
        out["get"] = _one(s, "GET", url)
    return out


def _host_worker(host: str, urls: list[str], out_path: Path, progress: dict):
    s = _session()
    last = [0.0]
    extra_wait = [0.0]

    def pace():
        gap = config.PER_HOST_DELAY_S + extra_wait[0]
        wait = last[0] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        extra_wait[0] = 0.0
        last[0] = time.monotonic()

    for url in urls:
        rec = check_url(s, url, pace)
        rec["host"] = host
        final = rec.get("get") or rec.get("head") or {}
        if final.get("status") == 429 or (rec.get("head") or {}).get("status") == 429:
            ra = (rec.get("head") or {}).get("retry_after")
            try:
                ra = float(ra)
            except (TypeError, ValueError):
                ra = 0.0
            extra_wait[0] = max(config.RATE_LIMIT_BACKOFF_S, min(ra, 600))
        with _write_lock:
            with out_path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            progress["done"] += 1
    s.close()


def load_results(out_path: Path) -> list[dict]:
    if not out_path.exists():
        return []
    return [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]


def run(urls, out_path: Path, log_every: int = 200) -> Path:
    """Check every distinct URL not already in out_path. Resumable."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {r["url"] for r in load_results(out_path)}
    todo = [u for u in dict.fromkeys(urls) if u not in done]
    by_host = defaultdict(list)
    for u in todo:
        by_host[urlsplit(u).netloc.lower() or "<none>"].append(u)
    # Biggest hosts first, so the long tail of small hosts fills in around them.
    jobs = sorted(by_host.items(), key=lambda kv: -len(kv[1]))
    progress = {"done": 0, "total": len(todo)}
    print(f"{len(done)} already checked; {len(todo)} to check across {len(by_host)} hosts")

    stop = threading.Event()

    def reporter():
        while not stop.wait(60):
            print(f"  {datetime.now(timezone.utc):%H:%M:%S}Z  {progress['done']}/{progress['total']}", flush=True)

    t = threading.Thread(target=reporter, daemon=True)
    t.start()
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        list(ex.map(lambda kv: _host_worker(kv[0], kv[1], out_path, progress), jobs))
    stop.set()
    print(f"finished: {progress['done']}/{progress['total']}")
    return out_path
