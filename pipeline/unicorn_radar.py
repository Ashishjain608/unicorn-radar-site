#!/usr/bin/env python3
"""unicorn_radar: snapshot of recently-funded AI companies, tagged by funding
bracket, frontier/ai-native/ai-adopter, India presence, and open applied-AI roles.

Sources (all free): Forbes AI 50 + CB Insights AI 100 (seeds.json), YC directory
(2023-2026 AI batches), discover.py's events.json (FinSMEs, SEC Form D, TechCrunch,
StartupTalky, topstartups.io, unicorn list) filtered by classify.py, and jobs.py's
scan of every known ATS board.

Hiring is a *discovery* source, not just an enrichment: a company with an open AI req
is relevant by construction, so jobs.py admits it with no funding event and no LLM
call. Public JSON APIs of Greenhouse / Lever / Ashby / Workable / SmartRecruiters,
seeded by the ats-scrapers slug dumps, with a careers-page ATS sniff as fallback.

Usage: python3 unicorn_radar.py [--skip-jobs] [--workers N]
Output: companies.db (sqlite) + report.csv + web/data/*.json
"""
import argparse, csv, datetime, io, json, re, sqlite3, ssl, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import certifi
CTX = ssl.create_default_context(cafile=certifi.where())  # macOS framework python lacks system CAs

HERE = Path(__file__).parent
UA = {"User-Agent": "unicorn-radar/1.0 (research script)"}
RECENT_BATCHES = {f"{s} {y}" for y in (2023, 2024, 2025, 2026)
                  for s in ("Winter", "Spring", "Summer", "Fall")}
YC_AI_TAGS = {"Artificial Intelligence", "AI", "Generative AI", "Machine Learning",
              "AI Assistant", "AIOps", "Deep Learning", "NLP", "Computer Vision", "AI Agents"}
AI_ROLE = re.compile(r"\b(AI|ML|LLM|GenAI|Machine Learning|Deep Learning|Applied Scientist|"
                     r"Research (Scientist|Engineer)|Forward.Deployed|Prompt|Agentic|AI Agents?|NLP|"
                     r"Computer Vision|MLOps|(Foundation|Frontier|Language) Model|Inference)\b", re.I)
# Bare "Agent" used to be an AI signal, which made cleaning staff ("Agent d'assistance et
# de nettoyage") and call-centre reps rank as top AI employers. The second cluster below
# drops the data-labelling gig farms (AI trainer / annotator / rater) — they post AI roles
# by the hundred, but not the kind an applied-AI engineer is looking for.
NOT_AI_ROLE = re.compile(
    r"\b(customer|support|sales|call|travel|insurance|service|leasing|booking|reservation)\s+agents?\b"
    r"|\bagents?\s+d[e'’]"
    r"|\b(annotator|annotation|labeler|labeller|rater|transcriptionist|transcriber|linguist"
    r"|translator|interpreter|tutor|trainer|teacher|survey|freelance|data collector"
    r"|content specialist|quality specialist)s?\b"  # plural: "AI Trainers Network - Afrikaans" x100
    # localisation / data-services cluster: real jobs, but not applied-AI engineering
    r"|\b(reviewer|tester|evaluator|moderator|proofreader|voice talent|data specialist"
    r"|contributor|validator|validation specialist)s?\b"
    r"|\bnative\s+(language|speaker)"
    # non-technical functions at AI companies — an engineer scanning this list wants roles,
    # not headcount. Sales/advisory titles otherwise inflate the big labs to the top.
    r"|\b(account executive|sales specialist|sales manager|recruiter|talent acquisition"
    r"|advisory (consultant|principal))\b", re.I)
INDIA = re.compile(r"\b(India|Bengaluru|Bangalore|Hyderabad|Pune|Mumbai|Delhi|Gurgaon|"
                   r"Gurugram|Noida|Chennai|Kolkata|Ahmedabad)\b", re.I)
FRONTIER = re.compile(r"\b(foundation model|models|chips?|semiconductor|robot|superintelligence|"
                      r"compute|inference|infrastructure|servers|spatial intelligence|GPU)\b", re.I)


class Throttled(Exception):
    """Transient fetch failure (429/5xx/timeout) — NOT evidence of an empty board.

    Swallowing these is how a rate-limited scan silently becomes 'this company is not
    hiring'. A 32-worker board scan lost 2.5k boards this way, and 57% of them answered
    fine on a re-probe at low concurrency."""


def get_bytes(url, timeout=8, retries=3, data=None, headers=None):
    """Body bytes, None on a definitive miss (401/403/404/410), Throttled on transient
    failure. `data` makes it a POST (Workday's job search is POST-only)."""
    delay = 1.0
    h = {**UA, **(headers or {})}
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h),
                                        timeout=timeout, context=CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 410):
                return None  # definitive: no such board / not public
            if i == retries:
                raise Throttled(f"{e.code} {url}") from e
        except Exception as e:
            if i == retries:
                raise Throttled(f"{type(e).__name__} {url}") from e
        time.sleep(delay)
        delay *= 2
    return None


def get_json(url, timeout=8, retries=3, data=None, headers=None):
    b = get_bytes(url, timeout, retries, data, headers)
    if b is None:
        return None
    try:
        return json.loads(b)
    except ValueError:
        return None  # HTML error page / SPA shell served with 200: not a JSON board


def norm(name):
    """Merge key. classify.py imports THIS — the two must never drift apart again:
    when they did, 1565 companies tagged AI were looked up under a key classify.py
    never wrote and were silently dropped."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    return re.sub(r"(inc|llc|ltd|corp|technologiesinc|labs)$", "", n)


# headline fragments scraped from news slugs arrive as "company names"; recover the
# real name where the sentence shape allows, drop the rest (events, not companies).
APPOSITIVE = re.compile(r"^(.{2,40}?),\s+(?:a|an|the)\s+.*$", re.I)
STARTUP_TAIL = re.compile(r"\b(?:startup|company|firm|maker|platform|developer)\s+"
                          r"([A-Z][\w.&-]*(?:\s[A-Z][\w.&-]*)?)$")
EVENT_HEADLINE = re.compile(r"\b(receives|acquires?|to acquire|to buy|merges?|raises|secures|"
                            r"launches|announces|partners|valuation|credit facility|ipo)\b", re.I)


def clean_name(name):
    """'Sesame, the conversational AI startup...' -> 'Sesame'.  Returns '' to drop."""
    n = " ".join((name or "").split()).strip(" ,;:-")
    if n.lower().startswith("sources:"):
        n = n[8:].strip()
    m = APPOSITIVE.match(n)
    if m:
        n = m.group(1).strip()
    else:
        m = STARTUP_TAIL.search(n)
        if m:
            n = m.group(1).strip()
    n = re.sub(r"\s+(has|is|to|and)$", "", n, flags=re.I).strip(" ,;:-")
    if not n or len(n) < 2 or len(n.split()) > 6 or EVENT_HEADLINE.search(n):
        return ""
    return n


def bracket(musd):
    if musd is None:
        return "unknown"
    if musd < 10:
        return "<$10M"
    if musd < 50:
        return "$10-50M"
    if musd < 200:
        return "$50-200M"
    return "$200M+"


def slug_candidates(name, website):
    cands = []
    if website:
        host = re.sub(r"^https?://(www\.)?", "", website).split("/")[0]
        cands.append(host.split(".")[0])
    n = name.lower()
    cands += [re.sub(r"[^a-z0-9]", "", n),
              re.sub(r"[^a-z0-9]+", "-", n).strip("-"),
              re.sub(r"\s*(ai|labs|inc)$", "", n).replace(" ", "")]
    seen, out = set(), []
    for c in cands:
        if c and len(c) >= 3 and c not in seen:
            seen.add(c)
            out.append(c)
    return out


MCA_STOP = {"ai", "india", "private", "limited", "pvt", "ltd", "technologies", "technology",
            "software", "solutions", "systems", "labs", "services", "digital", "global", "tech"}
MCA_STEMS = set()  # normalized stems of MCA-registered foreign subsidiaries


def stem(name):
    toks = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in MCA_STOP]
    s = "".join(toks)
    # ponytail: "ambientai" fuses when MCA drops the "ai" token but the brand spells it closed;
    # strip trailing fused "ai" only when the remainder is long enough to stay unambiguous.
    # "openai" (len("open")==4 < 5) is intentionally left alone.
    while s.endswith("ai") and len(s) - 2 >= 5:
        s = s[:-2]
    return s


def load_mca_index():
    p = HERE / "cache" / "mca_subs.json"
    if not p.exists():
        print("mca index: cache/mca_subs.json missing (run mca_pull.py) — skipping")
        return
    for r in json.load(open(p)):
        s = stem(r.get("CompanyName") or "")
        if len(s) >= 5:  # ponytail: short stems ("zoom") collide; 5+ chars keeps precision
            MCA_STEMS.add(s)
    print(f"mca index: {len(MCA_STEMS)} foreign-subsidiary stems")


SALARY_INDEX = {}  # stem(company) -> latest in-window post dict from salaries.json
SALARY_INDIA = set()  # stems with an in-window India salary post (₹/India city/ambitionbox)


def _salary_india(posts, today):
    """Pure helper: stems with any in-window India-evidenced salary post."""
    cutoff = (today - datetime.timedelta(days=183)).isoformat()
    return {stem(p.get("company") or "") for p in posts
            if (p.get("date") or "") >= cutoff and stem(p.get("company") or "")
            and (p.get("source") == "ambitionbox" or "₹" in p.get("salary_text", "")
                 or INDIA.search(p.get("salary_text", "")))}


def _salary_index(posts, today):
    """Pure helper: filter to 183-day window, index stem -> latest post (date desc, leetcode wins)."""
    cutoff = (today - datetime.timedelta(days=183)).isoformat()
    idx = {}
    for p in posts:
        if (p.get("date") or "") < cutoff:
            continue
        s = stem(p.get("company") or "")
        if not s:
            continue
        cur = idx.get(s)
        if cur is None:
            idx[s] = p
        elif p["date"] > cur["date"]:
            idx[s] = p
        elif p["date"] == cur["date"] and p.get("source") == "leetcode" and cur.get("source") != "leetcode":
            idx[s] = p
    return idx


def load_salary_index():
    p = HERE / "cache" / "salaries.json"
    if not p.exists():
        print("salary index: cache/salaries.json missing — skipping")
        return
    data = json.load(open(p))
    SALARY_INDEX.update(_salary_index(data.get("posts", []), datetime.date.today()))
    SALARY_INDIA.update(_salary_india(data.get("posts", []), datetime.date.today()))
    print(f"salary index: {len(SALARY_INDEX)} company stems, {len(SALARY_INDIA)} with India salary posts")


# Pre-scanned ATS boards (jobs.py). Replaces the Adzuna layer: an aggregator
# re-indexes other people's postings and keeps them live after the employer closes
# them, whereas a company's own board is the system of record.
BOARDS = {}  # norm(name) -> analysed board record


# Job marketplaces and gig/staffing platforms. Their "board" lists other companies'
# vacancies, so the roles are real but attributed to entirely the wrong employer —
# a role-title regex cannot detect this, only the identity of the board can. Extend
# when a new one shows up implausibly high in the ai_roles ranking.
AGGREGATOR_BOARDS = {"jobgether", "towardjobs", "invisible", "invisibleagency",
                     "10xteam", "humanagency", "remotasks", "weekday", "weekdayai"}


def load_boards_index():
    p = HERE / "cache" / "boards.json"
    if not p.exists():
        print("boards index: cache/boards.json missing (run jobs.py) — skipping")
        return
    data = json.load(open(p)).get("boards", {})
    dropped = [k for k in data if k in AGGREGATOR_BOARDS]
    BOARDS.update({k: v for k, v in data.items() if k not in AGGREGATOR_BOARDS})
    if dropped:
        print(f"boards index: dropped {len(dropped)} job aggregators ({', '.join(sorted(dropped))})")
    hiring = sum(1 for b in BOARDS.values() if b["ai_roles"] > 0)
    ind = sum(1 for b in BOARDS.values() if b.get("india_ai_roles", 0) > 0)
    print(f"boards index: {len(BOARDS)} boards, {hiring} posting AI roles ({ind} with India AI roles)")


MOMENTUM = {}  # norm(name) -> ai_roles at the previous snapshot
MOMENTUM_FILE = HERE / "cache" / "momentum.json"


def load_momentum():
    """Previous run's AI-role counts, so the site can show the *derivative* —
    who is opening AI roles faster than last time, not just who raised money."""
    if not MOMENTUM_FILE.exists():
        print("momentum: no prior snapshot — deltas start next run")
        return
    hist = json.load(open(MOMENTUM_FILE))
    if not hist:
        return
    last = sorted(hist)[-1]
    MOMENTUM.update(hist[last])
    print(f"momentum: comparing against snapshot {last} ({len(MOMENTUM)} companies)")


def save_momentum(rows):
    hist = json.load(open(MOMENTUM_FILE)) if MOMENTUM_FILE.exists() else {}
    hist[datetime.date.today().isoformat()] = {norm(r["name"]): r["ai_roles"]
                                               for r in rows if r["ai_roles"]}
    for old in sorted(hist)[:-12]:  # keep a year of weekly snapshots
        del hist[old]
    MOMENTUM_FILE.parent.mkdir(exist_ok=True)
    json.dump(hist, open(MOMENTUM_FILE, "w"))


ATS_DUMP = "https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/ats-companies/{}.csv"
ATS_INDEX = {}  # norm(name) -> (ats, slug), built once from the ats-scrapers dumps


def load_ats_index():
    (HERE / "cache").mkdir(exist_ok=True)
    for ats in ("greenhouse", "lever", "ashby"):
        cache = HERE / "cache" / f"ats_{ats}.csv"
        if not cache.exists():
            try:
                req = urllib.request.Request(ATS_DUMP.format(ats), headers=UA)
                cache.write_bytes(urllib.request.urlopen(req, timeout=60, context=CTX).read())
            except Exception as e:
                print(f"  ! ats dump {ats}: {e}")
                continue
        rows = list(csv.DictReader(open(cache, errors="replace")))
        name_col = next((c for c in rows[0] if "name" in c.lower()), None) if rows else None
        slug_col = next((c for c in rows[0] if c.lower() in ("slug", "token", "board")), None) if rows else None
        for r in rows:
            n, s = norm(r.get(name_col, "")), (r.get(slug_col) or "").strip()
            if n and s:
                ATS_INDEX.setdefault(n, (ats, s))
    print(f"ats index: {len(ATS_INDEX)} known boards")


def fetch_jobs(ats, slug):
    if ats == "greenhouse":
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if d and d.get("jobs"):
            return [(j["title"], (j.get("location") or {}).get("name", "")) for j in d["jobs"]]
    elif ats == "lever":
        d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if isinstance(d, list) and d:
            return [(j.get("text", ""), (j.get("categories") or {}).get("location", "")) for j in d]
    elif ats == "ashby":
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if d and d.get("jobs"):
            return [(j.get("title", ""), j.get("location", "")) for j in d["jobs"]]
    elif ats == "workable":
        d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
        if d and d.get("jobs"):
            return [(j.get("title", ""), j.get("location", "")) for j in d["jobs"]]
    elif ats == "smartrecruiters":
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
        if d and d.get("content"):
            return [(j.get("name", ""), ", ".join(filter(None, [
                (j.get("location") or {}).get("city"), (j.get("location") or {}).get("country")])))
                for j in d["content"]]
    return []


ATS_LINK = re.compile(r"(?:boards(?:-api)?\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io/embed/job_board\?for=|"
                      r"jobs\.lever\.co|jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board|"
                      r"apply\.workable\.com/|jobs\.smartrecruiters\.com/)/?([A-Za-z0-9_-]{2,40})")
# order matters: match the longest platform token first so "workable" isn't caught by a
# substring of another host. Verified public JSON only — Keka/Darwinbox/Recruitee expose none.
SNIFF_PLATFORMS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")


def sniff_careers(website):
    """Fallback: find an embedded ATS link on the company's site/careers page."""
    base = website.rstrip("/")
    for url in (base + "/careers", base + "/jobs", base):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=8, context=CTX).read(400_000).decode(errors="replace")
        except Exception:
            continue
        m = ATS_LINK.search(html)
        if m:
            for ats in SNIFF_PLATFORMS:
                if ats in m.group(0):
                    return ats, m.group(1)
    return None, None


def probe_ats(company):
    """Try the public job APIs. Returns (ats, slug, jobs). Best-effort: a throttled
    probe yields no data rather than failing the run (jobs.py is the reliable path)."""
    try:
        return _probe_ats(company)
    except Throttled:
        return None, None, []


def _probe_ats(company):
    known = ATS_INDEX.get(norm(company["name"]))
    if known:
        jobs = fetch_jobs(*known)
        if jobs:
            return known[0], known[1], jobs
    for slug in slug_candidates(company["name"], company.get("website")):
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if d and d.get("jobs"):
            meta = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            board = norm((meta or {}).get("name", ""))
            if board and (norm(company["name"]) in board or board in norm(company["name"])):
                return "greenhouse", slug, [(j["title"], (j.get("location") or {}).get("name", ""))
                                            for j in d["jobs"]]
        d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if isinstance(d, list) and d:
            return "lever", slug, [(j.get("text", ""), (j.get("categories") or {}).get("location", ""))
                                   for j in d]
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if d and d.get("jobs"):
            return "ashby", slug, [(j.get("title", ""), j.get("location", "")) for j in d["jobs"]]
    if company.get("website"):
        ats, slug = sniff_careers(company["website"])
        if ats:
            jobs = fetch_jobs(ats, slug)
            if jobs:
                return ats, slug, jobs
    return None, None, []


def analyze_jobs(jobs):
    ai = [(t, loc) for t, loc in jobs if AI_ROLE.search(t) and not NOT_AI_ROLE.search(t)]
    india = any(INDIA.search(loc or "") for _, loc in jobs)
    # Count DISTINCT roles, not postings. Two things inflate raw counts: one req posted
    # per city, and one req posted per language ("Generative AI Analyst | Arabic",
    # "| Bulgarian", ... 196 times). Collapsing the pipe-delimited variant catches the
    # second without touching ordinary titles like "AI Engineer - FDE (...)".
    seen, titles = set(), []
    for t, _ in sorted(ai):
        key = re.sub(r"\s*\|\s*.*$", "", t).strip().lower()
        if key and key not in seen:
            seen.add(key)
            titles.append(t.strip())
    return len(jobs), len(titles), "; ".join(titles[:6]), india


def load_companies():
    companies = {}
    for s in json.load(open(HERE / "seeds.json")):
        key = norm(s["name"])
        if key in companies:  # ponytail: Forbes entry wins (has funding), just append source
            companies[key]["source"] += "+" + s["source"]
            continue
        companies[key] = {**s, "website": None, "batch": None, "is_hiring": None}
    yc = json.load(open(HERE / "yc_cache.json"))
    n_yc = 0
    for c in yc:
        if (c.get("status") == "Active" and c.get("batch") in RECENT_BATCHES
                and YC_AI_TAGS & set(c.get("tags", []))):
            key = norm(c["name"])
            if key in companies:
                companies[key]["source"] += "+yc"
                companies[key]["website"] = companies[key]["website"] or c.get("website")
                continue
            companies[key] = {
                "name": c["name"], "source": f"yc-{c['batch']}", "funding_musd": None,
                "hq": c.get("all_locations"), "desc": c.get("one_liner") or "",
                "category": c.get("subindustry") or c.get("industry") or "",
                "website": c.get("website"), "batch": c.get("batch"),
                "is_hiring": c.get("isHiring"),
                # ponytail: batch year = YC raise year; misses later rounds we can't see for free
                "last_funding_year": int(c["batch"].split()[-1]),
            }
            n_yc += 1

    # discovered funding events (discover.py) filtered by LLM tags (classify.py)
    ev_path, cl_path = HERE / "events.json", HERE / "cache" / "classified.json"
    n_new = 0
    if ev_path.exists() and cl_path.exists():
        tags = json.load(open(cl_path))
        for c in json.load(open(ev_path)):
            key = norm(c["name"])
            tag = tags.get(key)
            last = c["last_event"]
            year = int(last["date"][:4]) if last["date"][:4].isdigit() else None
            if key in companies:  # augment known company with funding-event detail
                x = companies[key]
                x["source"] += "+news"
                x["last_round"] = last.get("round") or x.get("last_round")
                x["last_funding_date"] = last["date"]
                x["last_funding_year"] = max(filter(None, [x.get("last_funding_year"), year]), default=None)
                x["investors"] = "; ".join(c.get("investors", [])[:6]) or x.get("investors")
                x["news_url"] = last.get("url")
                x["website"] = x.get("website") or c.get("website")
                continue
            if tag not in ("frontier", "ai-native", "ai-adopter"):
                continue  # not-ai / unknown / unclassified -> drop
            name = clean_name(c["name"])
            if not name:
                continue  # headline fragment, not a company
            # re-key on the CLEANED name so "Sesame, the conversational AI startup..."
            # merges into "Sesame" instead of shipping as a second company.
            key = norm(name)
            if key in companies:
                companies[key]["source"] += "+news"
                continue
            companies[key] = {
                "name": name, "source": "+".join(c["sources"]), "tag": tag,
                "funding_musd": c.get("last_amount_musd"),
                "hq": c.get("hq") or c.get("country"), "desc": c.get("desc") or "",
                "category": "", "website": c.get("website"), "batch": None, "is_hiring": None,
                "last_funding_year": year, "last_round": last.get("round") or "",
                "last_funding_date": last["date"], "news_url": last.get("url"),
                "investors": "; ".join(c.get("investors", [])[:6]),
            }
            n_new += 1
    # hiring as a discovery source: a company with an open AI req is relevant by
    # construction, so no funding event and no LLM tag are needed to admit it.
    n_board = 0
    for key, b in BOARDS.items():
        if b["ai_roles"] < 1:
            continue
        if key in companies:
            companies[key]["source"] += "+board"
            continue
        name = clean_name(b["name"])
        if not name:
            continue
        # deterministic tag: AI-heavy board => the product is the AI; otherwise a
        # company that merely staffs an AI team.
        # ponytail: role-share heuristic, not a claim about the product. Swap for the
        # LLM tag only if the adopter/native split ever actually matters.
        ai_share = b["ai_roles"] / max(b["open_roles"], 1)
        companies[key] = {
            "name": name, "source": "board", "tag": "ai-native" if ai_share >= 0.5 else "ai-adopter",
            "funding_musd": None, "hq": "", "desc": "", "category": "", "website": None,
            "batch": None, "is_hiring": True, "last_funding_year": None, "last_round": "",
            "last_funding_date": "", "news_url": None, "investors": "",
        }
        n_board += 1
    print(f"loaded {len(companies)} companies ({n_yc} from YC, {n_new} from discovery, "
          f"{n_board} from job boards)")
    return list(companies.values())


def enrich(company):
    # jobs.py already fetched and analysed every known board — reuse that instead
    # of re-probing over HTTP (same data, ~4k fewer round trips).
    b = BOARDS.get(norm(company["name"]))
    if b:
        ats, slug = b["ats"], b["slug"]
        total, ai_n, ai_titles = b["open_roles"], b["ai_roles"], b["ai_titles"]
        india_jobs, india_ai_roles = b["india_jobs"], b.get("india_ai_roles", 0)
    else:
        ats, slug, jobs = probe_ats(company)
        total, ai_n, ai_titles, india_jobs = analyze_jobs(jobs)
        india_ai_roles = sum(1 for t, loc in jobs if AI_ROLE.search(t)
                             and not NOT_AI_ROLE.search(t) and INDIA.search(loc or ""))
    india_entity = len(stem(company["name"])) >= 5 and stem(company["name"]) in MCA_STEMS
    sal = SALARY_INDEX.get(stem(company["name"])) or {}
    company.update(
        ats=ats, ats_slug=slug, open_roles=total, ai_roles=ai_n, ai_role_titles=ai_titles,
        ai_roles_delta=ai_n - MOMENTUM.get(norm(company["name"]), ai_n),
        india_entity=india_entity, india_ai_roles=india_ai_roles,
        india_office=bool(india_jobs or india_entity or india_ai_roles > 0 or stem(company["name"]) in SALARY_INDIA
                          or INDIA.search(company.get("hq") or "")),
        bracket=bracket(company.get("funding_musd")),
        tag=company.get("tag") or ("frontier" if FRONTIER.search(
            f'{company.get("category","")} {company.get("desc","")}') else "ai-native"),
        salary_text=sal.get("salary_text", ""),
        salary_url=sal.get("url", ""),
        salary_date=sal.get("date", ""),
        salary_source=sal.get("source", ""),
    )
    return company


COLS = ["name", "source", "website", "hq", "india_office", "funding_musd", "bracket", "tag",
        "ai_roles", "ai_roles_delta", "open_roles", "ai_role_titles", "ats", "ats_slug", "is_hiring", "desc",
        "last_funding_year", "last_round", "last_funding_date", "investors", "news_url",
        "india_entity", "india_ai_roles",
        "salary_text", "salary_url", "salary_date", "salary_source"]


def save(rows):
    rows.sort(key=lambda r: (-r["ai_roles"], -r["india_office"], -(r["funding_musd"] or 0)))
    db = sqlite3.connect(HERE / "companies.db")
    db.execute("DROP TABLE IF EXISTS companies")
    db.execute(f"CREATE TABLE companies ({','.join(COLS)})")
    db.executemany(f"INSERT INTO companies VALUES ({','.join('?' * len(COLS))})",
                   [[r.get(c) for c in COLS] for r in rows])
    db.commit()
    with open(HERE / "report.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    export_web(rows)
    print(f"saved {len(rows)} companies -> companies.db, report.csv, web/data/")
    hot = [r for r in rows if r["ai_roles"] > 0]
    print(f"\n{len(hot)} companies with open AI roles; top 20:")
    for r in hot[:20]:
        flag = " [INDIA]" if r["india_office"] else ""
        print(f'  {r["name"]:<24} {r["bracket"]:<9} {r["ai_roles"]:>3} AI roles{flag}  {r["ai_role_titles"][:70]}')


JOB_URL = {"greenhouse": "https://job-boards.greenhouse.io/{}",
           "lever": "https://jobs.lever.co/{}",
           "ashby": "https://jobs.ashbyhq.com/{}",
           "workable": "https://apply.workable.com/{}/",
           "smartrecruiters": "https://jobs.smartrecruiters.com/{}",
           "bamboohr": "https://{}.bamboohr.com/careers", "rippling": "https://ats.rippling.com/{}/jobs",
           "personio": "https://{}.jobs.personio.com", "teamtailor": "https://{}.teamtailor.com/jobs",
           "breezy": "https://{}.breezy.hr", "recruitee": "https://{}.recruitee.com"}


def export_web(rows):
    (HERE / "web" / "data").mkdir(parents=True, exist_ok=True)
    out = []
    for r in rows:
        d = {c: r.get(c) for c in COLS}
        tpl = JOB_URL.get(r.get("ats") or "")
        b = BOARDS.get(norm(r["name"])) or {}
        d["jobs_url"] = b.get("url") or (tpl.format(r["ats_slug"]) if tpl and r.get("ats_slug") else None)
        out.append(d)
    json.dump(out, open(HERE / "web" / "data" / "companies.json", "w"))
    meta_p = HERE / "web" / "data" / "meta.json"
    # jobs.py owns the jobs_* keys (refreshed daily); a company rebuild must not erase them
    kept = {k: v for k, v in (json.load(open(meta_p)) if meta_p.exists() else {}).items() if k.startswith("jobs")}
    meta = {
        **kept,
        "snapshot": datetime.date.today().isoformat(),
        "sources": ["Forbes AI 50 (2026)", "CB Insights AI 100 (2026)", "Y Combinator 2023-2026",
                    "FinSMEs archive", "SEC Form D", "TechCrunch", "StartupTalky India",
                    "topstartups.io", "CB Insights unicorn list", "MCA India foreign-subsidiary register",
                    "Live ATS job boards (Greenhouse/Lever/Ashby/Workday/+8 more)", "Hacker News: Who is hiring"],
        "total": len(rows),
        "hiring_ai": sum(1 for r in rows if r["ai_roles"] > 0),
        "india": sum(1 for r in rows if r["india_office"]),
        "total_funding_musd": sum(r.get("funding_musd") or 0 for r in rows),
    }
    json.dump(meta, open(meta_p, "w"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-jobs", action="store_true")
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args()
    load_boards_index()   # before load_companies: boards are a discovery source now
    load_momentum()
    companies = load_companies()
    load_mca_index()
    load_salary_index()
    if args.skip_jobs:
        rows = [enrich_offline(c) for c in companies]
    else:
        load_ats_index()
        with ThreadPoolExecutor(args.workers) as ex:
            rows = list(ex.map(enrich, companies))
    save(rows)
    save_momentum(rows)


def enrich_offline(c):
    b = BOARDS.get(norm(c["name"])) or {}
    india_entity = len(stem(c["name"])) >= 5 and stem(c["name"]) in MCA_STEMS
    india_ai_roles = b.get("india_ai_roles", 0)
    sal = SALARY_INDEX.get(stem(c["name"])) or {}
    c.update(ats=b.get("ats"), ats_slug=b.get("slug"), open_roles=b.get("open_roles", 0),
             ai_roles=b.get("ai_roles", 0), ai_role_titles=b.get("ai_titles", ""),
             ai_roles_delta=b.get("ai_roles", 0) - MOMENTUM.get(norm(c["name"]), b.get("ai_roles", 0)),
             india_entity=india_entity, india_ai_roles=india_ai_roles,
             india_office=bool(india_entity or india_ai_roles > 0 or b.get("india_jobs")
                               or stem(c["name"]) in SALARY_INDIA or INDIA.search(c.get("hq") or "")),
             bracket=bracket(c.get("funding_musd")),
             tag=c.get("tag") or ("frontier" if FRONTIER.search(
                 f'{c.get("category","")} {c.get("desc","")}') else "ai-native"),
             salary_text=sal.get("salary_text", ""),
             salary_url=sal.get("url", ""),
             salary_date=sal.get("date", ""),
             salary_source=sal.get("source", ""))
    return c


if __name__ == "__main__":
    main()

# self-check: python3 -c "import unicorn_radar as f; assert f.bracket(5)=='<$10M'..."
def _selfcheck():
    assert bracket(5) == "<$10M" and bracket(50) == "$50-200M" and bracket(None) == "unknown"
    assert AI_ROLE.search("Applied AI Engineer") and not NOT_AI_ROLE.search("Applied AI Engineer")
    assert NOT_AI_ROLE.search("Customer Support Agent Lead")
    assert NOT_AI_ROLE.search("AI Trainers Network - Afrikaans")

    # real titles that ranked as "top AI employers" before the role filter was tightened
    def is_ai(t):
        return bool(AI_ROLE.search(t) and not NOT_AI_ROLE.search(t))
    for junk in ["Agent d'assistance et de nettoyage", "Customer Service Agent - Survey Assistant",
                 "British Sign Language (BSL) - Freelance AI Trainer Project",
                 "AI Data Annotator - French (Canada)", "AI Data Quality Specialist - English",
                 "AI Content Specialist -  Flexible Hours", "Agent de Comptoir en Location",
                 "Czech AI Ads Reviewer", "AI Speech Tester (Dutch native speaker)",
                 "AI Data Specialist - Bengali", "AI Assistant In-Car Tester",
                 "AI Product Sales Specialist", "AI Advisory Consultant"]:
        assert not is_ai(junk), f"junk role slipped through: {junk!r}"
    for real in ["Staff Machine Learning Engineer", "Research Scientist, Foundation Models",
                 "AI Agent Engineer", "Senior MLOps Engineer", "Applied Scientist, NLP",
                 "Computer Vision Engineer", "Forward Deployed Engineer"]:
        assert is_ai(real), f"real AI role rejected: {real!r}"

    # one req posted in many cities must count once, not once per city
    _dup = [("ML Engineer", "Austin"), ("ML Engineer", "Denver"), ("ML Engineer", "Pune")]
    assert analyze_jobs(_dup)[1] == 1, f"location-spam not deduped: {analyze_jobs(_dup)[1]}"
    # ...nor once per language ("Generative AI Analyst | Arabic", "| Bulgarian", ...)
    _lang = [("Generative AI Analyst | Arabic (Morocco)", "Remote"),
             ("Generative AI Analyst | Bulgarian (Bulgaria)", "Remote"),
             ("AI ML Research& Development Engineer", "Remote")]
    assert analyze_jobs(_lang)[1] == 2, f"language-spam not deduped: {analyze_jobs(_lang)[1]}"
    # but genuinely distinct roles must survive both collapses
    assert analyze_jobs([("AI Engineer - FDE (Forward Deployed)", "SF"),
                         ("AI Engineer, Gov", "NY")])[1] == 2
    assert INDIA.search("Bengaluru, India")
    assert "cursor" in slug_candidates("Cursor", "https://www.cursor.com/about")

    # stem() normalization symmetry: both sides must canonicalize identically
    assert stem("Ambient.ai") == stem("AMBIENTAI INDIA PRIVATE LIMITED"), \
        f"stem asymmetry: {stem('Ambient.ai')!r} != {stem('AMBIENTAI INDIA PRIVATE LIMITED')!r}"
    assert stem("Eudia") == stem("EUDIA AI INDIA PRIVATE LIMITED"), \
        f"stem asymmetry: {stem('Eudia')!r} != {stem('EUDIA AI INDIA PRIVATE LIMITED')!r}"
    # "openai": remainder after stripping "ai" would be "open" (len 4 < 5) — must NOT be stripped
    assert stem("OpenAI") == "openai", f"stem('OpenAI') should be 'openai', got {stem('OpenAI')!r}"

    # norm() must strip legal suffixes: classify.py keys cache/classified.json with THIS
    # function, and when a local copy drifted, 1565 tagged companies were dropped.
    import classify
    assert classify.norm is norm, "classify.py must import norm, not redefine it"
    assert norm("X.AI CORP") == norm("xAI") == "xai", f"got {norm('X.AI CORP')!r}"
    assert norm("Deep 6 AI Inc.") == norm("Deep 6 AI") == "deep6ai"

    # clean_name(): recover the company from a headline fragment, drop pure events
    assert clean_name("Sesame, the conversational AI startup from Oculus founders,") == "Sesame"
    assert clean_name("11x.ai, a developer of AI sales reps, has") == "11x.ai"
    assert clean_name("Legally embattled AI music startup Suno") == "Suno"
    assert clean_name("Sources: AI synthetic research startup Aaru") == "Aaru"
    assert clean_name("Databricks Receives Usd5 25 Billion Credit Facility") == ""
    assert clean_name("Alphasense To Acquire Tegus For 930M") == ""
    assert clean_name("Hakimo") == "Hakimo"  # ordinary names pass through untouched
    assert clean_name("Ambient.ai") == "Ambient.ai"

    # momentum delta: absent from the prior snapshot => 0, not a phantom spike
    assert 7 - {"x": 7}.get("x", 7) == 0 and 9 - {}.get("new", 9) == 0

    # salary index: fixed today = 2026-07-12 (cutoff = 2026-01-10 i.e. today - 183 days)
    _today = datetime.date(2026, 7, 12)
    _posts = [
        # 7-month-old post (2025-12-10 < cutoff 2026-01-10) — must be excluded
        {"company": "Acme", "salary_text": "₹50 LPA", "date": "2025-12-10", "url": "u1", "source": "ambitionbox"},
        # in-window, older of two
        {"company": "Acme", "salary_text": "₹60 LPA", "date": "2026-02-01", "url": "u2", "source": "ambitionbox"},
        # in-window, newer — must win over same-company older post
        {"company": "Acme", "salary_text": "₹70 LPA", "date": "2026-06-01", "url": "u3", "source": "ambitionbox"},
        # same date as the winner above but leetcode — must lose (date tie: leetcode wins only on equal date)
        # use a different company to test tie-break cleanly
        {"company": "Beta", "salary_text": "₹80 LPA", "date": "2026-05-15", "url": "u4", "source": "ambitionbox"},
        {"company": "Beta", "salary_text": "₹85 LPA", "date": "2026-05-15", "url": "u5", "source": "leetcode"},
    ]
    _si = _salary_index(_posts, _today)
    # stale post excluded: only in-window posts remain for Acme
    assert _si[stem("Acme")]["salary_text"] == "₹70 LPA", \
        f"latest-wins failed: {_si[stem('Acme')]['salary_text']!r}"
    # leetcode beats ambitionbox on same date
    assert _si[stem("Beta")]["source"] == "leetcode", \
        f"leetcode tie-break failed: {_si[stem('Beta')]['source']!r}"
    assert _si[stem("Beta")]["url"] == "u5"
    # stale-only company must not appear
    _stale_posts = [{"company": "Ghost", "salary_text": "₹40 LPA", "date": "2025-12-01", "url": "u6", "source": "ambitionbox"}]
    assert stem("Ghost") not in _salary_index(_stale_posts, _today), "stale post must be excluded"

    # india-from-salary signal: ₹/city/ambitionbox posts flag India; $-only does not
    _t = datetime.date(2026, 7, 12)
    _ip = _salary_india([
        {"company": "Rippling", "salary_text": "₹1 Cr · Senior SWE · Bangalore", "date": "2026-07-10", "source": "leetcode"},
        {"company": "UsOnly", "salary_text": "$300k · SWE · NYC", "date": "2026-06-01", "source": "leetcode"},
        {"company": "AbCo", "salary_text": "₹20L CTC · Engineer", "date": "2026-05-01", "source": "ambitionbox"},
        {"company": "StaleCo", "salary_text": "₹50 LPA", "date": "2025-11-01", "source": "leetcode"},
    ], _t)
    assert stem("Rippling") in _ip and stem("AbCo") in _ip
    assert stem("UsOnly") not in _ip and stem("StaleCo") not in _ip

    print("selfcheck ok")
