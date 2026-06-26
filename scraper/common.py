"""Shared config, HTTP session, and generic Wayback/text/date helpers.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
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
REDDIT_PROBE_STRATEGY = "v2-host-scheme-variants"
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
# author links carry one or more numeric ids before the slug; multi-author
# bylines comma-join them (/view/authors/339880,915643/Name.php), so accept
# [\d,]+ rather than a single \d+ run (which silently dropped those bylines).
AUTHOR_RE = re.compile(r'href="[^"]*?/view/authors/[\d,]+/[^"]+?\.php"[^>]*>([^<]{2,60})<')
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
# game/summary/authors let a curated include (data/postmortem_url_includes.toml)
# win over the values derived from the page — see load_curated_postmortems and
# the meta-copy step in parse_article. authors is the escape hatch for bylines
# the page either hides (staff-reposted classics) or mangles (shared surnames).
CURATED_META_KEYS = SERIES_META_KEYS + ("thumbnail", "game", "summary", "authors")
def _clean_url(u):
    u = u.replace(":80/", "/")
    return u
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
WAYBACK_TS_RE = re.compile(r"^\d{14}$")
def _cdx_timestamps(url, extra=""):
    """Run a CDX timestamp query and return only well-formed 14-digit captures.

    The CDX endpoint serves an HTML error page (`<html><body><h1>503 ...`) when
    it's overloaded, with a normal 200/4xx/5xx status that callers used to ignore
    -- the first line of that error page then masqueraded as a capture timestamp
    and got baked straight into a `web/<ts>/<url>` link. Gate on the HTTP status
    *and* validate every line against the 14-digit timestamp shape so a sick
    Archive yields an empty result (a recoverable "unknown"), never garbage."""
    q = ("http://web.archive.org/cdx/search/cdx?url="
         + urllib.parse.quote(url, safe="")
         + "&output=text&fl=timestamp&filter=statuscode:200" + extra)
    r = SESSION.get(q, timeout=60)
    if r.status_code != 200:
        return []
    tokens = (ln.split()[0] for ln in r.text.splitlines() if ln.split())
    return [t for t in tokens if WAYBACK_TS_RE.match(t)]
def earliest_good_ts(url):
    """Targeted CDX lookup: earliest 200 capture for an exact URL."""
    try:
        tss = _cdx_timestamps(url, "&limit=1")
        return tss[0] if tss else None
    except Exception:
        return None
def cdx_captures(url, limit=40):
    """Distinct 200/text-html capture timestamps for an exact URL, oldest first."""
    try:
        return _cdx_timestamps(
            url, "&filter=mimetype:text/html&collapse=digest&limit=" + str(limit))
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
def clean(s):
    if not s:
        return ""
    s = htmllib.unescape(s)            # &amp; -> &, &#039; -> '
    s = re.sub(r"\s+", " ", s).strip()
    return "".join(c for c in s if c.isprintable())
TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
def clean_html_text(s):
    s = re.sub(r"(?i)<br\s*/?>", " ", s or "")
    s = TAG_RE.sub(" ", s)
    return clean(s)
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
def merge_sidecar_rows(articles, path, table):
    """Overlay article sidecar rows by id for enrichment/matching passes."""
    path = Path(path)
    if not path.exists():
        return articles
    by_id = {row.get("id"): row for row in load_toml(path).get(table, [])}
    return [{**article, **(by_id.get(article.get("id")) or {})} for article in articles]
BLOG_FEATURE_RE = re.compile(r"/blogs/[^/]+/\d{8}/(\d+)/([^/?#]+)", re.I)
POSTMORTEM_HAY_RE = re.compile(r"post[\s_-]?mortem", re.I)
# --------------------------------------------------------------- de-duplication
def ascii_fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))
