import { parse } from "https://cdn.jsdelivr.net/npm/smol-toml@1.3.1/dist/index.js";

const rowsEl = document.getElementById("rows");
const statusEl = document.getElementById("status");
const searchEl = document.getElementById("search");
const filterBtn = document.getElementById("filterBtn");
const filterPanel = document.getElementById("filterPanel");
const filterCols = document.getElementById("filterCols");
const filterClear = document.getElementById("filterClear");
const filterSummary = document.getElementById("filterSummary");
// Active filters as prefixed values: "cat:<type>", "tag:<tag>", "studio:<dev>",
// or "notable". Faceted: OR within a group (e.g. two decades), AND across groups.
const selected = new Set();
const countEl = document.getElementById("count");
const sortBtn = document.getElementById("sortBtn");
const sortPanel = document.getElementById("sortPanel");
const sortList = document.getElementById("sortList");
const tableScroll = document.querySelector(".table-scroll");
const tableWrap = document.querySelector(".table-wrap");

let DATA = [];
let NOTABLE_AUTHORS = new Map(); // author name -> Wikipedia URL
let sortKey = "agg_score";
let sortDir = -1; // -1 desc, 1 asc

const SORT_LABELS = {
  agg_score: "Balanced",
  title: "Title",
  authors: "Author",
  category: "Type",
  date: "Date",
  hn_points: "Best HN points",
  hn_comments: "Best HN comments",
  hn_points_sum: "Total HN points",
  hn_comments_sum: "Total HN comments",
  hn_submissions: "HN submissions",
  reddit_score_sum: "Total Reddit score",
  reddit_comments_sum: "Total Reddit comments",
  reddit_submissions: "Reddit submissions",
  copies_sold: "Copies sold",
  wayback_captures: "Wayback captures",
  author_notable: "Notable author",
};

const esc = (s) => (s ?? "").replace(/[&<>"]/g, c => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const BLANK_HN = {
  hn_points: 0,
  hn_comments: 0,
  hn_points_sum: 0,
  hn_comments_sum: 0,
  hn_submissions: 0,
  hn_threads: [],
};

async function loadSidecar(path, table, defaults = {}) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  const rows = parse(await res.text())[table] || [];
  const byId = new Map(rows.map(row => [row.id, row]));
  DATA = DATA.map(d => ({ ...defaults, ...d, ...(byId.get(d.id) || {}) }));
}

async function loadHnMetrics() {
  await loadSidecar("data/hn_postmortem_threads.toml", "hn_postmortem", BLANK_HN);
}

const BLANK_REDDIT = {
  reddit_score: 0,
  reddit_comments: 0,
  reddit_score_sum: 0,
  reddit_comments_sum: 0,
  reddit_submissions: 0,
  reddit_threads: [],
};

const BLANK_SALES = {
  copies_sold: 0,
  sales_note: "",
  wiki_title: "",
  wiki_url: "",
};

async function loadOptionalSidecar(path, table, defaults = {}) {
  try {
    await loadSidecar(path, table, defaults);
  } catch (e) {
    // Optional, best-effort signals; absence should not brick the catalogue.
    DATA = DATA.map(d => ({ ...defaults, ...d }));

  }
}

async function loadNotableAuthors() {
  // Optional: maps a notable author's name to their Wikipedia page so the
  // byline can link straight to it. Absence just leaves the ★ unlinked.
  try {
    const res = await fetch("data/notable_authors.toml", { cache: "no-cache" });
    if (!res.ok) return;
    const rows = parse(await res.text()).notable_author || [];
    // name -> { url, half }: a redirect-only match is worth half a notability
    // point (see computeAggregate), and still links to its canonical page.
    NOTABLE_AUTHORS = new Map(
      rows.filter(r => r.name && r.wiki_url)
          .map(r => [r.name, { url: r.wiki_url, half: !!r.via_redirect }]));
  } catch (e) {
    // best-effort; the catalogue works fine without author links.
  }
}

async function loadPhaseTwoMetrics() {
  await loadOptionalSidecar("data/reddit_postmortem_threads.toml", "reddit_postmortem", BLANK_REDDIT);
  await loadOptionalSidecar("data/wikipedia_game_sales.toml", "wiki_game_sales", BLANK_SALES);
}

async function loadTags() {
  // Browse tags (era/platform/studio/business) from data/tags.toml; absence
  // just leaves entries untagged and the tag filter empty.
  await loadOptionalSidecar("data/tags.toml", "tag", { tags: [] });
}

async function loadEditorialTags() {
  // Editorial tags + extracted facts (studio/engine/team_size) from the LLM
  // pass, in their own sidecar; union tags onto the deterministic/wiki tags.
  // Optional — absent until `just tags-llm` has run.
  try {
    const res = await fetch("data/tags_llm.toml", { cache: "no-cache" });
    if (!res.ok) return;
    const byId = new Map((parse(await res.text()).editorial_tag || []).map(r => [r.id, r]));
    DATA = DATA.map(d => {
      const r = byId.get(d.id);
      if (!r) return d;
      return { ...d, tags: [...new Set([...(d.tags || []), ...(r.tags || [])])],
               studio: r.studio, engine: r.engine, team_size: r.team_size };
    });
  } catch (e) { /* best-effort */ }
}

let STUDIOS = []; // notable studios (>= threshold), classified once: {name, class, count, ids}
async function loadStudios() {
  // Notable recurring developers (pass 2). Attaches a canonical studio_name to
  // each member entry and folds the studio class into its tags. Optional.
  try {
    const res = await fetch("data/tags_studios.toml", { cache: "no-cache" });
    if (!res.ok) return;
    STUDIOS = parse(await res.text()).studio || [];
    const byId = new Map();
    for (const s of STUDIOS) for (const id of s.ids || []) byId.set(id, s);
    DATA = DATA.map(d => {
      const s = byId.get(d.id);
      if (!s) return d;
      const cls = s.class && s.class !== "unknown" ? [s.class] : [];
      return { ...d, studio_name: s.name, tags: [...new Set([...(d.tags || []), ...cls])] };
    });
  } catch (e) { /* best-effort */ }
}

async function loadMirrorSidecars() {
  await loadSidecar("data/archive_is_mirrors.toml", "archive_mirror");
  await loadSidecar("data/gamedeveloper_live_urls.toml", "gamedeveloper_live");
  // Wayback liveness: the canonical capture and the print variant verified
  // separately. Overlays the catalogue so a print link only shows when a real
  // ?print=1 capture exists; default to no print link when the sidecar is absent.
  await loadOptionalSidecar("data/wayback_links.toml", "wayback_link",
    { wayback_print: "", wayback_print_ok: false });
}

// ---- Balanced aggregate sort ------------------------------------------------
// Each metric surfaces a different, often small slice of the catalogue (HN
// points cover ~16% of entries, copies-sold ~37%, captures ~98%). Sorting by
// any single one leaves most articles tied at zero. The balanced score blends
// the axes so an entry rises when it stands out on *any* of them, and rises
// further when it stands out on several — giving the whole catalogue a
// meaningful default order instead of an alphabetical blob.
//
// Axes are on wildly different scales (captures 0–15 vs copies-sold up to tens
// of millions), so we can't sum raw values. Instead each entry's value is
// mapped to its percentile *among the entries that have any signal on that
// axis*; zeros stay at zero, so an entry only earns credit where it actually
// shows up. The per-axis scores are then combined as a weighted average.
// A live archive.is mirror is a deliberate preservation the Wayback capture
// count misses, so it nudges archival reach — worth a few captures, not a flood.
const AIS_REACH_BONUS = 3;
const AGG_AXES = [
  { key: "hn_points_sum", weight: 1.0, label: "HN points" },
  { key: "copies_sold", weight: 0.9, label: "copies sold" },
  { key: "archive_reach", weight: 0.7, label: "archival reach" },
  { key: "hn_comments_sum", weight: 0.5, label: "HN discussion" },
  { key: "reddit_score_sum", weight: 0.5, label: "Reddit score" },
  { key: "author_notable", weight: 0.5, label: "notable author", unit: true },
  { key: "hn_points", weight: 0.35, label: "top HN thread" },
  { key: "reddit_comments_sum", weight: 0.3, label: "Reddit discussion" },
  { key: "hn_comments", weight: 0.2, label: "top HN thread" },
  { key: "hn_submissions", weight: 0.15, label: "HN reach" },
  { key: "reddit_submissions", weight: 0.15, label: "Reddit reach" },
];

// Map each distinct value to its plotting-position percentile in (0,1), with
// tied values sharing the average rank. Input is the nonzero values only.
function percentileMap(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const n = sorted.length;
  const map = new Map();
  for (let i = 0; i < n;) {
    let j = i;
    while (j < n && sorted[j] === sorted[i]) j++;
    const midRank = (i + j - 1) / 2;        // average 0-based rank of the tie
    map.set(sorted[i], (midRank + 0.5) / n); // Hazen position, always in (0,1)
    i = j;
  }
  return map;
}

function computeAggregate() {
  const maps = {};
  const rarity = {};
  let totalWeight = 0;
  for (const ax of AGG_AXES) {
    totalWeight += ax.weight;
    const have = DATA.filter(d => (ax.binary ? !!d[ax.key] : (Number(d[ax.key]) || 0) > 0));
    // How distinctive a signal is: an axis almost everyone has (captures) tells
    // you little about why a given entry ranked; a rare one (HN points) tells
    // you a lot. Floor keeps a near-universal axis usable as a last resort.
    rarity[ax.key] = Math.max(1 - have.length / (DATA.length || 1), 0.02);
    // binary axes contribute a flat 0/1; unit axes carry a ready 0..1 score
    // (e.g. author_notable: 1 for an own-article author, 0.5 via redirect) and
    // are used as-is — neither gets percentile-mapped.
    if (ax.binary || ax.unit) continue;
    maps[ax.key] = percentileMap(have.map(d => Number(d[ax.key]) || 0));
  }
  for (const d of DATA) {
    let score = 0;
    const contrib = [];
    for (const ax of AGG_AXES) {
      const v = Number(d[ax.key]) || 0;
      const norm = ax.binary ? (v ? 1 : 0)
        : ax.unit ? Math.min(Math.max(v, 0), 1)
        : (v > 0 ? (maps[ax.key].get(v) || 0) : 0);
      const part = norm * ax.weight;
      score += part;
      if (part > 0) contrib.push({ label: ax.label, part, show: part * rarity[ax.key] });
    }
    d.agg_score = totalWeight ? score / totalWeight : 0;
    // Explain why the entry ranked under the balanced sort: lead with its most
    // distinctive signal, and only keep a second when it's genuinely meaningful
    // (not a near-universal axis riding along behind the real one).
    contrib.sort((a, b) => b.show - a.show);
    const lead = contrib[0];
    d.agg_top = lead
      ? contrib.filter(c => c === lead || c.show >= lead.show * 0.25).slice(0, 2).map(c => c.label)
      : [];
  }
  // Assign each entry a stable balanced rank (1..N) that sticks to it no matter
  // how the table is later sorted or filtered. Same tiebreak as render() so the
  // numbers read 1, 2, 3… straight down under the default balanced sort.
  const ranked = [...DATA].sort((a, b) =>
    (b.agg_score - a.agg_score) || a.title.localeCompare(b.title));
  ranked.forEach((d, i) => { d.balanced_rank = i + 1; });
}

async function load() {
  try {
    const res = await fetch("data/postmortems.toml", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    DATA = (parse(await res.text()).postmortem) || [];
    await loadHnMetrics();
    await loadPhaseTwoMetrics();
    await loadMirrorSidecars();
    await loadTags();
    await loadEditorialTags();
    await loadStudios();
    await loadNotableAuthors();
  } catch (e) {
    statusEl.textContent = "Could not load data/postmortems.toml — run the scraper first. (" + e + ")";
    return;
  }
  // Re-derive notability as a 0..1 score from the sidecar: an author with their
  // own Wikipedia article scores 1, one that only resolves through a redirect
  // (a name variant, or a person who redirects to their studio/game) scores 0.5.
  // Computed on the ungrouped entries so a series then takes the max of its
  // parts. Falls back to the baked flag if the sidecar didn't load.
  if (NOTABLE_AUTHORS.size) {
    for (const d of DATA) {
      let best = 0;
      for (const name of d.authors || []) {
        const e = NOTABLE_AUTHORS.get(name);
        if (e) best = Math.max(best, e.half ? 0.5 : 1);
      }
      d.author_notable = best;
    }
  }
  // Collapse multi-part series into one card before ranking, so a series
  // competes in the balanced sort as a single unit.
  DATA = groupSeries(DATA);
  // Archival reach drives the captures axis: canonical Wayback captures (now
  // populated for curated entries too, via the wayback_links sidecar — without
  // it they'd sit at zero) nudged when a live archive.is mirror also preserves
  // the page. The raw capture count is still shown in its own column.
  for (const d of DATA) {
    d.archive_reach = (Number(d.wayback_captures) || 0) + (d.archive_today_ok ? AIS_REACH_BONUS : 0);
  }
  computeAggregate();
  buildFilterOptions();
  buildSortOptions();
  statusEl.textContent = "";
  render();
}

// One unified filter: the big "Type" categories plus the tag axes
// (era / platform / studio / business), grouped into a custom 2-column multiselect
// popover. Values are prefixed so the filter knows whether a pick matches the
// category field ("cat:…"), a tag ("tag:…"), or a developer ("studio:…").
const AXIS_GROUPS = [["era", "Era"], ["platform", "Platform"], ["studio", "Studio"],
  ["business", "Business"], ["theme", "Theme"]];
function buildFilterOptions() {
  const cats = [...new Set(DATA.map(d => d.category).filter(Boolean))].sort();
  const byAxis = {};
  for (const d of DATA) for (const t of d.tags || []) (byAxis[tagAxis(t)] ||= new Set()).add(t);

  // Authors first so it lands top-left; Type pushed down; Developer (longest) last.
  const groups = [["Authors", [["notable", "Notable authors"]]]];
  for (const [axis, label] of AXIS_GROUPS)
    if (byAxis[axis]) groups.push([label, [...byAxis[axis]].sort().map(t => ["tag:" + t, tagLabel(t)])]);
  groups.push(["Type", cats.map(c => ["cat:" + c, c])]);
  if (STUDIOS.length) groups.push(["Developer", STUDIOS.map(s => ["studio:" + s.name, s.name])]);

  filterCols.innerHTML = groups.map(([label, opts]) => opts.length ? `
    <div class="filter-group"><h4>${esc(label)}</h4>${opts.map(([val, text]) =>
      `<label class="filter-opt"><input type="checkbox" value="${esc(val)}"`
      + `${selected.has(val) ? " checked" : ""}><span class="filter-opt-name">${esc(text)}</span>`
      + `<span class="filter-count"></span></label>`).join("")}</div>`
    : "").join("");
  syncFilterUI();
}

// Reflect the selected set into the button (label + count + active state), the
// header summary, the Clear button, and the panel checkboxes; then re-narrow.
function syncFilterUI() {
  const n = selected.size;
  filterBtn.querySelector(".filter-btn-label").textContent = n ? `Filter (${n})` : "Filter";
  filterBtn.classList.toggle("on", n > 0);
  filterClear.disabled = n === 0;
  filterSummary.textContent = n ? `${n} active` : "none selected";
  filterCols.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = selected.has(cb.value); });
  refreshFilterCounts();
}

// Dynamic narrowing: for each option, count entries that pass the search plus
// every active facet *except its own* (within-facet is OR, so an option is judged
// against the other facets) and also match the option. Zero-count, unselected
// options are disabled and dimmed; the live count is shown alongside.
function refreshFilterCounts() {
  const q = searchEl.value.trim().toLowerCase();
  const facets = {};
  for (const val of selected) (facets[filterFacet(val)] ||= []).push(val);
  const facetEntries = Object.entries(facets);
  const passSearch = d => !q ||
    (d.title + " " + d.game + " " + (d.authors || []).join(" ") + " " + (d._search || "")).toLowerCase().includes(q);

  filterCols.querySelectorAll(".filter-opt").forEach(opt => {
    const cb = opt.querySelector("input");
    const val = cb.value, own = filterFacet(val);
    let n = 0;
    for (const d of DATA) {
      if (!passSearch(d)) continue;
      let ok = true;
      for (const [f, vals] of facetEntries) {
        if (f === own) continue;
        if (!vals.some(v => matchesFilter(d, v))) { ok = false; break; }
      }
      if (ok && matchesFilter(d, val)) n++;
    }
    const sel = selected.has(val);
    cb.disabled = n === 0 && !sel;
    opt.classList.toggle("zero", n === 0 && !sel);
    opt.querySelector(".filter-count").textContent = n;
  });
  // hide a group once all its options have dropped out
  filterCols.querySelectorAll(".filter-group").forEach(g => {
    const anyLeft = [...g.querySelectorAll(".filter-opt")].some(o => !o.classList.contains("zero"));
    g.classList.toggle("zero", !anyLeft);
  });
}

// Toggle one filter value, then refresh the UI and the table.
function setFilter(val, on) {
  if (on) selected.add(val); else selected.delete(val);
  syncFilterUI();
  render();
}

// ---- Sort: custom single-select dropdown in the filter style ----
const SORT_OPTIONS = [
  ["agg_score:-1", "Balanced (default)"], ["hn_points_sum:-1", "Most total HN points"],
  ["hn_points:-1", "Best HN thread"], ["date:-1", "Newest first"], ["date:1", "Oldest first"],
  ["title:1", "Title A–Z"], ["authors:1", "Author A–Z"], ["author_notable:-1", "Notable authors first"],
  ["category:1", "Type"], ["hn_comments_sum:-1", "Most total HN comments"],
  ["hn_comments:-1", "Best HN comments"], ["hn_submissions:-1", "Most HN submissions"],
  ["reddit_score_sum:-1", "Most Reddit score"], ["reddit_comments_sum:-1", "Most Reddit comments"],
  ["copies_sold:-1", "Most copies sold"], ["wayback_captures:-1", "Most captures"],
];
function buildSortOptions() {
  sortList.innerHTML = SORT_OPTIONS.map(([val, label]) =>
    `<button type="button" class="sort-opt" role="option" data-val="${esc(val)}">${esc(label)}</button>`).join("");
}
function syncSortUI() {
  const val = `${sortKey}:${sortDir}`;
  const found = SORT_OPTIONS.find(([v]) => v === val);
  sortBtn.querySelector(".sort-btn-label").textContent = found ? found[1] : "Sorted";
  sortList.querySelectorAll(".sort-opt").forEach(o => {
    const on = o.dataset.val === val;
    o.classList.toggle("on", on);
    o.setAttribute("aria-selected", on);
  });
}

function sortVal(d, k) {
  if (k === "authors") return (d.authors?.[0] || "").toLowerCase();
  if (k === "author_notable") return Number(d.author_notable) || 0;
  const v = d[k];
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  return (v ?? "").toString().toLowerCase();
}

// Show an edge fade on whichever side the table can still scroll toward.
function updateScrollFades() {
  if (!tableWrap || !tableScroll) return;
  const max = tableWrap.scrollWidth - tableWrap.clientWidth;
  const x = tableWrap.scrollLeft;
  tableScroll.classList.toggle("can-left", x > 1);
  tableScroll.classList.toggle("can-right", x < max - 1);
  // confine the header's edge fades to the header band (see .head-fade in css)
  const head = tableWrap.querySelector("thead");
  tableScroll.style.setProperty("--head-h", (head ? head.offsetHeight : 0) + "px");
}

// Which facet an active value belongs to, so we OR within a facet and AND across
// facets (pick 90s + 00s → either decade; add Indie → also indie).
function filterFacet(val) {
  if (val === "notable") return "notable";
  const ci = val.indexOf(":");
  const kind = val.slice(0, ci);
  return kind === "tag" ? "tag:" + tagAxis(val.slice(ci + 1)) : kind;
}
function matchesFilter(d, val) {
  if (val === "notable") return !!d.author_notable;
  const ci = val.indexOf(":");
  const kind = val.slice(0, ci), v = val.slice(ci + 1);
  if (kind === "cat") return d.category === v;
  if (kind === "tag") return (d.tags || []).includes(v);
  if (kind === "studio") return d.studio_name === v;
  return true;
}

function render() {
  const q = searchEl.value.trim().toLowerCase();
  const facets = {};
  for (const val of selected) (facets[filterFacet(val)] ||= []).push(val);
  const facetVals = Object.values(facets);

  let list = DATA.filter(d => {
    for (const vals of facetVals) if (!vals.some(v => matchesFilter(d, v))) return false;
    if (q) {
      const hay = (d.title + " " + d.game + " " + (d.authors || []).join(" ") + " " + (d._search || "")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  list.sort((a, b) => {
    const x = sortVal(a, sortKey), y = sortVal(b, sortKey);
    if (x < y) return -1 * sortDir;
    if (x > y) return 1 * sortDir;
    return a.title.localeCompare(b.title);
  });

  countEl.textContent = `${list.length} of ${DATA.length} postmortems`;
  rowsEl.innerHTML = list.length
    ? list.map(rowHTML).join("")
    : `<tr><td colspan="8" class="empty">No postmortems match your filters.</td></tr>`;
  document.querySelectorAll("th.sortable").forEach(th => {
    th.classList.remove("sorted-asc", "sorted-desc");
    th.removeAttribute("aria-sort");
    if (th.dataset.key === sortKey) {
      th.classList.add(sortDir === 1 ? "sorted-asc" : "sorted-desc");
      th.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
    }
  });
  // keep the custom sort control's label/active option in sync
  syncSortUI();
  updateScrollFades();
  stickyHead.refresh();
}

if (tableWrap) tableWrap.addEventListener("scroll", updateScrollFades, { passive: true });
window.addEventListener("resize", updateScrollFades);

// ---- Frozen header ---------------------------------------------------------
// The horizontal-scroll wrapper stops a CSS-sticky <thead> from anchoring to
// the page, so pin a cloned header in a position:fixed strip that mirrors the
// table's horizontal scroll and column widths. Table-layout:fixed + the shared
// colgroup keep the columns aligned. Degrades to nothing if anything's missing.
const stickyHead = (() => {
  const table = document.getElementById("grid");
  if (!table || !tableWrap) return { refresh() {} };
  const overlay = document.createElement("div");
  overlay.className = "sticky-head";
  overlay.hidden = true;
  overlay.setAttribute("aria-hidden", "true");
  const clone = document.createElement("table");
  overlay.appendChild(clone);
  document.body.appendChild(overlay);

  // a click on the pinned header re-uses the real header's sort handler
  overlay.addEventListener("click", e => {
    const th = e.target.closest("th[data-key]");
    const real = th && table.querySelector(`thead th[data-key="${th.dataset.key}"]`);
    if (real) real.click();
  });

  function rebuild() {
    const cg = table.querySelector("colgroup");
    const head = table.tHead;
    clone.replaceChildren(...[cg, head].filter(Boolean).map(n => n.cloneNode(true)));
  }
  function sync() {
    const head = table.tHead;
    const h = head ? head.offsetHeight : 0;
    const r = table.getBoundingClientRect();
    // pin only once the real header has scrolled above the top while the body is
    // still on screen; never in the mobile card layout (the table goes block)
    if (h === 0 || r.top >= 0 || r.bottom <= h || getComputedStyle(table).display === "block") {
      overlay.hidden = true;
      return;
    }
    const wrap = tableWrap.getBoundingClientRect();
    overlay.hidden = false;
    overlay.style.left = wrap.left + "px";
    overlay.style.width = tableWrap.clientWidth + "px";
    clone.style.width = table.offsetWidth + "px";
    clone.style.transform = `translateX(${-tableWrap.scrollLeft}px)`;
    // mirror the edge fades so the pinned header reads as scrollable too
    const max = tableWrap.scrollWidth - tableWrap.clientWidth;
    overlay.classList.toggle("can-left", tableWrap.scrollLeft > 1);
    overlay.classList.toggle("can-right", tableWrap.scrollLeft < max - 1);
  }
  function refresh() { rebuild(); sync(); }
  window.addEventListener("scroll", sync, { passive: true });
  window.addEventListener("resize", refresh, { passive: true });
  tableWrap.addEventListener("scroll", sync, { passive: true });
  rebuild();
  return { refresh, sync, rebuild };
})();

function catClass(c) {
  c = (c || "").toLowerCase();
  if (c.includes("blog")) return "blog";
  if (c.includes("video")) return "video";
  if (c.includes("indie")) return "indie";
  if (c.includes("audio")) return "audio";
  if (c.includes("middleware")) return "middleware";
  return "";
}

// Only badge a distinct category; a plain "Postmortem" is the default and
// labelling every entry with it just adds noise.
function catBadge(c) {
  if (!c || c.trim().toLowerCase() === "postmortem") return "";
  return `<span class="cat ${catClass(c)}">${esc(c)}</span>`;
}

// Tag axis a tag belongs to, for colour-coding the chips. Eras are NNs.
const BUSINESS_TAGS = ["kickstarter", "early-access", "self-published", "work-for-hire"];
function tagAxis(t) {
  if (/^\d0s$/.test(t)) return "era";
  if (["pc", "console", "handheld", "mobile", "arcade", "flash", "web", "vr"].includes(t)) return "platform";
  if (["indie", "student", "aaa", "solo", "hobbyist"].includes(t)) return "studio";
  if (BUSINESS_TAGS.includes(t)) return "business";
  return "theme"; // outcome + production themes (crunch, port, breakout, …)
}

// Display label for a tag chip / filter option: title-case the multi-word slugs
// (hyphens become spaces), but keep decade eras lowercase (90s) and known
// acronyms uppercase (PC / VR / AAA). Filter values still use the raw slug.
const TAG_ACRONYMS = { pc: "PC", vr: "VR", aaa: "AAA" };
function tagLabel(t) {
  if (/^\d0s$/.test(t)) return t;                 // eras: keep the lowercase s
  if (t in TAG_ACRONYMS) return TAG_ACRONYMS[t];
  return t.split("-").map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(" ");
}

// Browse tags (era/platform/studio/business) from data/tags.toml, rendered as
// small colour-coded chips under the headline. Clicking one drives the unified
// filter dropdown (and re-clicking the active one clears it) — same control as
// the big Type categories. Same vintage idiom as the type badge.
// Order chips by axis (era, then platform, studio, business), alpha within an
// axis — reads more sensibly than the raw alphabetical sort from the sidecar.
const TAG_AXIS_RANK = { era: 0, platform: 1, studio: 2, business: 3, theme: 4 };
function tagsHTML(d) {
  const tags = [...(d.tags || [])].sort((a, b) =>
    (TAG_AXIS_RANK[tagAxis(a)] - TAG_AXIS_RANK[tagAxis(b)]) || a.localeCompare(b));
  const chips = tags.map(t => {
    const v = "tag:" + t, on = selected.has(v);
    return `<button type="button" class="tag tag-${tagAxis(t)}${on ? " on" : ""}"`
      + ` data-val="${esc(v)}" aria-pressed="${on}">${esc(tagLabel(t))}</button>`;
  });
  // a chip for the canonical developer when the entry belongs to a notable studio
  if (d.studio_name) {
    const v = "studio:" + d.studio_name, on = selected.has(v);
    chips.push(`<button type="button" class="tag tag-studio${on ? " on" : ""}"`
      + ` data-val="${esc(v)}" aria-pressed="${on}">${esc(d.studio_name)}</button>`);
  }
  return chips.length ? `<span class="tags">${chips.join("")}</span>` : "";
}

function metricValue(d, k) {
  const v = d[k];
  if (k === "date") return d.date ? (d.date_estimated ? `~${d.date}` : d.date) : "—";
  if (k === "authors") return (d.authors || []).join(", ") || "—";
  if (k === "author_notable") return d.author_notable >= 1 ? "yes" : d.author_notable > 0 ? "half" : "no";
  if (typeof v === "number") return v ? v.toLocaleString() : "0";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return (v ?? "—").toString();
}

// Sort keys whose value is already visible elsewhere on the card (date line,
// type badge, byline). Showing a sort chip for these just duplicates info.
const SHOWN_SORTS = new Set(["title", "date", "category", "authors", "author_notable"]);
// Compact labels for the metrics that aren't otherwise on the card.
const SORT_SIGNAL_LABELS = {
  hn_points: "HN pts (top thread)",
  hn_comments: "HN comments (top thread)",
  hn_points_sum: "HN points",
  hn_comments_sum: "HN comments",
  hn_submissions: "HN submissions",
  reddit_score_sum: "Total Reddit score",
  reddit_comments_sum: "Total Reddit comments",
  reddit_submissions: "Reddit submissions",
  copies_sold: "Copies sold",
  wayback_captures: "Wayback captures",
};

// A small chip naming why this entry sorted where it did — only when the
// active sort isn't already shown on the card and the value is meaningful.
function sortSignalHTML(d) {
  if (sortKey === "agg_score") {
    const top = d.agg_top || [];
    if (!top.length) return "";
    return ` <span class="m-sort" title="Balanced rank — strongest signals for this entry">strong on ${top.map(esc).join(" · ")}</span>`;
  }
  if (SHOWN_SORTS.has(sortKey)) return "";
  const v = d[sortKey];
  if (!v) return "";
  const label = SORT_SIGNAL_LABELS[sortKey] || SORT_LABELS[sortKey] || sortKey;
  const val = typeof v === "number" ? v.toLocaleString() : esc(String(v));
  return ` <span class="m-sort" title="Current sort metric">${val} ${esc(label)}</span>`;
}

// Desktop counterpart to the meta-top sort chip: the meta line is mobile-only,
// so when balanced is the active sort we show the "why it ranked" hint at the
// right of the headline cell (across from the byline). CSS hides this copy on
// mobile to avoid doubling up with the meta-top chip.
function whyBalancedHTML(d) {
  if (sortKey !== "agg_score") return "";
  const top = d.agg_top || [];
  if (!top.length) return "";
  return `<span class="why-balanced" title="Balanced rank — strongest signals for this entry"><span class="wb-label">strong on</span> ${top.map(esc).join(" · ")}</span>`;
}

function hnThreadsHTML(d) {
  const threads = d.hn_threads || [];
  if (!threads.length) return "";
  const shown = threads.slice(0, 3).map((t, i) => {
    const label = threads.length === 1 ? "discussion" : `#${i + 1}`;
    const detail = `${t.points || 0} pts/${t.comments || 0} c`;
    return `<a href="${esc(t.url)}" target="_blank" rel="noopener" title="${esc(t.title || 'Hacker News discussion')}">${label} (${detail})</a>`;
  });
  if (threads.length > shown.length) shown.push(`<span title="${threads.length} matching HN submissions total">+${threads.length - shown.length} more</span>`);
  return `<span class="discussions"><span class="line-label">HN:</span> ${shown.join(" · ")}</span>`;
}

function redditThreadsHTML(d) {
  const threads = d.reddit_threads || [];
  if (!threads.length) return "";
  const shown = threads.slice(0, 3).map((t, i) => {
    const label = threads.length === 1 ? (t.subreddit ? `r/${t.subreddit}` : "thread") : `#${i + 1}`;
    const detail = `${t.score || 0} pts/${t.comments || 0} c`;
    return `<a href="${esc(t.permalink)}" target="_blank" rel="noopener" title="${esc(t.title || 'Reddit discussion')}">${label} (${detail})</a>`;
  });
  if (threads.length > shown.length) shown.push(`<span title="${threads.length} matching Reddit submissions total">+${threads.length - shown.length} more</span>`);
  return `<span class="discussions reddit"><span class="line-label">Reddit:</span> ${shown.join(" · ")}</span>`;
}


function usableLink(ok, url) {
  return ok !== false && !!url;
}

function sameUrl(a, b) {
  return a && b && a === b;
}

function linkHTML(url, label, title, primary) {
  const body = esc(label);
  const attrs = `href="${esc(url)}" target="_blank" rel="noopener" title="${esc(title)}"`;
  return primary
    ? `<a class="primary-link" ${attrs}><strong>${body}</strong></a>`
    : `<a ${attrs}>${body}</a>`;
}

function cleanTitle(title) {
  return (title || "")
    .replace(/^\s*(?:(?:classic|indie|audio|faculty|game|game design|middleware|mobile|student|tool)\s+)*post\s*-?\s*mortem\s*(?::|[-–—])\s*/i, "")
    .replace(/^\s*(?:postmortem|post-mortem)\s+/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

// True for developer-blog entries (curated /blogs/ postmortems), which read
// differently from the magazine features and earn a "contributor blog" tag.
function isContribBlog(d) {
  return /\/blogs\//.test(d.original_url || "");
}

// The game name as a subhead — only worth showing when the title doesn't already
// name the game. Suppress whenever the game appears *within* the title as a
// phrase ("Goat Simulator Post Mortem" needs no "Goat Simulator" subhead); show
// it only when the game lives in the body, not the title ("How I wasted $4k…"
// -> Drunk Shotgun). Comparison ignores case and punctuation, and is word-
// bounded so a short name can't match inside an unrelated word.
function displayGame(d) {
  const game = (d.game || "").trim();
  if (!game) return "";
  const title = cleanTitle(d.title);
  if (!title) return game;
  const norm = s => " " + cleanTitle(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim() + " ";
  const g = norm(game);
  return g.trim() && norm(title).includes(g) ? "" : game;
}

function authorHTML(name) {
  const e = NOTABLE_AUTHORS.get(name);
  if (e) {
    const note = e.half ? " (via Wikipedia redirect)" : "";
    return `<a class="author-link wiki-link" href="${esc(e.url)}" target="_blank" rel="noopener" title="Wikipedia: ${esc(name)}${note}">${esc(name)}</a>`;
  }
  return `<span class="author-name no-wiki">${esc(name)}</span>`;
}

function compactSold(v) {
  v = Number(v) || 0;
  if (!v) return "";
  if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (v >= 1e3) return Math.round(v / 1e3) + "k";
  return v.toLocaleString();
}

// One compact, right-aligned metric cell: a bold primary number with an
// optional muted second line (comments · submission count). Replaces the old
// spread of five HN + three Reddit columns, which read as noise.
function metricCell(primary, secondary, title) {
  const titleAttr = title ? ` title="${esc(title)}"` : "";
  if (!primary && !secondary) return `<td class="num metric zero"${titleAttr}>–</td>`;
  const sub = secondary ? `<span class="m-sub">${secondary}</span>` : "";
  return `<td class="num metric"${titleAttr}><span class="m-main">${primary || "–"}</span>${sub}</td>`;
}

function discussionSub(comments, subs) {
  const parts = [];
  if (comments) parts.push(`${Number(comments).toLocaleString()} c`);
  if (subs) parts.push(`×${subs}`);
  return parts.join(" · ");
}

// Resolve the best primary target plus the muted "links:" line for an entry,
// in reading order (live / wayback / wayback-full / archive.is / original).
// Shared by the card headline and by each part of a consolidated series.
function resolveLinks(d) {
  const wbFull = usableLink(d.wayback_print_ok, d.wayback_print)
    && /[?&]print=1(?:[#&]|$)/.test(d.wayback_print || "") ? d.wayback_print : "";
  const wbAny = usableLink(d.wayback_ok, d.wayback) ? d.wayback : "";
  const azFull = usableLink(d.archive_today_print_ok, d.archive_today_print) ? d.archive_today_print : "";
  // Only surface archive.is when we've actually confirmed a live capture; a
  // speculative /newest/ link is worse than no archive.is at all (it lands on
  // archive.is's "save this page?" form, not a real mirror).
  const azAny = usableLink(d.archive_today_ok, d.archive_today) ? d.archive_today : "";
  const liveUrl = usableLink(d.live_ok, d.live_url) ? d.live_url : "";
  const origUrl = d.original_url || "";
  const archiveUrl = azFull || azAny;
  const pagesNote = d.pages > 1 ? ` (one page; was ${d.pages})` : "";
  // If a ?print=1 full-text capture exists, the headline/thumbnail use it.
  const primary = wbFull || azFull || wbAny || liveUrl || origUrl || archiveUrl;
  // Wayback's base snapshot and its ?print=1 one-page view are distinct
  // captures, so surface both when we have them.
  const mirrorLinks = [
    { url: liveUrl, label: "live", title: "Live on Game Developer today (migrated formatting may be rough)" },
    { url: wbAny, label: "wayback", title: "Wayback Machine snapshot of the original page" },
    { url: wbFull, label: "wayback (full page)", title: `Wayback Machine — full article on one page${pagesNote}` },
    { url: archiveUrl, label: azFull ? "archive.is (full page)" : "archive.is",
      title: azFull ? "archive.is — full-text mirror" : "archive.is mirror (fallback)" },
    { url: origUrl, label: "original", title: "Original Gamasutra URL (likely dead)" },
  ].filter(link => link.url);
  const seenMirrorUrls = new Set();
  const mirrorParts = mirrorLinks.flatMap(link => {
    if (seenMirrorUrls.has(link.url)) return [];
    seenMirrorUrls.add(link.url);
    return [linkHTML(link.url, link.label, link.title, sameUrl(link.url, primary))];
  });
  const mirrorLine = mirrorParts.length
    ? `<span class="mirror-links"><span class="line-label">links:</span> ${mirrorParts.join(" · ")}</span>`
    : "";
  return { primary, mirrorLine };
}

// ---- Series consolidation ---------------------------------------------------
// Multi-part developer-blog postmortems (Octodad Pt 1–3, "How much do indie PC
// devs make" Pt 1/8) arrive as one entry per part, each carrying a shared
// series_id. Collapse them into a single card so a 3-part series reads as one
// work instead of three near-duplicate rows — competing in the balanced sort as
// a unit, with the individual parts listed in the detail row.
function groupSeries(entries) {
  const groups = new Map();   // series_id -> member entries
  const order = [];           // preserves first-appearance order, mixing both kinds
  for (const d of entries) {
    if (d.series_id) {
      if (!groups.has(d.series_id)) { groups.set(d.series_id, []); order.push({ s: d.series_id }); }
      groups.get(d.series_id).push(d);
    } else {
      order.push({ d });
    }
  }
  // A series with only one curated part so far renders as a normal entry (it
  // keeps its own "Pt 1/2" badge); only genuinely multi-part groups collapse.
  return order.map(o => {
    if (!o.s) return o.d;
    const members = groups.get(o.s);
    return members.length > 1 ? mergeSeries(members) : members[0];
  });
}

function mergeSeries(parts) {
  const sorted = [...parts].sort((a, b) => (a.part_no || 0) - (b.part_no || 0));
  const first = sorted[0];
  // Sums where the series-wide total is meaningful (discussion, archive
  // footprint); max where parts share one value or only the strongest matters
  // (copies sold for one game, the single best HN/Reddit thread).
  const sum = k => sorted.reduce((s, p) => s + (Number(p[k]) || 0), 0);
  const max = k => sorted.reduce((m, p) => Math.max(m, Number(p[k]) || 0), 0);
  const dates = sorted.map(p => p.date).filter(Boolean).sort();
  const startY = dates[0] ? String(dates[0]).slice(0, 4) : "";
  const endY = dates.length ? String(dates[dates.length - 1]).slice(0, 4) : "";
  const earliest = sorted.find(p => p.date === dates[0]) || first;
  return {
    ...first,
    is_series: true,
    parts: sorted,
    id: first.series_id,
    title: first.series || first.title,
    _search: sorted.map(p => p.title).join(" "),  // keep part titles searchable
    // The series card wears part 1's image; if part 1 has none, fall back to the
    // first part that does, so a series is never needlessly thumbnail-less.
    thumbnail: (sorted.find(p => p.thumbnail) || first).thumbnail || "",
    authors: [...new Set(sorted.flatMap(p => p.authors || []))],
    author_notable: Math.max(...sorted.map(p => Number(p.author_notable) || 0)),
    date: dates[0] || first.date,                 // earliest, for sorting
    date_estimated: earliest.date_estimated,
    date_label: startY && endY && startY !== endY ? `${startY}–${endY}` : startY,
    hn_points: max("hn_points"),
    hn_comments: max("hn_comments"),
    hn_points_sum: sum("hn_points_sum"),
    hn_comments_sum: sum("hn_comments_sum"),
    hn_submissions: sum("hn_submissions"),
    reddit_score: max("reddit_score"),
    reddit_comments: max("reddit_comments"),
    reddit_score_sum: sum("reddit_score_sum"),
    reddit_comments_sum: sum("reddit_comments_sum"),
    reddit_submissions: sum("reddit_submissions"),
    copies_sold: max("copies_sold"),
    wayback_captures: sum("wayback_captures"),
    hn_threads: sorted.flatMap(p => p.hn_threads || []),
    reddit_threads: sorted.flatMap(p => p.reddit_threads || []),
    // Clear the per-part fields so the card renders as a series, not a part.
    part_no: undefined, part_total: undefined, part_label: undefined, series_id: undefined,
  };
}

// Outlined "Series · N parts" marker for a consolidated multi-part card.
function seriesBadge(d) {
  if (!d.is_series) return "";
  const n = d.parts.length;
  return `<span class="series-badge" title="${esc(d.title)} — consolidated ${n}-part series">Series · ${n} parts</span>`;
}

// The ordered list of parts shown in a series card's detail row, each linking to
// its own best mirror.
function seriesPartsHTML(d) {
  if (!d.is_series) return "";
  const items = d.parts.map(p => {
    const { primary } = resolveLinks(p);
    const t = cleanTitle(p.title) || p.title;
    const partNo = p.part_no ? `Pt ${p.part_no}${p.part_total ? `/${p.part_total}` : ""}` : "";
    const lbl = p.part_label ? ` · ${esc(p.part_label)}` : "";
    const tag = partNo ? `<span class="part-no">${partNo}${lbl}</span> ` : "";
    const link = primary
      ? `<a href="${esc(primary)}" target="_blank" rel="noopener">${esc(t)}</a>`
      : esc(t);
    return `<li>${tag}${link}</li>`;
  });
  return `<span class="series-parts"><span class="line-label">parts:</span><ol>${items.join("")}</ol></span>`;
}

// "Pt 2/3 · Production" badge for an entry that belongs to a multi-part series;
// renders nothing for standalone postmortems.
function partBadge(d) {
  if (!d.part_no) return "";
  const count = d.part_total ? `/${d.part_total}` : "";
  const label = d.part_label ? ` · ${esc(d.part_label)}` : "";
  const series = d.series ? esc(d.series) : "";
  const title = series
    ? `Part ${d.part_no}${d.part_total ? ` of ${d.part_total}` : ""} of “${series}”`
    : "Part of a series";
  return `<span class="part-badge" title="${title}">Pt ${d.part_no}${count}${label}</span>`;
}

function rowHTML(d) {
  const date = d.is_series && d.date_label
    ? `<span title="Series spans ${esc(d.date_label)}">${esc(d.date_label)}</span>`
    : d.date
      ? (d.date_estimated ? `<span class="est" title="Estimated from earliest Wayback capture">~${d.date}</span>` : d.date)
      : '<span class=zero>—</span>';
  const summary = d.summary ? `<span class="summary">${esc(d.summary)}</span>` : "";
  // Blogs often don't name their game in the title, so carry the game subhead for
  // them; features keep it hidden (it just echoed the title there). On desktop it
  // sits in the right-hand aside, opposite the title (see headlineAside below).
  const game = isContribBlog(d) ? displayGame(d) : "";
  const gameLine = game ? `<span class="game">${esc(game)}</span>` : "";
  // A series card's headline points at part 1 and lists every part in the detail
  // row; a standalone entry shows its own "links:" line of mirrors.
  const { primary, mirrorLine } = d.is_series
    ? { primary: resolveLinks(d.parts[0]).primary, mirrorLine: "" }
    : resolveLinks(d);
  const seriesParts = seriesPartsHTML(d);
  const discussions = hnThreadsHTML(d) + redditThreadsHTML(d);
  const mirrors = seriesParts || mirrorLine || discussions
    ? `<span class="mirrors">${seriesParts}${mirrorLine}${discussions}</span>`
    : "";
  const tags = tagsHTML(d);
  // Mobile-only meta line above the headline (desktop carries the date in its
  // own column and the type/tag/part badges sit above the title instead).
  const metaTop = `<span class="meta-top">`
    + `<span class="m-date">${date}</span>`
    + catBadge(d.category)
    + tags
    + partBadge(d)
    + seriesBadge(d)
    + sortSignalHTML(d)
    + `</span>`;
  // Type + tag + series badges above the title; shown on desktop, hidden on
  // mobile where the meta line carries them.
  const topBadges = (catBadge(d.category) || tags || partBadge(d) || seriesBadge(d))
    ? `<span class="top-badges">${catBadge(d.category)}${tags}${partBadge(d)}${seriesBadge(d)}</span>`
    : "";
  const byline = d.authors && d.authors.length
    ? `<span class="byline">by ${d.authors.map(name => authorHTML(name)).join(", ")}</span>`
    : "";
  // The game label floats opposite the title, so only a game makes the title
  // wrap (no game -> no float -> full-width title). The byline and the muted
  // "strong on …" hint share the line below, so the hint sits opposite the
  // author and never touches the title. On mobile the hint is hidden (CSS).
  const rankedOn = whyBalancedHTML(d);
  const headlineFoot = (byline || rankedOn)
    ? `<div class="headline-foot">${byline}${rankedOn}</div>` : "";
  const shownTitle = cleanTitle(d.title) || d.title;
  const title = primary
    ? `<a class="title-cell" href="${esc(primary)}" target="_blank" rel="noopener">${esc(shownTitle)}</a>`
    : `<span class="title-cell">${esc(shownTitle)}</span>`;
  const thumb = d.thumbnail && primary
    ? `<a href="${esc(primary)}" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="${esc(d.thumbnail)}" alt="" onerror="this.closest('td').classList.add('no-thumb');this.remove()"></a>`
    : "";
  const hnCell = metricCell(
    d.hn_points_sum ? Number(d.hn_points_sum).toLocaleString() : "",
    discussionSub(d.hn_comments_sum, d.hn_submissions),
    "Hacker News — total points; comments and submission count below");
  const redditCell = metricCell(
    d.reddit_score_sum ? Number(d.reddit_score_sum).toLocaleString() : "",
    discussionSub(d.reddit_comments_sum, d.reddit_submissions),
    "Reddit — total score; comments and submission count below");
  const salesCell = metricCell(compactSold(d.copies_sold), "", d.sales_note || "Copies sold (Wikipedia-derived)");
  const capsCell = metricCell(
    d.wayback_captures ? Number(d.wayback_captures).toLocaleString() : "", "", "Wayback capture count");
  // Each entry is two rows: a compact headline row (metadata in columns on
  // desktop) and a wide detail row that spans the text columns underneath.
  return `<tr class="r-main">
    <td class="rank-cell" rowspan="2" title="Balanced rank (1–${DATA.length})">${d.balanced_rank}</td>
    <td class="thumb-cell${thumb ? "" : " no-thumb"}" rowspan="2">${thumb}</td>
    <td class="main-cell">${topBadges}${metaTop}${gameLine}${title}${headlineFoot}</td>
    <td class="num date-cell">${date}</td>
    ${hnCell}
    ${redditCell}
    ${salesCell}
    ${capsCell}
  </tr>
  <tr class="r-detail"><td class="detail-cell" colspan="6"><div class="detail-pin">${summary}${mirrors}</div></td></tr>`;
}

document.querySelectorAll("th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (sortKey === k) sortDir *= -1;
    else { sortKey = k; sortDir = (k === "title" || k === "authors" || k === "category") ? 1 : -1; }
    render();
  });
  th.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); th.click(); }
  });
});
searchEl.addEventListener("input", () => { render(); refreshFilterCounts(); });

// ---- Popovers: custom multiselect filter + custom single-select sort ----
// Only one panel open at a time; opening one closes the other.
function openPanel(panel, btn, open) {
  const show = open === undefined ? panel.hidden : open;
  if (show) {
    filterPanel.hidden = sortPanel.hidden = true;
    filterBtn.setAttribute("aria-expanded", "false");
    sortBtn.setAttribute("aria-expanded", "false");
  }
  panel.hidden = !show;
  btn.setAttribute("aria-expanded", String(show));
}
filterBtn.addEventListener("click", e => { e.stopPropagation(); openPanel(filterPanel, filterBtn); });
sortBtn.addEventListener("click", e => { e.stopPropagation(); openPanel(sortPanel, sortBtn); });
filterCols.addEventListener("change", e => {
  const cb = e.target.closest("input[type=checkbox]");
  if (cb) setFilter(cb.value, cb.checked);
});
filterClear.addEventListener("click", () => { selected.clear(); syncFilterUI(); render(); });
sortList.addEventListener("click", e => {
  const o = e.target.closest(".sort-opt");
  if (!o) return;
  const [k, dir] = o.dataset.val.split(":");
  sortKey = k; sortDir = parseInt(dir, 10);
  openPanel(sortPanel, sortBtn, false);
  render();
});
// click-away closes whichever panel is open
document.addEventListener("click", e => {
  if (!filterPanel.hidden && !e.target.closest("#filter")) openPanel(filterPanel, filterBtn, false);
  if (!sortPanel.hidden && !e.target.closest("#sort")) openPanel(sortPanel, sortBtn, false);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!filterPanel.hidden) { openPanel(filterPanel, filterBtn, false); filterBtn.focus(); }
  if (!sortPanel.hidden) { openPanel(sortPanel, sortBtn, false); sortBtn.focus(); }
});

// Tag chips live inside re-rendered rows, so delegate. A chip toggles its value in
// the multiselect filter (and reflects the active state back via syncFilterUI).
rowsEl.addEventListener("click", e => {
  const b = e.target.closest(".tag");
  if (!b) return;
  e.preventDefault();
  setFilter(b.dataset.val, !selected.has(b.dataset.val));
});

// ---- Theme toggle (light / dark), persisted in localStorage ----
const themeToggle = document.getElementById("themeToggle");
const root = document.documentElement;

function syncThemeButton() {
  const dark = root.dataset.theme === "dark";
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  themeToggle.textContent = dark ? "☀" : "☾";
  themeToggle.setAttribute("aria-pressed", String(dark));
  themeToggle.setAttribute("aria-label", label);
  themeToggle.title = label;
}

themeToggle.addEventListener("click", () => {
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) { /* ignore */ }
  syncThemeButton();
});

// Follow the OS theme until the user makes an explicit choice.
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
  try { if (localStorage.getItem("theme")) return; } catch (_) { /* ignore */ }
  root.dataset.theme = e.matches ? "dark" : "light";
  syncThemeButton();
});

syncThemeButton();

// Press "/" to jump to the search box (unless already typing somewhere).
document.addEventListener("keydown", e => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) return;
  e.preventDefault();
  searchEl.focus();
  searchEl.select();
});

load();
