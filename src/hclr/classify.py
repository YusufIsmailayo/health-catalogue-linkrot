"""Step 3: turn verbatim check results into the NDL's three classes.

The NDL taxonomy, which I mirror so my numbers can sit beside theirs:
  ok         - the link answered with a 2xx (and wasn't a bot challenge)
  hard_dead  - 404, 410, DNS failure, connection failure
  uncertain  - 403, timeout, 400, 500, 429, and anything else that isn't clearly either

A real-browser re-check can move a link out of "uncertain", but only when the
browser actually reached the page (no challenge left on screen).

Separately I flag *diagnostics* that don't change the class but do change how
much an "ok" means: a redirect to the site's homepage is a 200, but it is very
likely a soft 404.
"""
from __future__ import annotations

import math
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

HARD_DEAD_ERRORS = {"dns_failure", "tunnel_502_dns_or_connect", "connection_refused",
                    "connection_reset", "connection_error"}
HARD_DEAD_STATUS = {404, 410}
WEB_ARCHIVE_HOSTS = {"webarchive.nationalarchives.gov.uk"}
LEGACY_HOSTS = {"www.hscic.gov.uk", "hscic.gov.uk", "www.ic.nhs.uk", "ic.nhs.uk"}


def _reason(status, error, challenge) -> tuple[str, str]:
    """(class, reason) from the automated HTTP pass alone."""
    # pandas hands me NaN for "missing", and NaN is truthy - so normalise first.
    error = None if pd.isna(error) else error
    challenge = None if pd.isna(challenge) else challenge
    if status is None or pd.isna(status):
        status = None
    if error == "invalid_url":
        return "uncertain", "not_an_http_url"
    if error == "stopped_at_challenging_host":
        return "uncertain", "waf_challenge"
    if error in HARD_DEAD_ERRORS:
        return "hard_dead", "dns_or_connection_failure"
    if error == "timeout":
        return "uncertain", "timeout"
    if error:
        return "uncertain", error
    if challenge:
        return "uncertain", "waf_challenge"
    if status is None:
        return "uncertain", "no_response_recorded"
    s = int(status)
    if 200 <= s < 300:
        return "ok", f"http_{s}"
    if s in HARD_DEAD_STATUS:
        return "hard_dead", f"http_{s}"
    return "uncertain", f"http_{s}"


def _path_is_home(url: str | None) -> bool:
    if not isinstance(url, str) or not url:
        return False
    p = urlsplit(url).path.rstrip("/").lower()
    return p in ("", "/index.html", "/index.htm", "/default.aspx", "/home", "/index.php")


_TWO_LABEL_SUFFIXES = ("gov.uk", "nhs.uk", "co.uk", "org.uk", "ac.uk", "nhs.net", "police.uk", "ltd.uk")


def _bare(host: str) -> str:
    """Crude registrable domain: www.england.nhs.uk -> england.nhs.uk, assets.publishing.service.gov.uk -> service.gov.uk.

    Good enough to tell "moved to another organisation's site" from "same site, different subdomain".
    """
    h = host.lower().split(":")[0].removeprefix("www.")
    if h == "gov.uk" or h.endswith("publishing.service.gov.uk"):
        return "gov.uk"  # GOV.UK serves its attachments from its own asset host
    labels = h.split(".")
    keep = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def classify(links: pd.DataFrame) -> pd.DataFrame:
    df = links.copy()
    has_get = "get.status" in df.columns
    df["http_status"] = (df["get.status"].where(df["get.status"].notna(), df["head.status"])
                         if has_get else df["head.status"])
    get_err = df["get.error_kind"] if "get.error_kind" in df.columns else pd.Series(None, index=df.index)
    get_used = df["get.method"].notna() if "get.method" in df.columns else pd.Series(False, index=df.index)
    df["http_error"] = np.where(get_used, get_err, df.get("head.error_kind"))
    df["http_error"] = df["http_error"].where(df["http_error"].notna(), df.get("error_kind"))
    df["http_challenge"] = df.get("head.challenge")
    df["http_final_url"] = (df["get.final_url"].where(get_used, df["head.final_url"])
                            if has_get else df["head.final_url"])

    cr = [_reason(s, e, c) for s, e, c in zip(df["http_status"], df["http_error"], df["http_challenge"])]
    df["http_class"] = [c for c, _ in cr]
    df["http_reason"] = [r for _, r in cr]

    # Browser fallback: only overrides when the browser genuinely got through.
    df["final_class"], df["final_reason"], df["resolved_by"] = df["http_class"], df["http_reason"], "http"
    if "browser_status" in df.columns:
        for c, default in [("browser_skipped", None), ("browser_challenged", None), ("browser_download", False)]:
            if c not in df:
                df[c] = default
        challenged = df["browser_challenged"].astype("boolean").fillna(False).astype(bool)
        download = df["browser_download"].astype("boolean").fillna(False).astype(bool)
        visited = df["browser_skipped"].isna() & (df["browser_status"].notna() | download)
        clear = visited & ~challenged
        bs = df["browser_status"]
        ok = clear & (bs.between(200, 299) | download)
        dead = clear & bs.isin(list(HARD_DEAD_STATUS))
        other = clear & ~ok & ~dead
        df.loc[ok, ["final_class", "resolved_by"]] = ["ok", "browser"]
        df.loc[ok, "final_reason"] = "browser_" + bs[ok].astype("Int64").astype(str)
        df.loc[dead, ["final_class", "resolved_by"]] = ["hard_dead", "browser"]
        df.loc[dead, "final_reason"] = "browser_" + bs[dead].astype("Int64").astype(str)
        df.loc[other, "final_reason"] = "browser_" + bs[other].astype("Int64").astype(str)
        df.loc[other, "resolved_by"] = "browser"
        blocked = challenged | df["browser_skipped"].notna()
        df.loc[blocked & (df["final_class"] == "uncertain"), "final_reason"] = "waf_challenge_browser_also_blocked"
        final_url = df["browser_final_url"].where(clear, df["http_final_url"])
    else:
        final_url = df["http_final_url"]
    df["final_url_seen"] = final_url

    # Diagnostics - these never change the class.
    orig_host = df["url"].map(lambda u: urlsplit(u).netloc.lower() if u else "")
    fin_host = df["final_url_seen"].map(lambda u: urlsplit(u).netloc.lower() if isinstance(u, str) else "")
    df["host"] = orig_host
    df["diag_redirect_to_homepage"] = (df["final_class"] == "ok") & df["final_url_seen"].map(_path_is_home) \
        & ~df["url"].map(_path_is_home)
    df["diag_domain_changed"] = (fin_host != "") & (orig_host.map(_bare) != fin_host.map(_bare))
    df["diag_web_archive_link"] = orig_host.isin(WEB_ARCHIVE_HOSTS)
    df["diag_legacy_domain"] = orig_host.isin(LEGACY_HOSTS)
    return df


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval. Treats links as independent, which they aren't
    (links cluster within datasets), so read these as the *narrowest* honest band."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarise(frame: pd.DataFrame, by: str, cls: str = "final_class") -> pd.DataFrame:
    t = frame.pivot_table(index=by, columns=cls, values="url", aggfunc="size", fill_value=0)
    for c in ["ok", "hard_dead", "uncertain"]:
        if c not in t:
            t[c] = 0
    t = t[["ok", "hard_dead", "uncertain"]]
    t["links"] = t.sum(axis=1)
    for c in ["ok", "hard_dead", "uncertain"]:
        t[f"{c}_pct"] = 100 * t[c] / t["links"]
    ci = [wilson(int(k), int(n)) for k, n in zip(t["hard_dead"], t["links"])]
    t["hard_dead_ci_lo"] = [100 * a for a, _ in ci]
    t["hard_dead_ci_hi"] = [100 * b for _, b in ci]
    # The honest band for "broken": floor = only confirmed dead; ceiling = if every uncertain link were dead.
    t["broken_floor_pct"] = t["hard_dead_pct"]
    t["broken_ceiling_pct"] = t["hard_dead_pct"] + t["uncertain_pct"]
    return t.sort_values("hard_dead_pct", ascending=False)
