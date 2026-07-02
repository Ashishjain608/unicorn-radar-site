/* UNICORN·RADAR — fetch JSON, filter, render. No deps. */
const PAGE = 120;
const state = { q: "", sort: "ai_roles", toggle: new Set(), bracket: new Set(), tag: new Set(), source: new Set(), shown: PAGE };
let DATA = [], MAXAI = 1;

const $ = (s) => document.querySelector(s);
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtM = (m) => m >= 1000 ? `$${(m / 1000).toFixed(m >= 10000 ? 0 : 1)}B` : `$${Math.round(m)}M`;

async function boot() {
  const [companies, meta] = await Promise.all([
    fetch("data/companies.json").then((r) => r.json()),
    fetch("data/meta.json").then((r) => r.json()),
  ]);
  DATA = companies;
  MAXAI = Math.max(...DATA.map((c) => c.ai_roles || 0), 1);
  $("#snapshot-stamp").textContent = `SNAPSHOT ${meta.snapshot}`;
  countUp("total", meta.total);
  countUp("hiring_ai", meta.hiring_ai);
  countUp("india", meta.india);
  countUp("funding", meta.total_funding_musd, fmtM);
  render();
}

function countUp(key, target, fmt = (n) => Math.round(n).toLocaleString()) {
  const el = document.querySelector(`[data-stat="${key}"]`);
  const t0 = performance.now(), dur = 1100;
  (function tick(t) {
    const p = Math.min((t - t0) / dur, 1);
    el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  })(t0);
}

function filtered() {
  const q = state.q.toLowerCase();
  let rows = DATA.filter((c) => {
    if (state.toggle.has("hiring") && !(c.ai_roles > 0)) return false;
    if (state.toggle.has("india") && !c.india_office) return false;
    if (state.toggle.has("fresh") && !(c.last_funding_year >= 2025)) return false;
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
    : (a, b) => (b[key] || 0) - (a[key] || 0) || (b.funding_musd || 0) - (a.funding_musd || 0)
  );
  return rows;
}

function srcLabel(s) {
  return s.replace("forbes-ai50-2026", "FORBES50").replace("cbinsights-ai100-2026", "CBI100")
    .replace(/yc-(\w+) (\d{4})/, (_, sea, y) => `YC ${sea[0]}${y.slice(2)}`).replace("+yc", "+YC");
}

function rowHTML(c, i) {
  const pct = c.ai_roles ? Math.max(6, Math.sqrt(c.ai_roles / MAXAI) * 100) : 0;
  const funding = c.funding_musd != null ? fmtM(c.funding_musd) : "—";
  return `<li class="row" style="animation-delay:${Math.min(i % PAGE, 20) * 22}ms" data-i="${DATA.indexOf(c)}">
    <div class="row-main" role="button" tabindex="0" aria-expanded="false">
      <span class="rank">${i + 1}</span>
      <span class="cname"><b>${esc(c.name)}</b>${c.india_office ? '<span class="badge-in">IN</span>' : ""}</span>
      <span class="tag ${c.tag}">${c.tag === "frontier" ? "◆ frontier" : "· ai-native"}</span>
      <span class="bracket b-${c.bracket === "unknown" ? "unknown" : "known"}">${c.bracket === "unknown" ? "—" : c.bracket}</span>
      <span class="signal"><span class="signal-bar"><span class="signal-fill" style="width:${pct}%"></span></span>
        <span class="signal-n ${c.ai_roles ? "" : "zero"}">${c.ai_roles ? c.ai_roles + " AI" : c.ats ? "0 AI" : "no ATS"}</span></span>
      <span class="src">${esc(srcLabel(c.source))}</span>
    </div>
    <div class="row-detail">
      <div class="desc">${esc(c.desc) || "—"}</div>
      <div class="meta-line">${esc(c.hq || "HQ unknown")} · raised ${funding}${c.last_funding_year ? ` · last round ${c.last_funding_year}` : ""} · ${c.open_roles || 0} open roles${c.is_hiring ? " · YC: actively hiring" : ""}</div>
      ${c.ai_role_titles ? `<div class="roles">▸ ${esc(c.ai_role_titles)}</div>` : ""}
      <div class="actions">
        ${c.jobs_url ? `<a class="btn" href="${esc(c.jobs_url)}" target="_blank" rel="noopener">VIEW ${c.open_roles} OPEN ROLES ↗</a>` : ""}
        ${c.website ? `<a class="btn ghost" href="${esc(c.website)}" target="_blank" rel="noopener">WEBSITE ↗</a>` : ""}
        <a class="btn ghost" href="https://www.google.com/search?q=${encodeURIComponent(c.name + " funding")}" target="_blank" rel="noopener">SEARCH FUNDING ↗</a>
      </div>
    </div>
  </li>`;
}

function render() {
  const rows = filtered();
  $("#result-count").textContent = `${rows.length} TARGETS`;
  const slice = rows.slice(0, state.shown);
  $("#list").innerHTML = slice.length
    ? slice.map(rowHTML).join("")
    : '<li class="empty">NO SIGNALS — widen the filters</li>';
  $("#more").hidden = rows.length <= state.shown;
  const active = state.toggle.size + state.bracket.size + state.tag.size + state.source.size > 0;
  $("#clear-filters").hidden = !active;
}

/* events */
$("#search").addEventListener("input", (e) => { state.q = e.target.value; state.shown = PAGE; render(); });
$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; state.shown = PAGE; render(); });
$("#more").addEventListener("click", () => { state.shown += PAGE; render(); });

$("#chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  if (chip.id === "clear-filters") {
    ["toggle", "bracket", "tag", "source"].forEach((g) => state[g].clear());
    document.querySelectorAll(".chip.on").forEach((c) => c.classList.remove("on"));
    state.shown = PAGE; render(); return;
  }
  const group = chip.closest(".chip-group").dataset.group;
  const key = chip.dataset.key;
  chip.classList.toggle("on");
  state[group].has(key) ? state[group].delete(key) : state[group].add(key);
  state.shown = PAGE; render();
});

$("#list").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const row = e.target.closest(".row");
  if (row) row.classList.toggle("open");
});
$("#list").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("row-main")) e.target.closest(".row").classList.toggle("open");
});

boot();
