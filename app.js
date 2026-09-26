/* UNICORN·RADAR — fetch JSON, filter, rank, render. No deps.
   Multi-user and static: the pipeline publishes user-independent facts per posting;
   each visitor's profile (kept in localStorage, shareable as ?p=) ranks them here. */
const PAGE = 120, JPAGE = 60;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtM = (m) => m >= 1000 ? `$${(m / 1000).toFixed(m >= 10000 ? 0 : 1)}B` : `$${Math.round(m)}M`;
const fmtK = (n) => `$${Math.round(n / 1000)}k`;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
const TODAY = new Date().toISOString().slice(0, 10);
const daysAgo = (iso) => iso ? Math.round((Date.parse(TODAY) - Date.parse(iso)) / 864e5) : null;
const isoAgo = (n) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);

// must match features.py
const FAMILIES = [["ai-applied", "Applied AI / LLM"], ["cv", "Computer vision"], ["ml-infra", "ML infra / inference"],
  ["research", "Research"], ["ml", "ML / data science"], ["backend", "Backend"], ["frontend", "Frontend"],
  ["fullstack", "Full-stack"], ["infra", "Infra / SRE"], ["data", "Data eng"], ["mobile", "Mobile"],
  ["security", "Security"], ["embedded", "Embedded / robotics"], ["eng-mgmt", "Eng management"], ["product", "Product / TPM"]];
const AI_FAMS = ["ai-applied", "cv", "ml-infra", "research", "ml"];
const FAM = Object.fromEntries(FAMILIES);
const LEVEL = ["intern", "junior", "mid", "senior", "staff", "principal+"];
const FX = { USD: 1, EUR: 1.1, GBP: 1.3, CAD: 0.73, AUD: 0.66, CHF: 1.15, SGD: 0.75, INR: 0.012, JPY: 0.0068, SEK: 0.095, PLN: 0.25, ILS: 0.27 };
const TIER = { top: 3, high: 2, mid: 1, low: 0 };
const COUNTRY = { US: "United States", CA: "Canada", GB: "United Kingdom", IE: "Ireland", DE: "Germany", FR: "France",
  NL: "Netherlands", CH: "Switzerland", ES: "Spain", PT: "Portugal", IT: "Italy", SE: "Sweden", DK: "Denmark", NO: "Norway",
  FI: "Finland", PL: "Poland", CZ: "Czechia", AT: "Austria", BE: "Belgium", EE: "Estonia", RO: "Romania", UA: "Ukraine",
  IL: "Israel", AE: "UAE", IN: "India", SG: "Singapore", JP: "Japan", KR: "South Korea", CN: "China", HK: "Hong Kong",
  TW: "Taiwan", AU: "Australia", NZ: "New Zealand", BR: "Brazil", MX: "Mexico", AR: "Argentina", CO: "Colombia",
  PH: "Philippines", VN: "Vietnam", ID: "Indonesia", MY: "Malaysia", PK: "Pakistan", NG: "Nigeria", KE: "Kenya",
  ZA: "South Africa", EG: "Egypt", TR: "Türkiye" };

let DATA = [], MAXAI = 1, JOBS = [], EMP = [], EVENTS = [], SKILLS = null, META = {};
let JIDX = null, GEN_AGE = 0;           // jobs/index.json; days since it was generated
const LOADED = new Set();              // family shards fetched so far

/* ── profile ─────────────────────────────────────────────────────────────── */
function guessHome() {
  const loc = (navigator.language || "") + " " + (Intl.DateTimeFormat().resolvedOptions().locale || "");
  const m = loc.match(/-([A-Z]{2})\b/);
  return m && COUNTRY[m[1]] ? m[1] : "US";
}
const DEFAULT_P = { fam: AI_FAMS, lv: 0, home: guessHome(), where: "home", pay: 0, tier: "", sk: "", noghost: true, aionly: false };
let P = { ...DEFAULT_P, ...store.get("ur-profile", {}) };
try {
  const q = new URLSearchParams(location.search).get("p");
  if (q) { P = { ...DEFAULT_P, ...JSON.parse(decodeURIComponent(escape(atob(q)))) }; store.set("ur-profile", P); }
} catch {}
// "last visit" = the most recent earlier *day* you opened the site, so reloading today
// doesn't wipe today's NEW badges
const VISIT = store.get("ur-visit", { prev: null, cur: null });
if (VISIT.cur !== TODAY) { VISIT.prev = VISIT.cur; VISIT.cur = TODAY; store.set("ur-visit", VISIT); }
const PREV_VISIT = VISIT.prev;
const NEW_SINCE = PREV_VISIT || isoAgo(7);

function bindProfile() {
  $("#pf-families").innerHTML = FAMILIES.map(([k, v]) =>
    `<button type="button" class="chip${P.fam.includes(k) ? " on" : ""}" data-fam="${k}">${esc(v)}</button>`).join("");
  $("#pf-home").innerHTML = Object.entries(COUNTRY).sort((a, b) => a[1].localeCompare(b[1]))
    .map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#pf-level").value = P.lv; $("#pf-home").value = P.home; $("#pf-where").value = P.where;
  $("#pf-pay").value = P.pay; $("#pf-tier").value = P.tier; $("#pf-skills").value = P.sk;
  $("#pf-noghost").checked = P.noghost; $("#pf-aionly").checked = P.aionly;
  $("#pf-families").addEventListener("click", (e) => {
    const c = e.target.closest("[data-fam]"); if (!c) return;
    c.classList.toggle("on");
    P.fam = [...document.querySelectorAll("#pf-families .chip.on")].map((x) => x.dataset.fam);
    saveProfile();
  });
  const bind = (id, key, cast = (v) => v, ev = "change") =>
    $(id).addEventListener(ev, (e) => { P[key] = cast(e.target.type === "checkbox" ? e.target.checked : e.target.value); saveProfile(); });
  bind("#pf-level", "lv", Number); bind("#pf-home", "home"); bind("#pf-where", "where");
  bind("#pf-pay", "pay", Number); bind("#pf-tier", "tier"); bind("#pf-noghost", "noghost"); bind("#pf-aionly", "aionly");
  let t; $("#pf-skills").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { P.sk = e.target.value; saveProfile(); }, 250); });
  $("#pf-share").addEventListener("click", async (e) => {
    const url = `${location.origin}${location.pathname}?p=${btoa(unescape(encodeURIComponent(JSON.stringify(P))))}#jobs`;
    try { await navigator.clipboard.writeText(url); e.target.textContent = "LINK COPIED"; }
    catch { prompt("Copy your profile link", url); }
    setTimeout(() => (e.target.textContent = "COPY PROFILE LINK"), 1800);
  });
}
async function saveProfile() { store.set("ur-profile", P); jstate.shown = JPAGE; await ensureShards(); renderJobs(); }
function profileSummary() {
  const fams = P.fam.length ? P.fam.map((f) => FAM[f]).join(", ") : "all roles";
  const where = { home: `${COUNTRY[P.home]} + remote`, relocate: "open to relocate", remote: "remote only", anywhere: "anywhere" }[P.where];
  return `${fams} · ${P.lv ? LEVEL[P.lv] + "+" : "any level"} · ${where}${P.pay ? " · " + fmtK(P.pay) + "+" : ""}`;
}

/* ── jobs ────────────────────────────────────────────────────────────────── */
const jstate = { q: "", sort: "match", newOnly: false, shown: JPAGE };

// shards are per family, so a visitor downloads only the roles in their profile
async function ensureShards() {
  if (!JIDX) return;
  const want = (P.fam.length ? P.fam : Object.keys(JIDX.families)).filter((f) => JIDX.families[f] && !LOADED.has(f));
  if (!want.length) return;
  $("#job-count").textContent = "LOADING JOBS…";
  const shards = await Promise.all(want.map((f) => fetch(`data/jobs/${f}.json`, { cache: "no-cache" })
    .then((r) => r.json()).then((d) => [f, d.rows]).catch(() => [f, []])));
  for (const [f, rows] of shards) { LOADED.add(f); for (const r of rows) JOBS.push(toJob(r, f)); }
}

const offIso = (off) => off == null ? null : isoAgo(off + GEN_AGE);
function toJob(r, fam) {
  const j = { f: fam }; JIDX.cols.forEach((c, i) => (j[c] = r[i]));
  j.e = EMP[j.c];
  j.co = DATA[j.e.ci];
  j.cc = j.cc ? j.cc.split(",") : []; j.fl = j.fl ? j.fl.split(",") : [];
  j.sk = j.sk.map((i) => JIDX.skills[i]);
  j.u = (j.e.up || "") + j.u;
  j.age = j.po != null ? j.po + GEN_AGE : j.fs != null ? j.fs + GEN_AGE : null;
  j.po = offIso(j.po); j.fs = offIso(j.fs); j.rp = offIso(j.rp);
  // rank value: top of band capped at 2.5x the floor (features.pay_rank)
  j.payUSD = j.pay ? Math.min(j.pay[1], j.pay[0] ? j.pay[0] * 2.5 : Infinity) * (FX[j.pay[2]] || 0) : null;
  j.isNew = j.fs > NEW_SINCE && !j.rp;
  j.hay = `${j.t} ${j.e.n} ${j.loc}`.toLowerCase();
  return j;
}

// Can this visitor plausibly take this job? -> [ok, score bonus, short reason]
function eligibility(j) {
  if (P.where === "anywhere") return [true, 0, ""];
  const home = P.home, abroad = () => {
    if (P.where !== "relocate") return [false];
    if (j.fl.includes("clearance")) return [false];
    if (j.fl.includes("no-sponsor") || j.fl.includes("auth-required")) return [true, -3, "no sponsorship"];
    if (j.fl.includes("sponsor")) return [true, 1.5, "sponsors / relocates"];
    return [true, -0.5, "sponsorship not stated"];
  };
  if (j.wp === "remote") {
    if (j.sc === "global") return [true, 1, "remote worldwide"];
    if (!j.sc) return [true, -0.5, "remote, region unstated"];
    if (j.sc.split(",").includes(home)) return [true, 0.5, ""];
    return P.where === "remote" ? [false] : abroad();
  }
  if (P.where === "remote") return [false];
  if (!j.cc.length) return [true, -0.7, "location unclear"];
  if (j.cc.includes(home)) return [true, 0, ""];
  return abroad();
}

function jobScore(j, el, tokens) {
  let s = el[1];
  if (j.payUSD) s += Math.min(Math.max((j.payUSD - 120000) / 60000, 0), 4);
  s += [0, 0.5, 1.2, 2][TIER[j.e.pt] ?? 0] || 0;
  s += j.age == null ? 0 : j.age <= 3 ? 2.5 : j.age <= 7 ? 2 : j.age <= 30 ? 1 : j.age > 180 ? -2 : 0;
  if (j.rp) s -= 1;
  let hits = 0;
  for (const t of tokens) if (j.sk.includes(t) || j.hay.includes(t)) hits++;
  s += Math.min(hits * 0.8, 4);
  if (j.co && j.co.tag === "frontier") s += 0.5;
  if (P.pay && !j.payUSD) s -= 0.5;
  return s;
}

function filteredJobs() {
  const q = jstate.q.toLowerCase(), fams = new Set(P.fam), minTier = TIER[P.tier] ?? -1;
  const tokens = P.sk.toLowerCase().split(/[,;]+/).map((x) => x.trim()).filter(Boolean);
  const out = [];
  for (const j of JOBS) {
    if (fams.size && !fams.has(j.f)) continue;
    if (j.lv < P.lv) continue;
    if (P.pay && j.payUSD && j.payUSD < P.pay) continue;
    if (P.tier && (TIER[j.e.pt] ?? -1) < minTier) continue;
    if (P.noghost && j.age > 180) continue;
    if (P.aionly && !j.co) continue;
    if (jstate.newOnly && !j.isNew) continue;
    if (q && !j.hay.includes(q)) continue;
    const el = eligibility(j);
    if (!el[0]) continue;
    j.why = el[2]; j.score = jobScore(j, el, tokens);
    out.push(j);
  }
  const k = jstate.sort;
  out.sort(k === "new" ? (a, b) => (b.po || b.fs).localeCompare(a.po || a.fs)
    : k === "pay" ? (a, b) => (b.payUSD || 0) - (a.payUSD || 0)
    : (a, b) => b.score - a.score);
  return out;
}

function wpLabel(j) {
  if (j.wp === "remote") return j.sc === "global" ? "remote · worldwide" : j.sc ? `remote · ${j.sc.split(",").slice(0, 3).join("/")}${j.sc.split(",").length > 3 ? "+" : ""}` : "remote";
  return j.wp || "";
}
function ageLabel(j) {
  if (j.age == null) return "";
  return j.age <= 0 ? "today" : j.age < 30 ? `${j.age}d` : j.age < 365 ? `${Math.round(j.age / 30)}mo` : `${(j.age / 365).toFixed(1)}y`;
}
function payLabel(j) {
  if (!j.pay) return "";
  const [lo, hi, cur] = j.pay, sym = { USD: "$", EUR: "€", GBP: "£", INR: "₹" }[cur];
  const f = (n) => cur === "INR" ? `${(n / 1e5).toFixed(0)}L` : n >= 1000 ? `${Math.round(n / 1000)}k` : n;
  return sym ? `${sym}${f(lo)}–${f(hi)}` : `${cur} ${f(lo)}–${f(hi)}`;
}

function jobHTML(j, i) {
  const flags = [];
  if (j.fl.includes("sponsor")) flags.push('<span class="flag good">sponsors / relocates</span>');
  if (j.fl.includes("no-sponsor")) flags.push('<span class="flag bad">no sponsorship</span>');
  if (j.fl.includes("clearance")) flags.push('<span class="flag bad">clearance / citizenship</span>');
  else if (j.fl.includes("auth-required")) flags.push('<span class="flag warn">work authorisation required</span>');
  if (j.rp) flags.push('<span class="flag warn">reposted</span>');
  if (j.age > 180) flags.push('<span class="flag warn">open 6+ months</span>');
  const tier = j.e.pt && j.e.pt !== "low" ? `<span class="tier t-${j.e.pt}" title="p75 of this employer's posted pay bands">${j.e.pt === "top" ? "$300k+ payer" : j.e.pt === "high" ? "$220k+ payer" : "$150k+ payer"}</span>` : "";
  return `<li class="job" style="animation-delay:${Math.min(i % JPAGE, 20) * 18}ms">
    <div class="job-main">
      <div class="job-title">${j.isNew ? '<span class="new">NEW</span>' : ""}<a href="${esc(j.u)}" target="_blank" rel="noopener">${esc(j.t)}</a></div>
      <div class="job-co"><b>${esc(j.e.n)}</b>${tier}${j.co && j.co.tag === "frontier" ? '<span class="tag frontier">◆ frontier</span>' : ""}
        <span class="dim">${esc(FAM[j.f] || j.f)} · ${LEVEL[j.lv]}</span></div>
      <div class="job-meta"><span>${esc(j.loc || "location not stated")}</span>${wpLabel(j) ? `<span class="wp">${esc(wpLabel(j))}</span>` : ""}
        ${j.pay ? `<span class="pay">${esc(payLabel(j))}</span>` : ""}<span class="age">${ageLabel(j)}</span>
        ${j.why && !/sponsor/.test(j.why) ? `<span class="why">${esc(j.why)}</span>` : ""}${flags.join("")}</div>
      ${j.sk.length ? `<div class="job-sk">${j.sk.slice(0, 8).map((s) => `<span>${esc(s)}</span>`).join("")}</div>` : ""}
    </div>
    <a class="btn apply" href="${esc(j.u)}" target="_blank" rel="noopener">APPLY ↗</a>
  </li>`;
}

function renderJobs() {
  if (!JOBS.length) return;
  const rows = filteredJobs();
  const nNew = rows.filter((j) => j.isNew).length;
  $("#profile-sum").textContent = profileSummary();
  $("#job-count").textContent = `${rows.length.toLocaleString()} JOBS FOR YOU · ${nNew.toLocaleString()} NEW`;
  $("#job-hint").textContent = PREV_VISIT ? `new = first seen after your last visit (${PREV_VISIT})` : "new = first seen in the last 7 days";
  $("#job-list").innerHTML = rows.slice(0, jstate.shown).map(jobHTML).join("")
    || '<li class="empty">NO MATCHES — loosen the profile (level, location or pay floor)</li>';
  $("#job-more").hidden = rows.length <= jstate.shown;
  $("#new-only").classList.toggle("on", jstate.newOnly);
  const fams = P.fam.length ? P.fam : FAMILIES.map((f) => f[0]).filter((f) => f !== "other-eng");
  $("#feeds").innerHTML = `Daily feeds (new postings, any reader): ${fams.map((f) => `<a href="feeds/${f}.xml">${esc(FAM[f])}</a>`).join(" · ")}`;
}

$("#job-search").addEventListener("input", (() => { let t; return (e) => { clearTimeout(t); t = setTimeout(() => { jstate.q = e.target.value; jstate.shown = JPAGE; renderJobs(); }, 150); }; })());
$("#job-sort").addEventListener("change", (e) => { jstate.sort = e.target.value; jstate.shown = JPAGE; renderJobs(); });
$("#new-only").addEventListener("click", () => { jstate.newOnly = !jstate.newOnly; jstate.shown = JPAGE; renderJobs(); });
$("#job-more").addEventListener("click", () => { jstate.shown += JPAGE; renderJobs(); });

/* ── companies ───────────────────────────────────────────────────────────── */
const state = { q: "", sort: "relevance", toggle: new Set(), bracket: new Set(), tag: new Set(), source: new Set(), shown: PAGE };

function filtered() {
  const q = state.q.toLowerCase();
  let rows = DATA.filter((c) => {
    if (state.toggle.has("hiring") && !(c.ai_roles > 0)) return false;
    if (state.toggle.has("india") && !c.india_office) return false;
    if (state.toggle.has("fresh") && !(c.last_funding_year >= 2025)) return false;
    if (state.toggle.has("paytop") && !(c.pay_tier === "high" || c.pay_tier === "top")) return false;
    if (state.bracket.size && !state.bracket.has(c.bracket)) return false;
    if (state.tag.size && !state.tag.has(c.tag)) return false;
    if (state.source.size && ![...state.source].some((s) => (c.source || "").includes(s))) return false;
    if (q && !`${c.name} ${c.desc} ${c.category} ${c.ai_role_titles} ${c.hq}`.toLowerCase().includes(q)) return false;
    return true;
  });
  const key = state.sort;
  rows.sort(
    key === "name" ? (a, b) => a.name.localeCompare(b.name)
    : key === "funding" ? (a, b) => (b.funding_musd || 0) - (a.funding_musd || 0)
    : key === "recency" ? (a, b) => (b.last_funding_year || 0) - (a.last_funding_year || 0) || (b.ai_roles || 0) - (a.ai_roles || 0)
    : key === "relevance" ? (a, b) => score(b) - score(a)
    : (a, b) => (b[key] || 0) - (a[key] || 0) || (b.funding_musd || 0) - (a.funding_musd || 0)
  );
  return rows;
}

// relevance for an AI job-seeker scouting employers: live AI hiring dominates, then
// posted pay, hiring momentum, funding freshness, India presence, size
function score(c) {
  return Math.sqrt(c.ai_roles || 0) * 4
    + (TIER[c.pay_tier] || 0) * 1.2
    + Math.max(Math.min((c.tech_delta_7d || 0) / 5, 2), -1)
    + (c.last_funding_year >= 2026 ? 3 : c.last_funding_year === 2025 ? 2 : c.last_funding_year === 2024 ? 1 : 0)
    + (c.india_office ? 1.5 : 0)
    + (c.is_hiring ? 1 : 0)
    + Math.min(Math.log10((c.funding_musd || 0) + 1), 3);
}

function srcLabel(s) {
  return (s || "").replace("forbes-ai50-2026", "FORBES50").replace("cbinsights-ai100-2026", "CBI100")
    .replace(/yc-(\w+) (\d{4})/, (_, sea, y) => `YC ${sea[0]}${y.slice(2)}`).replace("+yc", "+YC")
    .replace("finsmes", "NEWS").replace("techcrunch", "TC").replace("startuptalky", "IN-NEWS")
    .replace("topstartups", "PORTFOLIO").replace("unicorns", "UNICORN").replace("formd", "SEC")
    .replace(/\+board$/, "+HIRING").replace(/^board$/, "HIRING")
    .replace(/\+news$/, "");
}

function roundLabel(c) {
  const bits = [];
  if (c.funding_musd) bits.push(fmtM(c.funding_musd));
  if (c.last_round) bits.push(c.last_round);
  if (c.last_funding_date) bits.push(c.last_funding_date);
  return bits.join(" · ");
}

function rowHTML(c, i) {
  const pct = c.ai_roles ? Math.max(6, Math.sqrt(c.ai_roles / MAXAI) * 100) : 0;
  const delta = c.tech_delta_7d ? `<span class="delta ${c.tech_delta_7d > 0 ? "up" : "down"}">${c.tech_delta_7d > 0 ? "▲" : "▼"}${Math.abs(c.tech_delta_7d)}</span>` : "";
  return `<li class="row" style="animation-delay:${Math.min(i % PAGE, 20) * 22}ms">
    <div class="row-main" role="button" tabindex="0" aria-expanded="false">
      <span class="rank">${i + 1}</span>
      <span class="cname"><b>${esc(c.name)}</b>${c.india_office ? '<span class="badge-in">IN</span>' : ""}${c.pay_tier === "top" || c.pay_tier === "high" ? `<span class="tier t-${c.pay_tier}">${c.pay_tier === "top" ? "$300k+" : "$220k+"}</span>` : ""}</span>
      <span class="tag ${c.tag}">${c.tag === "frontier" ? "◆ frontier" : c.tag === "ai-adopter" ? "○ ai-adopter" : "· ai-native"}</span>
      <span class="bracket b-${c.bracket === "unknown" ? "unknown" : "known"}">${c.bracket === "unknown" ? "—" : c.bracket}</span>
      <span class="signal"><span class="signal-bar"><span class="signal-fill" style="width:${pct}%"></span></span>
        <span class="signal-n ${c.ai_roles ? "" : "zero"}">${c.ai_roles ? c.ai_roles + " AI" : c.ats ? "0 AI" : "no ATS"}${delta}</span></span>
      <span class="src">${esc(srcLabel(c.source))}</span>
    </div>
    <div class="row-detail">
      <div class="desc">${esc(c.desc) || "—"}</div>
      <div class="meta-line">${esc(c.hq || "HQ unknown")}
        · ${roundLabel(c) ? `last known round: ${esc(roundLabel(c))}` : "funding undisclosed"}
        · ${c.open_roles || 0} open roles${c.tech_open ? ` (${c.tech_open} technical)` : ""}${c.is_hiring ? " · YC: actively hiring" : ""}${c.india_entity ? " · 🇮🇳 MCA-registered India entity" : ""}${c.india_ai_roles > 0 ? ` · 🇮🇳 ${c.india_ai_roles} AI roles in India` : ""}</div>
      ${c.pay_top || c.ttc ? `<div class="meta-line">${c.pay_top ? `💵 posted pay reaches ${fmtK(c.pay_top)} (p75 top of band)` : ""}${c.pay_top && c.ttc ? " · " : ""}${c.ttc ? `⏱ postings close in ~${c.ttc} days (median)` : ""}</div>` : ""}
      ${c.salary_text ? `<div class="meta-line">💰 <a href="${esc(c.salary_url)}" target="_blank" rel="noopener">${esc(c.salary_text)} · ${esc((c.salary_date || "").slice(0, 7))} (${esc(c.salary_source)}) ↗</a></div>` : ""}
      ${c.investors ? `<div class="meta-line">backed by ${esc(c.investors)}</div>` : ""}
      ${c.ai_role_titles ? `<div class="roles">▸ ${esc(c.ai_role_titles)}</div>` : ""}
      <div class="actions">
        ${c.tech_open ? `<a class="btn" href="#jobs" data-company="${esc(c.name)}">SEE ${c.tech_open} TECH JOBS</a>` : ""}
        ${c.jobs_url ? `<a class="btn ghost" href="${esc(c.jobs_url)}" target="_blank" rel="noopener">JOB BOARD ↗</a>` : ""}
        ${c.website ? `<a class="btn ghost" href="${esc(c.website)}" target="_blank" rel="noopener">WEBSITE ↗</a>` : ""}
        ${c.news_url ? `<a class="btn ghost" href="${esc(c.news_url)}" target="_blank" rel="noopener">FUNDING NEWS ↗</a>` : ""}
      </div>
    </div>
  </li>`;
}

function render() {
  const rows = filtered();
  $("#result-count").textContent = `${rows.length} TARGETS`;
  $("#list").innerHTML = rows.slice(0, state.shown).map(rowHTML).join("")
    || '<li class="empty">NO SIGNALS — widen the filters</li>';
  $("#more").hidden = rows.length <= state.shown;
  $("#clear-filters").hidden = state.toggle.size + state.bracket.size + state.tag.size + state.source.size === 0;
}

$("#search").addEventListener("input", (e) => { state.q = e.target.value; state.shown = PAGE; render(); });
$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; state.shown = PAGE; render(); });
$("#more").addEventListener("click", () => { state.shown += PAGE; render(); });
$("#chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  if (chip.id === "clear-filters") {
    ["toggle", "bracket", "tag", "source"].forEach((g) => state[g].clear());
    document.querySelectorAll("#chips .chip.on").forEach((c) => c.classList.remove("on"));
    state.shown = PAGE; render(); return;
  }
  const group = chip.closest(".chip-group").dataset.group, key = chip.dataset.key;
  chip.classList.toggle("on");
  state[group].has(key) ? state[group].delete(key) : state[group].add(key);
  state.shown = PAGE; render();
});
$("#list").addEventListener("click", (e) => {
  const jump = e.target.closest("[data-company]");
  if (jump) { jstate.q = jump.dataset.company; $("#job-search").value = jstate.q; return; } // hashchange shows jobs
  if (e.target.closest("a")) return;
  const row = e.target.closest(".row");
  if (row) row.classList.toggle("open");
});
$("#list").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("row-main")) e.target.closest(".row").classList.toggle("open");
});

/* ── signals ─────────────────────────────────────────────────────────────── */
const SIG = { "new-country": "new country", "first-staff": "first Staff+ role", "build-out": "team build-out", "new-india-entity": "new India entity" };
let sigType = "";
function renderSignals() {
  const rows = EVENTS.filter((e) => !sigType || e.type === sigType).slice(0, 400);
  $("#sig-list").innerHTML = rows.map((e) => `<li class="sig">
      <span class="sig-date">${esc(e.date)}</span><span class="sig-type t-${esc(e.type)}">${esc(SIG[e.type] || e.type)}</span>
      <span class="sig-co"><b>${esc(e.co)}</b></span>
      <span class="sig-detail">${esc(e.type === "new-country" ? `first opening in ${COUNTRY[e.detail] || e.detail}` : e.detail)}</span>
      <a class="btn ghost" href="#jobs" data-company="${esc(e.co)}">JOBS</a></li>`).join("")
    || '<li class="empty">NO SIGNALS YET — they need a few days of daily history</li>';
}
$("#sig-chips").addEventListener("click", (e) => {
  const c = e.target.closest("[data-type]"); if (!c) return;
  document.querySelectorAll("#sig-chips .chip").forEach((x) => x.classList.toggle("on", x === c));
  sigType = c.dataset.type; renderSignals();
});
$("#sig-list").addEventListener("click", (e) => {
  const jump = e.target.closest("[data-company]");
  if (jump) { jstate.q = jump.dataset.company; $("#job-search").value = jstate.q; }
});

/* ── skills ──────────────────────────────────────────────────────────────── */
function renderSkills() {
  if (!SKILLS) return;
  const mine = new Set(P.sk.toLowerCase().split(/[,;]+/).map((x) => x.trim()).filter(Boolean));
  const rows = Object.entries(SKILLS.skills).sort((a, b) => (b[1].toppay || 0) - (a[1].toppay || 0));
  const max = Math.max(...rows.map(([, v]) => Math.max(v.all || 0, v.staff || 0, v.toppay || 0)), 1);
  const cell = (v) => `<td class="n"><span class="sbar"><i style="width:${((v || 0) / max) * 100}%"></i></span>${(v || 0).toFixed(1)}%</td>`;
  $("#skills-table").innerHTML = `<thead><tr><th>skill</th><th>all postings (${SKILLS.n.all.toLocaleString()})</th>
    <th>Staff+ (${SKILLS.n.staff.toLocaleString()})</th><th>$220k+ payers (${SKILLS.n.toppay.toLocaleString()})</th></tr></thead><tbody>`
    + rows.map(([k, v]) => `<tr class="${mine.has(k) ? "mine" : ""}"><td>${esc(k)}</td>${cell(v.all)}${cell(v.staff)}${cell(v.toppay)}</tr>`).join("") + "</tbody>";
}

/* ── views & boot ────────────────────────────────────────────────────────── */
function show() {
  const v = (location.hash || "#jobs").slice(1);
  const view = ["jobs", "companies", "signals", "skills"].includes(v) ? v : "jobs";
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.id !== `view-${view}`));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === view));
  ({ jobs: renderJobs, companies: render, signals: renderSignals, skills: renderSkills })[view]();
}
window.addEventListener("hashchange", show);

function countUp(key, target, fmt = (n) => Math.round(n).toLocaleString()) {
  const el = document.querySelector(`[data-stat="${key}"]`);
  if (!el || target == null) return;
  const t0 = performance.now(), dur = 1100;
  (function tick(t) {
    const p = Math.min((t - t0) / dur, 1);
    el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  })(t0);
}

async function boot() {
  const get = (f) => fetch(`data/${f}`, { cache: "no-cache" }).then((r) => r.ok ? r.json() : null).catch(() => null);
  const [companies, meta, jidx, events, skills] = await Promise.all(
    ["companies.json", "meta.json", "jobs/index.json", "events.json", "skills.json"].map(get));
  DATA = companies || []; META = meta || {};
  MAXAI = Math.max(...DATA.map((c) => c.ai_roles || 0), 1);
  if (jidx) { JIDX = jidx; EMP = jidx.employers; GEN_AGE = daysAgo(jidx.generated) || 0; }
  EVENTS = events || []; SKILLS = skills;
  $("#snapshot-stamp").textContent = `JOBS ${META.jobs_generated || "—"} · COMPANIES ${META.snapshot || "—"}`;
  countUp("jobs", META.jobs); countUp("jobs_new_7d", META.jobs_new_7d);
  countUp("total", META.total); countUp("hiring_ai", META.hiring_ai);
  bindProfile();
  await ensureShards();
  $("#profile").open = !store.get("ur-profile-set", false);
  $("#profile").addEventListener("toggle", () => store.set("ur-profile-set", true));
  show();
}

boot();
