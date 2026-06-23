import { parse } from "https://cdn.jsdelivr.net/npm/smol-toml@1.3.1/dist/index.js";

const rowsEl = document.getElementById("rows");
const statusEl = document.getElementById("status");
const searchEl = document.getElementById("search");
const catEl = document.getElementById("category");
const notableEl = document.getElementById("notableOnly");
const countEl = document.getElementById("count");
const sortSel = document.getElementById("sortSel");

let DATA = [];
let sortKey = "date";
let sortDir = -1; // -1 desc, 1 asc

const esc = (s) => (s ?? "").replace(/[&<>"]/g, c => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function load() {
  try {
    const res = await fetch("data/postmortems.toml", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    DATA = (parse(await res.text()).postmortem) || [];
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
  rowsEl.innerHTML = list.map(rowHTML).join("");
  document.querySelectorAll("th.sortable").forEach(th => {
    th.classList.remove("sorted-asc", "sorted-desc");
    if (th.dataset.key === sortKey)
      th.classList.add(sortDir === 1 ? "sorted-asc" : "sorted-desc");
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

function gamedeveloperUrl(originalUrl) {
  if (!originalUrl) return "";
  try {
    const u = new URL(originalUrl);
    u.protocol = "https:";
    u.hostname = "www.gamedeveloper.com";
    return u.toString();
  } catch {
    return originalUrl.replace(/^https?:\/\/(?:www\.)?gamasutra\.com/i, "https://www.gamedeveloper.com");
  }
}

function printArchiveUrl(d) {
  const url = d.wayback_print || "";
  return /[?&]print=1(?:[#&]|$)/.test(url) ? url : "";
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
  const primary = fullText || d.wayback;
  // archive.today fallback — derived from the original URL, newest snapshot
  const at = `https://archive.ph/newest/${encodeURIComponent(d.original_url)}`;
  const live = d.live_url || gamedeveloperUrl(d.original_url);
  const fullTitle = d.pages > 1
    ? `Full article on one page (${d.pages} pages)`
    : "Archived print view / full text";
  const mirrors = `<span class="mirrors">`
    + (fullText ? `<span title="${esc(fullTitle)}">primary: full text</span> · ` : "")
    + `<a href="${esc(d.wayback)}" target="_blank" rel="noopener" title="Internet Archive snapshot of the original page">wayback</a>`
    + ` · <a href="${esc(d.original_url)}" target="_blank" rel="noopener" title="Original Gamasutra URL (often dead)">original</a>`
    + ` · <a href="${esc(live)}" target="_blank" rel="noopener" title="Live Game Developer URL (may have broken formatting)">live</a>`
    + ` · <a href="${esc(at)}" target="_blank" rel="noopener" title="archive.today mirror (fallback)">archive.today</a>`
    + `</span>`;
  // vintage dateline: metadata line shown ABOVE the headline (esp. on mobile)
  const metaTop = `<span class="meta-top">`
    + `<span class="m-date">${date}</span>`
    + ` <span class="cat ${catClass(d.category)}">${esc(d.category)}</span>`
    + (d.authors && d.authors.length
        ? ` <span class="m-auth">by ${esc(d.authors.join(", "))}${d.author_notable ? " ★" : ""}</span>`
        : "")
    + `</span>`;
  const thumb = d.thumbnail
    ? `<a href="${esc(primary)}" target="_blank" rel="noopener"><img class="thumb" loading="lazy" src="${esc(d.thumbnail)}" alt="" onerror="this.closest('td').classList.add('no-thumb');this.remove()"></a>`
    : "";
  return `<tr>
    <td class="thumb-cell${d.thumbnail ? "" : " no-thumb"}">${thumb}</td>
    <td class="main-cell">${metaTop}<a class="title-cell" href="${esc(primary)}" target="_blank" rel="noopener">${esc(d.title)}</a>${gameLine}${summary}${mirrors}</td>
    <td>${authors}${star}</td>
    <td><span class="cat ${catClass(d.category)}">${esc(d.category)}</span></td>
    <td class="num">${date}</td>
    ${num(d.hn_points)}${num(d.hn_comments)}${num(d.wayback_captures)}
  </tr>`;
}

document.querySelectorAll("th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (sortKey === k) sortDir *= -1;
    else { sortKey = k; sortDir = (k === "title" || k === "authors" || k === "category") ? 1 : -1; }
    render();
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

load();
