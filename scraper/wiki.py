"""Wikipedia enrichment: notable authors, sales signals, author bios.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w
from common import AUTHOR_BIOS, NOTABLE_AUTHORS, SESSION, WIKI_SALES, ascii_fold, clean, load_toml, log
from parse import extract_author_bio

# ------------------------------------------------------------ Wikipedia enrich
_wiki_cache = {}
def author_wiki(name):
    """Resolved Wikipedia page for a game-industry author, or None.

    A name counts as "notable" when its Wikipedia article exists and reads like
    a games-industry figure.  Returns the resolved title + canonical URL (after
    redirects) so the catalogue can link the byline straight to the page.
    """
    if not name:
        return None
    if name in _wiki_cache:
        return _wiki_cache[name]
    info = None
    try:
        r = SESSION.get("https://en.wikipedia.org/w/api.php",
                        params={"action": "query", "format": "json",
                                "titles": name, "prop": "extracts|info",
                                "inprop": "url", "exintro": 1,
                                "explaintext": 1, "redirects": 1},
                        timeout=20)
        query = r.json().get("query", {})
        # A name that only resolves *through* a redirect (e.g. a maiden name or
        # a studio that redirects to a person) is a weaker notability signal than
        # one with its own article; flag it so the catalogue can half-weight it.
        via_redirect = bool(query.get("redirects"))
        pages = query.get("pages", {})
        for _, p in pages.items():
            if "missing" in p:
                continue
            extract = (p.get("extract") or "").lower()
            if any(k in extract for k in
                   ("game", "developer", "designer", "programmer", "studio")):
                title = p.get("title") or name
                info = {
                    "name": name,
                    "wiki_title": title,
                    "wiki_url": p.get("fullurl")
                    or "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                    "via_redirect": via_redirect,
                }
                break
    except Exception:
        pass
    _wiki_cache[name] = info
    return info
def author_notable(name):
    return author_wiki(name) is not None
def refresh_notable_authors(data_path, out_path=NOTABLE_AUTHORS, limit=0):
    """Sidecar of distinct authors with a Wikipedia page, name -> wiki URL."""
    payload = load_toml(data_path)
    names, seen = [], set()
    for article in payload.get("postmortem", []):
        for name in article.get("authors", []) or []:
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    if limit:
        names = names[:limit]
    rows = []
    for i, name in enumerate(names, 1):
        info = author_wiki(name)
        log(f"[*] author {i}/{len(names)} {name}: {'wiki' if info else '—'}")
        if info:
            rows.append(info)
        time.sleep(0.2)
    if names and not rows:
        raise RuntimeError("no authors resolved to Wikipedia pages; likely a network failure, not writing an empty sidecar")
    rows.sort(key=lambda r: r["name"].lower())
    Path(out_path).write_bytes(tomli_w.dumps({"notable_author": rows}).encode())
    return len(rows)
# --------------------------------------------------------- Wikipedia sales data
SALES_PATTERNS = [
    re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>million|billion)\s+(?:copies|units)\b", re.I),
    re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>million|billion)\s+sales\b", re.I),
    re.compile(r"sold\s+(?:over|more than|at least|approximately|around|about)?\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>million|billion)\b", re.I),
    re.compile(r"(?P<num>\d{1,3}(?:,\d{3})+)\s+(?:copies|units)\b", re.I),
]
def wiki_search_titles(query, limit=5):
    try:
        r = SESSION.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
        }, timeout=20)
        r.raise_for_status()
        return [row.get("title", "") for row in r.json().get("query", {}).get("search", []) if row.get("title")]
    except requests.RequestException:
        return []
def wiki_page(title):
    try:
        r = SESSION.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query",
            "format": "json",
            "titles": title,
            "prop": "extracts|pageprops|categories",
            "explaintext": 1,
            "redirects": 1,
            "cllimit": 50,
        }, timeout=20)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            if "missing" not in page:
                return page
    except requests.RequestException:
        return None
    return None
def game_title_candidates(game):
    game = clean(game)
    if not game:
        return []
    parts = [game]
    # The catalogue intentionally preserves publisher/studio possessives in the
    # headline; Wikipedia page titles usually don't.
    parts.append(re.sub(r"^.+?'s\s+", "", game))
    parts.append(re.sub(r"^.+?:\s*", "", game))
    out, seen = [], set()
    for part in parts:
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out
def page_looks_like_game(page):
    text = ((page or {}).get("extract") or "").lower()[:3000]
    cats = " ".join(c.get("title", "") for c in (page or {}).get("categories", [])).lower()
    return "video game" in text or "video games" in cats or "video game" in cats
def comparable_game_title(title):
    title = ascii_fold(title or "").lower()
    title = re.sub(r"\([^)]*\)", " ", title)
    title = re.sub(r"^(?:.+?'s\s+)", "", title)
    title = re.sub(r"\b(?:video game|game|postmortem|mobile|student|tool|faculty)\b", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title).strip()
    return title
def wiki_title_match_score(candidate, page_title):
    cand = comparable_game_title(candidate)
    page = comparable_game_title(page_title)
    if not cand or not page:
        return 0.0
    ratio = SequenceMatcher(None, cand, page).ratio()
    cand_tokens = [t for t in cand.split() if len(t) > 2]
    page_tokens = set(page.split())
    if cand_tokens:
        overlap = sum(1 for t in cand_tokens if t in page_tokens) / len(cand_tokens)
        ratio = max(ratio, overlap)
    if cand in page or page in cand:
        ratio = max(ratio, 0.9)
    return ratio
def find_wiki_game_page(article):
    best = (0.0, None)
    for candidate in game_title_candidates(article.get("game") or article.get("title") or ""):
        titles = [candidate] + wiki_search_titles(candidate + " video game", limit=5)
        seen = set()
        for title in titles:
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            page = wiki_page(title)
            if not page or not page_looks_like_game(page):
                continue
            score = wiki_title_match_score(candidate, page.get("title", ""))
            if score > best[0]:
                best = (score, page)
    return best[1] if best[0] >= 0.58 else None
def parse_sales_number(match):
    raw = match.group("num").replace(",", "")
    value = float(raw)
    unit = (match.groupdict().get("unit") or "").lower()
    if unit == "million":
        value *= 1_000_000
    elif unit == "billion":
        value *= 1_000_000_000
    return int(value)
def extract_sales_signal(extract):
    if not extract:
        return 0, ""
    best = (0, "")
    # Sentence-ish chunks are enough and avoid dumping paragraphs into data.
    for sentence in re.split(r"(?<=[.!?])\s+", extract):
        if not re.search(r"\b(sold|sales|copies|units)\b", sentence, re.I):
            continue
        for rx in SALES_PATTERNS:
            m = rx.search(sentence)
            if not m:
                continue
            copies = parse_sales_number(m)
            if copies > best[0]:
                best = (copies, clean(sentence)[:260])
    return best
def refresh_wiki_sales(data_path, out_path=WIKI_SALES, limit=0, offset=0):
    payload = load_toml(data_path)
    all_articles = payload.get("postmortem", [])
    articles = all_articles[offset:]
    if limit:
        articles = articles[:limit]

    out_path = Path(out_path)
    existing = {}
    if out_path.exists():
        existing = {row["id"]: row for row in load_toml(out_path).get("wiki_game_sales", [])}

    rows = []
    skipped = 0
    for i, article in enumerate(articles, 1):
        # Wikipedia lookups are the slow pole; an id already in the sidecar keeps its
        # cached row instead of being re-queried every run. Delete the row (or the
        # whole sidecar) to force a re-resolve.
        if article["id"] in existing:
            skipped += 1
            continue
        log(f"[*] wiki sales {offset + i}/{len(all_articles)} {article.get('id')} {article.get('game')}")
        page = find_wiki_game_page(article)
        copies, sentence = extract_sales_signal((page or {}).get("extract") or "")
        rows.append({
            "id": article["id"],
            "wiki_title": (page or {}).get("title", ""),
            "wiki_url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(((page or {}).get("title") or "").replace(" ", "_")) if page else "",
            "copies_sold": copies,
            "sales_note": sentence,
        })
    if skipped:
        log(f"[*] wiki sales: {skipped} cached-skip, {len(rows)} freshly resolved")

    existing.update({row["id"]: row for row in rows})
    ordered = [existing[a["id"]] for a in all_articles if a["id"] in existing]
    out_path.write_bytes(tomli_w.dumps({"wiki_game_sales": ordered}).encode())
    return len(rows)
def refresh_author_bios(data_path, out_path=AUTHOR_BIOS, limit=0):
    articles = load_toml(data_path).get("postmortem", [])
    if limit:
        articles = articles[:limit]
    rows = []
    for i, article in enumerate(articles, 1):
        url = article.get("wayback_print") or article.get("wayback")
        bio = ""
        if url:
            try:
                r = SESSION.get(url, timeout=35)
                r.raise_for_status()
                bio = extract_author_bio(article, r.text)
            except requests.RequestException as exc:
                log(f"  [!] author bio fetch failed for {article.get('id')}: {exc}")
        if bio:
            rows.append({"id": str(article.get("id", "")), "bio": bio})
        if i % 25 == 0 or i == len(articles):
            log(f"[*] extracted author bios {i}/{len(articles)} ({len(rows)} found)")
        time.sleep(0.15)
    Path(out_path).write_bytes(tomli_w.dumps({"author_bio": rows}).encode())
    return len(rows)
