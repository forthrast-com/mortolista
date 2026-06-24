#!/usr/bin/env python3
"""Surgically add curated /blogs/ postmortem includes to postmortems.toml.

These developer-blog entries are not found by the CDX postmortem-slug sweep, so
a normal targeted refresh never produces them. Rather than force a full
`just scrape` (network-heavy, rewrites the whole dataset), this parses only the
curated includes that are missing from the dataset and merges them in, matching
the existing schema and the pipeline's final sort. Entries already present
(by id or alt_id) are skipped.
"""
import sys
from pathlib import Path

import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402

DATA = scrape.DATA
OUT = DATA / "postmortems.toml"


def main():
    payload = scrape.load_toml(OUT)
    articles = payload.get("postmortem", [])
    known = {a["id"] for a in articles}
    for a in articles:
        known.update(str(x) for x in a.get("alt_ids", []) or [])

    curated = scrape.load_curated_postmortems()
    added = []
    for aid, rec in curated.items():
        if aid in known:
            continue
        art = scrape.parse_article(aid, rec)
        if not art:
            scrape.log(f"  [!] {aid} did not parse; skipping")
            continue
        art.setdefault("alt_ids", [])
        art["author_notable"] = any(scrape.author_notable(a) for a in art.get("authors", []))
        added.append(art)
        series = art.get("series", "")
        scrape.log(f"  [+] {aid} {art['title'][:50]!r} part_no={art.get('part_no')} series={series!r}")

    if not added:
        scrape.log("[*] nothing to add; all curated includes already present")
        return

    merged = articles + added
    merged.sort(key=lambda a: (a.get("date") or "0000", a.get("title", "")))
    OUT.write_bytes(tomli_w.dumps({"postmortem": merged}).encode())
    scrape.log(f"[*] added {len(added)} curated entries -> {OUT} (now {len(merged)})")


if __name__ == "__main__":
    main()
