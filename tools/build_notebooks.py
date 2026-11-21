"""Builds the notebooks from the cell sources below, so the .ipynb files diff cleanly.

Usage:
    python tools/build_notebooks.py                   # build only notebooks that don't exist yet
    python tools/build_notebooks.py 03_finding 04_chart   # rebuild these (wipes their outputs)
"""
import sys
from pathlib import Path

import nbformat as nbf

NB = Path(__file__).resolve().parents[1] / "notebooks"

SETUP = """\
import sys, pathlib
ROOT = pathlib.Path.cwd().resolve()
ROOT = ROOT.parent if ROOT.name == "notebooks" else ROOT
sys.path.insert(0, str(ROOT / "src"))
import pandas as pd
pd.set_option("display.width", 160); pd.set_option("display.max_columns", 30)
from hclr import config
"""


def md(s):
    return nbf.v4.new_markdown_cell(s)


def code(s):
    return nbf.v4.new_code_cell(s)


def build(name, cells):
    wanted = [a.removesuffix(".ipynb") for a in sys.argv[1:]]
    if (NB / name).exists() and name.removesuffix(".ipynb") not in wanted:
        return  # I don't clobber an executed notebook unless asked to
    nb = nbf.v4.new_notebook()
    nb.cells = cells
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    nbf.write(nb, NB / name)


build("01_catalogue_snapshot.ipynb", [
    md("""# 01 · Catalogue snapshot (Bronze → Silver)

I pull every dataset listing for the health and social care publishers from the
data.gov.uk CKAN API and save the raw responses to Bronze under a UTC timestamp.
That raw snapshot is evidence in its own right. It records what the catalogue
said on the day I looked, which is the same habit the article argues for.

**Endpoint.** `https://www.data.gov.uk/api/action/*` 301-redirects to
`https://ckan.publishing.service.gov.uk/api/action/*`, so I call the CKAN host directly.

**Slugs.** I confirmed these against the `organization` facet of `package_search`
on 21 November 2026. See `config.PUBLISHERS`. NHS Digital and Public Health England
are predecessor bodies whose entries were never re-homed. I keep them as separate
rows and never fold them into NHS England or UKHSA."""),
    code(SETUP),
    code("""\
from hclr import catalogue
# Re-running this cell takes a NEW dated snapshot; it never overwrites an old one.
RUN_SNAPSHOT = not list((config.BRONZE / "catalogue").glob("*Z"))
snap = catalogue.snapshot_publishers() if RUN_SNAPSHOT else catalogue.latest_snapshot()
print("snapshot:", snap.relative_to(ROOT))"""),
    code("""\
import json
manifest = json.loads((snap / "manifest.json").read_text())
pd.DataFrame(manifest["files"])[["publisher", "page", "count_reported", "rows_in_page", "fetched_utc", "sha256"]]"""),
    code("""\
datasets, resources = catalogue.flatten(snap)
datasets["snapshot_utc"] = resources["snapshot_utc"] = manifest["snapshot_utc"]
config.SILVER.mkdir(parents=True, exist_ok=True)
datasets.to_parquet(config.SILVER / "datasets.parquet", index=False)
resources.to_parquet(config.SILVER / "resources.parquet", index=False)
print(len(datasets), "datasets;", len(resources), "resource links;", resources["url"].nunique(), "distinct URLs")"""),
    md("""## What the snapshot holds, per publisher

`zero_resource_datasets` counts listings with no links left at all. Those are
catalogue entries that survive as metadata with nothing to follow."""),
    code("""\
per_pub = (datasets.groupby("publisher_slug")
    .agg(datasets=("dataset_id", "size"),
         zero_resource_datasets=("num_resources", lambda s: int((s == 0).sum())),
         harvest_marker=("harvest_marker", "sum"),
         oldest_modified=("metadata_modified", "min"),
         newest_modified=("metadata_modified", "max")))
per_pub = per_pub.join(resources.groupby("publisher_slug").agg(
    resource_links=("url", "size"), distinct_urls=("url", "nunique")))
per_pub"""),
    md("""## When were these records last touched?

I check this because a cluster of `metadata_modified` dates in mid-2026 would be the
cleanup's own fingerprint. If so, "last updated" in this snapshot partly records when
the NDL edited a listing, not when the publisher updated the data."""),
    code("""\
(datasets.assign(ym=datasets["metadata_modified"].dt.to_period("M"))
   .query("ym >= '2026-01'")
   .pivot_table(index="ym", columns="publisher_slug", values="dataset_id", aggfunc="count", fill_value=0))"""),
    code("""\
from urllib.parse import urlsplit
resources["host"] = resources["url"].map(lambda u: urlsplit(u).netloc.lower())
(resources.groupby(["publisher_slug", "host"]).size().rename("links")
   .reset_index().sort_values(["publisher_slug", "links"], ascending=[True, False])
   .groupby("publisher_slug").head(5))"""),
])

build("02_link_check.ipynb", [
    md("""# 02 · Link check (polite HTTP pass + real-browser fallback)

I check every **distinct** resource URL once and join the results back to every
catalogue link that uses it.

How I behave towards the servers (all set in `hclr/config.py`):
- a descriptive User-Agent that names the project;
- at most one request in flight per host, at least 2 s between requests to the same host, and at most 4 hosts at once;
- HEAD first, then one GET (headers only, body never read) only if HEAD returned an error;
- timeouts of 10 s to connect and 30 s to read, with **no retries**;
- a 429 is recorded as-is, and I then back off that host for at least 60 s;
- a **circuit breaker**: once a host has answered 10 requests in a row with a WAF challenge, I stop sending
  it requests and record the URL I'd reached. Every further request there would only ever be answered with
  "prove you're human".

Status codes and error messages are stored verbatim in the Bronze JSONL. Any
classification happens later, in notebook 03, so I can audit and change the rules
without touching a server again."""),
    code(SETUP),
    code("""\
from hclr import checker, browser
resources = pd.read_parquet(config.SILVER / "resources.parquet")
stamp = resources["snapshot_utc"].iloc[0]
LC_DIR = config.BRONZE / "linkcheck" / stamp
HTTP_OUT, BROWSER_OUT = LC_DIR / "http_results.jsonl", LC_DIR / "browser_results.jsonl"
urls = resources["url"].drop_duplicates().tolist()
print(len(urls), "distinct URLs to check ->", HTTP_OUT.relative_to(ROOT))"""),
    md("""## Pass 1: HTTP

This is resumable. If it is interrupted, re-running the cell carries on from where it stopped."""),
    code("checker.run(urls, HTTP_OUT);"),
    code("""\
http = pd.json_normalize(checker.load_results(HTTP_OUT))
eff_status = http["get.status"].fillna(http["head.status"]) if "get.status" in http else http["head.status"]
http["effective_status"] = eff_status
http["effective_error"] = http.get("get.error_kind", pd.Series(index=http.index)).fillna(http["head.error_kind"])
http["challenge"] = http["head.challenge"]
print(len(http), "URLs checked")
http.groupby(["effective_status", "effective_error", "challenge"], dropna=False).size().sort_values(ascending=False).head(30)"""),
    md("""## Pass 2: real browser, only for 403s, timeouts and WAF challenges

The brief says, and the NDL's method says, that a 403 is not a dead dataset. I send
stock headless Chromium at these URLs only, with 3 s between navigations per host.
If the first five on a host all stay challenged, I stop trying that host and record why."""),
    code("""\
recheck = http[(http["effective_status"] == 403) | (http["effective_error"].isin(["timeout", "stopped_at_challenging_host"]))
               | http["challenge"].notna()]
print(len(recheck), "URLs flagged for a browser re-check")
browser.run(recheck["url"].tolist(), BROWSER_OUT);"""),
    md("""## Pass 2b: one patient second visit, on hosts the browser *can* get through

Some challenge pages hadn't resolved within 6 s. Where the browser got through on at least one
other URL on the same host, the host clearly isn't refusing browsers. So I give each of those
still-challenged URLs exactly **one** more visit, with 20 s for the challenge to resolve. I don't
do this on hosts where the browser never got through at all."""),
    code("""\
import json
PATIENT_OUT = LC_DIR / "browser_results_patient.jsonl"
br1 = pd.DataFrame([json.loads(l) for l in BROWSER_OUT.read_text().splitlines()]) if BROWSER_OUT.exists() else pd.DataFrame(columns=["url", "host"])
if len(br1):
    passable = set(br1.loc[br1["browser_challenged"].eq(False) & br1["browser_status"].notna(), "host"])
    patient = br1[br1["browser_challenged"].eq(True) & br1["host"].isin(passable)]["url"].tolist()
    print(len(patient), "URLs get one patient second visit on", sorted(passable & set(br1.loc[br1['url'].isin(patient), 'host'])))
    if patient:
        browser.run(patient, PATIENT_OUT, settle_ms=20000)"""),
    code("""\
br = pd.DataFrame([json.loads(l) for l in BROWSER_OUT.read_text().splitlines()]) if BROWSER_OUT.exists() else pd.DataFrame(columns=["url"])
if PATIENT_OUT.exists():
    # The second visit, where there was one, supersedes the first. I keep both raw files in Bronze.
    br2 = pd.DataFrame([json.loads(l) for l in PATIENT_OUT.read_text().splitlines()]).assign(browser_pass="patient")
    br = pd.concat([br.assign(browser_pass="first")[~br["url"].isin(br2["url"])], br2], ignore_index=True)
links = http.merge(br.drop(columns=["host", "checked_utc"], errors="ignore"), on="url", how="left")
# Lists don't round-trip nicely through Parquet with mixed nulls, so I keep them as JSON text.
for c in [c for c in links.columns if links[c].map(lambda v: isinstance(v, list)).any()]:
    links[c] = links[c].map(lambda v: json.dumps(v) if isinstance(v, list) else None)
links.to_parquet(config.SILVER / "link_checks.parquet", index=False)
print(len(links), "rows ->", (config.SILVER / "link_checks.parquet").relative_to(ROOT))
br.groupby(["browser_skipped", "browser_status", "browser_challenged"], dropna=False).size() if len(br) else "no browser re-checks" """),
])
build("03_finding.ipynb", [
    md("""# 03 · The finding: health publishers vs the national corridor

**Reconciliation note, which I keep in force all the way through.** I cannot rebuild the National Data
Library's national figure. Their scope was the whole catalogue, tested in June 2026, before the
cleanup. Mine is six health publishers, tested on the date of my snapshot, *after* the cleanup.
So I quote their figures in one table and compute mine in another, and I never blend the two
into a single number. The national figures below are as quoted in "The National Data Library –
cleaning up data.gov.uk" (12 Nov 2026). I have not been able to retrieve that post myself.

**Classes, mirroring the NDL's split:**
- `ok`: 2xx, and not a bot challenge
- `hard_dead`: 404, 410, DNS or connection failure
- `uncertain`: 403, timeout, 400, 500, 429, any other status, and unresolved WAF challenges

A browser re-check moves a link out of `uncertain` only if the browser actually reached the page."""),
    code(SETUP),
    code("""\
import json, time, requests
from hclr import classify as C
resources = pd.read_parquet(config.SILVER / "resources.parquet")
datasets = pd.read_parquet(config.SILVER / "datasets.parquet")
links = C.classify(pd.read_parquet(config.SILVER / "link_checks.parquet"))
labels = {k: v["label"] for k, v in config.PUBLISHERS.items()}
print(len(resources), "resource links ->", len(links), "distinct URLs classified")"""),
    md("""## Corroborating the DNS failures

Through this machine's egress proxy, a DNS or connect failure shows up as a `502` on the CONNECT
tunnel. I check each such host once against public DNS-over-HTTPS, so I can say which are truly
unregistered (NXDOMAIN) and which still resolve but refuse connections. Both count as `hard_dead`
under the NDL's rules. The split just tells me which kind of death it is."""),
    code("""\
dns_out = config.BRONZE / "linkcheck" / resources["snapshot_utc"].iloc[0] / "dns_corroboration.json"
fail_hosts = sorted(links.loc[links["http_reason"] == "dns_or_connection_failure", "host"].unique())
if dns_out.exists():
    dns = json.loads(dns_out.read_text())
else:
    dns = {}
    for h in fail_hosts:
        try:
            r = requests.get("https://dns.google/resolve", params={"name": h.split(":")[0], "type": "A"},
                             headers={"User-Agent": config.USER_AGENT}, timeout=15)
            dns[h] = {"Status": r.json().get("Status"), "Answer": r.json().get("Answer")}
        except Exception as e:
            dns[h] = {"Status": None, "error": str(e)}
        time.sleep(1)
    dns_out.write_text(json.dumps(dns, indent=2))
links["dns_rcode"] = links["host"].map(lambda h: (dns.get(h) or {}).get("Status"))
pd.Series({h: ("NXDOMAIN" if v.get("Status") == 3 else "no address record (NOERROR, empty answer)" if v.get("Status") == 0 and not v.get("Answer")
               else "resolves" if v.get("Status") == 0 else v.get("Status"))
           for h, v in dns.items()}).value_counts()"""),
    md("""## Reading Cloudflare's 409s

A `409` from Cloudflare usually means Cloudflare is fronting a hostname whose own origin
DNS has gone (Cloudflare error 1001, "DNS resolution error"). By the NDL's rules that's a DNS
failure, and so `hard_dead`, even though the status code hides it. For each host that returned a
Cloudflare 409, I fetch **one** of its error pages and record the Cloudflare error code. I keep
the raw page in Bronze. If it's 1001, every 409 on that host counts as a DNS failure. The error
belongs to the hostname, not to the path."""),
    code("""\
import re
cf_out = config.BRONZE / "linkcheck" / resources["snapshot_utc"].iloc[0] / "cloudflare_409_corroboration.json"
cf409 = links[(links["http_status"] == 409) & links["head.server"].fillna("").str.contains("cloudflare", case=False)]
if cf_out.exists():
    cf = json.loads(cf_out.read_text())
else:
    cf = {}
    for host, g in cf409.groupby("host"):
        r = requests.get(g["url"].iloc[0], headers={"User-Agent": config.USER_AGENT}, timeout=(10, 30))
        title = re.search(r"<title>(.*?)</title>", r.text, re.S)
        code = re.search(r"errorCode:\\s*(\\d{4})", r.text)
        cf[host] = {"sample_url": g["url"].iloc[0], "status": r.status_code,
                    "title": title.group(1).strip() if title else None, "cloudflare_error": code.group(1) if code else None,
                    "fetched_utc": pd.Timestamp.now(tz="UTC").isoformat()}
        (cf_out.parent / f"cloudflare_409_{host}.html").write_text(r.text)
        time.sleep(2)
    cf_out.write_text(json.dumps(cf, indent=2))
dns_1001 = {h for h, v in cf.items() if v.get("cloudflare_error") == "1001"}
m = (links["http_status"] == 409) & links["host"].isin(dns_1001)
links.loc[m, ["http_class", "final_class"]] = "hard_dead"
links.loc[m, ["http_reason", "final_reason"]] = "cloudflare_1001_dns_resolution_error"
print({h: (v["cloudflare_error"], v["title"]) for h, v in cf.items()}, "->", int(m.sum()), "URLs reclassified")"""),
    md("""## Joining back to every catalogue link

The NDL counted *links*, so my headline unit is also the resource link. Each URL's
result applies to every catalogue entry that points at it. I show a distinct-URL view
next to it, because some publishers repeat the same URL across many resources."""),
    code("""\
res = resources.merge(links, on="url", how="left", suffixes=("", "_chk"))
assert res["final_class"].notna().all(), "every resource link must have a result"
res = res.merge(datasets[["dataset_id", "harvest_marker", "metadata_modified"]], on="dataset_id", how="left")
res["publisher"] = res["publisher_slug"].map(labels)
res.to_parquet(config.SILVER / "resource_link_status.parquet", index=False)

final = C.summarise(res, "publisher")
auto = C.summarise(res, "publisher", cls="http_class")
distinct = C.summarise(res.drop_duplicates(["publisher", "url"]), "publisher")
config.GOLD.mkdir(parents=True, exist_ok=True)
for name, t in {"by_publisher_final": final, "by_publisher_http_only": auto, "by_publisher_distinct_urls": distinct}.items():
    t.to_parquet(config.GOLD / f"{name}.parquet"); t.round(1).to_csv(config.GOLD / f"{name}.csv")
show = ["links", "ok", "hard_dead", "uncertain", "ok_pct", "hard_dead_pct", "uncertain_pct",
        "hard_dead_ci_lo", "hard_dead_ci_hi", "broken_floor_pct", "broken_ceiling_pct"]
print("HEALTH PUBLISHERS - resource links, after browser re-check (my figures)")
final[show].round(1)"""),
    code("""\
print("HTTP pass only, before any browser re-check (the analogue of the NDL's 'possible errors' stage)")
auto[show].round(1)"""),
    code("""\
print("Distinct URLs per publisher (each URL counted once)")
distinct[show].round(1)"""),
    md("""## The national backdrop, quoted and kept separate"""),
    code("""\
ndl = pd.DataFrame([
    ("links tested (June 2026)", 250_260, None),
    ("datasets", 51_203, None),
    ("possible errors", 89_217, 100 * 89_217 / 250_260),
    ("  confident dead: 404/410/DNS/connection", 45_681, 100 * 45_681 / 250_260),
    ("  needing investigation: 403/timeout/400/500/429", 43_536, 100 * 43_536 / 250_260),
    ("links removed", 60_818, 100 * 60_818 / 250_260),
    ("broken rate before (as stated)", None, 30.0),
    ("broken rate after (as stated)", None, 7.0),
], columns=["NDL national figure (quoted)", "count", "pct_of_links_tested"])
ndl.round(1)"""),
    md("""## Why links fail, per publisher"""),
    code("""\
(res[res["final_class"] != "ok"].groupby(["publisher", "final_class", "final_reason"]).size()
   .rename("links").reset_index().sort_values(["publisher", "links"], ascending=[True, False]))"""),
    md("""## Diagnostics that change what "ok" and "uncertain" mean

These don't change any class. They tell me how much weight each class can bear."""),
    code("""\
diag = res.groupby("publisher").agg(
    links=("url", "size"),
    ok=("final_class", lambda s: int((s == "ok").sum())),
    ok_but_redirects_to_homepage=("diag_redirect_to_homepage", "sum"),
    ok_on_a_different_domain=("diag_domain_changed", lambda s: int((s & (res.loc[s.index, "final_class"] == "ok")).sum())),
    web_archive_links=("diag_web_archive_link", "sum"),
    legacy_domain_links=("diag_legacy_domain", "sum"),
    browser_resolved=("resolved_by", lambda s: int((s == "browser").sum())),
    from_harvested_datasets=("harvest_marker", "sum"),
)
diag.to_csv(config.GOLD / "diagnostics_by_publisher.csv"); diag"""),
    md("""## Dataset-level view

Readers think in datasets, not links. How many listings have at least one hard-dead link,
and how many have nothing left to follow at all?"""),
    code("""\
ds_status = res.groupby(["publisher", "dataset_id"])["final_class"].agg(
    any_dead=lambda s: (s == "hard_dead").any(), all_ok=lambda s: (s == "ok").all()).reset_index()
ds_tab = ds_status.groupby("publisher").agg(datasets_with_links=("dataset_id", "size"),
                                            with_any_hard_dead=("any_dead", "sum"),
                                            all_links_ok=("all_ok", "sum"))
zero = datasets.assign(publisher=datasets["publisher_slug"].map(labels)).groupby("publisher")["num_resources"].agg(
    total_listings="size", listings_with_no_links=lambda s: int((s == 0).sum()))
ds_tab = zero.join(ds_tab).fillna(0).astype(int)
ds_tab.to_csv(config.GOLD / "datasets_by_publisher.csv"); ds_tab"""),
    md("""## Empty listings: the cleanup's footprint?

A listing with no links left survives as a title and metadata only. If its `metadata_modified`
falls in July 2026, that's consistent with the cleanup having removed its links. I can't *prove*
that, because CKAN's activity stream (which would show the edit) returns 403 to the public API."""),
    code("""\
datasets["touched_jul_2026"] = datasets["metadata_modified"].dt.strftime("%Y-%m").eq("2026-07")
shells = pd.crosstab(datasets["publisher_slug"].map(labels),
                     [datasets["num_resources"].eq(0).map({True: "no links", False: "has links"}),
                      datasets["touched_jul_2026"].map({True: "modified Jul 2026", False: "modified earlier/later"})])
shells.to_csv(config.GOLD / "empty_listings_by_publisher.csv"); shells"""),
    md("""## Where the uncertainty actually sits

If the uncertain mass is spread across many hosts, it's link rot of unknown kind. If it piles up on a
few hosts that block automated checks, then it's a verifiability problem, which is a different story."""),
    code("""\
unc = res[res["final_class"] == "uncertain"]
by_host = unc.groupby(["publisher", "host"]).size().rename("uncertain_links").reset_index().sort_values("uncertain_links", ascending=False)
by_host["share_of_all_uncertain_pct"] = 100 * by_host["uncertain_links"] / len(unc)
by_host.to_csv(config.GOLD / "uncertain_by_host.csv", index=False); by_host.head(10).round(1)"""),
    md("""## A sample for a human to check by hand

No automated method I'm willing to use gets past these walls. The only honest way to narrow
the uncertain band is a person with a normal browser checking a random sample. I draw it with a fixed
seed, stratified by publisher, so the sample is reproducible and can't be cherry-picked."""),
    code("""\
# Shuffle once with a fixed seed, then take the first 30 per publisher.
sample = unc.drop_duplicates("url").sample(frac=1, random_state=20261121).groupby("publisher_slug").head(30)
sample = sample[["publisher_slug", "dataset_name", "url", "host", "final_reason"]].assign(
    human_result="", human_notes="")
sample.to_csv(config.OUTPUTS / "manual_check_sample.csv", index=False)
sample.groupby("publisher_slug").size()"""),
    md("""## Harvested vs hand-published (NHS Digital is the only publisher here with harvest markers)

Caution: every harvested link here points at `digital.nhs.uk`, so this comparison confounds harvesting with host."""),
    code("""\
nd = res[res["publisher_slug"] == "nhs-digital"]
C.summarise(nd.assign(source=nd["harvest_marker"].map({True: "harvested", False: "not harvested"})), "source")[show].round(1)"""),
    md("""## Verdict against the corridor

For each publisher I put the honest band, from the floor (confirmed dead only) to the ceiling (if every
uncertain link were dead), next to the national ~7% after and ~30% before. A publisher sits
*materially outside* only if its whole band clears a reference line, or if the 95% interval on its
confirmed-dead share does."""),
    code("""\
v = final[["links", "broken_floor_pct", "broken_ceiling_pct", "hard_dead_ci_lo", "hard_dead_ci_hi", "uncertain_pct"]].copy()
def verdict(r):
    if r.links < 30:
        return "too few links to judge"
    if r.hard_dead_ci_lo > 30:
        return "confirmed-dead alone is ABOVE the national 'before' rate"
    if r.hard_dead_ci_lo > 7:
        return "confirmed-dead alone is ABOVE the national 'after' rate"
    if r.broken_ceiling_pct < 7:
        return "below 7% even if every uncertain link is dead"
    if r.uncertain_pct > 25:
        return "indeterminate: uncertain share too big to place it"
    return "inside the corridor"
v["verdict"] = v.apply(verdict, axis=1)
v.to_csv(config.GOLD / "verdict.csv"); v.round(1)"""),
])
build("04_chart.ipynb", [
    md("""# 04 · The chart

One horizontal stacked bar per health publisher, showing the share of resource links
that are hard-dead, uncertain or OK, with the link count printed on every bar.

**Reading it.** I order each bar *hard-dead, then uncertain, then OK*, left to right:
- The end of the red segment is the confirmed-dead share, the floor.
- The end of the grey segment is what the broken share would be if every uncertain link were dead, the ceiling.

The two dashed lines are the NDL's *national* rates as quoted (about 30% before the cleanup,
about 7% after). They're there for scale. They aren't my figures, and their scope (the whole catalogue,
tested June 2026) differs from mine (six health publishers, tested on the snapshot date).

**Colour.** Red is hard-dead. Neutral grey with hatching is uncertain: it has no hue on purpose,
because it means "unknown". Blue is OK. I validated these with the dataviz palette checker, which
passes on CVD separation, normal-vision separation and contrast. The grey fails only the chroma
floor, deliberately, and the hatching and legend carry it."""),
    code(SETUP),
    code("""\
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

t = pd.read_parquet(config.GOLD / "by_publisher_final.parquet")
snapshot = pd.read_parquet(config.SILVER / "resources.parquet")["snapshot_utc"].iloc[0]
MIN_N = 30  # below this I show the bar but flag it: a 100% bar of 2 links means nothing

short = {
    "NHS Digital (merged into NHS England, 2023)": "NHS Digital\\n(predecessor; now NHS England)",
    "Department of Health and Social Care": "Department of Health\\nand Social Care",
    "NHS England": "NHS England",
    "NHS Business Services Authority": "NHS Business Services\\nAuthority",
    "Public Health England (closed 2021; to UKHSA/OHID)": "Public Health England\\n(predecessor; closed 2021)",
    "UK Health Security Agency": "UK Health Security\\nAgency",
}
t["small"] = t["links"] < MIN_N
# Sorted by hard-dead share; ties broken by the uncertain ceiling, then size. Small-n bars always go last.
t = t.sort_values(["small", "hard_dead_pct", "broken_ceiling_pct", "links"], ascending=[True, False, False, False])
t[["links", "hard_dead", "uncertain", "ok", "hard_dead_pct", "uncertain_pct", "ok_pct"]].round(1)"""),
    code("""\
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0"
C_DEAD, C_UNC, C_OK = "#d03b3b", "#8c8a82", "#2a78d6"

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK})
fig, ax = plt.subplots(figsize=(10, 6.8), dpi=200)
fig.patch.set_facecolor(SURFACE); ax.set_facecolor(SURFACE)

rows = list(t.index)[::-1]          # matplotlib draws bottom-up
y = range(len(rows))
H = 0.56
for yi, pub in zip(y, rows):
    r = t.loc[pub]
    alpha = 0.45 if r["small"] else 1.0
    left = 0
    for val, colour, hatch in [(r["hard_dead_pct"], C_DEAD, None), (r["uncertain_pct"], C_UNC, "////"), (r["ok_pct"], C_OK, None)]:
        if val > 0:
            ax.barh(yi, val, left=left, height=H, color=colour, alpha=alpha, hatch=hatch,
                    edgecolor=SURFACE, linewidth=2)   # 2px surface gap between segments
        left += val
    # Shares printed inside a segment only where they fit comfortably.
    if r["uncertain_pct"] >= 9:
        ax.text(r["hard_dead_pct"] + r["uncertain_pct"] / 2, yi, f"{r['uncertain_pct']:.0f}% uncertain",
                ha="center", va="center", color="white", fontsize=8.5, fontweight="bold", zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", fc=C_UNC, ec="none", alpha=alpha))
    if r["ok_pct"] >= 12:
        ax.text(100 - r["ok_pct"] / 2, yi, f"{r['ok_pct']:.0f}% OK", ha="center", va="center",
                color="white", fontsize=8.5, fontweight="bold", alpha=1 if not r["small"] else 0.85)
    # The hard-dead share is the story, so it always gets a label, just past the red segment.
    dead_label = (f"{r['hard_dead_pct']:.1f}% hard-dead ({int(r['hard_dead'])} links)" if r["hard_dead"] else "0 hard-dead")
    ax.annotate(dead_label, xy=(0, yi + H / 2), xytext=(1, 3), textcoords="offset points",
                ha="left", va="bottom", fontsize=8, color=C_DEAD if r["hard_dead"] else MUTED,
                fontweight="bold" if r["hard_dead"] else "normal", zorder=7,
                bbox=dict(boxstyle="square,pad=0.15", fc=SURFACE, ec="none"))  # sits above the dashed lines
    # Link count on every bar, outside the 100% end, so no bar implies more than it holds.
    n_txt = f"n = {int(r['links']):,} links" + ("\\ntoo few to judge" if r["small"] else "")
    ax.text(101.5, yi, n_txt, ha="left", va="center", fontsize=8.5, color=INK2 if not r["small"] else MUTED)

# National reference lines: quoted, not mine.
for x, lab in [(7, "National ~7%\\nafter cleanup"), (30, "National ~30%\\nbefore cleanup")]:
    ax.axvline(x, color=INK, lw=1.1, ls=(0, (4, 3)), zorder=5)
    ax.text(x, len(rows) - 0.25, lab, ha="center", va="bottom", fontsize=8, color=INK, zorder=7,
            bbox=dict(boxstyle="square,pad=0.2", fc=SURFACE, ec="none"))

ax.set_yticks(list(y), [short[p] for p in rows])
ax.set_xlim(0, 100); ax.set_ylim(-0.6, len(rows) - 0.1 + 0.55)
ax.set_xticks([0, 20, 40, 60, 80, 100], ["0%", "20%", "40%", "60%", "80%", "100%"])
ax.tick_params(axis="y", length=0); ax.tick_params(axis="x", length=0)
ax.xaxis.grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
for side in ["top", "right", "left"]:
    ax.spines[side].set_visible(False)
ax.set_xlabel("Share of resource links on data.gov.uk")

fig.suptitle("Few health links are confirmed dead. Most of the legacy ones can't be checked at all",
             x=0.012, y=0.985, ha="left", fontsize=13, fontweight="bold")
fig.text(0.012, 0.935, f"Share of resource links by status, health and social care publishers on data.gov.uk, checked {snapshot[:10]}",
         ha="left", fontsize=9.5, color=INK2)
handles = [Patch(fc=C_DEAD, ec=SURFACE, label="Hard-dead (404, 410, DNS/connection failure)"),
           Patch(fc=C_UNC, ec=SURFACE, hatch="////", label="Uncertain (403, timeout, WAF/CAPTCHA, other)"),
           Patch(fc=C_OK, ec=SURFACE, label="OK (2xx)"),
           Line2D([0], [0], color=INK, lw=1.1, ls=(0, (4, 3)), label="National rate, as quoted by the NDL")]
fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.005, 0.075), ncol=2, frameon=False, fontsize=8.5)
fig.text(0.012, 0.012,
         "Source: data.gov.uk CKAN API snapshot and my own link check (HEAD-first, 2 s per host, no retries; browser re-check for 403s, timeouts and challenges).\\n"
         "National lines: National Data Library, 'cleaning up data.gov.uk' (12 Nov 2026), quoted and not independently verified. Different scope and date from mine: for scale only.\\n"
         "NHS Digital and Public Health England are predecessor bodies, shown separately and not merged into their successors.",
         fontsize=7, color=MUTED, ha="left", va="bottom")
fig.subplots_adjust(left=0.255, right=0.86, top=0.85, bottom=0.235)
config.OUTPUTS.mkdir(exist_ok=True)
fig.savefig(config.OUTPUTS / "health_linkrot_by_publisher.png", facecolor=SURFACE)
fig.savefig(config.OUTPUTS / "health_linkrot_by_publisher.svg", facecolor=SURFACE)
print("saved outputs/health_linkrot_by_publisher.png and .svg")"""),
    md("""## The table behind the chart

The chart is a static image for Medium, so the numbers live here and in `data/gold/by_publisher_final.csv`."""),
    code("""\
tbl = t[["links", "hard_dead", "uncertain", "ok", "hard_dead_pct", "hard_dead_ci_lo", "hard_dead_ci_hi",
         "uncertain_pct", "ok_pct", "broken_floor_pct", "broken_ceiling_pct"]].round(1)
tbl.to_csv(config.OUTPUTS / "chart_table.csv"); tbl"""),
])
print("built")
