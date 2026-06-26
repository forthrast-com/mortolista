"""Article listing, per-snapshot parsing, and canonical/dedupe/score.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w
from common import ANY_IMG_RE, AUTHOR_RE, BLOG_CURATION, BLOG_IMG_CHROME, BYLINE_RE, CACHE, CURATED_META_KEYS, CURATED_POSTMORTEMS, DATE_NEWS_RE, DATE_OLD_RE, GENERIC_TITLES, IMG_CHROME, IMG_RE, SESSION, _norm_date, article_record, ascii_fold, clean, clean_html_text, earliest_good_ts, extract_meta_content, fetch_snapshot, is_dead_page, load_toml, log, slug_title, strip_tags, strip_title, ts_to_date, verified_good_ts

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
        # thumbnail is a ready Wayback im_ URL; parse_article runs it through
        # wrap_im, which passes archived URLs through unchanged.
        out[aid] = {k: e[k] for k in ("game", "summary", "thumbnail") if e.get(k)}
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
def derive_game(title):
    t = strip_title(title)
    t = re.sub(r"^(Audio |Indie |Middleware )?Postmortem:?\s*", "", t, flags=re.I)
    # strip leading studio possessive: "Team Meat's Super Meat Boy" -> keep as-is is fine,
    # but "Studio's Game" we keep full; just tidy whitespace.
    return clean(t)
BIO_VERBS_RE = re.compile(
    r"\b(is|are|was|were|has|have|worked|works|founded|co-founded|serves|served|"
    r"joined|created|designed|developed|produced|wrote|writes|currently|previously|"
    r"can be reached|email|twitter)\b",
    re.I,
)
ARTICLE_WORDS_RE = re.compile(r"\b(postmortem|development|publisher|platform|engine|gameplay|project|team)\b", re.I)
RETURN_TO_FULL_RE = re.compile(r"(?is)<p[^>]*>\s*<a[^>]+>\s*Return to the full version.*")
PARA_RE = re.compile(r"(?is)<p\b[^>]*>(.*?)</p>")
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
