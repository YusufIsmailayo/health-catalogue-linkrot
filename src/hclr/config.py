"""Shared settings for the health-catalogue link-rot analysis.

I keep every politeness knob in one place so anyone reading the repo can see,
at a glance, exactly how hard this code leans on government servers.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRONZE = ROOT / "data" / "bronze"
SILVER = ROOT / "data" / "silver"
GOLD = ROOT / "data" / "gold"
OUTPUTS = ROOT / "outputs"

# www.data.gov.uk/api/action/* 301-redirects here; I call the CKAN host directly
# so I'm not paying for a redirect on every page of results.
CKAN_API = "https://ckan.publishing.service.gov.uk/api/action"

# Publisher slugs confirmed against the CKAN organisation facet on 21 Nov 2026.
# The four the brief names, plus the two predecessor bodies whose catalogue
# entries were never re-homed. I keep predecessors as their own rows: merging
# them into their successors would be my judgement, not the catalogue's.
PUBLISHERS = {
    "nhs-england": {"label": "NHS England", "role": "named"},
    "department-of-health": {"label": "Department of Health and Social Care", "role": "named"},
    "nhs-business-services-authority": {"label": "NHS Business Services Authority", "role": "named"},
    "uk-health-security-agency": {"label": "UK Health Security Agency", "role": "named"},
    "nhs-digital": {"label": "NHS Digital (merged into NHS England, 2023)", "role": "predecessor"},
    "public-health-england": {"label": "Public Health England (closed 2021; to UKHSA/OHID)", "role": "predecessor"},
}

# --- Politeness -------------------------------------------------------------
# I identify myself plainly and say what I'm doing. No contact email: the repo
# name is enough for a sysadmin to find me.
USER_AGENT = (
    "HealthCatalogueLinkCheck/1.0 (independent research into data.gov.uk link health; "
    "repo: health-catalogue-linkrot; HEAD-first, one request per host every 2s, no retries)"
)
CKAN_PAGE_SIZE = 100          # rows per package_search call
CKAN_DELAY_S = 1.0            # pause between CKAN API calls
PER_HOST_DELAY_S = 2.0        # minimum gap between any two requests to the same host
MAX_WORKERS = 4               # hosts checked in parallel; never >1 in flight per host
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
MAX_REDIRECTS = 10
RATE_LIMIT_BACKOFF_S = 60     # if a host says 429, I leave it alone for at least this long

BROWSER_DELAY_S = 3.0         # gap between browser navigations to the same host
BROWSER_SETTLE_MS = 6000      # time I give a JS challenge page to resolve itself
BROWSER_NAV_TIMEOUT_MS = 30000
BROWSER_HOST_PROBE = 5        # if the first N browser attempts on a host all stay
                              # challenged, I stop: the browser isn't getting in, and
                              # repeating it would just be hammering.

import os as _os
# Use a known Chromium binary when one exists (e.g. a pre-installed one); otherwise Playwright's default.
CHROMIUM_PATH = _os.environ.get("HCLR_CHROMIUM") or ("/opt/pw-browsers/chromium" if _os.path.exists("/opt/pw-browsers/chromium") else None)
