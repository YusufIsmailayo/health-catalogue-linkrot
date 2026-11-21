"""Step 1: snapshot the health publishers' catalogue entries from the CKAN API.

I save every raw API response to Bronze, untouched, under a UTC timestamp. The
snapshot is the point: if an entry disappears next month, this is my record of
what the catalogue said on the day I looked.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from . import config


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def snapshot_publishers(slugs=None, out_root: Path | None = None) -> Path:
    """Page through package_search for each slug and write raw JSON to Bronze.

    Returns the timestamped snapshot directory.
    """
    slugs = slugs or list(config.PUBLISHERS)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    snap = (out_root or config.BRONZE / "catalogue") / stamp
    snap.mkdir(parents=True, exist_ok=False)
    s = _session()
    manifest = {"snapshot_utc": stamp, "api": config.CKAN_API, "user_agent": config.USER_AGENT, "files": []}

    for slug in slugs:
        start, page = 0, 0
        while True:
            params = {"fq": f"organization:{slug}", "rows": config.CKAN_PAGE_SIZE,
                      "start": start, "sort": "name asc"}
            r = s.get(f"{config.CKAN_API}/package_search", params=params,
                      timeout=(config.CONNECT_TIMEOUT_S, 120))
            r.raise_for_status()
            body = r.content
            path = snap / slug / f"page_{page:03d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)  # verbatim bytes, not re-serialised
            result = r.json()["result"]
            manifest["files"].append({
                "publisher": slug, "page": page, "request_url": r.url,
                "fetched_utc": datetime.now(timezone.utc).isoformat(),
                "path": str(path.relative_to(snap)), "sha256": hashlib.sha256(body).hexdigest(),
                "count_reported": result["count"], "rows_in_page": len(result["results"]),
            })
            start += config.CKAN_PAGE_SIZE
            page += 1
            time.sleep(config.CKAN_DELAY_S)
            if start >= result["count"]:
                break

    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return snap


def latest_snapshot() -> Path:
    snaps = sorted((config.BRONZE / "catalogue").glob("*Z"))
    if not snaps:
        raise FileNotFoundError("No catalogue snapshot in Bronze yet - run notebook 01.")
    return snaps[-1]


def _extras(pkg) -> dict:
    return {e.get("key"): e.get("value") for e in pkg.get("extras") or []}


def flatten(snap: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bronze JSON -> (datasets, resources) DataFrames. One resource row per link."""
    ds_rows, res_rows = [], []
    for f in sorted(snap.glob("*/page_*.json")):
        slug = f.parent.name
        for pkg in json.loads(f.read_bytes())["result"]["results"]:
            ex = _extras(pkg)
            harvested = any(k and k.startswith("harvest") for k in ex) or bool(pkg.get("harvest_object_id"))
            ds_rows.append({
                "publisher_slug": slug,
                "publisher_label": config.PUBLISHERS.get(slug, {}).get("label", slug),
                "publisher_role": config.PUBLISHERS.get(slug, {}).get("role"),
                "dataset_id": pkg["id"], "dataset_name": pkg["name"], "title": pkg.get("title"),
                "metadata_created": pkg.get("metadata_created"),
                "metadata_modified": pkg.get("metadata_modified"),
                "last_major_modification": ex.get("last_major_modification"),
                "num_resources": len(pkg.get("resources") or []),
                "harvest_marker": harvested,
                "import_source": ex.get("import_source"),
            })
            for res in pkg.get("resources") or []:
                res_rows.append({
                    "publisher_slug": slug, "dataset_id": pkg["id"], "dataset_name": pkg["name"],
                    "resource_id": res.get("id"), "resource_name": res.get("name"),
                    "format": res.get("format"), "url": (res.get("url") or "").strip(),
                    "resource_created": res.get("created"), "resource_last_modified": res.get("last_modified"),
                })
    ds = pd.DataFrame(ds_rows)
    res = pd.DataFrame(res_rows)
    for c in ["metadata_created", "metadata_modified", "last_major_modification"]:
        ds[c] = pd.to_datetime(ds[c], errors="coerce", format="mixed")
    return ds, res
