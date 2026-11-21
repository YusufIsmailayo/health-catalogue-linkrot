# health-catalogue-linkrot

How much of the health and social care corner of data.gov.uk still resolves? This is a dated,
reproducible link check of every resource link that the health publishers list on data.gov.uk.
I measured it against the National Data Library's published national figures. I quote those
figures and never recompute or blend them.

## What's here

| Path | What it holds |
|---|---|
| `data/bronze/catalogue/<UTC stamp>/` | Raw CKAN `package_search` responses, byte for byte, with a manifest (request URL, fetch time, SHA-256). Committed on purpose: this is the record of what the catalogue said on the day. |
| `data/bronze/linkcheck/<UTC stamp>/` | Verbatim results of every request I made (`http_results.jsonl`), the browser re-checks (`browser_results.jsonl`) and the DNS corroboration. |
| `data/silver/` | Parquet: `datasets`, `resources` (one row per catalogue link), `link_checks` (one row per distinct URL), `resource_link_status` (the join). |
| `data/gold/` | The per-publisher tables that the finding rests on (Parquet and CSV). |
| `notebooks/01_catalogue_snapshot.ipynb` | Pulls and snapshots the catalogue. |
| `notebooks/02_link_check.ipynb` | Runs the polite HTTP check, then the browser fallback. |
| `notebooks/03_finding.ipynb` | Classifies the results, builds the per-publisher tables, compares them with the national corridor. |
| `notebooks/04_chart.ipynb` | The stacked-bar chart (`outputs/health_linkrot_by_publisher.png` / `.svg`). |
| `LIMITS.md` | What the numbers can and can't carry. **Read this before quoting anything.** |
| `outputs/manual_check_sample.csv` | A seeded random sample of uncertain links, for a person to check in a browser. |
| `src/hclr/` | The code the notebooks call. All the politeness settings are in `config.py`. |
| `tools/build_notebooks.py` | Rebuilds the notebooks from source, so they diff cleanly. |

## Publishers

I confirmed the slugs against the CKAN organisation facet on 21 November 2026:
`nhs-england`, `department-of-health` (titled "Department of Health and Social Care"),
`nhs-business-services-authority` and `uk-health-security-agency`. I also include two predecessor
bodies whose entries were never re-homed: `nhs-digital` (merged into NHS England in 2023) and
`public-health-england` (closed 2021). Each is kept as its own row and never merged into its successor.

## How the checker behaves

This analysis is about responsible stewardship of public data, so the checker is deliberately slow:

- It sends a User-Agent that names the project and says what it's doing.
- It keeps at most one request in flight per host, at least 2 s between requests to the same host, and works on at most 4 hosts at once.
- It sends HEAD first. It falls back to one GET (headers only, body never read) only if HEAD returned an error.
- Timeouts are 10 s to connect and 30 s to read. It **never retries**. If a host returns 429, it backs off that host for at least 60 s.
- Redirects are followed hop by hop. A **circuit breaker** stops all requests to a host once it has answered 10 in a row with a WAF challenge. Further requests would only ever be answered with "prove you're human".
- The browser fallback (stock headless Chromium, not disguised) is used only for 403s, timeouts and challenges, with 3 s between navigations. It gives up on a host after 5 consecutive challenges. I do not attempt to solve or bypass CAPTCHAs or bot protection.

## Classes

These mirror the NDL's split. `ok` means a 2xx that isn't a bot challenge. `hard_dead` means 404, 410, or a DNS or connection failure. `uncertain` covers everything else: 403, timeout, 400, 500, 429, other statuses, and unresolved challenges.

## Reproducing

```bash
pip install -r requirements.txt
```

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/01_catalogue_snapshot.ipynb
```

```bash
jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=-1 notebooks/02_link_check.ipynb
```

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/03_finding.ipynb
```

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/04_chart.ipynb
```

Notebook 01 takes a new dated snapshot only if none exists. Notebook 02 is resumable.
