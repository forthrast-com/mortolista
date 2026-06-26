"""Hacker News harvest, per-article metrics, and link audit/discovery.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w
from common import BLOG_FEATURE_RE, CACHE, DATA, HN_METRICS, HN_POSTS, POSTMORTEM_HAY_RE, REDDIT_POSTS, SESSION, clean, load_toml, log
from parse import detect_blog_part, load_curated_postmortems

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
# Hosts whose HN submissions we harvest. gamedeveloper.com is the live home of the
# migrated classics/Deep Dives (Katamari, Inkbound…), discussed on HN under that URL
# rather than any gamasutra path, so it needs its own search term.
HN_HOSTS = ("gamasutra.com", "gamedeveloper.com")
def hn_query_params(start, end, page=0, host="gamasutra.com"):
    return {
        "query": host,
        "restrictSearchableAttributes": "url",
        "tags": "story",
        "hitsPerPage": HN_PAGE_SIZE,
        "page": page,
        "numericFilters": f"created_at_i>={start},created_at_i<{end}",
    }
def fetch_hn_gamasutra_posts(start=HN_EARLIEST, end=None, host="gamasutra.com"):
    """Return all HN stories whose URL matches `host`.

    A plain Algolia search silently tops out around the first 1000 hits, which
    made per-article enrichment miss most older/low-ranked submissions.  This
    harvests the whole corpus by recursively partitioning on created_at_i.
    """
    end = end or int(time.time()) + 1
    first = hn_search(hn_query_params(start, end, 0, host))
    total = first.get("nbHits", 0) or 0
    if total > HN_SAFE_HIT_LIMIT and end - start > 1:
        mid = start + ((end - start) // 2)
        return (fetch_hn_gamasutra_posts(start, mid, host)
                + fetch_hn_gamasutra_posts(mid, end, host))

    posts = []
    pages = min(first.get("nbPages", 0) or 0, 10)
    for page in range(pages):
        data = first if page == 0 else hn_search(hn_query_params(start, end, page, host))
        for hit in data.get("hits", []):
            url = hit.get("url") or ""
            if host not in url.lower():
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
    posts = []
    for host in HN_HOSTS:
        posts += fetch_hn_gamasutra_posts(host=host)
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
            m = re.search(r"(/view/news/\d+/[a-z0-9_]+\.php)", url, re.I)
        if not m:
            m = re.search(r"(/blogs/[^/]+/\d{8}/\d+/[a-z0-9_]+\.php)", url, re.I)
        if m:
            paths.add(m.group(1).lower())
        # gamedeveloper.com reprints carry no feature id; key off their slug path so
        # an HN/Reddit post linking the same gd URL attaches via the substring match.
        gm = re.search(r"gamedeveloper\.com(/[a-z0-9-]+/[a-z0-9-]+)", url, re.I)
        if gm:
            paths.add(gm.group(1).lower())
    return ids, paths
def hn_feature_ids(url):
    """Feature ids in an HN URL, including bare /view/feature/<id>/ links.

    Some submissions link to Gamasutra's router form with no slug, or to a
    Wayback URL wrapping the original.  The feature id is safer than the slug:
    slugs are often truncated during site migrations, but ids and our alt_ids
    capture the old/new mapping.
    """
    ids = set(re.findall(r"/view/feature/(\d+)(?:/|$)", url, re.I))
    # Tier B / Round 2 classics live under /view/news/<id>/, not /view/feature/.
    ids.update(re.findall(r"/view/news/(\d+)(?:/|$)", url, re.I))
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
