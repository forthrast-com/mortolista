import { parse } from "https://cdn.jsdelivr.net/npm/smol-toml@1.3.1/dist/index.js";

const rowsEl = document.getElementById("rows");
const statusEl = document.getElementById("status");
const searchEl = document.getElementById("search");
const catEl = document.getElementById("category");
const notableEl = document.getElementById("notableOnly");
const countEl = document.getElementById("count");
const sortSel = document.getElementById("sortSel");

let DATA = [];
let sortKey = "hn_points_sum";
let sortDir = -1; // -1 desc, 1 asc

const SORT_LABELS = {
  title: "Title",
  authors: "Author",
  category: "Type",
  date: "Date",
  hn_points: "Best HN points",
  hn_comments: "Best HN comments",
  hn_points_sum: "Total HN points",
  hn_comments_sum: "Total HN comments",
  hn_submissions: "HN submissions",
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

async function loadMirrorSidecars() {
  await loadSidecar("data/archive_is_mirrors.toml", "archive_mirror");
  await loadSidecar("data/gamedeveloper_live_urls.toml", "gamedeveloper_live");
}

async function load() {
  try {
    const res = await fetch("data/postmortems.toml", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    DATA = (parse(await res.text()).postmortem) || [];
    await loadHnMetrics();
    await loadMirrorSidecars();
  } catch (e) {
    statusEl.textContent = "Could not load data/postmortems.toml — run the scraper first. (" + e + ")";
    return;
  }
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
    : `<tr><td colspan="7" class="empty">No postmortems match your filters.</td></tr>`;
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
}

function catClass(c) {
  c = (c || "").toLowerCase();
  if (c.includes("indie")) return "indie";
  if (c.includes("audio")) return "audio";
  if (c.includes("middleware")) return "middleware";
  return "";
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
  wayback_captures: "Wayback captures",
};

// A small chip naming why this entry sorted where it did — only when the
// active sort isn't already shown on the card and the value is meaningful.
function sortSignalHTML(d) {
  if (SHOWN_SORTS.has(sortKey)) return "";
  const v = d[sortKey];
  if (!v) return "";
  const label = SORT_SIGNAL_LABELS[sortKey] || SORT_LABELS[sortKey] || sortKey;
  const val = typeof v === "number" ? v.toLocaleString() : esc(String(v));
  return ` <span class="m-sort" title="Current sort metric">${val} ${esc(label)}</span>`;
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

function rowHTML(d) {
  const authors = (d.authors || []).map(esc).join(", ") || "<span class=zero>—</span>";
  const star = d.author_notable ? ' <span class="notable" title="Notable author (Wikipedia)">★</span>' : "";
  const date = d.date
    ? (d.date_estimated ? `<span class="est" title="Estimated from earliest Wayback capture">~${d.date}</span>` : d.date)
    : '<span class=zero>—</span>';
  const summary = d.summary ? `<span class="summary">${esc(d.summary)}</span>` : "";
  const gameLine = d.game && d.game !== d.title ? `<span class="game">${esc(d.game)}</span>` : "";
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
  const discussions = hnThreadsHTML(d);
  const mirrors = mirrorLine || discussions
    ? `<span class="mirrors">${mirrorLine}${discussions}</span>`
    : "";
  // vintage dateline + byline: shown above the headline on the mobile card
  // (hidden on desktop, where the columns carry this metadata instead)
  const metaTop = `<span class="meta-top">`
    + `<span class="m-date">${date}</span>`
    + ` <span class="cat ${catClass(d.category)}">${esc(d.category)}</span>`
    + sortSignalHTML(d)
    + `</span>`;
  const byline = d.authors && d.authors.length
    ? `<span class="byline">by ${esc(d.authors.join(", "))}${d.author_notable
        ? ' <span class="notable" title="Notable author (Wikipedia)">★</span>' : ""}</span>`
    : "";
  const title = primary
    ? `<a class="title-cell" href="${esc(primary)}" target="_blank" rel="noopener">${esc(d.title)}</a>`
    : `<span class="title-cell">${esc(d.title)}</span>`;
  const thumb = d.thumbnail && primary
    ? `<a href="${esc(primary)}" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="${esc(d.thumbnail)}" alt="" onerror="this.closest('td').classList.add('no-thumb');this.remove()"></a>`
    : "";
  // Each entry is two rows: a compact headline row (metadata in columns on
  // desktop) and a wide detail row that spans the text columns underneath.
  return `<tr class="r-main">
    <td class="thumb-cell${thumb ? "" : " no-thumb"}" rowspan="2">${thumb}</td>
    <td class="main-cell">${metaTop}${title}${gameLine}${byline}</td>
    <td>${authors}${star}</td>
    <td><span class="cat ${catClass(d.category)}">${esc(d.category)}</span></td>
    <td class="num">${date}</td>
    ${hnMetricHTML(d)}${num(d.wayback_captures)}
  </tr>
  <tr class="r-detail"><td class="detail-cell" colspan="6">${summary}${mirrors}</td></tr>`;
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
