"""Reddit (Arctic Shift) harvest and per-article metric matching.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w
from common import ARCHIVE_MIRRORS, GAMEDEV_LIVE, REDDIT_METRICS, REDDIT_POSTS, REDDIT_PROBED, REDDIT_PROBE_STRATEGY, SESSION, clean, load_toml, log, merge_sidecar_rows
from hn import article_hn_keys, hn_feature_ids

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
            # Arctic Shift matches the supplied URL *exactly*, and the same thread
            # often links the bare host or the other scheme (e.g. the HL2 r/Games
            # thread is only under http://gamasutra.com, not http://www.). Probe both
            # schemes × www/bare so we don't miss host-specific submissions.
            bare = host[4:] if host.startswith("www.") else host
            for h in {host, bare, "www." + bare}:
                for scheme in ("http", "https"):
                    urls.append(urllib.parse.urlunparse((scheme, h, path, "", "", "")))
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
