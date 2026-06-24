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
GAMEDEV_LIVE = DATA / "gamedeveloper_live_urls.toml"
AUTHOR_BIOS = DATA / "author_bios.toml"
CURATED_POSTMORTEMS = DATA / "postmortem_url_includes.toml"
CACHE = ROOT / "scraper" / ".cache"
CACHE.mkdir(exist_ok=True)

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
    desc = extract_meta_content(html, name="description") or extract_meta_content(html, prop="og:description")

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


def archive_today_available(url):
    ok, final = http_exists(url, allow_redirect=True, timeout=25)
    if not ok:
        return False
    host = urllib.parse.urlparse(final).netloc.lower()
    return any(h in host for h in ("archive.ph", "archive.today", "archive.is", "archive.vn"))


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
    article["wayback_print_ok"] = wayback_available(article.get("wayback_print", ""))
    article["original_ok"] = original_available(article.get("original_url", ""))
    return article


def archive_mirror_row(article):
    original = article.get("original_url", "")
    return {
        "id": article["id"],
        "archive_today": archive_today_url(original),
        # archive.is aggressively rate-limits automated checks; /newest/ remains
        # a valid human-facing fallback even when this VM gets a 429.
        "archive_today_ok": True,
        "archive_today_print": archive_today_url(print_url(original)),
        "archive_today_print_ok": True,
    }


def gamedev_live_row(article):
    live = find_live_gamedeveloper_url(article)
    return {"id": article["id"], "live_url": live, "live_ok": bool(live)}


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
        parsed = urllib.parse.urlparse(original)
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


def fetch_reddit_gamasutra_posts(url_queries=REDDIT_URL_QUERIES, articles=None):
    """Reddit posts linking to Gamasutra/GameDeveloper, via Arctic Shift."""
    by_id = {}
    for base in url_queries:
        try:
            add_reddit_api_rows(by_id, reddit_api_posts_for_url(base))
        except requests.RequestException as exc:
            log(f"  [!] arctic-shift query failed for {base}: {exc}")
            continue
        time.sleep(0.5)

    articles = articles or []
    total = sum(len(reddit_article_url_queries(article)) for article in articles)
    done = 0
    for i, article in enumerate(articles, 1):
        for url in reddit_article_url_queries(article):
            done += 1
            try:
                add_reddit_api_rows(by_id, reddit_api_posts_for_url(url, timeout=45))
            except requests.RequestException as exc:
                log(f"  [!] arctic-shift article query failed for {url}: {exc}")
            time.sleep(0.12)
        if articles and (i % 25 == 0 or i == len(articles)):
            log(f"[*] reddit exact URL probes {i}/{len(articles)} articles ({done}/{total} URLs)")
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
        pages = r.json().get("query", {}).get("pages", {})
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


def refresh_archive_mirrors(data_path, out_path=ARCHIVE_MIRRORS):
    payload = load_toml(data_path)
    rows = [archive_mirror_row(article) for article in payload.get("postmortem", [])]
    Path(out_path).write_bytes(tomli_w.dumps({"archive_mirror": rows}).encode())
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
        n = refresh_archive_mirrors(args.out)
        log(f"[*] refreshed archive.is mirrors for {n} entries -> {ARCHIVE_MIRRORS}")
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
