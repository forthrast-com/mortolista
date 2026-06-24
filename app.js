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
let AUTHOR_BIOS = new Map(); // article id -> end-of-article author bio
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
    NOTABLE_AUTHORS = new Map(
      rows.filter(r => r.name && r.wiki_url).map(r => [r.name, r.wiki_url]));
  } catch (e) {
    // best-effort; the catalogue works fine without author links.
  }
}

async function loadAuthorBios() {
  // Optional article-scoped bios pulled from the archived print pages. Kept as
  // a sidecar so missing/partial extraction never blocks the archive itself.
  try {
    const res = await fetch("data/author_bios.toml", { cache: "no-cache" });
    if (!res.ok) return;
    const rows = parse(await res.text()).author_bio || [];
    AUTHOR_BIOS = new Map(rows.filter(r => r.id && r.bio).map(r => [String(r.id), r.bio]));
  } catch (e) {
    // best-effort; author names just render as normal links/text.
  }
}

async function loadPhaseTwoMetrics() {
  await loadOptionalSidecar("data/reddit_postmortem_threads.toml", "reddit_postmortem", BLANK_REDDIT);
  await loadOptionalSidecar("data/wikipedia_game_sales.toml", "wiki_game_sales", BLANK_SALES);
}

async function loadMirrorSidecars() {
  await loadSidecar("data/archive_is_mirrors.toml", "archive_mirror");
  await loadSidecar("data/gamedeveloper_live_urls.toml", "gamedeveloper_live");
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
const AGG_AXES = [
  { key: "hn_points_sum", weight: 1.0, label: "HN points" },
  { key: "copies_sold", weight: 0.9, label: "copies sold" },
  { key: "wayback_captures", weight: 0.7, label: "captures" },
  { key: "hn_comments_sum", weight: 0.5, label: "HN discussion" },
  { key: "reddit_score_sum", weight: 0.5, label: "Reddit score" },
  { key: "author_notable", weight: 0.5, label: "notable author", binary: true },
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
    if (ax.binary) continue;
    maps[ax.key] = percentileMap(have.map(d => Number(d[ax.key]) || 0));
  }
  for (const d of DATA) {
    let score = 0;
    const contrib = [];
    for (const ax of AGG_AXES) {
      const v = Number(d[ax.key]) || 0;
      const norm = ax.binary ? (v ? 1 : 0) : (v > 0 ? (maps[ax.key].get(v) || 0) : 0);
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
    await loadAuthorBios();
  } catch (e) {
    statusEl.textContent = "Could not load data/postmortems.toml — run the scraper first. (" + e + ")";
    return;
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
  if (k === "author_notable") return d.author_notable ? 1 : 0;
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
      const hay = (d.title + " " + d.game + " " + (d.authors || []).join(" ")).toLowerCase();
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

function num(v) {
  return v ? `<td class="num">${v}</td>` : `<td class="num zero">–</td>`;
}

function hnMetricHTML(d) {
  const points = d.hn_points_sum || 0;
  const comments = d.hn_comments_sum || 0;
  if (!points && !comments) return `<td class="num hn-metric zero">–</td>`;
  return `<td class="num hn-metric"><span>${points.toLocaleString()}▲</span><span>${comments.toLocaleString()}｜</span></td>`;
}

function metricValue(d, k) {
  const v = d[k];
  if (k === "date") return d.date ? (d.date_estimated ? `~${d.date}` : d.date) : "—";
  if (k === "authors") return (d.authors || []).join(", ") || "—";
  if (k === "author_notable") return d.author_notable ? "yes" : "no";
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
    return ` <span class="m-sort" title="Balanced rank — strongest signals for this entry">ranked on: ${top.map(esc).join(" · ")}</span>`;
  }
  if (SHOWN_SORTS.has(sortKey)) return "";
  const v = d[sortKey];
  if (!v) return "";
  const label = SORT_SIGNAL_LABELS[sortKey] || SORT_LABELS[sortKey] || sortKey;
  const val = typeof v === "number" ? v.toLocaleString() : esc(String(v));
  return ` <span class="m-sort" title="Current sort metric">${val} ${esc(label)}</span>`;
}

// Desktop counterpart to the meta-top sort chip: the meta line is mobile-only,
// so when balanced is the active sort we echo the "why it ranked" hint into the
// detail row (which shows on desktop). CSS hides this copy on mobile to avoid
// doubling up with the meta-top chip.
function whyBalancedHTML(d) {
  if (sortKey !== "agg_score") return "";
  const top = d.agg_top || [];
  if (!top.length) return "";
  return `<span class="why-balanced" title="Balanced rank — strongest signals for this entry">ranked on: ${top.map(esc).join(" · ")}</span>`;
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

function checkedLink(ok, url) {
  return ok === true && !!url;
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

function printArchiveUrl(d) {
  const wayback = d.wayback_print || "";
  if (usableLink(d.wayback_print_ok, wayback) && /[?&]print=1(?:[#&]|$)/.test(wayback)) return wayback;
  const archiveToday = d.archive_today_print || "";
  return usableLink(d.archive_today_print_ok, archiveToday) ? archiveToday : "";
}

function cleanTitle(title) {
  return (title || "")
    .replace(/^\s*(?:(?:classic|indie|audio|faculty|game|game design|middleware|mobile|student|tool)\s+)*post\s*-?\s*mortem\s*(?::|[-–—])\s*/i, "")
    .replace(/^\s*(?:postmortem|post-mortem)\s+/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

function displayGame(d) {
  const game = (d.game || "").trim();
  if (!game) return "";
  const title = cleanTitle(d.title);
  if (!title) return game;
  const canonical = s => cleanTitle(s).toLowerCase();
  return canonical(game) === canonical(title) ? "" : game;
}

function articleBio(d) {
  return AUTHOR_BIOS.get(String(d.id)) || "";
}

function authorHTML(name, bio = "") {
  const url = NOTABLE_AUTHORS.get(name);
  const bioClass = bio ? " has-bio" : "";
  const bioAttr = bio ? ` data-bio="${esc(bio)}" aria-label="${esc(`${name}: ${bio}`)}"` : "";
  const titleAttr = !bio && url ? ` title="Wikipedia: ${esc(name)}"` : "";
  return url
    ? `<a class="author-link${bioClass}" href="${esc(url)}" target="_blank" rel="noopener"${titleAttr}${bioAttr}>${esc(name)}</a>`
    : `<span class="author-name${bioClass}"${bioAttr}>${esc(name)}</span>`;
}

function rowHTML(d) {
  const bio = articleBio(d);
  const authors = (d.authors || []).map(name => authorHTML(name, bio)).join(", ") || "<span class=zero>—</span>";
  const date = d.date
    ? (d.date_estimated ? `<span class="est" title="Estimated from earliest Wayback capture">~${d.date}</span>` : d.date)
    : '<span class=zero>—</span>';
  const summary = d.summary ? `<span class="summary">${esc(d.summary)}</span>` : "";
  const game = displayGame(d);
  const gameLine = game ? `<span class="game">${esc(game)}</span>` : "";
  const fullText = printArchiveUrl(d);
  const primary = fullText
    || (usableLink(d.wayback_ok, d.wayback) ? d.wayback : "")
    || (usableLink(d.original_ok, d.original_url) ? d.original_url : "")
    || (usableLink(d.live_ok, d.live_url) ? d.live_url : "")
    || (usableLink(d.archive_today_print_ok, d.archive_today_print) ? d.archive_today_print : "")
    || (usableLink(d.archive_today_ok, d.archive_today) ? d.archive_today : "");
  const fullTitle = d.pages > 1
    ? `Full article on one page (${d.pages} pages)`
    : "Archived print view / full text";
  const archiveToday = d.archive_today || (d.original_url
    ? `https://archive.is/newest/${encodeURIComponent(d.original_url)}`
    : "");
  const mirrorLinks = [
    { ok: !!fullText, url: fullText, label: "full text", title: fullTitle },
    { ok: usableLink(d.wayback_ok, d.wayback), url: d.wayback, label: "wayback", title: "Internet Archive snapshot of the original page" },
    { ok: checkedLink(d.original_ok, d.original_url), url: d.original_url, label: "original", title: "Original Gamasutra URL" },
    { ok: checkedLink(d.live_ok, d.live_url), url: d.live_url, label: "live", title: "Verified live Game Developer URL (may have broken formatting)" },
    { ok: usableLink(d.archive_today_print_ok, d.archive_today_print), url: d.archive_today_print, label: "archive.is full", title: "archive.is print/full-text mirror" },
    { ok: usableLink(d.archive_today_ok, archiveToday), url: archiveToday, label: "archive.is", title: "archive.is mirror (fallback)" },
  ].filter(link => link.ok && link.url);
  const seenMirrorUrls = new Set();
  const mirrorParts = mirrorLinks.flatMap(link => {
    if (seenMirrorUrls.has(link.url)) return [];
    seenMirrorUrls.add(link.url);
    return [linkHTML(link.url, link.label, link.title, sameUrl(link.url, primary))];
  });
  const mirrorLine = mirrorParts.length
    ? `<span class="mirror-links"><span class="line-label">links:</span> ${mirrorParts.join(" · ")}</span>`
    : "";
  const discussions = hnThreadsHTML(d) + redditThreadsHTML(d);
  const mirrors = mirrorLine || discussions
    ? `<span class="mirrors">${mirrorLine}${discussions}</span>`
    : "";
  // vintage dateline + byline: shown above the headline on the mobile card
  // (hidden on desktop, where the columns carry this metadata instead)
  const metaTop = `<span class="meta-top">`
    + `<span class="m-date">${date}</span>`
    + catBadge(d.category)
    + sortSignalHTML(d)
    + `</span>`;
  const byline = d.authors && d.authors.length
    ? `<span class="byline">by ${d.authors.map(name => authorHTML(name, bio)).join(", ")}</span>`
    : "";
  const shownTitle = cleanTitle(d.title) || d.title;
  const title = primary
    ? `<a class="title-cell" href="${esc(primary)}" target="_blank" rel="noopener">${esc(shownTitle)}</a>`
    : `<span class="title-cell">${esc(shownTitle)}</span>`;
  const thumb = d.thumbnail && primary
    ? `<a href="${esc(primary)}" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="${esc(d.thumbnail)}" alt="" onerror="this.closest('td').classList.add('no-thumb');this.remove()"></a>`
    : "";
  // Each entry is two rows: a compact headline row (metadata in columns on
  // desktop) and a wide detail row that spans the text columns underneath.
  return `<tr class="r-main">
    <td class="rank-cell" rowspan="2" title="Balanced rank (1–${DATA.length})">${d.balanced_rank}</td>
    <td class="thumb-cell${thumb ? "" : " no-thumb"}" rowspan="2">${thumb}</td>
    <td class="main-cell">${metaTop}${title}${gameLine}${byline}</td>
    <td>${authors}</td>
    <td>${catBadge(d.category)}</td>
    <td class="num">${date}</td>
    ${hnMetricHTML(d)}${num(d.wayback_captures)}
  </tr>
  <tr class="r-detail"><td class="detail-cell" colspan="6">${whyBalancedHTML(d)}${summary}${mirrors}</td></tr>`;
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
