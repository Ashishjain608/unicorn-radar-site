#!/usr/bin/env python3
"""fetchers_extra: job-board fetchers beyond the upstream ATS lists — icims, eightfold, pinpoint,
oracle (hiring.cafe's uncrawlable-by-us coverage), comeet, the ATSs Indian employers use (trakstar,
freshteam, gem, kula, darwinbox, SuccessFactors RMK), iCIMS Jibe, and single-employer career
APIs (amazon, microsoft, atlassian, nutanix). Boards for these live in cache/extra_boards.csv.

Every endpoint here was checked against the host's robots.txt (2026-10). NOT built, robots disallow:
TurboHire (Flipkart: jobs.turbohire.co Disallow: /), Uber (*/api/). No anonymous API: Google, Apple.

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
import datetime
import html
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import features as F
from jobs import J, day, text
from unicorn_radar import get_json, get_bytes, Throttled  # noqa: F401  (Throttled: re-exported for callers)


# ── eightfold ──────────────────────────────────────────────────────────────
# Two APIs: legacy /api/apply/v2/jobs (403 "Not authorized for PCSX" on PCSX tenants) and
# /api/pcsx/search (403 "PCSX is not enabled" on legacy tenants). Try PCSX, fall back to legacy.
# PCSX page size is fixed at 10 regardless of num=; sort_by=timestamp = newest first, so the
# EF_CAP keeps the freshest. Domain: `?domain=x` link OR HTML-entity-escaped JSON
# `&#34;domain&#34;: &#34;x&#34;` (the form Nvidia/Dolby pages use) on /careers or /home.
EF_DOMAIN_RE = re.compile(r"""(?:[?&]domain=|"domain"\s*:\s*")([a-zA-Z0-9][\w.-]*\.[a-zA-Z]{2,})""")


EF_CAP = 500  # PCSX = 10 rows/request (~1.2s each); newest-first, so 500 = 50 requests


def _ef_domain(host):
    for url in (f"https://{host}/careers", f"https://{host}/home"):
        raw = get_bytes(url, timeout=30)
        if raw:
            m = EF_DOMAIN_RE.search(html.unescape(raw.decode("utf-8", "replace")))
            if m:
                return m.group(1)
    return None


def _ef_pcsx(host, dom):
    out, off, total = [], 0, None
    while off < EF_CAP:
        d = get_json(f"https://{host}/api/pcsx/search?domain={dom}&query=&start={off}&num=10&sort_by=timestamp",
                     timeout=30)
        data = (d or {}).get("data")
        if not data:
            return None if off == 0 else out
        ps = data.get("positions") or []
        if not ps:
            break
        for p in ps:
            locs = p.get("locations") or []
            wl = p.get("workLocationOption")
            t = p.get("postedTs") or p.get("creationTs")
            out.append(J(p.get("displayJobId") or p.get("id"), p.get("name"), "; ".join(locs),
                         f"https://{host}{p.get('positionUrl') or '/careers/job/' + str(p.get('id'))}",
                         day(t * 1000) if t else None, wl if wl in ("remote", "hybrid") else None))
        off += len(ps)
        total = data.get("count") or total or 0
        if off >= total:
            break
    return out


def _ef_legacy(host, dom):
    out, off, total = [], 0, None
    while off < 1000:
        d = get_json(f"https://{host}/api/apply/v2/jobs?domain={dom}&start={off}&num=100", timeout=30)
        if d is None:
            return None if off == 0 else out
        positions = d.get("positions") or []
        if not positions:
            break
        for p in positions:
            locs = p.get("locations") or ([p["location"]] if p.get("location") else [])
            t = p.get("t_create")  # epoch SECONDS
            out.append(J(p.get("id"), p.get("name"), "; ".join(filter(None, locs)),
                         p.get("canonicalPositionUrl"), day(t * 1000) if t else None,
                         p.get("work_location_option")))
        off += len(positions)
        total = d.get("count") or total or 0
        if off >= total:
            break
    return out


def f_eightfold(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if not host.endswith(".eightfold.ai"):
        return None
    dom = b.get("domain") or _ef_domain(host)  # ats_eightfold.csv has a domain column
    if not dom:
        return None
    dom = urllib.parse.quote(dom)
    out = _ef_pcsx(host, dom)
    return out if out is not None else _ef_legacy(host, dom)


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


# ── comeet ───────────────────────────────────────────────────────────────────
# Israeli-heavy ATS (Qodo et al.). No public list API without a per-company token, but the
# hosted board page comeet.com/jobs/<slug>/<company uid> server-renders every position,
# descriptions included, as `COMPANY_POSITIONS_DATA = [...]`. slug here is "<slug>/<uid>".
COMEET_RE = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", re.S)


def _country_name(cc):
    """ISO code -> a name features.countries() parses ("IL" alone reads as Illinois)."""
    rx = F.COUNTRIES.get(cc)
    return rx.pattern[3:].split("|")[0].replace("\\b", "") if rx else ""


def f_comeet(b):
    raw = get_bytes(f"https://www.comeet.com/jobs/{b['slug']}", timeout=30)
    m = COMEET_RE.search(raw.decode("utf-8", "replace")) if raw else None
    if not m:
        return None
    out = []
    for p in json.loads(m.group(1)):
        l = p.get("location") or {}
        loc = []
        for part in (l.get("city") or l.get("name"), l.get("state"), _country_name(l.get("country")).title()):
            if part and not any(part.lower() in x.lower() for x in loc):
                loc.append(part)
        loc = ", ".join(loc)
        desc = " ".join(text(d.get("value")) for d in (p.get("custom_fields") or {}).get("details") or [])
        # workplace_type is authoritative; location.is_remote is also true for hybrid roles
        out.append(J(p.get("uid"), p.get("name"), loc, p.get("url_comeet_hosted_page"), None,
                     p.get("workplace_type"), desc or None))
    return out


def _tenant(b, suffix=None, host=None):
    """Tenant from the board URL (<t>.<suffix> or <host>/<t>), else bare slug."""
    u = urllib.parse.urlparse(b.get("url") or "")
    if suffix and u.netloc.endswith(suffix):
        return u.netloc[: -len(suffix)]
    if host and u.netloc == host and u.path.strip("/"):
        return u.path.strip("/").split("/")[0]
    s = b.get("slug") or ""
    return s if re.fullmatch(r"[\w-]+", s) else None


# ── trakstar hire / recruiterbox ─────────────────────────────────────────────
def f_trakstar(b):
    t = _tenant(b, ".hire.trakstar.com")
    if not t:
        return None
    out, off = [], 0
    while off < 1000:
        # an unknown client_name answers 400 {"client_name": "Invalid client name"}: None
        d = get_json("https://jsapi.recruiterbox.com/v1/openings?client_name="
                     f"{urllib.parse.quote(t)}&limit=100&offset={off}", timeout=30)
        if d is None or "objects" not in d:
            return None if off == 0 else out
        objs = d["objects"]
        for o in objs:
            l = o.get("location") or {}
            loc = ", ".join(filter(None, [l.get("city"), l.get("state"), l.get("country")]))
            out.append(J(o.get("id"), o.get("title"), loc, o.get("hosted_url"), None,
                         "remote" if o.get("allows_remote") else None, text(o.get("description")) or None))
        off += len(objs)
        if not objs or off >= ((d.get("meta") or {}).get("total") or 0):
            break
    return out


# ── freshteam ────────────────────────────────────────────────────────────────
FT_RE = re.compile(r'<a href="(?P<path>/jobs/(?P<id>\w+)/[^"]*)"[^>]*?data-portal-location="(?P<loc>[^"]*)"'
                   r'[^>]*?data-portal-remote-location=(?P<rem>\w+)[^>]*>.*?class="job-title">\s*(?P<title>[^<]*?)\s*</div>',
                   re.S)


def f_freshteam(b):
    t = _tenant(b, ".freshteam.com")
    if not t:
        return None
    raw = get_bytes(f"https://{t}.freshteam.com/jobs", timeout=30)
    if raw is None:
        return None
    seen, out = set(), []
    for m in FT_RE.finditer(raw.decode("utf-8", "replace")):
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        out.append(J(m["id"], text(m["title"]), text(m["loc"]).strip(), f"https://{t}.freshteam.com{m['path']}",
                     None, "remote" if m["rem"] == "true" else None))
    return out[:1000]


# ── gem ──────────────────────────────────────────────────────────────────────
def f_gem(b):
    t = _tenant(b, host="jobs.gem.com")
    if not t:
        return None
    d = get_json(f"https://api.gem.com/job_board/v0/{urllib.parse.quote(t)}/job_posts/", timeout=30)
    if not isinstance(d, list):
        return None
    out = []
    for p in d[:1000]:
        lt = p.get("location_type")
        out.append(J(p.get("id"), p.get("title"), (p.get("location") or {}).get("name"), p.get("absolute_url"),
                     day(p.get("first_published_at") or p.get("created_at")),
                     lt if lt in ("remote", "hybrid") else None, p.get("content_plain") or text(p.get("content")) or None))
    return out


# ── kula ─────────────────────────────────────────────────────────────────────
# Next.js/Chakra page, all jobs server-rendered in one page (cashfree: 27 cards, no paging).
# Class names are hashed, so key off structure: card -> first 3 <p class="chakra-text"> are
# title, location, "Full time • On-Site"; job link is /<tenant>/<id>-<slug>.
KULA_P = re.compile(r'<p class="chakra-text[^"]*">(.*?)</p>', re.S)


def f_kula(b):
    t = _tenant(b, host="careers.kula.ai")
    if not t:
        return None
    raw = get_bytes(f"https://careers.kula.ai/{urllib.parse.quote(t)}", timeout=30)
    if raw is None:
        return None
    out, seen = [], set()
    link = re.compile(r'href="(/%s/((\d+)-[^"/?]+))"' % re.escape(t))
    for c in raw.decode("utf-8", "replace").split('class="chakra-card')[1:]:
        m, ps = link.search(c), KULA_P.findall(c)
        if not m or len(ps) < 2 or m[3] in seen:
            continue
        seen.add(m[3])
        mode = text(ps[2]).lower() if len(ps) > 2 else ""
        out.append(J(m[3], text(ps[0]).strip(), text(ps[1]).strip(), f"https://careers.kula.ai{m[1]}",
                     None, "remote" if "remote" in mode else "hybrid" if "hybrid" in mode else None))
    return out[:1000]


# ── darwinbox ────────────────────────────────────────────────────────────────
# The careers SPA (/ms/candidate/careers) calls /ms/candidateapi/job?page=N anonymously:
# 1-indexed, 10 per page, {"message": {"jobscount": total, "jobs": [...]}}. An unknown
# tenant answers 200 {"status":"error"}; a tenant with nothing public answers jobscount 0.
# ponytail: list API has no is_remote/desc (needs /job/<id> per job) -> remote=None, desc=None.
def f_darwinbox(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if ".darwinbox." not in host:
        return None
    out, total = [], None
    for page in range(1, 101):  # hard cap 1000 jobs
        d = get_json(f"https://{host}/ms/candidateapi/job?page={page}", timeout=30)
        m = d.get("message") if isinstance(d, dict) and d.get("status") == "success" else None
        if not isinstance(m, dict):
            return None if page == 1 else out
        jobs = m.get("jobs") or []
        for j in jobs:
            locs = j.get("tool_tip_locations") or [j.get("officelocation_show_arr")]
            ts = j.get("job_posting_on")
            out.append(J(j["id"], j.get("designation_display_name") or j.get("title"),
                         "; ".join(x.strip() for x in locs if x and x.strip()),
                         f"https://{host}/ms/candidate/careers/{j['id']}",
                         day(int(ts) * 1000) if str(ts or "").isdigit() else None))
        total = int(m.get("jobscount") or 0)
        if not jobs or len(out) >= total:
            break
    return out


# ── successfactors RMK (Recruiting Marketing; careers.wipro.com etc.) ────────
# /sitemap1.xml is the tenant's Google-for-Jobs RSS feed: every open job with title,
# link, g:location, description. One big request (Wipro ~30MB), no paging, no dates.
G = "{http://base.google.com/ns/1.0}"


def f_sfrmk(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    raw = get_bytes(f"https://{host}/sitemap1.xml", timeout=180)
    if raw is None:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    out = []
    for it in root.iter("item"):
        if len(out) >= 20000:  # hard cap
            break
        loc = it.findtext(G + "location") or ""
        title = it.findtext("title") or ""
        if loc and title.endswith(f"({loc})"):  # Wipro/Chargebee append "(loc)" to the title
            title = title[:-len(loc) - 2].strip()
        if re.fullmatch(r"[A-Z]{2}", loc):  # HCLTech: bare ISO code
            loc = _country_name(loc).title() or loc
        out.append(J(it.findtext(G + "id") or it.findtext("guid"), title, loc, it.findtext("link"),
                     None, "remote" if loc.lower().startswith("remote") else None,
                     text(it.findtext("description"))[:2000].strip() or None))
    return out


def _remote(*parts):
    s = " ".join(p for p in parts if p).lower()
    return "remote" if "remote" in s else ("hybrid" if "hybrid" in s else None)


# ── atlassian ────────────────────────────────────────────────────────────────
# One JSON array, all ~350 jobs, no paging. iCIMS sits behind it but the shape is
# Atlassian's own (not a generic iCIMS JSON pattern). No posted date in the payload.
ATL_API = "https://www.atlassian.com/endpoint/careers/listings"


def f_atlassian(b):
    d = get_json(ATL_API, timeout=60)
    if not isinstance(d, list):
        return None
    out = []
    for p in d[:2000]:
        locs = p.get("locations") or []
        out.append(J(p.get("id"), p.get("title"), "; ".join(locs),
                     f"https://www.atlassian.com/company/careers/details/{p.get('id')}", None,
                     _remote(*locs), text(p.get("overview")) or None))
    return out


# ── jibe (iCIMS Jibe: /api/jobs on the employer's own careers host) ──────────
# GET /api/jobs?page=N&limit=100 -> {"jobs":[{"data":{...}}], "totalCount":N}; limit capped at 100
# (limit=500 -> error body without "jobs"). Human page: https://<host>/jobs/<req_id> (redirects).
def f_jibe(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    if not host:
        return None
    out, total = [], None
    for page in range(1, 11):
        d = get_json(f"https://{host}/api/jobs?page={page}&limit=100", timeout=60)
        if not d or "jobs" not in d:
            return None if page == 1 else out
        rows = d["jobs"]
        if not rows:
            break
        for r in rows:
            j = r.get("data") or {}
            loc = j.get("full_location") or ", ".join(filter(None, [j.get("city"), j.get("country")]))
            sal = None
            if j.get("salary_min_value") not in (None, "", "0", 0):
                sal = f"{j.get('salary_min_value')}-{j.get('salary_max_value')}"
            out.append(J(j.get("req_id") or j.get("slug"), j.get("title"), loc,
                         f"https://{host}/jobs/{j.get('slug') or j.get('req_id')}", day(j.get("posted_date")),
                         _remote(j.get("title"), loc), text(j.get("description")) or None, sal))
        total = d.get("totalCount") or total or 0
        if len(out) >= total:
            break
    return out


# ── nutanix ──────────────────────────────────────────────────────────────────
# nutanix.eightfold.ai is dead (every path 404 "Group ID not found"). careers.nutanix.com is a
# custom ASP.NET site: /en/jobs/ ignores every query param (page/search/pagesize) so it only ever
# shows the newest 20; the full list is sitemap.xml (<loc>/en/jobs/<id>/<slug>/). Detail pages
# carry JSON-LD (datePosted, jobLocation). To bound requests we fetch detail only for slugs whose
# title passes features.family (the pipeline drops the rest anyway) — cap NX_CAP detail pages.
NX_URL_RE = re.compile(r"<loc>(https://careers\.nutanix\.com/en/jobs/(\d+)/([^/<]+)/)</loc>")
NX_TITLE_RE = re.compile(r'og:title" content="([^"]*)"')
NX_DATE_RE = re.compile(r'"datePosted":"([^"]*)"')
NX_LOC_RE = re.compile(r'"jobLocation":\{"@type":"Place","name":"([^"]*)"')
NX_CAP = 150


def f_nutanix(b):
    raw = get_bytes("https://careers.nutanix.com/sitemap.xml", timeout=30)
    if raw is None:
        return None
    out = []
    for url, id_, slug in NX_URL_RE.findall(raw.decode("utf-8", "replace")):
        if len(out) >= NX_CAP:
            break
        if not F.family(slug.replace("-", " ")):
            continue
        h = get_bytes(url, timeout=30)
        if h is None:
            continue
        h = h.decode("utf-8", "replace")
        t, d, l = NX_TITLE_RE.search(h), NX_DATE_RE.search(h), NX_LOC_RE.search(h)
        title = html.unescape(t.group(1)) if t else slug.replace("-", " ")
        loc = html.unescape(l.group(1)) if l else ""
        out.append(J(id_, title, loc, url, day(d.group(1)) if d else None, _remote(title, loc)))
    return out


# ── amazon (robots: only /internal disallowed) ───────────────────────────────────────────────────────────────────
# category[] is OR across values; each filtered set is far below the 10000-hit cap
# (plain unfiltered search reports hits=10000 and returns 0 rows at offset>=10000).
AMZ_CATS = ["software-development", "machine-learning-science", "research-science", "data-science",
            "solutions-architect", "hardware-development", "systems-quality-security-engineering"]
AMZ_PAGE, AMZ_MAX_PAGES = 100, 80  # cap 8000 rows (measured set ~5.2k)


def _amz_date(s):  # "September  4, 2026"
    try:
        return datetime.datetime.strptime(" ".join((s or "").split()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _amz_job(j):
    locs = []
    for l in j.get("locations") or []:
        try:
            locs.append(json.loads(l).get("normalizedLocation"))
        except (ValueError, AttributeError):
            pass
    loc = "; ".join(dict.fromkeys(filter(None, locs))) or j.get("normalized_location") or j.get("location")
    return J(j.get("id_icims") or j.get("id"), j.get("title"), loc, "https://www.amazon.jobs" + (j.get("job_path") or ""),
             _amz_date(j.get("posted_date")), None, text(j.get("description")) or None)


def f_amazon(b):
    qs = "&".join("category[]=" + c for c in AMZ_CATS)
    out, off = [], 0
    for _ in range(AMZ_MAX_PAGES):
        d = get_json(f"https://www.amazon.jobs/en/search.json?result_limit={AMZ_PAGE}&offset={off}&{qs}", timeout=40)
        if d is None:
            return None if off == 0 else out
        jobs = d.get("jobs") or []
        out += [_amz_job(j) for j in jobs]
        off += len(jobs)
        if not jobs or off >= (d.get("hits") or 0):
            break
    return out


# ── microsoft (an Eightfold PCSX tenant on its own host; robots: Disallow / but Allow /api/pcsx) ────────────────────────────────────────────────────────────────
# Server ignores `num` (fixed 10/page); `start` pages. filter_<name> repeated = OR; two
# passes (profession, career_discipline) are deduped by id.
MS_BASE = "https://apply.careers.microsoft.com"
MS_PROF = ["software engineering", "hardware engineering", "research, applied, & data sciences",
           "security engineering", "quantum computing", "engineering"]
MS_DISC = ["Cloud Solution Architecture", "Solution Architecture", "Solution Engineering", "Data Science",
           "Applied Sciences", "Research Sciences", "Firmware Engineering", "Silicon Engineering"]
MS_MAX_PAGES = 120  # per pass: 1200 rows (measured pass sizes ~840 / ~400)


def _ms_job(p):
    locs = list(dict.fromkeys((p.get("standardizedLocations") or []) + (p.get("locations") or [])))
    w = (p.get("workLocationOption") or "").lower()
    ts = p.get("postedTs")
    return J(p.get("displayJobId") or p.get("id"), p.get("name"), "; ".join(locs), MS_BASE + (p.get("positionUrl") or ""),
             day(ts * 1000) if ts else None, w if w in ("remote", "hybrid") else None)


def _ms_get(url):
    # Sliding-window 429 after ~70 fast pages: wait it out (3 x 30s) before letting Throttled propagate.
    for i in range(4):
        try:
            return get_json(url, timeout=40)
        except Throttled:
            if i == 3:
                raise
            time.sleep(30)


def f_microsoft(b):
    out, seen = [], set()
    for name, vals in (("profession", MS_PROF), ("career_discipline", MS_DISC)):
        qs = "&".join(f"filter_{name}=" + urllib.parse.quote(v, safe=",") for v in vals)
        for pg in range(MS_MAX_PAGES):
            d = _ms_get(f"{MS_BASE}/api/pcsx/search?domain=microsoft.com&sort_by=timestamp&start={pg * 10}&{qs}")
            if d is None:
                if not out and pg == 0:
                    return None
                break
            pos = (d.get("data") or {}).get("positions") or []
            for p in pos:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    out.append(_ms_job(p))
            if not pos:
                break
            time.sleep(1.2)  # 0.4s -> 429 after ~30 pages; ~1 req/s sustained
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
    assert _country_name("IL") == "israel" and _country_name("US") == "united states", _country_name("IL")
    assert F.countries(f"Tel-Aviv, {_country_name('IL')}") == ["IL"]
    page = 'var x;\n COMPANY_POSITIONS_DATA = [{"uid": "45.F68", "name": "Data Analyst"}];\n var y;'
    assert json.loads(COMEET_RE.search(page).group(1))[0]["uid"] == "45.F68"
    assert _tenant({"url": "https://moengage.hire.trakstar.com/", "slug": "x"}, ".hire.trakstar.com") == "moengage"
    assert _tenant({"url": "", "slug": "moengage"}, ".hire.trakstar.com") == "moengage"
    assert _tenant({"url": "https://careers.kula.ai/cashfree", "slug": ""}, host="careers.kula.ai") == "cashfree"
    assert _tenant({"url": "https://jobs.gem.com/promptql", "slug": ""}, host="jobs.gem.com") == "promptql"

    ft = '''<a href="/jobs/68V3608ei65Q/devops-engineer-2" class="heading" data-portal-title="x" data-portal-location="Mumbai, India" data-portal-job-type="1" data-portal-remote-location=false>
      <div class="row"><div class="job-list-info"><div class="job-title">Devops Engineer - 2</div></div></div></a>
      <a href="/jobs/XjoJ/voice-ai" class="heading" data-portal-location="Remote, India" data-portal-job-type="2" data-portal-remote-location=true>
      <div class="job-title">Voice AI Engineer</div></div></a>'''
    ms = list(FT_RE.finditer(ft))
    assert [(m["id"], m["loc"], m["rem"], m["title"]) for m in ms] == [
        ("68V3608ei65Q", "Mumbai, India", "false", "Devops Engineer - 2"), ("XjoJ", "Remote, India", "true", "Voice AI Engineer")]

    card = ('<div class="chakra-card css-1"><span class="css-3snkxd">Engineering</span>'
            '<p class="chakra-text css-a">Software Development Engineer 1</p>'
            '<p class="chakra-text css-b">Bellandur, Karnataka, India</p>'
            '<p class="chakra-text css-c">Full time<!-- --> • Hybrid</p>'
            '<a class="chakra-link" href="/cashfree/5565-software-development-engineer-1">Apply</a></div>')
    c = card.split('class="chakra-card')[1]
    assert KULA_P.findall(c)[1] == "Bellandur, Karnataka, India"
    assert re.search(r'href="(/cashfree/((\d+)-[^"/?]+))"', c)[3] == "5565"
    from unittest import mock
    page = {"status": "success", "message": {"jobscount": 2, "jobs": [
        {"id": "a1", "title": "Associate", "designation_display_name": "SDE II",
         "tool_tip_locations": ["HQ, Bangalore, Karnataka, India "], "job_posting_on": 1767810600}]}}
    seq = [page, {"status": "success", "message": {"jobscount": 2, "jobs": [{"id": "a2", "title": "T", "officelocation_show_arr": "X"}]}}]
    with mock.patch(f"{__name__}.get_json", side_effect=lambda *a, **k: seq.pop(0)):
        r = f_darwinbox({"url": "https://t.darwinbox.in/ms/candidate/careers"})
    assert [j["id"] for j in r] == ["a1", "a2"] and r[0]["title"] == "SDE II" and r[0]["posted"] == "2026-01-07"
    assert r[0]["url"] == "https://t.darwinbox.in/ms/candidate/careers/a1" and r[0]["loc"] == "HQ, Bangalore, Karnataka, India"
    with mock.patch(f"{__name__}.get_json", return_value={"status": "error"}):
        assert f_darwinbox({"url": "https://t.darwinbox.in/x"}) is None
    xml = (b'<rss xmlns:g="http://base.google.com/ns/1.0"><channel><item><title>Dev (Remote, US)</title>'
           b'<description>&lt;p&gt;Build it&lt;/p&gt;</description><link>https://j.x.com/job/a/1/</link><guid>1</guid>'
           b'<g:id>1</g:id><g:location>Remote, US</g:location></item></channel></rss>')
    with mock.patch(f"{__name__}.get_bytes", return_value=xml):
        r = f_sfrmk({"url": "https://j.x.com/"})
    assert r[0]["title"] == "Dev" and r[0]["remote"] == "remote" and r[0]["desc"].strip() == "Build it", r
    with mock.patch(f"{__name__}.get_bytes", return_value=None):
        assert f_sfrmk({"url": "https://j.x.com/"}) is None
    assert _remote("Remote - India") == "remote" and _remote("Hybrid") == "hybrid" and _remote("Paris") is None
    sm = '<url><loc>https://careers.nutanix.com/en/jobs/32805/senior-systems-engineer/</loc></url>'
    assert NX_URL_RE.findall(sm) == [("https://careers.nutanix.com/en/jobs/32805/senior-systems-engineer/", "32805", "senior-systems-engineer")]
    ld = '"datePosted":"2026-10-02","jobLocation":{"@type":"Place","name":"Singapore, Singapore","address"'
    assert NX_DATE_RE.search(ld).group(1) == "2026-10-02" and NX_LOC_RE.search(ld).group(1) == "Singapore, Singapore"
    assert EF_DOMAIN_RE.search(html.unescape('{&#34;domain&#34;: &#34;nvidia.com&#34;, &#34;configPath&#34;')).group(1) == "nvidia.com"
    assert EF_DOMAIN_RE.search('href="/x?domain=eaton.com&y=1"').group(1) == "eaton.com"
    assert day(1791365492 * 1000) == "2026-10-07"
    a = _amz_job({"id": "x", "id_icims": "123", "title": "SDE II", "job_path": "/en/jobs/123/sde",
                  "posted_date": "September  4, 2026", "description": "a<br/>b",
                  "locations": ['{"normalizedLocation":"Bengaluru, Karnataka, IND"}']})
    assert a["id"] == "123" and a["posted"] == "2026-09-04" and a["loc"] == "Bengaluru, Karnataka, IND"
    assert a["url"] == "https://www.amazon.jobs/en/jobs/123/sde"
    m = _ms_job({"id": 9, "displayJobId": "200", "name": "SWE", "locations": ["India, KA, Bengaluru"],
                 "standardizedLocations": ["Bengaluru, KA, IN"], "postedTs": 1791364892,
                 "workLocationOption": "hybrid", "positionUrl": "/careers/job/9"})
    assert m["posted"] == "2026-10-07" and m["remote"] == "hybrid" and m["url"].endswith("/careers/job/9")
    assert "Bengaluru, KA, IN" in m["loc"]
    print("fetchers_extra: selfcheck OK")


EXTRA_FETCH = {"eightfold": f_eightfold, "oracle": f_oracle, "pinpoint": f_pinpoint, "icims": f_icims,
               "comeet": f_comeet, "trakstar": f_trakstar, "freshteam": f_freshteam, "gem": f_gem, "kula": f_kula,
               "darwinbox": f_darwinbox, "sfrmk": f_sfrmk,
               "jibe": f_jibe, "atlassian": f_atlassian, "nutanix": f_nutanix,
               "amazon": f_amazon, "microsoft": f_microsoft}
EXTRA_PLATFORMS = tuple(EXTRA_FETCH)


if __name__ == "__main__":
    _selfcheck()
