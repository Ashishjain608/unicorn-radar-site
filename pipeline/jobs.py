#!/usr/bin/env python3
"""jobs: scan every known job board, keep a posting-level ledger, publish the job feed.

Replaces boards.py. The unit is the *posting*, not the company: each technical posting
is tracked with first_seen / last_seen, so the site can say "new since your last visit",
close postings the day the employer does, and derive things only history can show
(time-to-close, reposts, first job in a new country).

Multi-user by construction: everything computed here is user-independent (features.py).
Each visitor's profile ranks the published postings in the browser.

State (cache/ledger.json.gz, cache/history.json) is the only thing a run needs from the
last one. Throttled boards carry their postings forward untouched — a rate-limited
fetch must never read as "every job here closed".

Usage: python3 jobs.py [--workers 12] [--only greenhouse,lever] [--limit N] [--no-refresh]
Outputs: cache/ledger.json.gz, cache/history.json, cache/boards.json (unicorn_radar.py input),
         web/data/jobs.json, web/data/skills.json, web/data/events.json, web/feeds/*.xml
"""
import argparse, csv, datetime, gzip, html, json, os, re, statistics, threading, time
import urllib.parse, xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import features as F
import unicorn_radar as u
from unicorn_radar import HERE, Throttled, get_json, get_bytes, norm

TODAY = datetime.date.today().isoformat()
LEDGER, HISTORY = HERE / "cache" / "ledger.json.gz", HERE / "cache" / "history.json"
EXTRA_BOARDS = HERE / "cache" / "extra_boards.csv"  # discover_boards.py output
WEB = HERE / "web"
PLATFORMS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday", "bamboohr",
             "rippling", "personio", "teamtailor", "breezy", "recruitee",
             "oracle", "pinpoint", "icims", "eightfold")  # last four: fetchers_extra.py
CLOSED_KEEP_DAYS = 45       # closed postings are kept this long for repost + time-to-close
DETAIL_CAP = 8000           # per-run cap on per-posting detail fetches (Workday/SmartRecruiters)
JSONH = {"Content-Type": "application/json", "Accept": "application/json"}
WORKER_SCALE = {"workday": 2, "workable": 0.34}  # multiples of --workers (see scan_all)


def day(v):
    """ISO timestamp / epoch-ms / date string -> 'YYYY-MM-DD' (or None)."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc).date().isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v))
    return m.group(1) if m and m.group(1) <= TODAY else None


def text(v):
    return html.unescape(re.sub(r"<[^>]+>", " ", v or ""))


def J(id, title, loc="", url=None, posted=None, remote=None, desc=None, comp=None, co=None):
    return {"id": str(id), "title": (title or "").strip(), "loc": loc or "", "url": url, "posted": posted,
            "remote": remote, "desc": desc, "comp": comp, "co": co}


# ── board lists ──────────────────────────────────────────────────────────────
def refresh_lists(max_age_days=7):
    """Upstream ats-scrapers lists grow ~19%/quarter; a stale copy silently caps coverage."""
    for p in PLATFORMS:
        f = HERE / "cache" / f"ats_{p}.csv"
        if f.exists() and time.time() - f.stat().st_mtime < max_age_days * 86400:
            continue
        try:
            f.write_bytes(get_bytes(u.ATS_DUMP.format(p), timeout=60) or b"")
        except Throttled as e:
            print(f"  ! board list {p}: {e} — using cached copy")


def all_boards(only=None):
    seen, out = set(), []
    srcs = [(p, HERE / "cache" / f"ats_{p}.csv") for p in PLATFORMS] + [(None, EXTRA_BOARDS)]
    for plat, path in srcs:
        if not path.exists():
            continue
        for r in csv.DictReader(open(path, errors="replace")):
            p = plat or r.get("platform")
            slug, name = (r.get("slug") or "").strip(), (r.get("name") or "").strip()
            if (p in PLATFORMS and slug and name and (p, slug) not in seen and (not only or p in only)
                    and norm(name) not in u.AGGREGATOR_BOARDS):  # marketplaces list other firms' jobs
                seen.add((p, slug))
                out.append({"p": p, "slug": slug, "name": name, "url": r.get("url") or ""})
    out.append({"p": "hn", "slug": "whoishiring", "name": "Hacker News: Who is hiring", "url": ""})
    return [b for b in out if not only or b["p"] in only]


# ── fetchers: (board) -> [job] | None (definitive: no such board) ; raise Throttled ──
def f_greenhouse(b, content=False):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs"
                 + ("?content=true" if content else ""), timeout=40)
    if d is None:
        return None
    return [J(j["id"], j.get("title"), (j.get("location") or {}).get("name", ""), j.get("absolute_url"),
              day(j.get("first_published") or j.get("updated_at")),
              desc=html.unescape(j.get("content") or "") if content else None) for j in d.get("jobs", [])]


def f_lever(b):
    d = get_json(f"https://api.lever.co/v0/postings/{b['slug']}?mode=json", timeout=40)
    if d is None or not isinstance(d, list):
        return None
    out = []
    for j in d:
        sr = j.get("salaryRange") or {}
        comp = (f"{sr.get('currency', 'USD')} {sr['min']} - {sr['max']}"
                if sr.get("min") and "year" in (sr.get("interval") or "year") else None)
        desc = " ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
                        + [text(l.get("content")) for l in j.get("lists", [])])
        out.append(J(j["id"], j.get("text"), (j.get("categories") or {}).get("location", ""), j.get("hostedUrl"),
                     day(j.get("createdAt")), j.get("workplaceType"), desc, comp))
    return out


def f_ashby(b):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}?includeCompensation=true", timeout=40)
    if d is None:
        return None
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location") or ""] + [l.get("location", "") for l in j.get("secondaryLocations") or []]
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append(J(j.get("id"), j.get("title"), "; ".join(filter(None, locs)), j.get("jobUrl"),
                     day(j.get("publishedAt")), j.get("workplaceType") or ("remote" if j.get("isRemote") else None),
                     j.get("descriptionPlain") or text(j.get("descriptionHtml")), comp))
    return out


def f_workable(b):
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{b['slug']}?details=true", timeout=40)
    if d is None:
        return None
    return [J(j.get("shortcode"), j.get("title"), ", ".join(filter(None, [j.get("city"), j.get("country")])),
              j.get("url"), day(j.get("published_on") or j.get("created_at")),
              "remote" if j.get("telecommuting") else None, text(j.get("description"))) for j in d.get("jobs", [])]


def f_smartrecruiters(b):
    out, off = [], 0
    while off < 1000:
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{b['slug']}/postings?limit=100&offset={off}",
                     timeout=30)
        if d is None:
            return None if off == 0 else out
        for j in d.get("content", []):
            l = j.get("location") or {}
            out.append(J(j["id"], j.get("name"), l.get("fullLocation") or ", ".join(filter(None, [l.get("city"), l.get("country")])),
                         f"https://jobs.smartrecruiters.com/{b['slug']}/{j['id']}", day(j.get("releasedDate")),
                         "remote" if l.get("remote") else ("hybrid" if l.get("hybrid") else None)))
        off += 100
        if off >= (d.get("totalFound") or 0):
            break
    return out


def d_smartrecruiters(b, j):
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{b['slug']}/postings/{j['id']}", timeout=20) or {}
    secs = ((d.get("jobAd") or {}).get("sections") or {}).values()
    return " ".join(text(s.get("text")) for s in secs if isinstance(s, dict))


def _wd(b):
    host = urllib.parse.urlparse(b["url"]).netloc
    tenant, site = b["slug"].split("/", 1)
    return host, f"https://{host}/wday/cxs/{tenant}/{site}", site


def _wd_posted(s):
    s = (s or "").lower()
    if "today" in s:
        return TODAY
    if "yesterday" in s:
        return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+) days? ago", s)
    if m and "+" not in s:
        return (datetime.date.today() - datetime.timedelta(days=int(m.group(1)))).isoformat()
    return None  # "30+ Days Ago": older than a month, date unknown


# Workday pages are 20 postings at ~2 s each and tenants include hospitals and retailers;
# searching server-side for technical titles cuts the page count for non-tech tenants
# ~10x. OR is honoured (measured: NVIDIA "engineer scientist" 137 vs "engineer OR scientist" 2000).
# ponytail: product managers at Workday tenants are missed; add "product manager" if wanted.
WD_QUERY = "engineer OR engineering OR developer OR scientist OR architect OR researcher OR programmer"


def f_workday(b):
    if "/" not in b["slug"] or not b["url"]:
        return None
    host, api, site = _wd(b)
    out, off, total = [], 0, None
    while off < 2000:  # ponytail: 2k cap per tenant; the giants post more, mostly non-tech
        d = get_json(f"{api}/jobs", timeout=30, headers=JSONH,
                     data=json.dumps({"appliedFacets": {}, "limit": 20, "offset": off, "searchText": WD_QUERY}).encode())
        if d is None:
            return None if off == 0 else out
        total = total or d.get("total") or 0  # Workday reports total on page 1 only
        posts = d.get("jobPostings") or []
        for j in posts:
            path = j.get("externalPath") or ""
            out.append(J(path, j.get("title"), j.get("locationsText", ""), f"https://{host}/{site}{path}",
                         _wd_posted(j.get("postedOn"))))
        off += 20
        if not posts or off >= total:
            break
    return out


def d_workday(b, j):
    _, api, _ = _wd(b)
    d = (get_json(f"{api}{j['id']}", timeout=20) or {}).get("jobPostingInfo") or {}
    locs = [d.get("location") or ""] + list(d.get("additionalLocations") or [])
    if locs[0]:
        j["loc"] = "; ".join(filter(None, locs))
    j["remote"] = d.get("remoteType") or j["remote"]
    j["posted"] = day(d.get("startDate")) or j["posted"]
    return text(d.get("jobDescription"))


def f_bamboohr(b):
    d = get_json(f"https://{b['slug']}.bamboohr.com/careers/list", timeout=30)
    if d is None:
        return None
    out = []
    for j in d.get("result") or []:
        l, a = j.get("location") or {}, j.get("atsLocation") or {}
        loc = ", ".join(filter(None, [l.get("city") or a.get("city"), l.get("state") or a.get("state"), a.get("country")]))
        out.append(J(j["id"], j.get("jobOpeningName"), loc, f"https://{b['slug']}.bamboohr.com/careers/{j['id']}",
                     remote="remote" if j.get("isRemote") else None))
    return out


def f_rippling(b):
    out, page = [], 0
    while page < 20:
        d = get_json(f"https://ats.rippling.com/api/v2/board/{b['slug']}/jobs?page={page}", timeout=30)
        if d is None:
            return None if page == 0 else out
        for j in d.get("items") or []:
            ls = j.get("locations") or []
            out.append(J(j["id"], j.get("name"), "; ".join(l.get("name", "") for l in ls), j.get("url"),
                         remote=next((l.get("workplaceType") for l in ls if l.get("workplaceType")), None)))
        page += 1
        if page >= (d.get("totalPages") or 1):
            break
    return out


def f_personio(b):
    raw = get_bytes(f"https://{b['slug']}.jobs.personio.com/xml?language=en", timeout=40)
    if raw is None:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    out = []
    for p in root.iter("position"):
        g = lambda k: (p.findtext(k) or "").strip()
        offices = [g("office")] + [o.text or "" for o in p.findall("additionalOffices/office")]
        desc = " ".join(text(v.text) for v in p.findall("jobDescriptions/jobDescription/value"))
        out.append(J(g("id"), g("name"), "; ".join(filter(None, offices)),
                     f"https://{b['slug']}.jobs.personio.com/job/{g('id')}", day(g("createdAt")), desc=desc))
    return out


def f_teamtailor(b):
    url, out, pages = f"https://{b['slug']}.teamtailor.com/jobs.json", [], 0
    while url and pages < 10:
        d = get_json(url, timeout=30)
        if d is None:
            return None if pages == 0 else out
        for j in d.get("items") or []:
            out.append(J(j.get("id"), j.get("title"), "", j.get("url"), day(j.get("date_published")),
                         desc=text(j.get("content_html"))))
        url, pages = d.get("next_url"), pages + 1
    return out


def f_breezy(b):
    d = get_json(f"https://{b['slug']}.breezy.hr/json", timeout=30)
    if d is None or not isinstance(d, list):
        return None
    return [J(j["id"], j.get("name"), (j.get("location") or {}).get("name", ""), j.get("url"),
              day(j.get("published_date")), "remote" if (j.get("location") or {}).get("is_remote") else None,
              comp=j.get("salary") or None) for j in d]


def f_recruitee(b):
    d = get_json(f"https://{b['slug']}.recruitee.com/api/offers/", timeout=30)
    if d is None:
        return None
    out = []
    for j in d.get("offers") or []:
        sal = j.get("salary") or {}
        comp = (f"{sal.get('currency') or ''} {sal.get('min')} - {sal.get('max')}"
                if isinstance(sal, dict) and sal.get("min") and sal.get("period") in (None, "year") else None)
        out.append(J(j["id"], j.get("title"), j.get("location") or ", ".join(filter(None, [j.get("city"), j.get("country")])),
                     j.get("careers_url"), day(j.get("published_at") or j.get("created_at")),
                     "remote" if j.get("remote") else ("hybrid" if j.get("hybrid") else None),
                     text(j.get("description")) + " " + text(j.get("requirements")), comp))
    return out


HN_ROLE = re.compile(r"engineer|developer|scientist|researcher|architect|sre|devops|ml|ai\b", re.I)


def f_hn(b):
    """Monthly 'Ask HN: Who is hiring?' — every post is written by the employer itself,
    so it passes the rule that excluded aggregators, and it reaches companies with no ATS."""
    s = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    hit = next((h for h in (s or {}).get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not hit:
        return None
    d = get_json(f"https://hn.algolia.com/api/v1/items/{hit['objectID']}", timeout=40) or {}
    out = []
    for c in d.get("children") or []:
        raw = c.get("text") or ""
        if not raw or c.get("author") is None:
            continue
        header = text(re.split(r"<p>", raw, maxsplit=1)[0]).strip()
        parts = [p.strip() for p in header.split("|") if p.strip()]
        if len(parts) < 2 or len(parts[0]) > 60:
            continue  # not the conventional "Company | Role | Location" header
        role = next((p for p in parts[1:] if F.family(p)), None) or next((p for p in parts[1:] if HN_ROLE.search(p)), parts[1])
        loc = "; ".join(p for p in parts[1:] if F.countries(p) or F.REMOTE.search(p) or F.HYBRID.search(p))
        out.append(J(c["id"], role[:140], loc, f"https://news.ycombinator.com/item?id={c['id']}",
                     day(c.get("created_at")), "remote" if F.REMOTE.search(header) else None,
                     text(raw), co=re.sub(r"\s*(\(.*?\)|https?://\S+)\s*", " ", parts[0]).strip()[:60]))
    return out


FETCH = {"greenhouse": f_greenhouse, "lever": f_lever, "ashby": f_ashby, "workable": f_workable,
         "smartrecruiters": f_smartrecruiters, "workday": f_workday, "bamboohr": f_bamboohr,
         "rippling": f_rippling, "personio": f_personio, "teamtailor": f_teamtailor, "breezy": f_breezy,
         "recruitee": f_recruitee, "hn": f_hn}
DETAIL = {"workday": d_workday, "smartrecruiters": d_smartrecruiters}


def load_extra():
    """fetchers_extra imports J/day/text from this module, so it is loaded lazily."""
    from fetchers_extra import EXTRA_FETCH
    FETCH.update(EXTRA_FETCH)


# ── scan ─────────────────────────────────────────────────────────────────────
class Scan:
    def __init__(self, ledger):
        self.known = {k for k, j in ledger.items() if j.get("d")}  # postings whose description we've read
        self.detail_budget, self.lock = DETAIL_CAP, threading.Lock()

    def _take_detail(self):
        with self.lock:
            if self.detail_budget <= 0:
                return False
            self.detail_budget -= 1
            return True

    def board(self, b):
        """-> (board, jobs | None | "fail"). Descriptions are fetched only for technical
        postings we haven't read before, so steady-state runs stay cheap."""
        p = b["p"]
        try:
            jobs = FETCH[p](b)
            if not jobs:
                return b, jobs
            fresh = [j for j in jobs if F.family(j["title"]) and f"{p}:{b['slug']}:{j['id']}" not in self.known]
            if p == "greenhouse" and fresh:
                full = {j["id"]: j for j in f_greenhouse(b, content=True) or []}
                for j in jobs:
                    j["desc"] = (full.get(j["id"]) or {}).get("desc")
            elif p in DETAIL:
                for j in fresh:
                    if self._take_detail():
                        try:
                            j["desc"] = DETAIL[p](b, j)
                        except Throttled:
                            pass  # listing still counts; description retried next run
            return b, jobs
        except Throttled:
            return b, "fail"
        except Exception as e:  # malformed payload from one board must not sink the scan
            print(f"  ! {p}/{b['slug']}: {type(e).__name__}: {e}")
            return b, "fail"


def scan_all(boards, workers, ledger):
    """One pool per platform: the throttle that cost 2.5k boards at 32 workers is per API
    host, so platforms run side by side, each at `workers`."""
    sc, by_p = Scan(ledger), defaultdict(list)
    for b in boards:
        by_p[b["p"]].append(b)
    results, t0 = [], time.time()

    def run(p):
        # Workday tenants sit on many hosts (wd1..wd12, per-tenant subdomains) and each page is
        # slow, so it tolerates — and needs — twice the concurrency of a single-host API.
        # Workable throttles hard: 12 workers left 928 of 4,752 boards (20%) unanswered.
        with ThreadPoolExecutor(max(1, int(workers * WORKER_SCALE.get(p, 1)))) as ex:
            r = list(ex.map(sc.board, by_p[p]))
        fails = sum(1 for _, j in r if j == "fail")
        print(f"  {p:<16} {len(r):>5} boards  {sum(1 for _, j in r if j and j != 'fail'):>5} live  "
              f"{fails:>4} throttled  ({time.time() - t0:.0f}s)")
        return r

    with ThreadPoolExecutor(len(by_p)) as ex:
        for r in ex.map(run, list(by_p)):
            results += r
    return results


# ── ledger ───────────────────────────────────────────────────────────────────
def reclassify(ledger):
    """Title-only facts are recomputed every run, so a rule change in features.py applies
    to the whole ledger without a rescan (description-derived facts wait for new postings)."""
    for k in list(ledger):
        j = ledger[k]
        fam = F.family(j["t"])
        if not fam or j["ck"] in u.AGGREGATOR_BOARDS:
            del ledger[k]
            continue
        j["f"], j["lv"] = fam, F.level(j["t"])


def load_state():
    ledger = json.load(gzip.open(LEDGER)) if LEDGER.exists() else {}
    hist = json.load(open(HISTORY)) if HISTORY.exists() else {}
    return ledger, hist


def facts(p, j, company):
    """User-independent facts for one posting (see features.py)."""
    blob = " ".join(filter(None, [j.get("desc"), j.get("comp")]))
    workplace, scope, flags = F.eligibility(j["loc"], j["remote"], blob)
    pay = F.pay(j.get("comp") or "") or F.pay(j.get("desc") or "")
    return {"f": F.family(j["title"]), "lv": F.level(j["title"]), "cc": F.countries(j["loc"]),
            "wp": workplace, "sc": scope, "fl": flags, "pay": list(pay) if pay else None,
            "sk": F.skills(j["title"] + " " + (j.get("desc") or "")), "d": 1 if j.get("desc") else 0}


def merge(ledger, results):
    """Fold one scan into the ledger. Returns per-board stats for boards.json."""
    open_by_board, known_boards = defaultdict(set), {(j["p"], j["s"]) for j in ledger.values()}
    for k, j in ledger.items():
        if not j.get("cl"):
            open_by_board[(j["p"], j["s"])].add(k)
    recent_closed = {(j["ck"], j["t"].lower()): j for j in ledger.values()
                     if j.get("cl") and j["cl"] >= _ago(14)}
    stats, n_new, n_closed = {}, 0, 0
    for b, jobs in results:
        bk = (b["p"], b["slug"])
        if jobs == "fail":
            continue  # carried forward untouched
        live = set()
        # a board we've never read (first run, or newly listed upstream): its postings aren't
        # new today, they're new to us — date them by their own posted date
        bootstrap = bk not in known_boards
        for j in jobs or []:
            fam = F.family(j["title"])
            if not fam:
                continue
            k = f"{b['p']}:{b['slug']}:{j['id']}"
            live.add(k)
            co = j.get("co") or b["name"]
            prev = ledger.get(k)
            if prev and not prev.get("cl"):
                prev["ls"] = TODAY
                prev.update(t=j["title"], loc=j["loc"] or prev["loc"])
                if j.get("desc") and not prev.get("d"):
                    prev.update(facts(b["p"], j, co))
                continue
            rec = {"p": b["p"], "s": b["slug"], "co": co, "ck": norm(co), "t": j["title"], "loc": j["loc"],
                   "u": j["url"], "po": j["posted"], "fs": (j["posted"] or BACKFILL) if bootstrap else TODAY,
                   "ls": TODAY, **facts(b["p"], j, co)}
            twin = recent_closed.get((rec["ck"], rec["t"].lower()))
            if twin:  # same title re-opened within 2 weeks of closing: a repost, not new headcount
                rec["rp"] = twin.get("po") or twin["fs"]
            ledger[k] = rec
            n_new += 1
        for k in open_by_board.get(bk, set()) - live:
            ledger[k]["cl"] = TODAY
            n_closed += 1
        titles = [(j["title"], j["loc"]) for j in jobs or []]
        if titles and b["p"] != "hn":
            total, ai_n, ai_titles, india = u.analyze_jobs(titles)
            india_ai = len({t.strip() for t, loc in titles if u.AI_ROLE.search(t)
                            and not u.NOT_AI_ROLE.search(t) and u.INDIA.search(loc or "")})
            stats[bk] = {"name": b["name"], "ats": b["p"], "slug": b["slug"], "url": b["url"], "open_roles": total,
                         "ai_roles": ai_n, "ai_titles": ai_titles, "india_jobs": india, "india_ai_roles": india_ai}
        elif b["p"] != "hn":
            stats[bk] = {"name": b["name"], "empty": True}  # answered, genuinely empty
    for k in [k for k, j in ledger.items() if j.get("cl") and j["cl"] < _ago(CLOSED_KEEP_DAYS)]:
        del ledger[k]
    print(f"ledger: {sum(1 for j in ledger.values() if not j.get('cl'))} open technical postings, "
          f"+{n_new} new, -{n_closed} closed today")
    return stats


# first-seen stamp for an undated posting on a board we'd never read: it existed before we
# started tracking, so it must not count as "new today" (31.8k false NEWs on the first run)
BACKFILL = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()


def _ago(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def write_boards_json(stats):
    """Keep unicorn_radar.py's input contract (was boards.py's output)."""
    out = HERE / "cache" / "boards.json"
    prior = json.load(open(out)).get("boards", {}) if out.exists() else {}
    by_key, observed = {}, set()
    for r in stats.values():
        k = norm(r["name"])
        observed.add(k)  # answered definitively: has jobs, or genuinely has none
        if k and not r.get("empty") and (k not in by_key or r["ai_roles"] > by_key[k]["ai_roles"]):
            by_key[k] = r
    for k, rec in prior.items():
        if k not in by_key and k not in observed:
            by_key[k] = rec  # throttled this run: keep what we knew
    json.dump({"generated": TODAY, "boards": by_key}, open(out, "w"))


# ── company history, pay tier, events ───────────────────────────────────────
def update_history(ledger, hist):
    """Per-company memory the ledger alone can't hold: countries ever hired in, whether a
    Staff+ role ever appeared, a daily technical-headcount series. Emits today's events."""
    cos = hist.setdefault("companies", {})
    events = [e for e in hist.get("events", []) if e["date"] >= _ago(60)]
    first_run = not cos
    by_co = defaultdict(list)
    for j in ledger.values():
        if not j.get("cl"):
            by_co[j["ck"]].append(j)
    for ck, js in by_co.items():
        h = cos.setdefault(ck, {"cc": [], "staff": False, "n": {}})
        cc_now = {c for j in js for c in j["cc"]}
        staff_now = any(j["lv"] >= 4 for j in js)
        name = js[0]["co"]
        if not first_run:
            for c in sorted(cc_now - set(h["cc"])):
                events.append({"date": TODAY, "ck": ck, "co": name, "type": "new-country", "detail": c})
            if staff_now and not h["staff"]:
                events.append({"date": TODAY, "ck": ck, "co": name, "type": "first-staff",
                               "detail": next(j["t"] for j in js if j["lv"] >= 4)})
            before = h["n"].get(_ago(14)) or h["n"].get(_ago(13)) or h["n"].get(_ago(15))
            if before and len(js) >= before * 1.5 and len(js) - before >= 5:
                events.append({"date": TODAY, "ck": ck, "co": name, "type": "build-out",
                               "detail": f"{before} → {len(js)} technical openings in 2 weeks"})
        h["cc"] = sorted(set(h["cc"]) | cc_now)
        h["staff"] = h["staff"] or staff_now
        h["n"][TODAY] = len(js)
        h["n"] = {d: n for d, n in h["n"].items() if d >= _ago(45)}
    events += india_entity_events({e["ck"] for e in events if e["type"] == "new-india-entity"}, by_co)
    hist["events"] = events
    return hist


def india_entity_events(already, by_co):
    """A new Indian subsidiary is registered before the first Indian posting appears
    (87% of registered entities show no India roles yet) — a founding-team lead."""
    p = HERE / "cache" / "mca_subs.json"
    if not p.exists():
        return []
    recs = json.load(open(p))
    recs = recs.get("records", recs) if isinstance(recs, dict) else recs
    stems = {u.stem(js[0]["co"]): (ck, js[0]["co"]) for ck, js in by_co.items() if len(u.stem(js[0]["co"])) >= 5}
    out = []
    for r in recs:
        dt = str(r.get("CompanyRegistrationdate_date") or "")[:10]
        if dt < _ago(180):
            continue
        hit = stems.get(u.stem(r.get("CompanyName") or r.get("company_name") or ""))
        if hit and hit[0] not in already:
            out.append({"date": dt, "ck": hit[0], "co": hit[1], "type": "new-india-entity",
                        "detail": (r.get("CompanyName") or "").title()})
    return out


def company_stats(ledger):
    """pay tier from the company's own posted bands; median days-to-close; counts."""
    tops, closes, counts = defaultdict(list), defaultdict(list), Counter()
    for j in ledger.values():
        if j.get("pay"):
            tops[j["ck"]].append(F.pay_rank(j["pay"]))
        if j.get("cl") and not j.get("rp"):
            start = j.get("po") or j["fs"]
            closes[j["ck"]].append((datetime.date.fromisoformat(j["cl"]) - datetime.date.fromisoformat(start)).days)
        elif not j.get("cl"):
            counts[j["ck"]] += 1
    out = {}
    for ck in set(tops) | set(closes) | set(counts):
        t = sorted(x for x in tops.get(ck, []) if x)
        top = t[int(len(t) * 0.75)] if t else None  # p75 of top-of-band across its postings
        out[ck] = {"pay_top": top,
                   "pay_tier": None if not top else "top" if top >= 300_000 else "high" if top >= 220_000
                   else "mid" if top >= 150_000 else "low",
                   "ttc": round(statistics.median(closes[ck])) if len(closes.get(ck, [])) >= 3 else None,
                   "tech_open": counts.get(ck, 0)}
    return out


# ── export ───────────────────────────────────────────────────────────────────
# Row layout of web/data/jobs/<family>.json. Compact on purpose: 160k postings shipped as
# plain dicts were 42 MB. Dates are days before `generated`; URLs are the employer's `up`
# prefix + suffix; skills index into index.json's `skills`.
JOB_COLS = ["t", "c", "lv", "loc", "cc", "wp", "sc", "fl", "pay", "po", "fs", "rp", "sk", "u", "src"]
UNPUBLISHED = {"other-eng"}  # electrical/civil/quality engineering: kept in the ledger, not shipped


def _off(d):
    return (datetime.date.fromisoformat(TODAY) - datetime.date.fromisoformat(d)).days if d else None


def export(ledger, hist, cstats):
    out_dir = WEB / "data" / "jobs"
    out_dir.mkdir(parents=True, exist_ok=True)
    comp_path = WEB / "data" / "companies.json"
    companies = json.load(open(comp_path)) if comp_path.exists() else []
    cidx = {norm(c["name"]): i for i, c in enumerate(companies)}
    live = sorted((j for j in ledger.values() if not j.get("cl") and j["f"] not in UNPUBLISHED),
                  key=lambda j: j.get("po") or j["fs"], reverse=True)
    urls = defaultdict(list)
    for j in live:
        urls[j["ck"]].append(j["u"] or "")
    employers, eidx = [], {}
    for j in live:
        if j["ck"] in eidx:
            continue
        s = cstats.get(j["ck"]) or {}
        pre = os.path.commonprefix(urls[j["ck"]])
        pre = pre[:pre.rfind("/") + 1] if len(urls[j["ck"]]) > 1 else ""
        eidx[j["ck"]] = len(employers)
        employers.append({"n": j["co"], "ci": cidx.get(j["ck"], -1), "pt": s.get("pay_tier"),
                          "ptop": s.get("pay_top"), "ttc": s.get("ttc"), "up": pre})
    skill_names = sorted(F.SKILLS)
    sk_i = {k: i for i, k in enumerate(skill_names)}
    shards = defaultdict(list)
    for j in live:
        e = employers[eidx[j["ck"]]]
        shards[j["f"]].append([j["t"], eidx[j["ck"]], j["lv"], j["loc"][:80], ",".join(j["cc"]), j["wp"], j["sc"],
                               ",".join(j["fl"]), j["pay"], _off(j.get("po")), _off(j["fs"]), _off(j.get("rp")),
                               [sk_i[k] for k in j["sk"] if k in sk_i], (j["u"] or "")[len(e["up"]):], j["p"]])
    for old in out_dir.glob("*.json"):
        old.unlink()
    for fam, rows in shards.items():
        json.dump({"rows": rows}, open(out_dir / f"{fam}.json", "w"), separators=(",", ":"))
    json.dump({"generated": TODAY, "cols": JOB_COLS, "employers": employers, "skills": skill_names,
               "families": {f: len(r) for f, r in shards.items()}},
              open(out_dir / "index.json", "w"), separators=(",", ":"))
    # company-level fields refreshed daily without a full unicorn_radar.py rebuild
    for c in companies:
        ck = norm(c["name"])
        s = cstats.get(ck) or {}
        c.update(pay_top=s.get("pay_top"), pay_tier=s.get("pay_tier"), ttc=s.get("ttc"),
                 tech_open=s.get("tech_open", 0))
        wk = ((hist.get("companies", {}).get(ck) or {}).get("n") or {}).get(_ago(7))
        c["tech_delta_7d"] = (s.get("tech_open", 0) - wk) if wk is not None else None
    json.dump(companies, open(comp_path, "w"), separators=(",", ":"))
    export_skills(ledger, cstats)
    json.dump(sorted(hist.get("events", []), key=lambda e: e["date"], reverse=True),
              open(WEB / "data" / "events.json", "w"), separators=(",", ":"))
    export_feeds(ledger)
    meta_p = WEB / "data" / "meta.json"
    meta = json.load(open(meta_p)) if meta_p.exists() else {}
    meta.update(jobs=len(live), jobs_new_7d=sum(1 for j in live if j["fs"] >= _ago(7) and not j.get("rp")),
                jobs_generated=TODAY, job_sources=sorted({j["p"] for j in live}))
    json.dump(meta, open(meta_p, "w"))
    (WEB / "data" / "jobs.json").unlink(missing_ok=True)  # pre-shard format
    print(f"exported {len(live)} open postings in {len(shards)} family shards")


def export_skills(ledger, cstats):
    """What the market asks for: skill share among all technical postings, Staff+, and
    top-pay-tier employers — the last column is the 'what to learn' signal."""
    groups = {"all": [], "staff": [], "toppay": []}
    for j in ledger.values():
        if j.get("cl") or not j.get("d"):
            continue
        groups["all"].append(j)
        if j["lv"] >= 4:
            groups["staff"].append(j)
        if (cstats.get(j["ck"]) or {}).get("pay_tier") in ("top", "high"):
            groups["toppay"].append(j)
    out = {"generated": TODAY, "n": {g: len(v) for g, v in groups.items()}, "skills": {}}
    for g, js in groups.items():
        c = Counter(s for j in js for s in j["sk"])
        for s, n in c.items():
            out["skills"].setdefault(s, {})[g] = round(100 * n / max(len(js), 1), 1)
    json.dump(out, open(WEB / "data" / "skills.json", "w"), separators=(",", ":"))


FEED_FAMILIES = ["ai-applied", "cv", "ml-infra", "research", "ml", "embedded", "backend", "frontend", "fullstack",
                 "infra", "data", "mobile", "security", "eng-mgmt", "product"]


def export_feeds(ledger):
    """Per-family Atom feeds of postings first seen in the last 3 days: a daily digest
    any visitor can subscribe to, with no accounts and no server."""
    (WEB / "feeds").mkdir(exist_ok=True)
    esc = lambda s: html.escape(s or "", quote=True)
    for fam in FEED_FAMILIES:
        js = sorted((j for j in ledger.values() if not j.get("cl") and j["f"] == fam and j["fs"] >= _ago(3)
                     and not j.get("rp")), key=lambda j: (-j["lv"], j["fs"]))[:300]
        entries = "".join(
            f"<entry><title>{esc(j['t'])} — {esc(j['co'])}</title><link href=\"{esc(j['u'])}\"/>"
            f"<id>{esc(j['u'])}</id><updated>{j['fs']}T00:00:00Z</updated>"
            f"<summary>{esc(j['loc'])}{' · pay ' + esc(str(j['pay'][1]) + ' ' + j['pay'][2]) if j.get('pay') else ''}"
            f"</summary></entry>" for j in js)
        (WEB / "feeds" / f"{fam}.xml").write_text(
            f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>unicorn-radar · new {fam} jobs</title><id>unicorn-radar:{fam}</id>"
            f"<updated>{TODAY}T00:00:00Z</updated>{entries}</feed>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--only", default="", help="comma list of platforms (debug)")
    ap.add_argument("--limit", type=int, default=0, help="boards per platform (debug)")
    ap.add_argument("--no-refresh", action="store_true", help="skip re-downloading board lists")
    ap.add_argument("--export-only", action="store_true", help="re-derive + re-export the ledger, no scan")
    args = ap.parse_args()
    if not args.no_refresh:
        refresh_lists()
    only = set(filter(None, args.only.split(",")))
    load_extra()
    boards = [b for b in all_boards(only) if b["p"] in FETCH]
    if args.limit:
        per = defaultdict(int)
        boards = [b for b in boards if (per.__setitem__(b["p"], per[b["p"]] + 1) or per[b["p"]]) <= args.limit]
    ledger, hist = load_state()
    reclassify(ledger)
    if args.export_only:
        export(ledger, hist, company_stats(ledger))
        return
    print(f"scanning {len(boards)} boards across {len({b['p'] for b in boards})} platforms "
          f"({args.workers} workers each; ledger has {len(ledger)} postings)")
    results = scan_all(boards, args.workers, ledger)
    n_fail = sum(1 for _, j in results if j == "fail")
    if n_fail > len(results) * 0.1:
        print(f"  ! {100 * n_fail / len(results):.0f}% throttled — scan is DEGRADED; carried forward, "
              f"but re-run with fewer --workers before trusting today's closures")
    stats = merge(ledger, results)
    if not only and not args.limit:
        write_boards_json(stats)
    hist = update_history(ledger, hist)
    cstats = company_stats(ledger)
    LEDGER.parent.mkdir(exist_ok=True)
    with gzip.open(LEDGER, "wt") as f:
        json.dump(ledger, f, separators=(",", ":"))
    json.dump(hist, open(HISTORY, "w"), separators=(",", ":"))
    export(ledger, hist, cstats)


if __name__ == "__main__":
    main()
