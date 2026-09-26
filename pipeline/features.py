#!/usr/bin/env python3
"""features: per-posting facts derived once, server-side, and shared by every visitor.

The site is multi-user and static, so nothing here knows who is looking. Each posting
gets user-independent facts (family, level, pay, eligibility, countries, skills); the
browser ranks them against the visitor's own profile. Keep this module pure — no I/O —
so `_selfcheck()` can pin every real title that ever broke a rule.

Run: python3 features.py   (prints "features selfcheck ok")
"""
import html, re

# ── function / family ─────────────────────────────────────────────────────────
# Not tech roles. Checked first: "Sales Engineer" is an engineer by title only.
NOT_TECH = re.compile(
    r"\b(sales|solutions?|support|customer|field|implementation|service|pre-?sales|presales|"
    r"application support|technical account|onboarding|integration specialist)\s+(engineer|architect|consultant)"
    r"|\b(account (executive|manager)|recruit\w*|talent|marketing|counsel|attorney|paralegal|legal|"
    r"finance|accountant|controller|payroll|hr\b|people partner|office manager|executive assistant|"
    r"sales|business development|partnerships?|customer success|support specialist|copywriter|"
    r"content (writer|strategist)|community|social media|brand|designer|operations (manager|associate|specialist)|"
    r"annotator|annotation|labeler|rater|tutor|trainer|translator|linguist|intern\b.*\b(marketing|sales))\b"
    # physical-world engineering: real jobs, but not what a software/AI job-seeker scans for
    r"|\b(mechanical|civil|structural|manufacturing|process|chemical|construction|hvac|facilities|"
    r"maintenance|field application|electrician|technician|plumber|installer|welder|quality (control|assurance) inspector|"
    r"solcells|monteur|elektriker|warehouse|driver)\b", re.I)
TECH = re.compile(
    r"\b(engineer\w*|developer|programmer|scientist|architect|sre|devops|swe|sde|mlops|"
    r"tech(nical)? lead|cto|researcher|research (scientist|engineer)|data scientist|"
    r"head of (engineering|ai|ml|data|platform|infrastructure)|vp,? (of )?engineering|"
    r"engineering (manager|director|lead)|product manager|technical program manager|ingenieur|entwickler)\b", re.I)

AI = r"(\bai\b|\bml\b|machine learning|deep learning|\bllms?\b|genai|generative|\bnlp\b|language models?|foundation models?)"
FAMILY_RULES = [  # first match wins; order encodes specificity
    ("eng-mgmt", r"\b(engineering manager|manager,? (software )?engineering|director,? (of )?engineering|"
                 r"head of (engineering|ai|ml|data|platform|infrastructure)|vp,? (of )?engineering|\bcto\b)"),
    ("product", r"\b(product manager|technical program manager|\btpm\b|product lead)\b"),
    ("cv", r"(computer vision|\bperception\b|\bvision\b|\bvideo\b|\bimage|\bimaging\b|multimodal|3d reconstruction|\bslam\b|autonomy)"),
    ("ml-infra", rf"({AI}.*\b(infra\w*|platform|systems|serving|inference|compiler|kernels?|gpu|training|runtime|distributed)|"
                 r"\b(infra\w*|platform|systems|serving|compiler|kernels?|gpu|training|runtime|distributed)\b.*" + AI +
                 r"|\bmlops\b|\binference\b|\bgpu\b|\bcuda\b|model serving|ml platform)"),
    ("research", r"\b(research (scientist|engineer)|researcher|member of technical staff|\bmts\b)"),
    ("ai-applied", r"(\bllms?\b|\bagents?\b|agentic|genai|generative|applied ai|ai engineer|\bai\b.*\bengineer|"
                   r"forward.deployed|prompt|\brag\b|language models?|\bnlp\b)"),
    ("ml", r"(machine learning|\bml\b|deep learning|data scientist|applied scientist|\bai\b)"),
    ("security", r"\b(security|appsec|infosec|detection|offensive|red team)\b"),
    ("embedded", r"\b(firmware|embedded|robotics|fpga|asic|rtos|bsp|device driver|autonomy software|mechatronics software)\b"),
    ("infra", r"\b(sre|site reliability|devops|infrastructure|platform|cloud|systems|distributed|"
              r"kubernetes|networking|storage|database|reliability|performance)\b"),
    ("data", r"\b(data (engineer|platform|infrastructure)|analytics engineer|etl|data warehouse)\b"),
    ("mobile", r"\b(ios|android|mobile|react native|flutter)\b"),
    ("frontend", r"\b(front[- ]?end|ui engineer|web engineer|react|design engineer)\b"),
    ("fullstack", r"\b(full[- ]?stack)\b"),
    ("backend", r"\b(back[- ]?end|api|server|software engineer|software developer|swe|sde|developer|"
                r"product engineer|founding engineer|golang|java|python|software)\b"),
]
FAMILY_RULES = [(f, re.compile(rx, re.I)) for f, rx in FAMILY_RULES]
AI_FAMILIES = {"ai-applied", "cv", "ml-infra", "research", "ml"}


def family(title):
    """-> family id, or None when the posting is not a technical role."""
    if NOT_TECH.search(title) and not re.search(r"forward.deployed", title, re.I):
        return None
    if not TECH.search(title):
        return None
    for fam, rx in FAMILY_RULES:
        if rx.search(title):
            return fam
    return "other-eng"


# ── seniority ────────────────────────────────────────────────────────────────
# 0 intern · 1 junior · 2 mid · 3 senior · 4 staff · 5 principal+ (incl. director/head/VP)
LEVELS = [
    (5, r"\b(principal|distinguished|fellow|senior staff|sr\.? staff|director|head of|vp\b|vice president|chief|cto)\b"),
    (4, r"\b(staff|architect|lead engineer|tech(nical)? lead|engineering manager|founding)\b|\b(iv|l6|e6|ic5)\b"),
    (0, r"\b(intern|internship|co-?op|apprentice|praktikum|werkstudent|stagiaire)\b"),
    (3, r"\b(senior|sr\.?|lead|iii|l5|e5|ic4)\b|\bmanager\b"),
    (1, r"\b(junior|jr\.?|associate|entry|graduate|new grad|early career|trainee)\b|\b(i|l3|e3)$"),
]
LEVELS = [(n, re.compile(rx, re.I)) for n, rx in LEVELS]


def level(title):
    for n, rx in LEVELS:
        if rx.search(title):
            return n
    return 2


# ── pay ──────────────────────────────────────────────────────────────────────
# ponytail: fixed FX constants — tiering needs rank order, not today's rate.
FX = {"USD": 1, "EUR": 1.1, "GBP": 1.3, "CAD": 0.73, "AUD": 0.66, "CHF": 1.15, "SGD": 0.75,
      "INR": 0.012, "JPY": 0.0068, "SEK": 0.095, "PLN": 0.25, "ILS": 0.27}
SYM = {"$": "USD", "US$": "USD", "USD": "USD", "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP",
       "CA$": "CAD", "CAD": "CAD", "C$": "CAD", "A$": "AUD", "AUD": "AUD", "CHF": "CHF", "S$": "SGD",
       "SGD": "SGD", "₹": "INR", "INR": "INR", "¥": "JPY", "JPY": "JPY", "SEK": "SEK", "PLN": "PLN", "₪": "ILS", "ILS": "ILS"}
_CUR = r"(US\$|CA\$|C\$|A\$|S\$|USD|EUR|GBP|CAD|AUD|CHF|SGD|INR|JPY|SEK|PLN|ILS|[$€£₹¥₪])"
_NUM = r"(\d{1,3}(?:[,.\s]\d{3})+|\d+(?:\.\d+)?)\s*([kKmM]|LPA|lakhs?|L)?"
PAY_RANGE = re.compile(rf"{_CUR}\s?{_NUM}\s*(?:-|–|—|to)\s*{_CUR}?\s?{_NUM}(?:\s*{_CUR})?", re.I)


def _amount(num, suffix):
    v = float(re.sub(r"[,\s]", "", num) if re.search(r"[,\s]\d{3}\b", num) else num.replace(",", ""))
    if re.fullmatch(r"\d{1,3}\.\d{3}", num):  # European thousands "180.000"
        v = float(num.replace(".", ""))
    s = (suffix or "").lower()
    if s == "k":
        v *= 1e3
    elif s == "m":
        v *= 1e6
    elif s in ("lpa", "l", "lakh", "lakhs"):
        v *= 1e5
    return v


def pay(text):
    """-> (min, max, currency) of the widest annual range in text, or None.
    Hourly rates and funding amounts fall outside the annual-salary window."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    best = None
    for m in PAY_RANGE.finditer(text):
        cur = SYM.get((m.group(1) or "").upper(), SYM.get(m.group(1)))
        if not cur:
            continue
        try:
            # "$165-210k": the suffix on the upper bound applies to the lower one too
            lo, hi = _amount(m.group(2), m.group(3) or m.group(6)), _amount(m.group(5), m.group(6) or m.group(3))
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        usd_hi = hi * FX[cur]
        tail = text[m.end():m.end() + 25].lower()
        if not 15_000 <= usd_hi <= 2_000_000 or re.match(r"\s*(/|per)\s*(hr|hour|h\b)", tail):
            continue
        if best is None or usd_hi > best[1] * FX[best[2]]:
            best = (round(lo), round(hi), cur)
    return best


def usd(amount, cur):
    return round(amount * FX.get(cur, 0)) if amount else None


def pay_rank(p):
    """USD value to rank a band by. Tops are capped at 2.5x the floor: some employers post
    "$190k - $1.7M" (salary + equity upside), which would otherwise outrank every real band."""
    lo, hi, cur = p
    return usd(min(hi, lo * 2.5) if lo else hi, cur)


# ── location / eligibility ───────────────────────────────────────────────────
COUNTRIES = {
    "US": r"united states|\busa?\b|u\.s\.|america|san francisco|\bsf\b|bay area|new york|\bnyc\b|seattle|boston|austin|"
          r"los angeles|chicago|denver|atlanta|miami|palo alto|mountain view|menlo park|sunnyvale|san jose|san mateo|"
          r"redwood city|santa clara|san diego|washington,? d\.?c|pittsburgh|philadelphia|portland|salt lake|"
          r"raleigh|dallas|houston|boulder|cambridge, ma|ann arbor|minneapolis|nashville|phoenix",
    "CA": r"canada|toronto|vancouver|montreal|ottawa|waterloo|calgary",
    "GB": r"united kingdom|\buk\b|england|london|manchester|edinburgh|cambridge, uk|oxford|bristol|scotland",
    "IE": r"ireland|dublin",
    "DE": r"germany|deutschland|berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart",
    "FR": r"france|paris|lyon",
    "NL": r"netherlands|amsterdam|rotterdam|utrecht|eindhoven",
    "CH": r"switzerland|zurich|zürich|geneva|lausanne",
    "ES": r"spain|madrid|barcelona",
    "PT": r"portugal|lisbon|porto",
    "IT": r"italy|milan|rome",
    "SE": r"sweden|stockholm|gothenburg",
    "DK": r"denmark|copenhagen",
    "NO": r"norway|oslo",
    "FI": r"finland|helsinki",
    "PL": r"poland|warsaw|krakow|kraków|wroclaw",
    "CZ": r"czech|prague",
    "AT": r"austria|vienna",
    "BE": r"belgium|brussels",
    "EE": r"estonia|tallinn",
    "RO": r"romania|bucharest",
    "UA": r"ukraine|kyiv",
    "IL": r"israel|tel aviv|haifa|jerusalem",
    "AE": r"united arab emirates|\buae\b|dubai|abu dhabi",
    "IN": r"india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|kolkata|ahmedabad",
    "SG": r"singapore",
    "JP": r"japan|tokyo|osaka",
    "KR": r"korea|seoul",
    "CN": r"china|beijing|shanghai|shenzhen|hangzhou",
    "HK": r"hong kong",
    "TW": r"taiwan|taipei",
    "AU": r"australia|sydney|melbourne|brisbane|perth",
    "NZ": r"new zealand|auckland",
    "BR": r"brazil|são paulo|sao paulo|rio de janeiro",
    "MX": r"mexico|ciudad de méxico|guadalajara",
    "AR": r"argentina|buenos aires",
    "CO": r"colombia|bogota|bogotá|medellin",
    "PH": r"philippines|manila",
    "VN": r"vietnam|hanoi|ho chi minh",
    "ID": r"indonesia|jakarta",
    "MY": r"malaysia|kuala lumpur",
    "PK": r"pakistan|lahore|karachi",
    "NG": r"nigeria|lagos",
    "KE": r"kenya|nairobi",
    "ZA": r"south africa|cape town|johannesburg",
    "EG": r"egypt|cairo",
    "TR": r"turkey|türkiye|istanbul",
}
COUNTRIES = {c: re.compile(rf"(?:{rx})", re.I) for c, rx in COUNTRIES.items()}
US_STATE = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|"
                      r"NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b")
REGIONS = {"EU": {"DE", "FR", "NL", "ES", "PT", "IT", "SE", "DK", "FI", "PL", "CZ", "AT", "BE", "EE", "RO", "IE"},
           "EMEA": {"GB", "IE", "DE", "FR", "NL", "CH", "ES", "PT", "IT", "SE", "DK", "NO", "FI", "PL", "CZ", "AT",
                    "BE", "EE", "RO", "UA", "IL", "AE", "NG", "KE", "ZA", "EG", "TR"},
           "APAC": {"IN", "SG", "JP", "KR", "CN", "HK", "TW", "AU", "NZ", "PH", "VN", "ID", "MY", "PK"},
           "LATAM": {"BR", "MX", "AR", "CO"},
           "AMER": {"US", "CA", "BR", "MX", "AR", "CO"}}
REGION_RX = re.compile(r"\b(EU|Europe|EMEA|APAC|Asia|LATAM|Latin America|Americas|North America)\b", re.I)
REGION_NAME = {"eu": "EU", "europe": "EMEA", "emea": "EMEA", "apac": "APAC", "asia": "APAC", "latam": "LATAM",
               "latin america": "LATAM", "americas": "AMER", "north america": "AMER"}


def countries(loc):
    """Location string -> sorted ISO country codes it names (possibly several)."""
    loc = loc or ""
    out = {c for c, rx in COUNTRIES.items() if rx.search(loc)}
    if US_STATE.search(loc) and "CA" in out and not re.search(r"canada|toronto|vancouver|montreal", loc, re.I):
        out.discard("CA")  # "Palo Alto, CA" is California, not Canada
    if US_STATE.search(loc):
        out.add("US")
    for m in REGION_RX.finditer(loc):
        out |= REGIONS[REGION_NAME[m.group(1).lower()]]
    return sorted(out)


REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|distributed team|telecommut|fully distributed", re.I)
HYBRID = re.compile(r"\bhybrid\b", re.I)
WORLDWIDE = re.compile(r"(remote.{0,25}\b(worldwide|global(ly)?|anywhere)\b|\b(worldwide|global(ly)?|anywhere)\b.{0,20}remote"
                       r"|work from anywhere|any time ?zone|location[- ]agnostic|fully remote,? (worldwide|global))", re.I)
AUTH_REQUIRED = re.compile(
    r"(must|required to|need to) (be )?(legally )?(authori[sz]ed|eligible) to work in (the )?"
    r"(united states|us\b|u\.s\.|uk\b|united kingdom|canada|eu\b|european union|australia|india)"
    r"|(must|required to|need to) (reside|be located|be based|live) in (the )?(united states|us\b|u\.s\.|uk\b|canada|eu\b|europe)"
    r"|without (the need for )?(current or future )?(visa )?sponsorship", re.I)
CLEARANCE = re.compile(r"(security clearance|\bts/sci\b|top secret|secret clearance|\bu\.?s\.? citizen(ship)?\b|"
                       r"\bus persons?\b|\bitar\b|green card holders?)", re.I)
NO_SPONSOR = re.compile(r"((not|unable to|cannot|can't|won't|will not|do not|does not)\s+(currently\s+)?"
                        r"(be able to\s+)?(provide|offer|support|sponsor)\w*\s+(\w+\s+){0,3}?(visa|sponsorship|immigration)"
                        r"|no (visa )?sponsorship|sponsorship (is )?not (available|provided|offered))", re.I)
SPONSOR = re.compile(r"(visa sponsorship (is )?(available|provided|offered|possible)|we (will|can|do) sponsor|"
                     r"(offer|provide|support)s? (visa|immigration) sponsorship|sponsor (your |work )?visas?|"
                     r"relocation (assistance|support|package|bonus)|(help|support) (with |you )?(visa|relocat)|"
                     r"(visa|relocation) support)", re.I)


def eligibility(loc, remote_flag, text):
    """-> (workplace, remote_scope, flags)
    workplace: remote | hybrid | onsite | ''       remote_scope: 'global' | ISO codes joined by ',' | ''
    flags: subset of {sponsor, no-sponsor, auth-required, clearance}"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    loc = loc or ""
    rf = str(remote_flag or "").lower()
    if rf in ("remote", "true", "fully_remote", "remote_only") or REMOTE.search(loc):
        workplace = "remote"
    elif rf == "hybrid" or HYBRID.search(loc):
        workplace = "hybrid"
    elif rf in ("on-site", "onsite", "on_site", "in_office", "false"):
        workplace = "onsite"
    else:
        workplace = ""
    scope = ""
    if workplace == "remote":
        if WORLDWIDE.search(loc) or WORLDWIDE.search(text[:6000]):
            scope = "global"
        else:
            scope = ",".join(countries(loc))
    flags = set()
    if NO_SPONSOR.search(text):
        flags.add("no-sponsor")
    elif SPONSOR.search(text):
        flags.add("sponsor")
    if AUTH_REQUIRED.search(text):
        flags.add("auth-required")
    if CLEARANCE.search(text):
        flags.add("clearance")
    return workplace, scope, sorted(flags)


# ── skills ───────────────────────────────────────────────────────────────────
SKILLS = {
    "python": r"\bpython\b", "go": r"\bgolang\b|\bgo\b(?= (?:and|or|,|/|\)))", "rust": r"\brust\b", "c++": r"c\+\+",
    "java": r"\bjava\b", "typescript": r"\btypescript\b", "javascript": r"\bjavascript\b", "scala": r"\bscala\b",
    "kotlin": r"\bkotlin\b", "swift": r"\bswift\b", "react": r"\breact(?!ive)(\.js)?\b", "next.js": r"\bnext\.?js\b",
    "node": r"\bnode(\.js)?\b", "graphql": r"\bgraphql\b", "postgres": r"\bpostgres(ql)?\b", "kafka": r"\bkafka\b",
    "spark": r"\bspark\b", "airflow": r"\bairflow\b", "dbt": r"\bdbt\b", "snowflake": r"\bsnowflake\b",
    "kubernetes": r"\bkubernetes\b|\bk8s\b", "docker": r"\bdocker\b", "terraform": r"\bterraform\b",
    "aws": r"\baws\b", "gcp": r"\bgcp\b|google cloud", "azure": r"\bazure\b",
    "distributed-systems": r"distributed systems", "pytorch": r"\bpytorch\b", "tensorflow": r"\btensorflow\b",
    "jax": r"\bjax\b", "cuda": r"\bcuda\b", "triton": r"\btriton\b", "vllm": r"\bvllm\b", "tensorrt": r"\btensorrt\b",
    "onnx": r"\bonnx\b", "llm": r"\bllms?\b|large language model", "rag": r"\brag\b|retrieval.augmented",
    "agents": r"\bagents?\b|agentic", "fine-tuning": r"fine.tun", "rlhf": r"\brlhf\b|reinforcement learning from human",
    "evals": r"\bevals?\b|evaluation harness", "diffusion": r"\bdiffusion\b", "transformers": r"\btransformers?\b",
    "computer-vision": r"computer vision", "opencv": r"\bopencv\b", "video": r"\bvideo\b", "multimodal": r"multi.?modal",
    "edge": r"\bedge (devices?|computing|inference|deployment)\b|on.device", "embedded": r"\bembedded\b",
    "robotics": r"\brobotics?\b", "ros": r"\bros2?\b", "langchain": r"\blangchain\b", "mlops": r"\bmlops\b",
    "recsys": r"recommend(er|ation) systems?", "nlp": r"\bnlp\b|natural language", "speech": r"\bspeech\b|\basr\b|\btts\b",
}
SKILLS = {k: re.compile(rx, re.I) for k, rx in SKILLS.items()}


def skills(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return sorted(k for k, rx in SKILLS.items() if rx.search(text))


def _selfcheck():
    # family: every one of these is a real title from the 2026-09-23 sample
    assert family("Staff Software Engineer, Identity Infrastructure Engineering") == "infra"
    assert family("Senior Machine Learning Researcher, Large Behavior Models & Diffusion Policy") == "research"
    assert family("Forward Deployed Engineer, Operations & Sustainment (R5497)") == "ai-applied"
    assert family("Senior Deep Learning Engineer, Inference") == "ml-infra"
    assert family("Staff Engineer, Computer Vision") == "cv"
    assert family("Applied AI Engineer") == "ai-applied"
    assert family("Solutions Engineer (FedRAMP)") is None
    assert family("Journeyman Electrician") is None
    assert family("Solcellsmontör till 1KOMMA5°") is None
    assert family("Account Executive, AI") is None
    assert family("Staff Frontend Engineer") == "frontend"
    assert family("Senior Backend Engineer (Go)") == "backend"
    assert family("Engineering Manager, ML Platform") == "eng-mgmt"
    assert family("Senior Product Manager, Agents") == "product"
    assert family("Machine Learning Engineer") == "ml"
    assert family("Firmware Engineer, Robotics") == "embedded"
    assert family("Senior Software Test Development Engineer") == "backend"
    assert family("Electrical Engineer") == "other-eng"  # kept in the ledger, not published
    # level
    assert level("Principal Software Engineer") == 5 and level("Senior Staff Software Engineer, Identity") == 5
    assert level("Staff Security Reliability Engineer") == 4 and level("Senior Backend Engineer") == 3
    assert level("Software Engineer") == 2 and level("Software Engineering Intern") == 0
    assert level("Software Engineer I") == 1
    # pay
    assert pay("The base salary range is $180,000 - $250,000 USD") == (180000, 250000, "USD")
    assert pay("$180K–$250K + equity") == (180000, 250000, "USD")
    assert pay("<p>Salary: £90,000 to £120,000</p>") == (90000, 120000, "GBP")
    assert pay("€80.000 - €100.000 per year") == (80000, 100000, "EUR")
    assert pay("₹40L - ₹60L") == (4000000, 6000000, "INR")
    assert pay("$165-210k, equity") == (165000, 210000, "USD")
    assert "clearance" not in eligibility("SF", None, "we build for military and civil customers")[2]
    assert pay_rank((190000, 1700000, "USD")) == 475000 and pay_rank((405000, 850000, "USD")) == 850000
    assert pay("$45 - $60 per hour") is None, "hourly rates are not salaries"
    assert pay("We raised $100M - $120M in funding") is None
    # countries / eligibility
    assert countries("Palo Alto, CA") == ["US"] and countries("Toronto, Canada") == ["CA"]
    assert countries("Bengaluru, India") == ["IN"] and "DE" in countries("Remote - EU")
    assert eligibility("US - Remote", None, "")[:2] == ("remote", "US")
    assert eligibility("Remote", "remote", "We are a fully remote team, hiring worldwide. Work from anywhere.")[1] == "global"
    assert "no-sponsor" in eligibility("NYC", None, "We are unable to sponsor visas for this role.")[2]
    assert "sponsor" in eligibility("London", None, "Visa sponsorship is available and relocation support.")[2]
    assert "clearance" in eligibility("DC", None, "Active TS/SCI security clearance required")[2]
    assert "auth-required" in eligibility("SF", None, "Must be authorized to work in the United States")[2]
    assert skills("Experience with PyTorch, vLLM and Kubernetes; RAG pipelines") == ["kubernetes", "pytorch", "rag", "vllm"]
    print("features selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
