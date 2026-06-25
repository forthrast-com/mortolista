#!/usr/bin/env python3
"""
Scrape Gamasutra postmortem features from the Wayback Machine.

Pipeline:
 1. Query the IA CDX API for gamasutra.com/view/feature/* whose urlkey
    contains 'postmortem'. Collapse to one record per article id.
 2. For each article, fetch one archived snapshot (raw, via the `id_` modifier)
    and extract: title, game, authors, publish date, description, category.
 3. Enrich with Hacker News points/comments (Algolia API), Reddit link
    scores, Wikipedia-derived sales signals, and a Wikipedia 'notable author?' flag.
 4. Emit data/postmortems.toml

Usage:
  python scrape.py --sample 20      # quick sample run
  python scrape.py                   # full run
  python scrape.py --list-only       # just refresh the CDX url list cache
"""
import argparse, calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HN_POSTS = DATA / "hn_gamasutra_posts.toml"
HN_METRICS = DATA / "hn_postmortem_threads.toml"
REDDIT_METRICS = DATA / "reddit_postmortem_threads.toml"
REDDIT_POSTS = DATA / "reddit_gamasutra_posts.toml"
WIKI_SALES = DATA / "wikipedia_game_sales.toml"
NOTABLE_AUTHORS = DATA / "notable_authors.toml"
ARCHIVE_MIRRORS = DATA / "archive_is_mirrors.toml"
WAYBACK_LINKS = DATA / "wayback_links.toml"
GAMEDEV_LIVE = DATA / "gamedeveloper_live_urls.toml"
AUTHOR_BIOS = DATA / "author_bios.toml"
CURATED_POSTMORTEMS = DATA / "postmortem_url_includes.toml"
BLOG_CURATION = DATA / "blog_curation.toml"
CACHE = ROOT / "scraper" / ".cache"
CACHE.mkdir(exist_ok=True)
# URLs we've already queried against Arctic Shift. Historical reddit submissions
# for an old article URL don't change, so we never re-request a URL once probed
# (rm this file to force a full re-probe). The cache is tagged with a strategy
# version: bump REDDIT_PROBE_STRATEGY whenever reddit_article_url_queries or the
# Arctic Shift query params change, and the stale cache invalidates itself rather
# than silently skipping URLs the new strategy would have queried differently.
REDDIT_PROBED = CACHE / "reddit_probed_urls.txt"
REDDIT_PROBE_STRATEGY = "v1"

UA = "Mozilla/5.0 (compatible; GamasutraPostmortemArchive/0.1; +https://github.com/forthrast-com)"
HEAD = {"User-Agent": UA}
CHECK_HEAD = {**HEAD, "Accept": "text/html,application/xhtml+xml"}
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
                  "the art & business of making games",
                  # Wayback error/interstitial pages occasionally answer 200 with
                  # this title; treat it as generic so we fall back to the slug.
                  "wayback machine"}
# article hero images: new layout /db_area/images/feature/<id>/x.jpg,
# old layout /features/<yyyymmdd>/x.jpg
IMG_RE = re.compile(
    r'src="([^"]*?(?:/db_area/images/(?:feature|news)/\d+/|/features/\d{6,8}/)'
    r'[^"]+?\.(?:jpe?g|png|gif))"', re.I)
IMG_CHROME = re.compile(
    r'(arrowright|spacer|btn_|icon_|header\.gif|_off\.|_on\.|sitelogo|logo|masthead|nav_)',
    re.I,
)
# Developer-blog (/blogs/) pages are schematically unlike the feature layout:
# no og:image, and the real screenshots live in the post body on third-party
# hosts (imgur, the studio's own site). So we scan *any* <img> in document order
# and skip site chrome / ad / social furniture, taking the first content image.
ANY_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+?\.(?:jpe?g|png|gif))(?:["\'?#]|$)', re.I)
BLOG_IMG_CHROME = re.compile(
    r'(button_|twimgs\.com|/blog/(?:gcgmini|indiemini)|gamasutra_logo|spacer|'
    r'icon_|avatar|gravatar|/ads?/|doubleclick|adw|twitter\.gif|facebook|'
    r'feedburner|/images/[a-z_]+\.gif)',
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
def article_record(aid, slug, original, ts=None, status=None, first_ts=None, captures=0, meta=None):
    return {
        "slug": slug,
        "original": original,
        "captures": captures,
        "ts": ts,
        "status": status,
        "first_ts": first_ts or ts,
        # Optional curated metadata (e.g. multi-part series fields) carried
        # straight through to the emitted article record.
        "meta": meta or {},
    }


# Curated fields an include may carry: series metadata copied verbatim to the
# frontend, plus an optional thumbnail pin (handled specially, with im_ wrapping).
SERIES_META_KEYS = ("series", "series_id", "part_no", "part_total", "part_label")
# game/summary let the blog-curation overrides (data/blog_curation.toml) win over
# the values derived from the title/meta-description — see load_curated_postmortems.
CURATED_META_KEYS = SERIES_META_KEYS + ("thumbnail", "game", "summary")


def parse_feature_url(url):
    # /view/feature/ and /view/news/ share a shape; the "Classic Postmortem"
    # magazine reprints live under /view/news/ (with non-postmortem slugs), which
    # is why the feature sweep never found them — their heroes sit in
    # db_area/images/news/<id>/.
    m = re.match(r"https?://[^/]+(/view/(?:feature|news)/(\d+)/([^/?#]+?)(?:\.php)?)(?:[?#].*)?$", url, re.I)
    if m:
        path, aid, slug = m.group(1), m.group(2), m.group(3)
        if not path.endswith(".php"):
            path += ".php"
        return aid, slug, "http://www.gamasutra.com" + path
    # /blogs/<AuthorCamel>/<YYYYMMDD>/<id>/<slug>.php — the numeric 4th segment
    # is the feature id. Keep the full blog path as the canonical original; the
    # site never migrated these to /view/feature/ form.
    m = re.match(
        r"https?://[^/]+(/blogs/[^/]+/\d{8}/(\d+)/([^/?#]+?)(?:\.php)?)(?:[?#].*)?$",
        url, re.I)
    if m:
        path, aid, slug = m.group(1), m.group(2), m.group(3)
        if not path.endswith(".php"):
            path += ".php"
        return aid, slug, "http://www.gamasutra.com" + path
    # gamedeveloper.com classics: the post-migration site that the magazine
    # "Classic Postmortem" reprints live on. No numeric feature id in the URL and
    # the live page 403s scrapers, so we ingest the Wayback capture and derive a
    # stable id from the slug (prefixed to stay clear of gamasutra ids).
    m = re.match(
        r"https?://(?:www\.)?gamedeveloper\.com/([^?#]+?)/?(?:[?#].*)?$", url, re.I)
    if m:
        path = m.group(1)                      # "<section>/<slug>"
        slug = path.rsplit("/", 1)[-1]
        aid = "gd-" + slug[:60]
        return aid, slug, "https://www.gamedeveloper.com/" + path
    return None


def load_curated_postmortems(path=CURATED_POSTMORTEMS):
    path = Path(path)
    if not path.exists():
        return {}
    curation = load_blog_curation()
    out = {}
    for item in load_toml(path).get("include", []):
        parsed = parse_feature_url(item.get("url", ""))
        if not parsed:
            log(f"  [!] invalid curated postmortem URL: {item.get('url', '')}")
            continue
        aid, slug, original = parsed
        meta = {k: item[k] for k in CURATED_META_KEYS if k in item and item[k] != ""}
        # Blog-curation overrides win over both the include and the derived fields.
        # An empty game (essay/event) is dropped here so the derived title still
        # shows — but the frontend hides a subhead that just echoes the title.
        meta.update(curation.get(aid, {}))
        out[aid] = article_record(aid, slug, original, meta=meta)
    return out


def load_blog_curation(path=BLOG_CURATION):
    """id -> {game?, summary?} overrides for /blogs/ entries (data/blog_curation.toml).
    Empty values are dropped so they never blank out a derived field."""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for e in load_toml(path).get("entry", []):
        aid = e.get("id")
        if not aid:
            continue
        out[aid] = {k: e[k] for k in ("game", "summary") if e.get(k)}
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
    # Pre-wired for Tier C: GDC/video reprints (e.g. "video-postmortem-…",
    # "video-i-pitfall-i-…") sort into their own category when curated in.
    "video_postmortem": "Video Postmortem",
    "video-postmortem": "Video Postmortem",
    "video-i-": "Video Postmortem",
}


def categorize(slug, *, is_blog=False):
    if is_blog:
        return "Contributor Blog"   # provenance is the category for /blogs/ posts
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


def cdx_captures(url, limit=40):
    """Distinct 200/text-html capture timestamps for an exact URL, oldest first."""
    try:
        q = ("http://web.archive.org/cdx/search/cdx?url="
             + urllib.parse.quote(url, safe="")
             + "&output=text&fl=timestamp&filter=statuscode:200"
             + "&filter=mimetype:text/html&collapse=digest&limit=" + str(limit))
        out = SESSION.get(q, timeout=60).text.strip()
        return [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


def capture_renders(html, is_blog=False):
    """A capture is usable only if it actually rendered the article, not merely
    returned a CDX 200 (which also covers Wayback redirect stubs and soft-404s).
    is_dead_page keys on the feature byline, which blogs lack, so for blogs we
    lean on a real <title> plus enough body to be a genuine post."""
    if not html or is_dead_page(html):
        return False
    if is_blog:
        t = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        title = clean(t.group(1)).lower() if t else ""
        if not title or title in GENERIC_TITLES or "wayback machine" in title:
            return False
        return len(html) > 4000
    return True


def verified_good_ts(url, is_blog=False, max_fetches=6):
    """Oldest capture that fetch-verifies as the real article. Walks the capture
    list oldest→newest, fetching each until one renders; returns (ts, html) so
    the caller can reuse the body. Falls back to the oldest 200 if none verify."""
    tss = cdx_captures(url)
    for ts in tss[:max_fetches]:
        html = fetch_snapshot(ts, url)
        if capture_renders(html, is_blog):
            return ts, html
    return (tss[0] if tss else None), None


def parse_article(aid, rec):
    is_blog = bool(blog_url_parts(rec["original"])[0])
    ts = rec.get("ts")
    pre_html = None
    if not ts:
        # No preset timestamp (curated includes, incl. all /blogs/ entries): pick
        # the oldest *verified* capture rather than trusting the first CDX 200.
        ts, pre_html = verified_good_ts(rec["original"], is_blog)
        rec["ts"] = ts
        rec["first_ts"] = rec.get("first_ts") or ts
    html = pre_html if pre_html is not None else (fetch_snapshot(ts, rec["original"]) if ts else None)
    # If the chosen capture is missing or a dead post-shutdown landing page,
    # fall back to the earliest real 200 capture for this exact URL.
    if html is None or is_dead_page(html):
        alt = earliest_good_ts(rec["original"])
        if alt and alt != ts:
            alt_html = fetch_snapshot(alt, rec["original"])
            if alt_html and not is_dead_page(alt_html):
                ts, html = alt, alt_html
                rec["ts"] = alt
            elif not rec.get("first_ts") or alt < rec["first_ts"]:
                # even a stub earliest capture gives a better date proxy than
                # a misleading recent one
                rec["first_ts"] = alt
            if not rec.get("first_ts") or alt < rec["first_ts"]:
                rec["first_ts"] = alt
    if html is None or is_dead_page(html):
        log(f"  [!] {aid} no usable article snapshot")
        return None

    blog_author_camel, blog_date = blog_url_parts(rec["original"])  # is_blog set above

    # title via og:title or <title>
    title = None
    m = re.search(r'<meta[^>]+og:title[^>]+content="([^"]+)"', html, re.I)
    if m:
        title = m.group(1)
    if not title:
        m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"^.*?-\s*Features\s*-\s*", "", m.group(1))
    raw_title = title or ""
    title = strip_title(title) if title else ""
    if not title or title.lower() in GENERIC_TITLES:
        title = slug_title(rec["slug"])

    # description
    desc = strip_tags(extract_meta_content(html, name="description") or extract_meta_content(html, prop="og:description"))

    # authors: only from the byline span (avoids sidebar contributor lists)
    byline = BYLINE_RE.search(html)
    scope = byline.group(1) if byline else ""
    authors, seen = [], set()
    for disp in AUTHOR_RE.findall(scope):
        name = clean(disp)
        if name and name.lower() not in seen and "gamasutra" not in name.lower():
            seen.add(name.lower()); authors.append(name)

    # Developer-blog pages use a different byline layout the feature regexes miss.
    # The blog owner is the author: prefer the "<Name>'s Blog" title segment,
    # falling back to the CamelCase URL segment.
    if is_blog and not authors:
        owner = blog_owner_from_title(raw_title) or split_camel_name(blog_author_camel)
        if owner and "gamasutra" not in owner.lower():
            authors.append(owner)

    # gamedeveloper.com carries no scrapeable byline span, but its JSON-LD names
    # the author (often "Game Developer" for magazine reprints).
    if not authors:
        m = re.search(r'"author"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', html)
        if m:
            name = clean(strip_tags(m.group(1)))
            if name and "gamasutra" not in name.lower():
                authors.append(name)

    date = extract_date(html)
    date_estimated = False
    if not date and is_blog and blog_date:
        # The real publish date is encoded in the /blogs/.../<YYYYMMDD>/ segment.
        date = ts_to_date(blog_date)
    if not date:
        date = ts_to_date(rec.get("first_ts", ts))
        date_estimated = bool(date)

    # A curated include may pin a thumbnail (bare image URL or a ready im_ link);
    # bare URLs get wrapped to the capture's archived form. Otherwise auto-detect.
    curated_thumb = (rec.get("meta") or {}).get("thumbnail", "")
    thumb = wrap_im(curated_thumb, ts) if curated_thumb else extract_thumb(html, ts, is_blog=is_blog)
    pages = extract_pages(html)
    game = derive_game(title)
    article = {
        "id": aid,
        "title": title,
        "game": game,
        "authors": authors,
        "date": date,
        "date_estimated": date_estimated,
        "category": categorize(rec["slug"], is_blog=is_blog),
        "summary": desc,
        "thumbnail": thumb,
        "original_url": rec["original"],
        "wayback": f"https://web.archive.org/web/{ts}/{rec['original']}",
        # The print link is NOT fabricated from the base ts here: it's verified on
        # its own terms in the wayback_links sidecar (refresh_wayback_links), which
        # the frontend overlays. Baking a base-ts ?print=1 URL read as live even
        # when the print page was never archived.
        "pages": pages,
        "wayback_captures": rec["captures"],
    }
    # Carry curated series metadata (multi-part postmortems) onto the article.
    # thumbnail is handled above (with im_ wrapping), so don't re-copy it raw.
    for key, value in (rec.get("meta") or {}).items():
        if key != "thumbnail" and value != "":
            article[key] = value
    return article


def extract_meta_content(html, *, name=None, prop=None):
    """Extract a meta content value, tolerating old Gamasutra's unescaped quotes.

    Some archived descriptions contain literal `"Game of the Year"`-style quotes
    inside the content attribute. A normal `[^"]*` regex truncates those; taking
    the last quote before the tag close recovers the whole legacy value.
    """
    wanted = ("name", name.lower()) if name else ("property", prop.lower())
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I | re.S):
        attr_re = r"\b" + re.escape(wanted[0]) + r"\s*=\s*['\"]" + re.escape(wanted[1]) + r"['\"]"
        if not re.search(attr_re, tag, re.I):
            continue
        m = re.search(r'\bcontent\s*=\s*"(.*)"\s*/?\s*>$', tag, re.I | re.S)
        if m:
            return clean(m.group(1))
        m = re.search(r"\bcontent\s*=\s*'([^']*)'", tag, re.I | re.S)
        if m:
            return clean(m.group(1))
    return ""


def slug_title(slug):
    t = slug.replace("_", " ").strip(" _")
    t = re.sub(r"\s+", " ", t)
    return t[:1].upper() + t[1:] if t else slug


def strip_tags(s):
    """Drop inline HTML tags (gamedeveloper.com og:titles/summaries carry literal
    <i>…</i> around game names); gamasutra titles have none, so this is a no-op there."""
    return re.sub(r"<[^>]+>", "", s or "")


def strip_title(s):
    s = clean(strip_tags(s))
    # drop leading site/section breadcrumbs: "Gamasutra - Features - Foo" -> "Foo"
    s = re.sub(r"^\s*Gamasutra\s*-\s*(Features\s*-\s*)?", "", s, flags=re.I)
    # developer-blog breadcrumb: "Gamasutra: Jane Doe's Blog - Real Title" -> "Real Title"
    s = re.sub(r"^\s*Gamasutra:\s*.*?'s\s+Blog\s*-\s*", "", s, flags=re.I)
    return s.strip()


def blog_url_parts(url):
    """(author_camel, yyyymmdd) for a /blogs/ URL, else (None, None)."""
    m = re.search(r"/blogs/([^/]+)/(\d{8})/\d+/", url or "", re.I)
    return (m.group(1), m.group(2)) if m else (None, None)


def split_camel_name(camel):
    """'PhilTibitoski' -> 'Phil Tibitoski'. Best-effort; leaves odd casing alone."""
    if not camel:
        return ""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", camel)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return clean(spaced)


def blog_owner_from_title(title):
    """Author from a blog <title>: 'Gamasutra: Jane Doe's Blog - …' -> 'Jane Doe'."""
    m = re.search(r"Gamasutra:\s*(.*?)'s\s+Blog\s*-", title or "", re.I)
    return clean(m.group(1)) if m else ""


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
    # gamedeveloper.com (Contentstack) exposes the real republish date in JSON-LD
    # — the only reliable date on those pages, which otherwise look ~2021 (capture).
    m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', html)
    if m:
        return m.group(1)
    return ""


def extract_pages(html):
    """Number of pages in the article, from the 'Page N of M' marker."""
    m = re.search(r"Page\s+\d+\s+of\s+(\d+)", html, re.I)
    return int(m.group(1)) if m else 1


def wrap_im(url, ts):
    """Rewrite a page-relative or absolute image URL to its archived `im_` form.
    Passes through a URL that is already a Wayback link (avoids double-wrapping)."""
    if not url:
        return ""
    if "web.archive.org" in url:
        return url
    if url.startswith("//"):
        url = "http:" + url
    elif url.startswith("/"):
        url = "http://www.gamasutra.com" + url
    if not url.startswith(("http://", "https://")):
        return ""
    return f"https://web.archive.org/web/{ts}im_/{url}"


def wayback_image_ok(url):
    """True if an `im_` image URL actually resolves to archived image bytes —
    blogs lean on third-party hosts (imgur, studio sites) that the Archive may
    or may not have captured, so a body <img> is only usable once verified."""
    try:
        r = SESSION.get(url, timeout=25, stream=True)
        ok = r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image")
        r.close()
        return ok
    except Exception:
        return False


def first_blog_body_image(html, ts, verify=True):
    """First content image in a blog post body, skipping site/ad/social chrome,
    rewritten through `im_` and (by default) verified as archived."""
    seen = set()
    for src in ANY_IMG_RE.findall(html):
        if src in seen:
            continue
        seen.add(src)
        if BLOG_IMG_CHROME.search(src) or IMG_CHROME.search(src):
            continue
        cand = wrap_im(src, ts)
        if cand and (not verify or wayback_image_ok(cand)):
            return cand
    return ""


def extract_thumb(html, ts, is_blog=False):
    """Article hero image, rewritten to an archived `im_` Wayback URL.

    Order: og:image (modern feature layout) → for blogs, the first verified
    content image in the post body (their screenshots live there, off-site) →
    the in-body feature hero IMG_RE knows, preferring a full-size file over an
    's'-suffixed thumbnail."""
    pick = extract_meta_content(html, prop="og:image")
    if pick and IMG_CHROME.search(pick):
        pick = ""  # the default site/logo og:image is no better than nothing
    if not pick and is_blog:
        blog_img = first_blog_body_image(html, ts)
        if blog_img:
            return blog_img  # already wrapped + verified
    if not pick:
        cands = [u for u in IMG_RE.findall(html) if not IMG_CHROME.search(u)]
        if cands:
            # prefer images whose filename does NOT end in 's' before extension
            # (old layout uses e.g. 11post02s.jpg for small thumbs, 11post01.jpg full)
            full = [u for u in cands if not re.search(r"s\.(?:jpe?g|png|gif)$", u, re.I)]
            pick = (full or cands)[0]
    return wrap_im(pick, ts) if pick else ""


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


BIO_VERBS_RE = re.compile(
    r"\b(is|are|was|were|has|have|worked|works|founded|co-founded|serves|served|"
    r"joined|created|designed|developed|produced|wrote|writes|currently|previously|"
    r"can be reached|email|twitter)\b",
    re.I,
)
ARTICLE_WORDS_RE = re.compile(r"\b(postmortem|development|publisher|platform|engine|gameplay|project|team)\b", re.I)
RETURN_TO_FULL_RE = re.compile(r"(?is)<p[^>]*>\s*<a[^>]+>\s*Return to the full version.*")
PARA_RE = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>")
TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")


def clean_html_text(s):
    s = re.sub(r"(?i)<br\s*/?>", " ", s or "")
    s = TAG_RE.sub(" ", s)
    return clean(s)


def article_paragraphs(html):
    html = RETURN_TO_FULL_RE.split(html)[0]
    return [clean_html_text(p) for p in PARA_RE.findall(html)]


def author_tokens(name):
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    return [name.lower(), parts[-1].lower()] if parts else []


def looks_like_author_bio(text, authors):
    if not 45 <= len(text) <= 900:
        return False
    low = text.lower()
    if not any(tok and tok in low for name in authors for tok in author_tokens(name)):
        return False
    if not BIO_VERBS_RE.search(text):
        return False
    # Avoid regular article paragraphs that only mention a developer in passing.
    if len(text) > 360 and len(ARTICLE_WORDS_RE.findall(text)) > 4:
        return False
    return True


def trim_author_bio(text):
    text = clean(text)
    if len(text) <= 520:
        return text
    cut = text[:520].rsplit(". ", 1)[0]
    return (cut or text[:517].rstrip()) + "…"


def tighten_author_bio(text, authors):
    """Drop article-closing sentences that precede the actual bio."""
    sentences = re.split(r"(?<=[.!?])\s+", clean(text))
    tokens = [tok for name in authors for tok in author_tokens(name)]
    for i, sentence in enumerate(sentences):
        low = sentence.lower()
        if any(tok and tok in low for tok in tokens) and BIO_VERBS_RE.search(sentence):
            return " ".join(sentences[i:])
    return text


def extract_author_bio(article, html):
    authors = article.get("authors") or []
    if not authors:
        return ""
    candidates = []
    for idx, para in enumerate(article_paragraphs(html)):
        if looks_like_author_bio(para, authors):
            candidates.append((idx, para))
    if not candidates:
        # Old print pages sometimes leave the final bio loose in the body rather
        # than wrapping it in <p>. Mine only the end matter, not the full article.
        tail = clean_html_text(RETURN_TO_FULL_RE.split(html)[0][-3500:])
        sentences = re.split(r"(?<=[.!?])\s+", tail)
        for i in range(len(sentences)):
            chunk = " ".join(sentences[i:i + 4]).strip()
            if looks_like_author_bio(chunk, authors):
                candidates.append((10_000 + i, chunk))
    if not candidates:
        return ""
    return trim_author_bio(tighten_author_bio(candidates[-1][1], authors))


# --------------------------------------------------------------- link checks
def http_exists(url, *, allow_redirect=True, timeout=20):
    if not url:
        return False, ""
    try:
        r = SESSION.get(url, headers=CHECK_HEAD, timeout=timeout,
                        allow_redirects=allow_redirect, stream=True)
        final = r.url
        ok = 200 <= r.status_code < 400
        r.close()
        return ok, final
    except requests.RequestException:
        return False, ""


def wayback_available(url):
    ok, final = http_exists(url, allow_redirect=True)
    if not ok:
        return False
    return "/web/" in urllib.parse.urlparse(final).path


def archive_today_url(original_url, host="archive.is"):
    return f"https://{host}/newest/{urllib.parse.quote(original_url, safe='')}"


def print_url(original_url):
    if not original_url:
        return ""
    sep = "&" if "?" in original_url else "?"
    return original_url if re.search(r"[?&]print=1(?:[#&]|$)", original_url) else original_url + sep + "print=1"


def wayback_print_capture(original_url, tries=2):
    """Earliest real 200 capture of the *print* variant, found by its own CDX
    lookup. The catalogue used to fabricate the print link from the base
    capture's timestamp, which read as live even when the ?print=1 page was never
    archived; this verifies the print page on its own terms. Returns a capture
    timestamp, or None when the print variant genuinely isn't archived.

    A flaky CDX returns empty just like a genuine absence, so retry a couple of
    times -- callers never cache a None, but this keeps a sick Archive from
    spuriously dropping print links that do exist."""
    pu = print_url(original_url)
    if not pu:
        return None
    for attempt in range(tries):
        ts = earliest_good_ts(pu)
        if ts:
            return ts
        if attempt + 1 < tries:
            time.sleep(1.5)
    return None


ARCHIVE_TODAY_HOSTS = ("archive.ph", "archive.today", "archive.is", "archive.vn")


def is_archive_today_host(netloc):
    return any(h in netloc.lower() for h in ARCHIVE_TODAY_HOSTS)


def archive_today_status(url, timeout=25):
    """Three-state liveness for an archive.is /newest/ mirror URL.

    archive.is 429s automated checks, so a plain boolean can't tell "no snapshot"
    from "we got rate-limited". Distinguish:
      - "ok":      /newest/ redirected to a real snapshot permalink (dated path,
                   no longer /newest/) on an archive.* host -> a capture exists.
      - "absent":  archive.is answered but kept us on the /newest/ submission page
                   -> it has nothing archived for this URL.
      - "unknown": 429 / timeout / network flak -> we genuinely can't tell; the
                   caller should preserve whatever it knew before rather than
                   flipping a known-good mirror to dead.
    """
    if not url:
        return "absent"
    try:
        r = SESSION.get(url, headers=CHECK_HEAD, timeout=timeout,
                        allow_redirects=True, stream=True)
        status, final = r.status_code, r.url
        r.close()
    except requests.RequestException:
        return "unknown"
    parsed = urllib.parse.urlparse(final)
    # URL *shape* is the reliable signal, not the final status code: when a
    # capture exists archive.is redirects /newest/ to a dated permalink
    # (/20351231.../ or a shortcode) -- even if that final hop then 429s us, the
    # redirect already proved the mirror exists. Only a still-on-/newest/ landing
    # or a 404 means there's genuinely nothing archived.
    if is_archive_today_host(parsed.netloc) and parsed.path.strip("/") and "/newest/" not in parsed.path:
        return "ok"
    if status == 404:
        return "absent"
    if status == 429 or status >= 500:
        return "unknown"
    if 200 <= status < 400:
        return "absent"  # answered, but never left the /newest/ submission form
    return "unknown"


def archive_today_available(url):
    return archive_today_status(url) == "ok"


def original_available(url):
    ok, final = http_exists(url, allow_redirect=True, timeout=20)
    if not ok:
        return False
    parsed = urllib.parse.urlparse(final)
    if "gamedeveloper.com" in parsed.netloc.lower() and parsed.path.rstrip("/") in ("", "/", "/latest-news"):
        return False
    return True


_gamedev_sitemap_cache = {}


def gamedev_archive_sitemap_urls(article):
    date = article.get("date", "")
    if not re.match(r"\d{4}-\d{2}-\d{2}$", date):
        return []
    year = int(date[:4])
    month = calendar.month_name[int(date[5:7])].lower()
    return [f"https://www.gamedeveloper.com/article/archive/{year}/{month}.xml"]


def gamedev_live_candidates(article):
    candidates = []
    for sitemap_url in gamedev_archive_sitemap_urls(article):
        if sitemap_url not in _gamedev_sitemap_cache:
            try:
                r = SESSION.get(sitemap_url, headers=CHECK_HEAD, timeout=30)
                _gamedev_sitemap_cache[sitemap_url] = r.text if r.status_code == 200 else ""
            except requests.RequestException:
                _gamedev_sitemap_cache[sitemap_url] = ""
        for href in re.findall(r"<loc>(.*?)</loc>", _gamedev_sitemap_cache[sitemap_url], re.I):
            href = htmllib.unescape(href).strip()
            if "gamedeveloper.com" not in href:
                continue
            text = urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])
            text = re.sub(r"[-_]+", " ", text)
            candidates.append((href, text))
    return candidates


def find_live_gamedeveloper_url(article):
    title = canonical_title(article)
    slug = canonical_slug(article)
    best = (0.0, "")
    seen = set()
    for href, text in gamedev_live_candidates(article):
        if href in seen or "/keyword/" in href or "/search" in href:
            continue
        seen.add(href)
        text_key = re.sub(r"\W+", " ", ascii_fold(text).lower()).strip()
        score = SequenceMatcher(None, title, text_key).ratio() if title else 0
        if slug:
            score = max(score, SequenceMatcher(None, slug, text_key).ratio())
        if score > best[0]:
            best = (score, href)
    if best[0] < 0.62:
        return ""
    # GameDeveloper redirects missing articles to section indexes with HTTP 200.
    # Sitemap URLs are the canonical source here; do a cheap sanity check without
    # following those soft-404 redirects away from the candidate path.
    ok, final = http_exists(best[1], allow_redirect=False, timeout=20)
    if not ok:
        return ""
    path = urllib.parse.urlparse(best[1]).path.rstrip("/")
    if path in ("", "/", "/business", "/design", "/programming", "/latest-news"):
        return ""
    return best[1]


def check_article_links(article):
    article["wayback_ok"] = wayback_available(article.get("wayback", ""))
    # Verify the print capture on its own terms (the wayback_links sidecar is the
    # canonical home for this; kept consistent here so --check-links doesn't
    # diverge or lean on a fabricated base-ts print URL).
    ts = wayback_print_capture(article.get("original_url", ""))
    article["wayback_print"] = (
        f"https://web.archive.org/web/{ts}/{print_url(article.get('original_url',''))}" if ts else "")
    article["wayback_print_ok"] = bool(ts)
    article["original_ok"] = original_available(article.get("original_url", ""))
    return article


def _mirror_ok(status, prior, key):
    """Resolve a three-state archive.is probe to a stored boolean. On "unknown"
    (429/flak) keep the prior value if we had one; otherwise stay False — the
    frontend still offers the bare /newest/ link, so we lose no fallback, we just
    don't *claim* a capture we never confirmed."""
    if status == "ok":
        return True
    if status == "absent":
        return False
    return bool(prior.get(key, False)) if prior else False


def archive_mirror_row(article, prior=None, recheck=False):
    """One archive.is mirror row, with a sticky per-URL cache.

    archive.is is the touchiest provider here (it 429s automated checks), and a
    mirror's existence doesn't change between runs -- so once a URL resolves to a
    definitive "ok"/"absent" we reuse that verdict instead of re-requesting it.
    We only (re)probe when forced (recheck), when there's no prior verdict, or
    when the prior was inconclusive ("unknown", i.e. a 429/flak last time). The
    returned row carries a transient "_probed" flag so the caller throttles only
    on requests it actually made.
    """
    prior = prior or {}
    original = article.get("original_url", "")
    az = archive_today_url(original)
    az_print = archive_today_url(print_url(original))

    def resolve(target, url_key, st_key, ok_key):
        cached = (not recheck and prior.get(url_key) == target
                  and prior.get(st_key) in ("ok", "absent"))
        if cached:
            return prior[st_key], bool(prior.get(ok_key, False)), False
        st = archive_today_status(target)
        return st, _mirror_ok(st, prior, ok_key), True

    st, ok, probed = resolve(az, "archive_today", "archive_today_state", "archive_today_ok")
    if az_print == az:
        st_print, ok_print, probed_print = st, ok, False
    else:
        st_print, ok_print, probed_print = resolve(
            az_print, "archive_today_print", "archive_today_print_state", "archive_today_print_ok")
    return {
        "id": article["id"],
        "archive_today": az,
        "archive_today_ok": ok,
        "archive_today_state": st,
        "archive_today_print": az_print,
        "archive_today_print_ok": ok_print,
        "archive_today_print_state": st_print,
        "_probed": probed or probed_print,
    }


def gamedev_live_row(article):
    live = find_live_gamedeveloper_url(article)
    return {"id": article["id"], "live_url": live, "live_ok": bool(live)}


def wayback_link_row(article, prior=None, recheck=False):
    """Wayback liveness sidecar row: the canonical capture and the print variant
    checked *independently*, so the frontend reflects each on its own terms.

    The Internet Archive is touchy/flaky, and a capture that exists doesn't
    vanish, so a confirmed-live verdict is sticky -- reused from the prior sidecar
    instead of re-requested. We never cache a negative (a flaked lookup or a
    not-yet-archived print page should retry next run). recheck busts the cache.
    The row carries a transient "_probed" flag so the caller throttles only on
    requests it actually made.
    """
    prior = prior or {}
    original = article.get("original_url", "")
    probed = False

    # Canonical capture count + liveness. Features already carry a count from the
    # bulk CDX sweep; the curated entries (blogs/Tier B) were never swept and sit
    # at zero, leaving them invisible on the captures axis. Reuse an existing or
    # cached count and only hit CDX to fill a genuine zero. captures>0 == live.
    cached_caps = prior.get("wayback_captures") if not recheck else None
    if cached_caps is None:
        cached_caps = article.get("wayback_captures", 0) or 0
    if cached_caps > 0:
        wb_caps = cached_caps
    else:
        wb_caps = len(cdx_captures(original)) if original else 0
        probed = True
    wb_ok = wb_caps > 0

    # Print variant, counted by its own CDX (sticky once confirmed live).
    if not recheck and prior.get("wayback_print_ok") and prior.get("wayback_print"):
        wp, wp_ok, wp_caps = prior["wayback_print"], True, prior.get("wayback_print_captures", 0)
    else:
        pcaps = cdx_captures(print_url(original)) if original else []
        wp = f"https://web.archive.org/web/{pcaps[0]}/{print_url(original)}" if pcaps else ""
        wp_ok = bool(pcaps)
        wp_caps = len(pcaps)
        probed = True

    return {
        "id": article["id"],
        "wayback_captures": wb_caps,
        "wayback_print_captures": wp_caps,
        "wayback_ok": wb_ok,
        "wayback_print": wp,
        "wayback_print_ok": wp_ok,
        "_probed": probed,
    }


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
        if not m:
            m = re.search(r"(/blogs/[^/]+/\d{8}/\d+/[a-z0-9_]+\.php)", url, re.I)
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
    ids = set(re.findall(r"/view/feature/(\d+)(?:/|$)", url, re.I))
    # /blogs/<author>/<YYYYMMDD>/<id>/... — the id is the segment after the date.
    ids.update(re.findall(r"/blogs/[^/]+/\d{8}/(\d+)(?:/|$)", url, re.I))
    return ids


def hn_stats(article, hn_posts):
    """HN attention across cached submissions matching this article.

    `hn_points`/`hn_comments` are the peak individual thread, useful for
    finding the best discussion.  The *_sum fields capture total attention
    across duplicate submissions, because HN often resurfaces old Gamasutra
    URLs under migrated ids, print views, or Wayback links.
    """
    ids, paths = article_hn_keys(article)
    pts = cmts = total_pts = total_cmts = submissions = 0
    seen = set()
    for post in hn_posts:
        url = (post.get("url") or "").lower()
        id_match = bool(ids & hn_feature_ids(url))
        path_match = any(path in url for path in paths)
        if not (id_match or path_match):
            continue
        key = post.get("object_id") or url
        if key in seen:
            continue
        seen.add(key)
        p = int(post.get("points") or 0)
        c = int(post.get("num_comments") or 0)
        pts = max(pts, p)
        cmts = max(cmts, c)
        total_pts += p
        total_cmts += c
        submissions += 1
    threads = []
    for key in seen:
        # second pass below keeps output stable and sorted by thread weight.
        pass
    matched_threads = []
    for post in hn_posts:
        url = (post.get("url") or "").lower()
        id_match = bool(ids & hn_feature_ids(url))
        path_match = any(path in url for path in paths)
        if not (id_match or path_match):
            continue
        key = post.get("object_id") or url
        if key not in seen:
            continue
        matched_threads.append({
            "object_id": str(post.get("object_id") or ""),
            "title": post.get("title") or "",
            "url": f"https://news.ycombinator.com/item?id={post.get('object_id')}",
            "points": int(post.get("points") or 0),
            "comments": int(post.get("num_comments") or 0),
            "submitted_url": post.get("url") or "",
        })
    # Deduplicate while preserving the strongest copy if Algolia returned dupes.
    by_id = {}
    for thread in matched_threads:
        key = thread["object_id"] or thread["submitted_url"]
        old = by_id.get(key)
        if not old or (thread["points"], thread["comments"]) > (old["points"], old["comments"]):
            by_id[key] = thread
    threads = sorted(by_id.values(), key=lambda t: (t["points"], t["comments"]), reverse=True)
    return {
        "hn_points": pts,
        "hn_comments": cmts,
        "hn_points_sum": total_pts,
        "hn_comments_sum": total_cmts,
        "hn_submissions": submissions,
        "hn_threads": threads,
    }



# ------------------------------------------------------------- Reddit enrich
# Reddit submissions are harvested in bulk from the Arctic Shift archive
# (https://arctic-shift.photon-reddit.com), which indexes historical Reddit
# posts by linked URL.  One query per Gamasutra/GameDeveloper host variant
# returns posts that linked to either home for these articles; those are cached
# and matched by feature id, canonical path, or migrated live URL.
REDDIT_API = "https://arctic-shift.photon-reddit.com/api/posts/search"
REDDIT_FIELDS = "id,subreddit,author,title,score,num_comments,created_utc,url"
# Posts link to the site under both schemes and with/without www; Arctic Shift
# matches on the supplied URL, so query each variant and dedupe by post id.
REDDIT_URL_QUERIES = [
    "https://www.gamasutra.com",
    "http://www.gamasutra.com",
    "https://gamasutra.com",
    "http://gamasutra.com",
    "https://www.gamedeveloper.com",
    "http://www.gamedeveloper.com",
    "https://gamedeveloper.com",
    "http://gamedeveloper.com",
]


def reddit_api_posts_for_url(url, timeout=120):
    params = {"url": url, "limit": "auto", "fields": REDDIT_FIELDS}
    r = SESSION.get(REDDIT_API, params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def add_reddit_api_rows(by_id, items):
    for it in items or []:
        rid = str(it.get("id") or "")
        if not rid:
            continue
        row = {
            "id": rid,
            "subreddit": it.get("subreddit") or "",
            "author": it.get("author") or "",
            "title": clean(it.get("title") or ""),
            "score": int(it.get("score") or 0),
            "num_comments": int(it.get("num_comments") or 0),
            "created_utc": int(it.get("created_utc") or 0),
            "url": it.get("url") or "",
        }
        old = by_id.get(rid)
        # The same post can surface under several URL variants; keep the
        # highest-scored copy (scores drift between captures).
        if not old or row["score"] > old["score"]:
            by_id[rid] = row


def reddit_article_url_queries(article):
    """Exact article URL forms worth probing in Arctic Shift.

    Arctic Shift's broad host search is shallow: exact URL queries find older
    postmortem submissions that never appear in the host-wide result set.
    """
    urls = []
    original = article.get("original_url") or ""
    if original:
        ids = [str(article.get("id") or "")]
        ids.extend(str(aid) for aid in article.get("alt_ids", []) or [])
        ids = [aid for aid in dict.fromkeys(ids) if aid]
        for aid in ids:
            rewritten = re.sub(r"/view/feature/\d+/", f"/view/feature/{aid}/", original)
            parsed = urllib.parse.urlparse(rewritten)
            host = parsed.netloc.lower()
            path = parsed.path
            base = urllib.parse.urlunparse((parsed.scheme or "http", host, path, "", "", ""))
            urls.append(base)
    live = article.get("live_url") or ""
    if live:
        urls.append(live)
        parsed = urllib.parse.urlparse(live)
        if parsed.netloc.startswith("www."):
            urls.append(urllib.parse.urlunparse((parsed.scheme, parsed.netloc[4:], parsed.path, "", parsed.query, "")))
    seen = set()
    out = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def load_probed_urls(path=REDDIT_PROBED, strategy=REDDIT_PROBE_STRATEGY):
    """Probed-URL set, but only if it was written under the current probe strategy.
    A version mismatch (someone changed how we build/query URLs) discards the cache
    so every URL is re-probed under the new strategy instead of wrongly skipped."""
    try:
        lines = Path(path).read_text().split("\n")
    except FileNotFoundError:
        return set()
    if not lines or lines[0] != f"# strategy: {strategy}":
        log("[*] reddit probe cache strategy changed -> re-probing all URLs")
        return set()
    return {u for u in lines[1:] if u.strip()}


def save_probed_urls(urls, path=REDDIT_PROBED, strategy=REDDIT_PROBE_STRATEGY):
    Path(path).write_text(f"# strategy: {strategy}\n" + "\n".join(sorted(urls)) + "\n")


def fetch_reddit_gamasutra_posts(url_queries=REDDIT_URL_QUERIES, articles=None, reprobe=False):
    """Reddit posts linking to Gamasutra/GameDeveloper, via Arctic Shift.

    The per-article exact-URL probes are the expensive part and Arctic Shift's
    answer for an old URL doesn't change, so we cache which URLs we've probed and
    skip them on later runs. To make skipping safe (not lose posts we already
    found) we seed from the existing post cache first -- the probed-URL cache only
    avoids re-*requesting*, never drops data. Pass reprobe=True to ignore it.
    """
    by_id = {}
    # Seed with everything we cached before so skipped URLs keep their posts.
    for row in load_toml(REDDIT_POSTS).get("reddit_post", []):
        rid = str(row.get("id") or "")
        if rid:
            by_id[rid] = row
    for base in url_queries:
        try:
            add_reddit_api_rows(by_id, reddit_api_posts_for_url(base))
        except requests.RequestException as exc:
            log(f"  [!] arctic-shift query failed for {base}: {exc}")
            continue
        time.sleep(0.5)

    articles = articles or []
    probed = set() if reprobe else load_probed_urls()
    newly, done, skipped = set(), 0, 0
    for i, article in enumerate(articles, 1):
        for url in reddit_article_url_queries(article):
            if url in probed:
                skipped += 1
                continue
            done += 1
            try:
                add_reddit_api_rows(by_id, reddit_api_posts_for_url(url, timeout=45))
                newly.add(url)  # mark probed only on success, so flak retries next run
            except requests.RequestException as exc:
                log(f"  [!] arctic-shift article query failed for {url}: {exc}")
            time.sleep(0.12)
        if articles and (i % 25 == 0 or i == len(articles)):
            log(f"[*] reddit exact URL probes {i}/{len(articles)} articles "
                f"({done} probed, {skipped} cached-skip)")
    save_probed_urls(probed | newly)
    return sorted(by_id.values(), key=lambda p: p["score"], reverse=True)


def write_reddit_posts(path=REDDIT_POSTS, articles=None):
    posts = fetch_reddit_gamasutra_posts(articles=articles)
    if not posts:
        raise RuntimeError("arctic-shift returned no gamasutra posts; not writing an empty cache")
    Path(path).write_bytes(tomli_w.dumps({"reddit_post": posts}).encode())
    log(f"[*] cached {len(posts)} reddit article-link posts -> {path}")
    return posts


def load_reddit_posts(path=REDDIT_POSTS, refresh=False, articles=None):
    if refresh or not Path(path).exists():
        return write_reddit_posts(path, articles=articles)
    return load_toml(path).get("reddit_post", [])


def strip_wayback_url(url):
    """Return the wrapped target for Wayback URLs, otherwise the original URL."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.netloc.lower() not in {"web.archive.org", "archive.org"}:
        return url or ""
    m = re.match(r"^/web/\d+(?:[a-z_]+)?/(https?://.+)$", parsed.path, re.I)
    return urllib.parse.unquote(m.group(1)) if m else (url or "")


def canonical_discussion_url(url):
    """Stable matching key for submitted article URLs."""
    url = strip_wayback_url(url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path).rstrip("/").lower()
    if path.endswith(".php"):
        path = path[:-4]
    return f"{host}{path}"


def reddit_match_keys(article):
    """IDs and canonical URL paths that identify an article on Reddit.

    Redditors linked both old Gamasutra feature URLs and migrated
    GameDeveloper URLs.  The primary catalogue does not embed live URLs, so we
    merge the live sidecar before matching and key on every known article URL.
    """
    ids, paths = article_hn_keys(article)
    url_fields = (
        "original_url", "wayback", "wayback_print", "live_url",
        "archive_today", "archive_today_print",
    )
    url_keys = {canonical_discussion_url(article.get(k, "")) for k in url_fields}
    url_keys.discard("")
    return ids, paths, url_keys


def reddit_post_matches_article(post_url, ids, paths, url_keys):
    url = (post_url or "").lower()
    canonical = canonical_discussion_url(post_url)
    return (
        bool(ids & hn_feature_ids(url))
        or any(p in url for p in paths)
        or (canonical and canonical in url_keys)
    )


def reddit_stats(article, reddit_posts):
    """Reddit attention across cached posts that link to this article.

    A post counts when its linked URL carries one of the article's old
    Gamasutra feature ids/paths or exactly matches a known canonical URL form,
    including migrated GameDeveloper URLs from the live sidecar.
    """
    ids, paths, url_keys = reddit_match_keys(article)
    matched, seen = [], set()
    for post in reddit_posts:
        if not reddit_post_matches_article(post.get("url") or "", ids, paths, url_keys):
            continue
        rid = str(post.get("id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        matched.append({
            "reddit_id": rid,
            "subreddit": post.get("subreddit") or "",
            "author": post.get("author") or "",
            "title": clean(post.get("title") or ""),
            "url": post.get("url") or "",
            "permalink": f"https://www.reddit.com/comments/{rid}/",
            "score": int(post.get("score") or 0),
            "comments": int(post.get("num_comments") or 0),
            "created_utc": int(post.get("created_utc") or 0),
        })
    matched.sort(key=lambda t: (t["score"], t["comments"]), reverse=True)
    return {
        "id": article["id"],
        "reddit_score": max((t["score"] for t in matched), default=0),
        "reddit_comments": max((t["comments"] for t in matched), default=0),
        "reddit_score_sum": sum(t["score"] for t in matched),
        "reddit_comments_sum": sum(t["comments"] for t in matched),
        "reddit_submissions": len(matched),
        "reddit_threads": matched,
    }


def merge_sidecar_rows(articles, path, table):
    """Overlay article sidecar rows by id for enrichment/matching passes."""
    path = Path(path)
    if not path.exists():
        return articles
    by_id = {row.get("id"): row for row in load_toml(path).get(table, [])}
    return [{**article, **(by_id.get(article.get("id")) or {})} for article in articles]


def reddit_metric_rows(articles, reddit_posts):
    return [reddit_stats(a, reddit_posts) for a in articles]


def refresh_reddit_metrics(data_path, out_path=REDDIT_METRICS, limit=0, refresh_posts=True):
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    articles = merge_sidecar_rows(articles, ARCHIVE_MIRRORS, "archive_mirror")
    articles = merge_sidecar_rows(articles, GAMEDEV_LIVE, "gamedeveloper_live")
    if limit:
        articles = articles[:limit]
    reddit_posts = load_reddit_posts(refresh=refresh_posts, articles=articles)
    rows = reddit_metric_rows(articles, reddit_posts)
    matched = sum(1 for r in rows if r["reddit_submissions"])
    log(f"[*] reddit: {len(reddit_posts)} cached posts, {matched}/{len(rows)} articles matched")
    Path(out_path).write_bytes(tomli_w.dumps({"reddit_postmortem": rows}).encode())
    return len(rows)


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


BLOG_FEATURE_RE = re.compile(r"/blogs/[^/]+/\d{8}/(\d+)/([^/?#]+)", re.I)
POSTMORTEM_HAY_RE = re.compile(r"post[\s_-]?mortem", re.I)
ROMAN_PART = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}


def detect_blog_part(slug, title):
    """Recognise one instalment of a multi-part blog series from its slug/title.

    Returns (series_stem, part_no, part_total, part_label) or None. Handles
    Gamasutra's compact 'PostMortem_part_12' / 'part_22' slug convention (= part
    1 of 2, 2 of 2), plus 'Part N of M', 'Part N/M', 'Pt. N', and roman 'Part II'.
    The stem (slug minus the part token) groups instalments of one series so the
    surfacer can suggest them as parts rather than unrelated rows.
    """
    hay = f"{slug} {title}"
    no = tot = None
    m = re.search(r"\b(?:pt|part)\.?\s*(\d+)\s*(?:of|/)\s*(\d+)", hay, re.I)
    if m:
        no, tot = int(m.group(1)), int(m.group(2))
    elif (m := re.search(r"part[\s._-]*([1-9])([2-9])\b", slug, re.I)) and int(m.group(1)) <= int(m.group(2)):
        no, tot = int(m.group(1)), int(m.group(2))        # 'part_12' -> 1 of 2
    elif m := re.search(r"\b(?:pt|part)\.?[\s._-]*(\d+)\b", hay, re.I):
        no = int(m.group(1))
    elif m := re.search(r"\b(?:pt|part)\.?[\s._-]*(i{1,3}|iv|vi?)\b", hay, re.I):
        no = ROMAN_PART.get(m.group(1).lower())
    if not no:
        return None
    # Trim from the part token to the end (note '_' is a \w char, so we can't lean
    # on \b here — match the digits/roman run and swallow any trailing subtitle).
    stem = re.sub(r"_?(?:post[\s_-]?mortem)?_?(?:pt|part)[\s._-]*[0-9ivx]+.*$", "", slug, flags=re.I)
    stem = re.sub(r"[_\W]+$", "", stem).lower()
    label = f"Part {no}" + (f"/{tot}" if tot else "")
    return stem, no, tot, label


def blog_postmortem_candidates(hn_posts, reddit_posts, known_ids):
    """Surface /blogs/-shaped postmortem URLs from the HN+Reddit corpora.

    Developer-blog postmortems never appear in the /view/feature/ CDX sweep, so
    they're invisible to the catalogue until curated in by id. This flags the
    confident-looking ones (slug/title says "postmortem") for human review; we
    deliberately do not auto-add them.
    """
    cands = {}
    sources = (("hn", hn_posts, "points", "num_comments"),
               ("reddit", reddit_posts, "score", "num_comments"))
    for kind, posts, score_key, comment_key in sources:
        for post in posts:
            url = post.get("url", "") or ""
            m = BLOG_FEATURE_RE.search(url)
            if not m:
                continue
            bid, slug = m.group(1), m.group(2)
            title = clean(post.get("title", "") or "")
            if not POSTMORTEM_HAY_RE.search(slug + " " + title):
                continue
            score = int(post.get(score_key) or 0)
            comments = int(post.get(comment_key) or 0)
            rec = cands.setdefault(bid, {
                "feature_id": bid,
                "slug": slug,
                "title": title,
                "url": url,
                "in_dataset": bid in known_ids,
                "hn_points": 0, "hn_comments": 0,
                "reddit_score": 0, "reddit_comments": 0,
            })
            # keep the most descriptive title and the strongest per-source metrics
            if len(title) > len(rec["title"]):
                rec["title"], rec["url"], rec["slug"] = title, url, slug
            if kind == "hn":
                rec["hn_points"] = max(rec["hn_points"], score)
                rec["hn_comments"] = max(rec["hn_comments"], comments)
            else:
                rec["reddit_score"] = max(rec["reddit_score"], score)
                rec["reddit_comments"] = max(rec["reddit_comments"], comments)
    rows = list(cands.values())
    # Tag multi-part instalments so the surfacer suggests them as series parts.
    for r in rows:
        part = detect_blog_part(r["slug"], r["title"])
        if part:
            r["series_stem"], r["part_no"], r["part_total"], r["part_label"] = part

    # Cluster instalments of one series together (parts in order), ranking each
    # cluster by its strongest signal so a high-profile series stays near the top.
    def group_key(r):
        return r.get("series_stem") or r["feature_id"]

    best = {}
    for r in rows:
        signal = max(r["hn_points"], r["reddit_score"])
        best[group_key(r)] = max(best.get(group_key(r), 0), signal)
    rows.sort(key=lambda r: (-best[group_key(r)], group_key(r), r.get("part_no") or 0,
                             -(r["hn_comments"] + r["reddit_comments"])))
    return rows


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

    # Developer-blog (/blogs/) postmortems from both corpora: invisible to the
    # /view/feature/ sweep, surfaced here for review (not auto-added).
    known_ids = set(curated_ids) | postmortem_ids
    pm_path = DATA / "postmortems.toml"
    if pm_path.exists():
        for art in load_toml(pm_path).get("postmortem", []):
            known_ids.add(str(art.get("id", "")))
            known_ids.update(str(x) for x in art.get("alt_ids", []) or [])
    reddit_posts = load_toml(REDDIT_POSTS).get("reddit_post", []) if REDDIT_POSTS.exists() else []
    blog_candidates = blog_postmortem_candidates(hn_posts, reddit_posts, known_ids)
    blog_new = [c for c in blog_candidates if not c["in_dataset"]]

    audit = {
        "summary": {
            "hn_posts": len(hn_posts),
            "postmortem_feature_ids": len(postmortem_ids),
            "matched_feature_posts": len(matched),
            "unmatched_feature_posts": len(unmatched_feature),
            "postmortemish_no_feature_posts": len(postmortemish_no_feature),
            "review_candidates": len(candidates),
            "blog_postmortem_candidates": len(blog_candidates),
            "blog_postmortem_candidates_new": len(blog_new),
        },
        "review_candidate": candidates,
        "blog_postmortem_candidate": blog_candidates,
        "matched_feature_post": by_points(matched),
        "unmatched_feature_post": by_points(unmatched_feature),
        "postmortemish_no_feature_post": by_points(postmortemish_no_feature),
    }
    out = DATA / "hn_postmortem_audit.toml"
    out.write_bytes(tomli_w.dumps(audit).encode())
    return out, audit["summary"]


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
    for i, article in enumerate(articles, 1):
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

    existing.update({row["id"]: row for row in rows})
    ordered = [existing[a["id"]] for a in all_articles if a["id"] in existing]
    out_path.write_bytes(tomli_w.dumps({"wiki_game_sales": ordered}).encode())
    return len(rows)



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
        m = re.search(r"/blogs/[^/]+/\d{8}/\d+/([^/?#]+)\.php", url, re.I)
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
        best["hn_points_sum"] = (best.get("hn_points_sum", 0) or 0) + (art.get("hn_points_sum", 0) or 0)
        best["hn_comments_sum"] = (best.get("hn_comments_sum", 0) or 0) + (art.get("hn_comments_sum", 0) or 0)
        best["hn_submissions"] = (best.get("hn_submissions", 0) or 0) + (art.get("hn_submissions", 0) or 0)
        best.setdefault("hn_threads", [])
        best["hn_threads"].extend(art.get("hn_threads", []) or [])
        if not best.get("authors") and art.get("authors"):
            best["authors"] = art["authors"]
        if best.get("date_estimated") and art.get("date") and not art.get("date_estimated"):
            best["date"] = art["date"]
            best["date_estimated"] = False
    best["alt_ids"] = sorted(set(alt_ids), key=lambda x: int(x) if x.isdigit() else x)
    by_thread = {}
    for thread in best.get("hn_threads", []) or []:
        key = thread.get("object_id") or thread.get("url") or thread.get("submitted_url")
        old = by_thread.get(key)
        if not old or (thread.get("points", 0), thread.get("comments", 0)) > (old.get("points", 0), old.get("comments", 0)):
            by_thread[key] = thread
    best["hn_threads"] = sorted(by_thread.values(), key=lambda t: (t.get("points", 0), t.get("comments", 0)), reverse=True)
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


# ---------------------------------------------------------- local data refresh
HN_KEYS = ("hn_points", "hn_comments", "hn_points_sum", "hn_comments_sum", "hn_submissions", "hn_threads")


def hn_metric_rows(articles, hn_posts):
    rows = []
    for article in articles:
        row = {"id": article["id"]}
        row.update(hn_stats(article, hn_posts))
        rows.append(row)
    return rows


def write_hn_metrics(articles, hn_posts_path=HN_POSTS, metrics_path=HN_METRICS):
    """Write article-scoped HN metrics to a sidecar TOML.

    HN data is derived from `hn_gamasutra_posts.toml`; keeping it out of
    `postmortems.toml` prevents no-enrich or link-check refreshes from silently
    blanking discussion counts in the primary catalogue.
    """
    hn_posts = load_hn_posts(hn_posts_path)
    rows = hn_metric_rows(articles, hn_posts)
    assert_hn_not_blank(rows)
    Path(metrics_path).write_bytes(tomli_w.dumps({"hn_postmortem": rows}).encode())
    return len(rows)


def strip_hn_fields(article):
    for key in HN_KEYS:
        article.pop(key, None)
    return article


def refresh_hn_metrics(data_path, hn_posts_path=HN_POSTS, metrics_path=HN_METRICS):
    """Recompute sidecar HN metrics for an existing dataset from local cache."""
    payload = load_toml(data_path)
    return write_hn_metrics(payload.get("postmortem", []), hn_posts_path, metrics_path)


def assert_hn_not_blank(rows):
    """Catch accidental writes of a fully blank HN metric set."""
    if not rows:
        return
    if any((r.get("hn_points_sum") or r.get("hn_comments_sum") or r.get("hn_threads")) for r in rows):
        return
    raise RuntimeError("refusing to write fully blank HN metrics; refresh data/hn_gamasutra_posts.toml first")


def refresh_link_checks(data_path):
    """Refresh core Wayback/original link availability fields only."""
    data_path = Path(data_path)
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    for i, article in enumerate(articles, 1):
        strip_hn_fields(article)
        check_article_links(article)
        if i % 25 == 0:
            log(f"[*] checked links {i}/{len(articles)}")
    data_path.write_bytes(tomli_w.dumps(payload).encode())
    return len(articles)


def refresh_archive_mirrors(data_path, out_path=ARCHIVE_MIRRORS, limit=0, delay=4.0,
                            recheck=False):
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    if limit:
        articles = articles[:limit]
    prior = {}
    if Path(out_path).exists():  # absent on a first-ever run
        prior = {r.get("id"): r for r in load_toml(out_path).get("archive_mirror", [])}
    rows, live, probed = [], 0, 0
    for i, article in enumerate(articles, 1):
        row = archive_mirror_row(article, prior=prior.get(article["id"]), recheck=recheck)
        hit = row.pop("_probed", True)  # transient; never persist it
        rows.append(row)
        live += int(row["archive_today_ok"])
        if hit:
            probed += 1
            time.sleep(delay)  # only throttle on requests we actually made
        if i % 25 == 0:
            log(f"[*] archive.is {i}/{len(articles)} ({live} live, {probed} probed, "
                f"{i - probed} reused)")
    Path(out_path).write_bytes(tomli_w.dumps({"archive_mirror": rows}).encode())
    log(f"[*] archive.is: {live}/{len(rows)} confirmed-live mirrors "
        f"({probed} probed, {len(rows) - probed} reused from cache)")
    return len(rows)


def refresh_wayback_links(data_path, out_path=WAYBACK_LINKS, limit=0, delay=0.2,
                          recheck=False):
    """Wayback liveness sidecar: canonical + print capture checked separately."""
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    if limit:
        articles = articles[:limit]
    prior = {}
    if Path(out_path).exists():  # absent on a first-ever run
        prior = {r.get("id"): r for r in load_toml(out_path).get("wayback_link", [])}
    rows, base_live, print_live, probed = [], 0, 0, 0
    for i, article in enumerate(articles, 1):
        row = wayback_link_row(article, prior=prior.get(article["id"]), recheck=recheck)
        hit = row.pop("_probed", True)  # transient; never persist it
        rows.append(row)
        base_live += int(row["wayback_ok"])
        print_live += int(row["wayback_print_ok"])
        if hit:
            probed += 1
            time.sleep(delay)  # IA is touchy; stay gentle on the requests we make
        if i % 25 == 0:
            log(f"[*] wayback {i}/{len(articles)} ({base_live} canonical, "
                f"{print_live} print, {probed} probed)")
    Path(out_path).write_bytes(tomli_w.dumps({"wayback_link": rows}).encode())
    log(f"[*] wayback: {base_live}/{len(rows)} canonical live, "
        f"{print_live}/{len(rows)} print live ({probed} probed, "
        f"{len(rows) - probed} reused from cache)")
    return len(rows)


def refresh_gamedev_live(data_path, out_path=GAMEDEV_LIVE):
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    rows = []
    total_failures = 0
    for i, article in enumerate(articles, 1):
        rows.append(gamedev_live_row(article))
        if i % 25 == 0:
            log(f"[*] found live URLs {i}/{len(articles)}")
    Path(out_path).write_bytes(tomli_w.dumps({"gamedeveloper_live": rows}).encode())
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


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="only process N articles")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--hn-only", action="store_true", help="refresh data/hn_gamasutra_posts.toml and exit")
    ap.add_argument("--hn-audit", action="store_true", help="audit cached HN posts against local postmortem URL cache and exit")
    ap.add_argument("--refresh-hn-metrics", action="store_true", help="recompute HN fields in --out from local cache and exit")
    ap.add_argument("--check-links", action="store_true", help="slow: refresh core Wayback/original link availability fields")
    ap.add_argument("--archive-mirrors-only", action="store_true", help="write archive.is mirror sidecar and exit")
    ap.add_argument("--archive-recheck", action="store_true", help="re-probe archive.is mirrors already resolved ok/absent (bust the sticky cache)")
    ap.add_argument("--wayback-links-only", action="store_true", help="write wayback liveness sidecar (canonical + print, checked separately) and exit")
    ap.add_argument("--wayback-recheck", action="store_true", help="re-check wayback links already confirmed live (bust the sticky cache)")
    ap.add_argument("--gamedev-live-only", action="store_true", help="slow: discover live gamedeveloper.com URLs sidecar and exit")
    ap.add_argument("--reddit-only", action="store_true", help="harvest Reddit posts (Arctic Shift) and refresh the metrics sidecar, then exit")
    ap.add_argument("--reddit-recompute", action="store_true", help="re-match Reddit metrics from the cached posts without refetching, then exit")
    ap.add_argument("--notable-authors-only", action="store_true", help="resolve notable authors' Wikipedia pages sidecar and exit")
    ap.add_argument("--wiki-sales-only", action="store_true", help="slow/best-effort: refresh Wikipedia sales sidecar and exit")
    ap.add_argument("--author-bios-only", action="store_true", help="slow/best-effort: extract article-scoped author bio sidecar and exit")
    ap.add_argument("--limit", type=int, default=0, help="limit sidecar refresh rows for smoke tests")
    ap.add_argument("--offset", type=int, default=0, help="start sidecar refresh at this row offset")
    ap.add_argument("--hn-posts", default=str(HN_POSTS), help="cached HN gamasutra posts TOML")
    ap.add_argument("--hn-metrics", default=str(HN_METRICS), help="article HN metrics sidecar TOML")
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
    if args.refresh_hn_metrics:
        n = refresh_hn_metrics(args.out, args.hn_posts, args.hn_metrics)
        log(f"[*] refreshed HN metrics for {n} entries -> {args.hn_metrics}")
        return
    if args.archive_mirrors_only:
        n = refresh_archive_mirrors(args.out, limit=args.limit, recheck=args.archive_recheck)
        log(f"[*] refreshed archive.is mirrors for {n} entries -> {ARCHIVE_MIRRORS}")
        return
    if args.wayback_links_only:
        n = refresh_wayback_links(args.out, limit=args.limit, recheck=args.wayback_recheck)
        log(f"[*] refreshed wayback links for {n} entries -> {WAYBACK_LINKS}")
        return
    if args.gamedev_live_only:
        n = refresh_gamedev_live(args.out)
        log(f"[*] refreshed GameDeveloper live URLs for {n} entries -> {GAMEDEV_LIVE}")
        return
    if args.reddit_only:
        n = refresh_reddit_metrics(args.out, REDDIT_METRICS, args.limit)
        log(f"[*] refreshed Reddit metrics for {n} entries -> {REDDIT_METRICS}")
        return
    if args.reddit_recompute:
        n = refresh_reddit_metrics(args.out, REDDIT_METRICS, args.limit, refresh_posts=False)
        log(f"[*] recomputed Reddit metrics for {n} entries from cache -> {REDDIT_METRICS}")
        return
    if args.notable_authors_only:
        n = refresh_notable_authors(args.out)
        log(f"[*] resolved {n} notable authors -> {NOTABLE_AUTHORS}")
        return
    if args.wiki_sales_only:
        n = refresh_wiki_sales(args.out, WIKI_SALES, args.limit, args.offset)
        log(f"[*] refreshed Wikipedia sales signals for {n} entries -> {WIKI_SALES}")
        return
    if args.author_bios_only:
        n = refresh_author_bios(args.out, AUTHOR_BIOS, args.limit)
        log(f"[*] refreshed author bios for {n} entries -> {AUTHOR_BIOS}")
        return
    if args.check_links and args.no_enrich:
        n = refresh_link_checks(args.out)
        log(f"[*] refreshed link checks for {n} entries -> {args.out}")
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
        art["author_notable"] = False
        out.append(art)
        time.sleep(0.2)

    out = dedupe_articles(out)
    if not args.no_enrich:
        for i, art in enumerate(out, 1):
            art["author_notable"] = any(author_notable(a) for a in art["authors"])
            if args.check_links:
                check_article_links(art)
            if i % 25 == 0:
                log(f"[*] enriched {i}/{len(out)} deduped articles")
            time.sleep(0.3)

    out.sort(key=lambda a: (a["date"] or "0000", a["title"]))
    payload = {"postmortem": [strip_hn_fields(art) for art in out]}
    Path(args.out).write_bytes(tomli_w.dumps(payload).encode())
    log(f"[*] wrote {len(out)} entries -> {args.out}")
    if not args.no_enrich:
        n = write_hn_metrics(out, args.hn_posts, args.hn_metrics)
        log(f"[*] wrote HN metrics for {n} entries -> {args.hn_metrics}")


if __name__ == "__main__":
    main()
