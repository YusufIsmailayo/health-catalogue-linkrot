# What this analysis can and can't tell you

These are written up front so that nobody, me included, quotes a number for more than it can carry.
All figures come from the snapshot `2026-11-21T014501Z`: 6 publishers, 823 listings, 4,305 resource
links, 3,111 distinct URLs.

## 1. A status code can't tell a discontinued dataset from a badly harvested one

A 404 says the server has nothing at that address. It doesn't say whether the data was withdrawn,
moved without a redirect, renamed, or was never correctly linked in the first place. About 80% of
data.gov.uk listings arrive through automated harvesters (the NDL's figure, quoted), so a link's
status partly reflects how it was harvested, not whether the data survives.

I can't test that here. NHS Digital is the only publisher in scope with harvest markers (116
listings, 250 links), and every one of those links points at `digital.nhs.uk`, which sits behind
a Cloudflare bot check (see 3). Harvesting and host are completely tangled up, so the harvested vs
hand-published table in notebook 03 says nothing about harvesting.

## 2. This is an *after* picture, so there's no health "before"

My snapshot was taken after the NDL cleanup, which removed exactly the links that would have
been hard-dead. So the near-zero hard-dead rates are partly the cleanup working as intended. The fair
comparison is the national ~7% *after* figure, not the ~30% before. I can't recover what the health
publishers looked like before: CKAN's change history (`package_activity_list`) returns 403 to the
public API.

The cleanup has left a visible footprint, though: **34 of DHSC's 35 listings with no links left were
last modified in July 2026**, mostly from a 2010 bulk import. The title and metadata survive and the
link to the data is gone. That timing is consistent with the cleanup, but I can't prove it caused it.
(PHE's 128 empty listings are different: older ONS-hub imports that never had links. They aren't
cleanup.)

## 3. Two-thirds of the legacy links can't be verified by any polite automated method

| | Links | Uncertain |
|---|---|---|
| NHS Digital (predecessor) | 2,489 | 1,733 (69.6%) |
| DHSC | 1,160 | 762 (65.7%) |

**2,478 of all 2,515 uncertain links (98.5%) sit behind two bot walls:**
- NHS Digital's Cloudflare challenge: `hscic.gov.uk` redirects to `digital.nhs.uk`, plus `digital.nhs.uk` and `nchod.nhs.uk` themselves.
- The National Archives web archive, which shows an AWS WAF CAPTCHA ("Let's confirm you are human") even to a real browser. Its machine-readable index endpoints are behind the same CAPTCHA.

I deliberately didn't try to get around either one. So for DHSC and NHS Digital the "broken" share
could be anywhere from the confirmed floor (0.9% and 3.9%) to the ceiling (66.6% and 73.5%), a range
that straddles both national reference lines. **These two publishers can't be placed relative to the
corridor.** The only honest way to narrow that range is a person checking the seeded random sample in
`outputs/manual_check_sample.csv`.

## 4. Status codes get it wrong in both directions

- **A dead domain that looks alive to a status-code check.** 103 of the 106 hard-dead links are on `www.ic.nhs.uk` (102 distinct URLs, 25 listings). The other 3 are a domain with no address record at all. Cloudflare answers them with a **409**. Its error page says "error 1001, DNS resolution error", so the site behind it no longer exists. A 409 is in neither of the NDL's buckets, so a classifier that reads status codes alone files these as "uncertain" at best. I read one error page per host, which is reproducible in notebook 03, and reclassified them as hard-dead under the NDL's own DNS-failure rule. I don't know how the NDL handled 409s, so "these survived the cleanup because of the 409" is a plausible inference, not a finding.
- **Bot challenges that look like OKs.** NHS England's site answers *every* automated request with `202 + x-amzn-waf-action: challenge`, even for a page that doesn't exist. A naive checker would mark all of its links as OK; a slightly better one marks 97% of them as uncertain. Only the real-browser pass showed the true picture (419 of 439 OK).
- **Soft 404s counted as OK.** 10 DHSC links (the NHS Staff Survey CSVs) return 200, but only because they redirect to the site's homepage. The data has gone. They're counted as OK here, in line with the NDL's method, and flagged in `data/gold/diagnostics_by_publisher.csv`.

## 5. Small numbers

UKHSA has 1 listing with 2 links, and PHE has 17 links. Their 100%-OK bars are true and meaningless.
The chart fades them out and marks them "too few to judge".

The confidence intervals on the hard-dead shares treat links as independent. They aren't (links
cluster within listings, and a dead domain kills all of its links at once), so read the intervals as
the narrowest honest range, not a full one.

## 6. The national figures are quoted, not verified

The national figures (250,260 links tested, 89,217 possible errors, 60,818 removed, over 30% → about 7%)
come from "The National Data Library – cleaning up data.gov.uk" (12 Nov 2026) as supplied to me. I
couldn't retrieve that post: on 21 Nov 2026 the Data in Government blog listed nothing after
3 September, and `/2026/11/12/` returned 404. The nearest verifiable source is the
[25 March 2026 post](https://dataingovernment.blog.gov.uk/2026/03/25/whats-changing-on-data-gov-uk-and-why/)
("more than a quarter" of links broken). I also don't know exactly how "broken" is defined in the 7%.
89,217 ÷ 250,260 = 35.6%, so "over 30%" looks like it counts the uncertain group as broken.

I never blend these figures with mine. They differ in scope (the whole catalogue vs six publishers)
and in date (June 2026 vs 21 November 2026).

## 7. Does anything complicate the headline? Yes, three things

1. **The premise doesn't hold for confirmed rot.** No health publisher's confirmed-dead share is above
   the national ~7%. NHS England and NHS BSA would stay below 7% even if every uncertain link were dead.
2. **What *is* different is verifiability, not rot.** About two-thirds of the legacy publishers' links
   can't be checked by machine, against 17.4% needing investigation nationally even before the cleanup.
3. **DHSC already relies on the archive.** 757 of its 1,160 links (65%) point *into* the National Archives
   web archive. For DHSC the archive already is the record. That cuts against "removal leaves no public
   trace", and the archive is behind a CAPTCHA, so I can't confirm the archived copies exist.
