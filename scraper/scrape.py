#!/usr/bin/env python3
"""
Scrape Gamasutra postmortem features from the Wayback Machine.

Pipeline:
 1. Query the IA CDX API for gamasutra.com/view/feature/* whose urlkey
    contains 'postmortem'. Collapse to one record per article id.
 2. For each article, fetch one archived snapshot (raw, via the `id_` modifier)
    and extract: title, game, authors, publish date, description, category.
 3. Enrich with Hacker News points/comments (Algolia API) and a Wikipedia
    'notable author?' flag.
 4. Emit data/postmortems.toml

Usage:
  python scrape.py --sample 20      # quick sample run
  python scrape.py                   # full run
  python scrape.py --list-only       # just refresh the CDX url list cache
"""
import argparse, json, re, sys, time, urllib.parse
from pathlib import Path
from datetime import datetime
import requests
import tomli_w

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = ROOT / "scraper" / ".cache"
CACHE.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (compatible; GamasutraPostmortemArchive/0.1; +https://github.com/forthrast-com)"
HEAD = {"User-Agent": UA}
MONTHS = "January February March April May June July August September October November December"
_MONTH_ALT = MONTHS.replace(" ", "|")
# old-layout pages put the real publish date in an <i> just under the byline,
# e.g. `<b>Gamasutra</b><br /> <i>December 4 1998`
DATE_OLD_RE = re.compile(
    r"<i>\s*((?:%s)\s+\d{1,2},?\s+(?:19|20)\d{2})" % _MONTH_ALT, re.I)
AUTHOR_RE = re.compile(r'href="[^"]*?/view/authors/\d+/([^"]+?)\.php"[^>]*>([^<]{2,60})<')

SESSION = requests.Session()
SESSION.headers.update(HEAD)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- CDX listing
def fetch_article_list():
    """Return {article_id: {'slug','original'}} for all postmortem features."""
    cache = CACHE / "cdx_postmortems.txt"
    if cache.exists():
        raw = cache.read_text()
    else:
        log("[*] Querying CDX API (slow, ~1-2 min)...")
        url = ("http://web.archive.org/cdx/search/cdx?"
               "url=gamasutra.com/view/feature*&output=text&collapse=urlkey"
               "&filter=urlkey:.*postmortem.*&fl=original,timestamp,statuscode")
        raw = SESSION.get(url, timeout=300).text
        cache.write_text(raw)
    arts = {}
    for line in raw.splitlines():
        parts = line.split(" ")
        if len(parts) < 3:
            continue
        original, ts, status = parts[0], parts[1], parts[2]
        m = re.search(r"/view/feature/(\d+)/([a-z0-9_]+)\.php", original, re.I)
        if not m:
            continue
        # skip ?page= / ?print= variants
        if "?" in original:
            continue
        aid, slug = m.group(1), m.group(2)
        rec = arts.setdefault(aid, {"slug": slug, "original": _clean_url(original),
                                     "captures": 0, "ts": ts, "status": status,
                                     "first_ts": ts})
        rec["captures"] += 1
        if ts < rec["first_ts"]:
            rec["first_ts"] = ts
        # prefer a 200 snapshot timestamp for the parse fetch
        if status == "200" and rec["status"] != "200":
            rec["ts"], rec["status"] = ts, status
    return arts


def _clean_url(u):
    u = u.replace(":80/", "/")
    return u


# ------------------------------------------------------------- article parse
CATEGORY_HINTS = {
    "audio_postmortem": "Audio Postmortem",
    "indie_postmortem": "Indie Postmortem",
    "middleware_postmortem": "Middleware Postmortem",
}


def categorize(slug):
    for k, v in CATEGORY_HINTS.items():
        if k in slug:
            return v
    return "Postmortem"


def parse_article(aid, rec):
    ts = rec["ts"]
    snap = (f"https://web.archive.org/web/{ts}id_/{rec['original']}")
    try:
        r = SESSION.get(snap, timeout=40)
        if r.status_code != 200:
            log(f"  [!] {aid} snapshot {r.status_code}")
            return None
        html = r.text
    except Exception as e:
        log(f"  [!] {aid} fetch error: {e}")
        return None

    # title via og:title or <title>
    title = None
    m = re.search(r'<meta[^>]+og:title[^>]+content="([^"]+)"', html, re.I)
    if m:
        title = m.group(1)
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"^.*?-\s*Features\s*-\s*", "", m.group(1))
    title = strip_title(title) if title else rec["slug"].replace("_", " ").title()

    # description
    desc = ""
    m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html, re.I)
    if m:
        desc = clean(m.group(1))

    # authors (dedupe, preserve order)
    authors, seen = [], set()
    for slug_name, disp in AUTHOR_RE.findall(html):
        name = clean(disp)
        if name and name.lower() not in seen and "gamasutra" not in name.lower():
            seen.add(name.lower()); authors.append(name)

    date = extract_date(html)
    date_estimated = False
    if not date:
        date = ts_to_date(rec.get("first_ts", ts))
        date_estimated = bool(date)

    game = derive_game(title)
    return {
        "id": aid,
        "title": title,
        "game": game,
        "authors": authors,
        "date": date,
        "date_estimated": date_estimated,
        "category": categorize(rec["slug"]),
        "summary": desc,
        "original_url": rec["original"],
        "wayback": f"https://web.archive.org/web/{ts}/{rec['original']}",
        "wayback_captures": rec["captures"],
    }


def strip_title(s):
    s = clean(s)
    # drop leading site/section breadcrumbs: "Gamasutra - Features - Foo" -> "Foo"
    s = re.sub(r"^\s*Gamasutra\s*-\s*(Features\s*-\s*)?", "", s, flags=re.I)
    return s.strip()


def extract_date(html):
    """Real publish date from old-layout byline, else '' (caller falls back)."""
    m = DATE_OLD_RE.search(html)
    return _norm_date(m.group(1)) if m else ""


def ts_to_date(ts):
    try:
        return datetime.strptime(ts[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _norm_date(s):
    s = re.sub(r"\s+", " ", s).strip().replace(",", "")
    for fmt in ("%B %d %Y",):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def derive_game(title):
    t = strip_title(title)
    t = re.sub(r"^(Audio |Indie |Middleware )?Postmortem:?\s*", "", t, flags=re.I)
    # strip leading studio possessive: "Team Meat's Super Meat Boy" -> keep as-is is fine,
    # but "Studio's Game" we keep full; just tidy whitespace.
    return clean(t)


def clean(s):
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return "".join(c for c in s if c.isprintable())


# ------------------------------------------------------------------- HN enrich
def hn_stats(original_url):
    """Best points/comments across HN submissions matching this article URL."""
    # match on the stable path /view/feature/<id>/<slug>
    m = re.search(r"(/view/feature/\d+/[a-z0-9_]+\.php)", original_url, re.I)
    if not m:
        return 0, 0
    q = "gamasutra.com" + m.group(1)
    try:
        r = SESSION.get("https://hn.algolia.com/api/v1/search",
                        params={"query": q, "restrictSearchableAttributes": "url",
                                "hitsPerPage": 20}, timeout=20)
        hits = r.json().get("hits", [])
    except Exception:
        return 0, 0
    path = m.group(1).lower()
    pts = cmts = 0
    for h in hits:
        if path in (h.get("url") or "").lower():
            pts = max(pts, h.get("points") or 0)
            cmts = max(cmts, h.get("num_comments") or 0)
    return pts, cmts


# ------------------------------------------------------------ Wikipedia enrich
_wiki_cache = {}


def author_notable(name):
    if not name:
        return False
    if name in _wiki_cache:
        return _wiki_cache[name]
    notable = False
    try:
        r = SESSION.get("https://en.wikipedia.org/w/api.php",
                        params={"action": "query", "format": "json",
                                "titles": name, "prop": "extracts|categories",
                                "exintro": 1, "explaintext": 1, "redirects": 1},
                        timeout=20)
        pages = r.json().get("query", {}).get("pages", {})
        for _, p in pages.items():
            if "missing" in p:
                continue
            extract = (p.get("extract") or "").lower()
            if any(k in extract for k in
                   ("game", "developer", "designer", "programmer", "studio")):
                notable = True
    except Exception:
        pass
    _wiki_cache[name] = notable
    return notable


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="only process N articles")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--no-enrich", action="store_true", help="skip HN+Wikipedia")
    ap.add_argument("--out", default=str(DATA / "postmortems.toml"))
    args = ap.parse_args()

    arts = fetch_article_list()
    log(f"[*] {len(arts)} distinct postmortem articles found")
    if args.list_only:
        return

    ids = sorted(arts, key=int)
    if args.sample:
        # spread the sample across the id range for variety
        step = max(1, len(ids) // args.sample)
        ids = ids[::step][: args.sample]

    out = []
    for i, aid in enumerate(ids, 1):
        rec = arts[aid]
        log(f"[{i}/{len(ids)}] {aid} {rec['slug'][:50]}")
        art = parse_article(aid, rec)
        if not art:
            continue
        if not args.no_enrich:
            art["hn_points"], art["hn_comments"] = hn_stats(art["original_url"])
            art["author_notable"] = any(author_notable(a) for a in art["authors"])
            time.sleep(0.3)
        else:
            art["hn_points"] = art["hn_comments"] = 0
            art["author_notable"] = False
        # phase-2 placeholders
        art["reddit_points"] = 0
        art["copies_sold"] = ""
        out.append(art)
        time.sleep(0.2)

    out.sort(key=lambda a: (a["date"] or "0000", a["title"]))
    payload = {"postmortem": out}
    Path(args.out).write_bytes(tomli_w.dumps(payload).encode())
    log(f"[*] wrote {len(out)} entries -> {args.out}")


if __name__ == "__main__":
    main()
