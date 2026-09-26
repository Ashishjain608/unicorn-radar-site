#!/usr/bin/env python3
"""fetchers_extra: job-board fetchers for 5 more ATS platforms (icims, eightfold,
jobvite, pinpoint, oracle) — hiring.cafe's uncrawlable-by-us upstream coverage.

Same contract as jobs.py's f_<platform>(b) fetchers: b = {"p", "slug", "name", "url"},
return [J(...)] or None (board definitively doesn't exist / isn't public), let
Throttled propagate. Board CSVs (kalil0321/ats-scrapers) are dirty — some rows carry
the full URL in the "slug" column instead of a bare slug — so every fetcher here
derives its host from b["url"] (always the real board URL) and never from b["slug"].

jobvite is NOT implemented: modern jobs.jobvite.com career sites are an Angular SPA
that fetches its job list from a JS chunk not referenced in the static HTML (lazy
loaded post-bootstrap) — no server-rendered job data and no discoverable JSON/XML
endpoint short of running a JS engine. The legacy `app.jobvite.com/CompanyJobs/Xml.aspx`
feed 302s to a login-gated `Recruiting/Home.aspx`. Both dead ends for stdlib+regex.
"""
import re
import urllib.parse

from jobs import J, day, text
from unicorn_radar import get_json, get_bytes, Throttled  # noqa: F401  (Throttled: re-exported for callers)


# ── eightfold ────────────────────────────────────────────────────────────────
# The board's own "domain" (usually the employer's company domain, NOT the
# {slug}.eightfold.ai host) is required as a query param and isn't in our board
# record — it's embedded in the careers page's outbound links, e.g.
# ".eightfold.ai/careers?domain=qualcomm.com". Scrape it once, then page the API.
EF_DOMAIN_RE = re.compile(r"[?&]domain=([a-zA-Z0-9][\w.-]*\.[a-zA-Z]{2,})")


def _ef_domain(host):
    for url in (f"https://{host}/careers", f"https://{host}/home"):
        raw = get_bytes(url, timeout=30)
        if raw:
            m = EF_DOMAIN_RE.search(raw.decode("utf-8", "replace"))
            if m:
                return m.group(1)
    return None


def f_eightfold(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if not host.endswith(".eightfold.ai"):
        return None
    domain = _ef_domain(host)
    if not domain:
        return None  # no anonymous board here (or domain not discoverable) — skip
    out, off, total = [], 0, None
    while off < 1000:
        d = get_json(f"https://{host}/api/apply/v2/jobs?domain={urllib.parse.quote(domain)}"
                     f"&start={off}&num=100", timeout=30)
        if d is None:
            return None if off == 0 else out  # many tenants 401/403 anonymous API access
        positions = d.get("positions") or []
        if not positions:
            break
        for p in positions:
            locs = p.get("locations") or ([p["location"]] if p.get("location") else [])
            t = p.get("t_create")  # epoch SECONDS (day() wants ms)
            out.append(J(p.get("id"), p.get("name"), "; ".join(filter(None, locs)),
                         p.get("canonicalPositionUrl"), day(t * 1000) if t else None,
                         p.get("work_location_option")))
        off += len(positions)  # server caps page size (seen: 10, despite num=100 requested)
        total = d.get("count") or total or 0
        if off >= total:
            break
    return out


# ── oracle hcm cloud ─────────────────────────────────────────────────────────
def f_oracle(b):
    u = urllib.parse.urlparse(b["url"])
    host = u.netloc
    if "/sites/" not in u.path:
        return None
    site = urllib.parse.quote(u.path.split("/sites/", 1)[1].strip("/"), safe="")
    out, off, total = [], 0, None
    while off < 1000:
        finder = f"findReqs;siteNumber={site},limit=100,offset={off}"
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
        d = get_json(api, timeout=30)
        if d is None:
            return None if off == 0 else out
        items = d.get("items") or []
        reqs = items[0].get("requisitionList") if items else None
        if not reqs:
            break
        for r in reqs:
            locs = [r.get("PrimaryLocation")] + [sl.get("Name") for sl in r.get("secondaryLocations") or []]
            out.append(J(r.get("Id"), r.get("Title"), "; ".join(filter(None, locs)),
                         f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                         day(r.get("PostedDate")), r.get("WorkplaceType"),
                         text(r.get("ShortDescriptionStr")) or None))
        off += len(reqs)
        total = items[0].get("TotalJobsCount") or total or 0
        if off >= total:
            break
    return out


# ── pinpoint ─────────────────────────────────────────────────────────────────
def f_pinpoint(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if not host.endswith(".pinpointhq.com"):
        return None
    d = get_json(f"https://{host}/postings.json", timeout=30)
    if d is None:
        return None
    out = []
    for p in (d.get("data") or [])[:1000]:
        loc = (p.get("location") or {}).get("name")
        comp = None
        if p.get("compensation_visible") and (p.get("compensation_minimum") or p.get("compensation_maximum")):
            comp = (f"{p.get('compensation_currency') or 'USD'} "
                    f"{p.get('compensation_minimum')}-{p.get('compensation_maximum')}").strip()
        out.append(J(p.get("id"), p.get("title"), loc or "", p.get("url"), None,
                     p.get("workplace_type"), text(p.get("description")) or None, comp))
    return out


# ── icims ────────────────────────────────────────────────────────────────────
# No JSON/RSS feed found on the public search page; `in_iframe=1` skips the
# JS redirect the plain page does and serves the job cards server-rendered —
# scrape those with a regex. `pr=N` pages the results; an empty page ends it.
ICIMS_CARD_RE = re.compile(
    r'iCIMS_JobCardItem.*?Job Locations</span>\s*<span[^>]*>\s*(?P<loc>[^<]*?)\s*</span>.*?'
    r'<a href="(?P<url>[^"]+)"[^>]*class="iCIMS_Anchor"[^>]*>.*?<h3[^>]*>\s*(?P<title>[^<]*?)\s*</h3>'
    r'(?:.*?class="col-xs-12 description">\s*(?P<desc>.*?)\s*</div>)?',
    re.S)
ICIMS_ID_RE = re.compile(r"/jobs/(\d+)/")


def f_icims(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if not host.endswith(".icims.com"):
        return None
    out = []
    for pr in range(20):
        raw = get_bytes(f"https://{host}/jobs/search?pr={pr}&in_iframe=1", timeout=30)
        if raw is None:
            return None if pr == 0 else out
        cards = ICIMS_CARD_RE.findall(raw.decode("utf-8", "replace"))
        if not cards:
            break
        for loc, url, title, desc in cards:
            clean_url = url.split("?")[0]
            m = ICIMS_ID_RE.search(url)
            out.append(J(m.group(1) if m else clean_url, text(title), text(loc), clean_url,
                         None, desc=text(desc) or None))
        if len(out) >= 1000:
            break
    return out


def _selfcheck():
    sample = '''
    <li class="iCIMS_JobCardItem">
    <div class="row">
    <div class="col-xs-6 header left">
    <span class="sr-only field-label">Job Locations</span>
    <span >
    US-TN-Nashville</span>
    </div>
    <div class="col-xs-12 title">
    <a href="https://careers-360care.icims.com/jobs/5211/podiatrist/job?in_iframe=1" class="iCIMS_Anchor" title="5211 - Podiatrist">
    <span class="sr-only field-label">Title</span>
    <h3 >
    Podiatrist</h3>
    </a>
    </div>
    <div class="col-xs-12 description">
    Make a difference every day at 360care&nbsp;here.</div>
    </div>
    </li>
    '''
    cards = ICIMS_CARD_RE.findall(sample)
    assert len(cards) == 1, cards
    loc, url, title, desc = cards[0]
    assert loc == "US-TN-Nashville", loc
    assert title == "Podiatrist", title
    assert ICIMS_ID_RE.search(url).group(1) == "5211", url
    assert text(desc) == "Make a difference every day at 360care\xa0here.", repr(text(desc))

    m = EF_DOMAIN_RE.search('href="https://app.eightfold.ai/careers?domain=qualcomm.com\\" target="_blank"')
    assert m.group(1) == "qualcomm.com", m
    print("fetchers_extra: selfcheck OK")


EXTRA_FETCH = {"eightfold": f_eightfold, "oracle": f_oracle, "pinpoint": f_pinpoint, "icims": f_icims}
EXTRA_PLATFORMS = tuple(EXTRA_FETCH)


if __name__ == "__main__":
    _selfcheck()
