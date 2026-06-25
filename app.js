import { parse } from "https://cdn.jsdelivr.net/npm/smol-toml@1.3.1/dist/index.js";

const rowsEl = document.getElementById("rows");
const statusEl = document.getElementById("status");
const searchEl = document.getElementById("search");
const catEl = document.getElementById("category");
const notableEl = document.getElementById("notableOnly");
const countEl = document.getElementById("count");
const sortSel = document.getElementById("sortSel");
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
  const cats = [...new Set(DATA.map(d => d.category))].sort();
  for (const c of cats) {
    const o = document.createElement("option"); o.value = o.textContent = c;
    catEl.appendChild(o);
  }
  statusEl.textContent = "";
  render();
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
}

function render() {
  const q = searchEl.value.trim().toLowerCase();
  const cat = catEl.value;
  const notable = notableEl.checked;

  let list = DATA.filter(d => {
    if (cat && d.category !== cat) return false;
    if (notable && !d.author_notable) return false;
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
  // keep the mobile sort dropdown in sync (only if it has a matching option)
  const val = `${sortKey}:${sortDir}`;
  if ([...sortSel.options].some(o => o.value === val)) sortSel.value = val;
  updateScrollFades();
}

if (tableWrap) tableWrap.addEventListener("scroll", updateScrollFades, { passive: true });
window.addEventListener("resize", updateScrollFades);

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
  // Mobile-only meta line above the headline (desktop carries the date in its
  // own column and the type/part badges sit above the title instead).
  const metaTop = `<span class="meta-top">`
    + `<span class="m-date">${date}</span>`
    + catBadge(d.category)
    + partBadge(d)
    + seriesBadge(d)
    + sortSignalHTML(d)
    + `</span>`;
  // Type + series badges above the title; shown on desktop, hidden on mobile
  // where the meta line carries the type.
  const topBadges = (catBadge(d.category) || partBadge(d) || seriesBadge(d))
    ? `<span class="top-badges">${catBadge(d.category)}${partBadge(d)}${seriesBadge(d)}</span>`
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
sortSel.addEventListener("change", () => {
  const [k, dir] = sortSel.value.split(":");
  sortKey = k; sortDir = parseInt(dir, 10);
  render();
});
searchEl.addEventListener("input", render);
catEl.addEventListener("change", render);
notableEl.addEventListener("change", render);

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
