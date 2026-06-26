#!/usr/bin/env python3
"""Scrape Gamasutra postmortem features from the Wayback Machine.

CLI entry point and pipeline orchestration. The implementation is split into
sibling modules (common, parse, hn, reddit, links, wiki); this module re-exports
their public names so `import scrape; scrape.parse_article(...)` keeps working
for the helper scripts and the Justfile.

Usage:
  python scrape.py --sample 20      # quick sample run
  python scrape.py                   # full run
  python scrape.py --list-only       # just refresh the CDX url list cache
"""
import argparse
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w

from common import *  # noqa: F401,F403
from parse import *   # noqa: F401,F403
from hn import *      # noqa: F401,F403
from reddit import *  # noqa: F401,F403
from links import *   # noqa: F401,F403
from wiki import *    # noqa: F401,F403
from tags import *    # noqa: F401,F403
# underscore-prefixed names are not re-exported by *; pull the ones the CLI/tools use
from common import _clean_url, _norm_date  # noqa: F401


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
    ap.add_argument("--tags-only", action="store_true", help="derive era/platform/studio/business tags sidecar (data/tags.toml) and exit")
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
    if args.tags_only:
        n = refresh_tags(args.out, TAGS, args.limit)
        log(f"[*] wrote tags for {n} entries -> {TAGS}")
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

    # numeric gamasutra ids sort numerically; curated gamedeveloper.com "gd-<slug>"
    # ids have no number, so they sort lexically after (the two groups never compare
    # across types). Final output is re-sorted by date below regardless.
    ids = sorted(arts, key=lambda a: (0, int(a), "") if a.isdigit() else (1, 0, a))
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
