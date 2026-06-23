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
import argparse, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
import requests
import tomli_w

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HN_POSTS = DATA / "hn_gamasutra_posts.toml"
CURATED_POSTMORTEMS = DATA / "postmortem_url_includes.toml"
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
# Newer layout date lives under the article body in <td class="newsDate"><strong>...
DATE_NEWS_RE = re.compile(
    r'class="newsDate".*?<strong>\s*((?:%s)\s+\d{1,2},?\s+(?:19|20)\d{2})\s*</strong>' % _MONTH_ALT,
    re.I | re.S)
AUTHOR_RE = re.compile(r'href="[^"]*?/view/authors/\d+/[^"]+?\.php"[^>]*>([^<]{2,60})<')
# byline containers: new layout <span class="newsAuth">by ...</span>,
# old layout <span class="byline">By ...</span>
BYLINE_RE = re.compile(
    r'<span[^>]*class="(?:newsAuth|byline)"[^>]*>(.*?)</span>', re.I | re.S)
GENERIC_TITLES = {"news", "gamasutra", "features",
                  "the art & business of making games"}
# article hero images: new layout /db_area/images/feature/<id>/x.jpg,
# old layout /features/<yyyymmdd>/x.jpg
IMG_RE = re.compile(
    r'src="([^"]*?(?:/db_area/images/feature/\d+/|/features/\d{6,8}/)'
    r'[^"]+?\.(?:jpe?g|png|gif))"', re.I)
IMG_CHROME = re.compile(
    r'(arrowright|spacer|btn_|icon_|header\.gif|_off\.|_on\.|sitelogo|logo|masthead|nav_)',
    re.I,
)

SESSION = requests.Session()
SESSION.headers.update(HEAD)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    return tomllib.loads(Path(path).read_text())


# ---------------------------------------------------------------- CDX listing
def article_record(aid, slug, original, ts=None, status=None, first_ts=None, captures=0):
    return {
        "slug": slug,
        "original": original,
        "captures": captures,
        "ts": ts,
        "status": status,
        "first_ts": first_ts or ts,
    }


def parse_feature_url(url):
    m = re.match(r"https?://[^/]+(/view/feature/(\d+)/([^/?#]+?)(?:\.php)?)(?:[?#].*)?$", url, re.I)
    if not m:
        return None
    path, aid, slug = m.group(1), m.group(2), m.group(3)
    if not path.endswith(".php"):
        path += ".php"
    return aid, slug, "http://www.gamasutra.com" + path


def load_curated_postmortems(path=CURATED_POSTMORTEMS):
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for item in load_toml(path).get("include", []):
        parsed = parse_feature_url(item.get("url", ""))
        if not parsed:
            log(f"  [!] invalid curated postmortem URL: {item.get('url', '')}")
            continue
        aid, slug, original = parsed
        out[aid] = article_record(aid, slug, original)
    return out


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
        m = re.match(r"https?://[^/]+(/view/feature/(\d+)/([a-z0-9_]+)\.php)(.*)$",
                     original, re.I)
        if not m:
            continue
        path, aid, slug, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        # reject malformed rows with junk right after '.php' (e.g. '%22,', '.[31')
        if suffix and not suffix.startswith("?"):
            continue
        # among query variants, reject partial pages (page=2+); allow page=1/print=1
        mp = re.search(r"[?&]page=(\d+)", suffix)
        if mp and mp.group(1) != "1":
            continue
        canonical = "http://www.gamasutra.com" + path
        rec = arts.setdefault(aid, article_record(aid, slug, canonical, first_ts=ts))
        rec["captures"] += 1
        if ts < rec["first_ts"]:
            rec["first_ts"] = ts
        # choose the parse-fetch capture: prefer status 200, then earliest
        better = (
            rec["ts"] is None
            or (status == "200" and rec["status"] != "200")
            or (status == "200" and rec["status"] == "200" and ts < rec["ts"])
        )
        if better:
            rec["ts"], rec["status"] = ts, status

    curated = load_curated_postmortems()
    for aid, rec in curated.items():
        arts.setdefault(aid, rec)
    if curated:
        log(f"[*] loaded {len(curated)} curated postmortem URL includes")
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


def fetch_snapshot(ts, url):
    try:
        r = SESSION.get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=40)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def is_dead_page(html):
    """Post-shutdown landing pages report 200 but have no real article:
    a generic <title> and no byline span."""
    t = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = clean(t.group(1)).lower() if t else ""
    generic = (not title) or title in GENERIC_TITLES or title.endswith("- features")
    has_byline = bool(BYLINE_RE.search(html))
    return generic and not has_byline


def earliest_good_ts(url):
    """Targeted CDX lookup: earliest 200 capture for an exact URL."""
    try:
        q = ("http://web.archive.org/cdx/search/cdx?url="
             + urllib.parse.quote(url, safe="")
             + "&output=text&fl=timestamp&filter=statuscode:200&limit=1")
        out = SESSION.get(q, timeout=60).text.strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def parse_article(aid, rec):
    ts = rec.get("ts")
    if not ts:
        ts = earliest_good_ts(rec["original"])
        rec["ts"] = ts
        rec["first_ts"] = rec.get("first_ts") or ts
    html = fetch_snapshot(ts, rec["original"]) if ts else None
    # If the chosen capture is missing or a dead post-shutdown landing page,
    # fall back to the earliest real 200 capture for this exact URL.
    if html is None or is_dead_page(html):
        alt = earliest_good_ts(rec["original"])
        if alt and alt != ts:
            alt_html = fetch_snapshot(alt, rec["original"])
            if alt_html and not is_dead_page(alt_html):
                ts, html = alt, alt_html
                rec["ts"] = alt
            elif alt < rec["first_ts"]:
                # even a stub earliest capture gives a better date proxy than
                # a misleading recent one
                rec["first_ts"] = alt
            if alt < rec["first_ts"]:
                rec["first_ts"] = alt
    if html is None or is_dead_page(html):
        log(f"  [!] {aid} no usable article snapshot")
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
    title = strip_title(title) if title else ""
    if not title or title.lower() in GENERIC_TITLES:
        title = slug_title(rec["slug"])

    # description
    desc = ""
    m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html, re.I)
    if m:
        desc = clean(m.group(1))

    # authors: only from the byline span (avoids sidebar contributor lists)
    byline = BYLINE_RE.search(html)
    scope = byline.group(1) if byline else ""
    authors, seen = [], set()
    for disp in AUTHOR_RE.findall(scope):
        name = clean(disp)
        if name and name.lower() not in seen and "gamasutra" not in name.lower():
            seen.add(name.lower()); authors.append(name)

    date = extract_date(html)
    date_estimated = False
    if not date:
        date = ts_to_date(rec.get("first_ts", ts))
        date_estimated = bool(date)

    thumb = extract_thumb(html, ts)
    pages = extract_pages(html)
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
        "thumbnail": thumb,
        "original_url": rec["original"],
        "wayback": f"https://web.archive.org/web/{ts}/{rec['original']}",
        "wayback_print": f"https://web.archive.org/web/{ts}/{rec['original']}?print=1",
        "pages": pages,
        "wayback_captures": rec["captures"],
    }


def slug_title(slug):
    t = slug.replace("_", " ").strip(" _")
    t = re.sub(r"\s+", " ", t)
    return t[:1].upper() + t[1:] if t else slug


def strip_title(s):
    s = clean(s)
    # drop leading site/section breadcrumbs: "Gamasutra - Features - Foo" -> "Foo"
    s = re.sub(r"^\s*Gamasutra\s*-\s*(Features\s*-\s*)?", "", s, flags=re.I)
    return s.strip()


def extract_date(html):
    """Real publish date from article chrome, else '' (caller falls back).

    Gamasutra has at least two eras:
    - old layout: sidebar byline has `<i>October 25 2000`
    - newer layout: article body has `<td class="newsDate"><strong>April 14, 2011</strong>`
    Search the newer body marker first to avoid unrelated sidebar dates.
    """
    for rx in (DATE_NEWS_RE, DATE_OLD_RE):
        m = rx.search(html)
        if m:
            return _norm_date(m.group(1))
    return ""


def extract_pages(html):
    """Number of pages in the article, from the 'Page N of M' marker."""
    m = re.search(r"Page\s+\d+\s+of\s+(\d+)", html, re.I)
    return int(m.group(1)) if m else 1


def extract_thumb(html, ts):
    """First article hero image, rewritten to an archived `im_` Wayback URL.
    Prefers a full-size image over an 's'-suffixed thumbnail when both exist."""
    cands = [u for u in IMG_RE.findall(html) if not IMG_CHROME.search(u)]
    if not cands:
        return ""
    # prefer images whose filename does NOT end in 's' before extension
    # (old layout uses e.g. 11post02s.jpg for small thumbs, 11post01.jpg full)
    full = [u for u in cands if not re.search(r"s\.(?:jpe?g|png|gif)$", u, re.I)]
    pick = (full or cands)[0]
    if pick.startswith("/"):
        pick = "http://www.gamasutra.com" + pick
    return f"https://web.archive.org/web/{ts}im_/{pick}"


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
    s = htmllib.unescape(s)            # &amp; -> &, &#039; -> '
    s = re.sub(r"\s+", " ", s).strip()
    return "".join(c for c in s if c.isprintable())


# ------------------------------------------------------------------- HN enrich
HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_PAGE_SIZE = 100
# Algolia only exposes the first ~1000 hits for a query/page window.  Split any
# too-large date range until every leaf is safely pageable.
HN_SAFE_HIT_LIMIT = 900
HN_EARLIEST = int(datetime(2006, 10, 1, tzinfo=timezone.utc).timestamp())


def hn_search(params):
    r = SESSION.get(HN_API, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def hn_query_params(start, end, page=0):
    return {
        "query": "gamasutra.com",
        "restrictSearchableAttributes": "url",
        "tags": "story",
        "hitsPerPage": HN_PAGE_SIZE,
        "page": page,
        "numericFilters": f"created_at_i>={start},created_at_i<{end}",
    }


def fetch_hn_gamasutra_posts(start=HN_EARLIEST, end=None):
    """Return all HN stories whose URL matches gamasutra.com.

    A plain Algolia search silently tops out around the first 1000 hits, which
    made per-article enrichment miss most older/low-ranked submissions.  This
    harvests the whole corpus by recursively partitioning on created_at_i.
    """
    end = end or int(time.time()) + 1
    first = hn_search(hn_query_params(start, end, 0))
    total = first.get("nbHits", 0) or 0
    if total > HN_SAFE_HIT_LIMIT and end - start > 1:
        mid = start + ((end - start) // 2)
        return fetch_hn_gamasutra_posts(start, mid) + fetch_hn_gamasutra_posts(mid, end)

    posts = []
    pages = min(first.get("nbPages", 0) or 0, 10)
    for page in range(pages):
        data = first if page == 0 else hn_search(hn_query_params(start, end, page))
        for hit in data.get("hits", []):
            url = hit.get("url") or ""
            if "gamasutra.com" not in url.lower():
                continue
            posts.append({
                "object_id": str(hit.get("objectID") or ""),
                "title": clean(hit.get("title") or hit.get("story_title") or ""),
                "url": url,
                "created_at": hit.get("created_at") or "",
                "created_at_i": int(hit.get("created_at_i") or 0),
                "points": int(hit.get("points") or 0),
                "num_comments": int(hit.get("num_comments") or 0),
            })
        if page + 1 < pages:
            time.sleep(0.05)
    return posts


def write_hn_posts(path=HN_POSTS):
    posts = fetch_hn_gamasutra_posts()
    by_id = {}
    for post in posts:
        key = post["object_id"] or post["url"]
        old = by_id.get(key)
        if not old or (post["points"], post["num_comments"]) > (old["points"], old["num_comments"]):
            by_id[key] = post
    out = sorted(by_id.values(), key=lambda p: (p["created_at_i"], p["object_id"]))
    payload = {"hn_post": out}
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(tomli_w.dumps(payload).encode())
    return out


def load_hn_posts(path=HN_POSTS):
    path = Path(path)
    if not path.exists():
        log(f"[*] {path} missing; harvesting HN gamasutra posts")
        return write_hn_posts(path)
    return load_toml(path).get("hn_post", [])


def article_hn_keys(article):
    ids = {str(article.get("id", ""))}
    ids.update(str(aid) for aid in article.get("alt_ids", []) or [])
    ids.discard("")

    urls = [article.get("original_url", "")]
    for aid in ids:
        url = article.get("original_url", "")
        if url:
            urls.append(re.sub(r"/view/feature/\d+/", f"/view/feature/{aid}/", url))

    paths = set()
    for url in urls:
        m = re.search(r"(/view/feature/\d+/[a-z0-9_]+\.php)", url, re.I)
        if m:
            paths.add(m.group(1).lower())
    return ids, paths


def hn_feature_ids(url):
    """Feature ids in an HN URL, including bare /view/feature/<id>/ links.

    Some submissions link to Gamasutra's router form with no slug, or to a
    Wayback URL wrapping the original.  The feature id is safer than the slug:
    slugs are often truncated during site migrations, but ids and our alt_ids
    capture the old/new mapping.
    """
    return set(re.findall(r"/view/feature/(\d+)(?:/|$)", url, re.I))


def hn_stats(article, hn_posts):
    """Best points/comments across cached HN submissions matching this article."""
    ids, paths = article_hn_keys(article)
    pts = cmts = 0
    for post in hn_posts:
        url = (post.get("url") or "").lower()
        id_match = bool(ids & hn_feature_ids(url))
        path_match = any(path in url for path in paths)
        if id_match or path_match:
            pts = max(pts, int(post.get("points") or 0))
            cmts = max(cmts, int(post.get("num_comments") or 0))
    return pts, cmts




# --------------------------------------------------------------- local HN audit
def postmortem_ids_from_cdx_cache():
    """Feature ids from the local CDX postmortem URL cache. No network."""
    cache = CACHE / "cdx_postmortems.txt"
    ids = set()
    if not cache.exists():
        return ids
    for line in cache.read_text().splitlines():
        original = line.split(" ", 1)[0] if line.strip() else ""
        m = re.search(r"/view/feature/(\d+)/", original, re.I)
        if m:
            ids.add(m.group(1))
    return ids


def audit_hn_posts(path=HN_POSTS):
    """Write a local audit of cached HN Gamasutra posts vs postmortem URL ids."""
    hn_posts = load_hn_posts(path)
    postmortem_ids = postmortem_ids_from_cdx_cache()

    matched, unmatched_feature, postmortemish_no_feature = [], [], []
    for post in hn_posts:
        ids = hn_feature_ids(post.get("url", ""))
        rec = {
            "object_id": post.get("object_id", ""),
            "title": post.get("title", ""),
            "url": post.get("url", ""),
            "feature_ids": sorted(ids, key=lambda x: int(x) if x.isdigit() else x),
            "points": int(post.get("points") or 0),
            "num_comments": int(post.get("num_comments") or 0),
        }
        if ids & postmortem_ids:
            matched.append(rec)
        elif ids:
            unmatched_feature.append(rec)
        else:
            hay = (post.get("title", "") + " " + post.get("url", "")).lower()
            if any(needle in hay for needle in ("postmortem", "post_mortem", "post-mortem", "post mortem")):
                postmortemish_no_feature.append(rec)

    def by_points(rows):
        return sorted(rows, key=lambda r: (r["points"], r["num_comments"]), reverse=True)

    candidates = []
    curated_ids = set(load_curated_postmortems().keys())
    for rec in by_points(unmatched_feature):
        title = rec["title"].lower()
        if rec["points"] >= 50 and (
            "postmortem" in title
            or "post-mortem" in title
            or "post mortem" in title
            or "half-life" in title
            or "valve" in title
        ):
            rec = rec.copy()
            rec["suggested_include"] = not bool(set(rec["feature_ids"]) & curated_ids)
            candidates.append(rec)
    for rec in by_points(postmortemish_no_feature):
        if rec["points"] >= 10:
            rec = rec.copy()
            rec["suggested_include"] = False
            candidates.append(rec)

    audit = {
        "summary": {
            "hn_posts": len(hn_posts),
            "postmortem_feature_ids": len(postmortem_ids),
            "matched_feature_posts": len(matched),
            "unmatched_feature_posts": len(unmatched_feature),
            "postmortemish_no_feature_posts": len(postmortemish_no_feature),
            "review_candidates": len(candidates),
        },
        "review_candidate": candidates,
        "matched_feature_post": by_points(matched),
        "unmatched_feature_post": by_points(unmatched_feature),
        "postmortemish_no_feature_post": by_points(postmortemish_no_feature),
    }
    out = DATA / "hn_postmortem_audit.toml"
    out.write_bytes(tomli_w.dumps(audit).encode())
    return out, audit["summary"]


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


# --------------------------------------------------------------- de-duplication
def ascii_fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def canonical_title(article):
    title = ascii_fold(article.get("title", "")).lower()
    title = re.sub(r"\W+", " ", title).strip()
    return re.sub(r"^(audio|indie|middleware|mobile|student|tool|faculty)\s+", "", title)


def canonical_authors(article):
    names = []
    for raw in article.get("authors", []) or []:
        # Some migrated pages collapse "A, et al" into one author string.
        raw = re.sub(r",?\s*et\s+al\.?", "", raw, flags=re.I)
        for part in re.split(r"\s+and\s+|\s*,\s*", raw):
            name = re.sub(r"\W+", " ", ascii_fold(part).lower()).strip()
            if name:
                names.append(name)
    return "|".join(sorted(set(names)))


def canonical_slug(article):
    """URL-slug key, used as a fallback for bad migrated captures.

    The CDX listing contains occasional old-ID URLs whose archived content is a
    completely different article. Those records do not title-match their newer
    migrated duplicate, but the slug still identifies the intended article.
    """
    url = article.get("original_url", "")
    m = re.search(r"/view/feature/\d+/([^/?#]+)\.php", url, re.I)
    if not m:
        return ""
    slug = m.group(1).lower()
    slug = re.sub(r"^(audio_|indie_|middleware_)?postmortem_", "", slug)
    slug = re.sub(r"[^a-z0-9]+", " ", slug).strip()
    return slug


def canonical_key(article):
    """Stable-ish content key for old/new-ID duplicates.

    Gamasutra migrated many `/view/feature/<old_id>/...` pages to newer 13xxxx
    IDs. Title is the anchor; normalized authors improve safety but are allowed
    to be empty because no-author migrated duplicates exist too.  If a sparse or
    bad old-ID capture has no useful metadata, fall back to the URL slug so it
    can merge into the richer migrated record instead of polluting the dataset.
    """
    slug = canonical_slug(article)
    title = canonical_title(article)
    authors = canonical_authors(article)
    if title:
        slug_words = [w for w in slug.split() if len(w) > 3]
        title_words = set(title.split())
        overlap = sum(1 for w in slug_words[:4] if w in title_words)
        if slug_words and overlap == 0:
            return "slug::" + slug
        return title + "::" + authors
    if slug:
        return "slug::" + slug
    return "id::" + article.get("id", "")


def score_article(article):
    """Prefer richer, real article records over dead/stub captures."""
    return (
        4 if article.get("authors") else 0,
        3 if not article.get("date_estimated") else 0,
        2 if article.get("summary") else 0,
        1 if article.get("thumbnail") else 0,
        -len(article.get("id", "")),  # old short IDs are nicer canonical URLs
        -(int(article.get("id", "0")) if article.get("id", "0").isdigit() else 0),
    )


def merge_article_group(group):
    best = sorted(group, key=score_article, reverse=True)[0].copy()
    alt_ids = []
    captures = 0
    for art in group:
        captures += art.get("wayback_captures", 0) or 0
        if art["id"] != best["id"]:
            alt_ids.append(art["id"])
        for aid in art.get("alt_ids", []) or []:
            if aid != best["id"]:
                alt_ids.append(aid)
        # fill sparse fields from alternates
        for k in ("summary", "thumbnail", "date", "original_url", "wayback", "wayback_print"):
            if not best.get(k) and art.get(k):
                best[k] = art[k]
        best["hn_points"] = max(best.get("hn_points", 0) or 0, art.get("hn_points", 0) or 0)
        best["hn_comments"] = max(best.get("hn_comments", 0) or 0, art.get("hn_comments", 0) or 0)
        if not best.get("authors") and art.get("authors"):
            best["authors"] = art["authors"]
        if best.get("date_estimated") and art.get("date") and not art.get("date_estimated"):
            best["date"] = art["date"]
            best["date_estimated"] = False
    best["alt_ids"] = sorted(set(alt_ids), key=lambda x: int(x) if x.isdigit() else x)
    best["wayback_captures"] = captures or best.get("wayback_captures", 0)
    return best


def _merge_once(articles, keyfunc):
    groups = {}
    for art in articles:
        groups.setdefault(keyfunc(art), []).append(art)
    return [merge_article_group(g) for g in groups.values()]


def dedupe_articles(articles):
    # First merge obvious title+author duplicates (or slug fallback for bad
    # captures), then merge any remaining old/new-ID pairs that share a slug.
    # The second pass fixes cases where one duplicate parsed as a different
    # article but its URL slug still points at the intended postmortem.
    first = _merge_once(articles, canonical_key)
    return _merge_once(first, lambda art: "slug::" + canonical_slug(art) if canonical_slug(art) else canonical_key(art))


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="only process N articles")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--hn-only", action="store_true", help="refresh data/hn_gamasutra_posts.toml and exit")
    ap.add_argument("--hn-audit", action="store_true", help="audit cached HN posts against local postmortem URL cache and exit")
    ap.add_argument("--hn-posts", default=str(HN_POSTS), help="cached HN gamasutra posts TOML")
    ap.add_argument("--no-enrich", action="store_true", help="skip HN+Wikipedia")
    ap.add_argument("--out", default=str(DATA / "postmortems.toml"))
    args = ap.parse_args()

    if args.hn_only:
        posts = write_hn_posts(args.hn_posts)
        log(f"[*] wrote {len(posts)} HN gamasutra posts -> {args.hn_posts}")
        return
    if args.hn_audit:
        out, summary = audit_hn_posts(args.hn_posts)
        log(f"[*] wrote HN audit -> {out}")
        log(f"[*] {summary}")
        return

    arts = fetch_article_list()
    log(f"[*] {len(arts)} distinct postmortem articles found")
    if args.list_only:
        return

    hn_posts = [] if args.no_enrich else load_hn_posts(args.hn_posts)
    if hn_posts:
        log(f"[*] loaded {len(hn_posts)} HN gamasutra posts")

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
        art["hn_points"] = art["hn_comments"] = 0
        art["author_notable"] = False
        # phase-2 placeholders
        art["reddit_points"] = 0
        art["copies_sold"] = ""
        out.append(art)
        time.sleep(0.2)

    out = dedupe_articles(out)
    if not args.no_enrich:
        for i, art in enumerate(out, 1):
            art["hn_points"], art["hn_comments"] = hn_stats(art, hn_posts)
            art["author_notable"] = any(author_notable(a) for a in art["authors"])
            if i % 25 == 0:
                log(f"[*] enriched {i}/{len(out)} deduped articles")
            time.sleep(0.3)

    out.sort(key=lambda a: (a["date"] or "0000", a["title"]))
    payload = {"postmortem": out}
    Path(args.out).write_bytes(tomli_w.dumps(payload).encode())
    log(f"[*] wrote {len(out)} entries -> {args.out}")


if __name__ == "__main__":
    main()
