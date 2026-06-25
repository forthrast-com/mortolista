#!/usr/bin/env python3
"""Read-only liveness probe over the curated /blogs/ (Tier A) includes.

For each blog include, run the real capture-selection path (verified_good_ts)
and report whether it lands on a capture that actually *renders* as the post,
plus the timestamp chosen and how many CDX captures exist. Writes nothing to the
dataset — it just tells us which Tier A entries are live and which need URL
(re)discovery. Use --all to probe every include, not just /blogs/ ones.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402


def probe(aid, rec):
    url = rec["original"]
    is_blog = bool(scrape.blog_url_parts(url)[0])
    t0 = time.time()
    # Inline verified_good_ts's walk so we only hit the slow CDX endpoint once.
    # Walk oldest->newest then the newest few in reverse: a renderable capture
    # often sits past the first handful (or only survives at the recent end), and
    # capping at 6 was flagging live entries as STUB (see rediscover_tier_a.py).
    tss = scrape.cdx_captures(url)
    ts, html = (tss[0] if tss else None), None
    walk, seen = tss[:12] + list(reversed(tss[-3:])), set()
    for cand in walk:
        if cand in seen:
            continue
        seen.add(cand)
        h = scrape.fetch_snapshot(cand, url)
        if scrape.capture_renders(h, is_blog):
            ts, html = cand, h
            break
    renders = bool(html) and scrape.capture_renders(html, is_blog)
    dt = time.time() - t0
    return {
        "id": aid,
        "blog": is_blog,
        "url": url,
        "n_cdx": len(tss),
        "ts": ts,
        "renders": renders,
        "secs": round(dt, 1),
    }


def parse_limit():
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        if i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return 0


def main():
    only_blogs = "--all" not in sys.argv
    limit = parse_limit()
    curated = scrape.load_curated_postmortems()
    rows = []
    for aid, rec in curated.items():
        is_blog = bool(scrape.blog_url_parts(rec["original"])[0])
        if only_blogs and not is_blog:
            continue
        if limit and len(rows) >= limit:
            break
        r = probe(aid, rec)
        flag = "OK " if r["renders"] else ("DEAD" if r["n_cdx"] == 0 else "STUB")
        scrape.log(
            f"  [{flag}] {aid:>8}  cdx={r['n_cdx']:<3} ts={r['ts'] or '-':<14} "
            f"{r['secs']:>4}s  {r['url']}"
        )
        rows.append(r)

    live = sum(1 for r in rows if r["renders"])
    dead = [r for r in rows if r["n_cdx"] == 0]
    stub = [r for r in rows if r["n_cdx"] and not r["renders"]]
    scrape.log(f"\n[*] {live}/{len(rows)} render OK; "
               f"{len(stub)} stub/non-rendering; {len(dead)} no-capture")
    for r in dead + stub:
        scrape.log(f"    NEEDS-DISCOVERY {r['id']}  {r['url']}")


if __name__ == "__main__":
    main()
