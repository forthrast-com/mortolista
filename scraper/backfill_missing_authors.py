#!/usr/bin/env python3
"""Surgically backfill empty `authors` (and fix leading-"- " `game` fields) in
data/postmortems.toml without a full re-scrape.

Mirrors what a full `just scrape` would now produce after the parse.py byline
fixes + curated `authors` overrides, but touches only the affected fields on the
affected rows so the rest of each (network-derived) record is left intact.

- Curated-override rows (gd-* includes, plus 115711/264127): take the include's
  `authors` verbatim — that's exactly what the meta-copy step applies on a scrape.
- Everyone else: re-parse the row's stored capture and lift `authors` from the
  improved extractor.
- Any `game` that still carries the old "- " title-split junk is re-derived.
"""
import sys
from pathlib import Path

import tomli_w

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402

OUT = scrape.DATA / "postmortems.toml"


def stored_ts(entry):
    wb = entry.get("wayback", "")
    return wb.split("/web/")[1].split("/")[0] if "/web/" in wb else None


def main():
    payload = scrape.load_toml(OUT)
    articles = payload.get("postmortem", [])
    curated = scrape.load_curated_postmortems()

    author_fixed, author_empty, game_fixed = [], [], []
    for e in articles:
        aid = e["id"]
        if not (e.get("authors") or []):
            meta = curated.get(aid, {}).get("meta") or {}
            if "authors" in meta:
                # Explicit curated override; may be [] to pin "no verified author".
                authors = list(meta["authors"])
            else:
                parsed = scrape.parse_feature_url(e["original_url"])
                if not parsed:
                    author_empty.append((aid, "unparseable original_url"))
                    continue
                _, slug, original = parsed
                rec = scrape.article_record(aid, slug, original, ts=stored_ts(e))
                art = scrape.parse_article(aid, rec)
                authors = (art or {}).get("authors", [])
            if authors:
                e["authors"] = authors
                e["author_notable"] = any(scrape.author_notable(a) for a in authors)
                author_fixed.append((aid, authors))
            else:
                author_empty.append((aid, e["title"][:60]))

        game = e.get("game", "")
        if game.startswith("- "):
            e["game"] = scrape.derive_game(e["title"])
            game_fixed.append((aid, game, e["game"]))

    OUT.write_bytes(tomli_w.dumps({"postmortem": articles}).encode())

    scrape.log(f"[*] authors backfilled: {len(author_fixed)}")
    for aid, a in author_fixed:
        scrape.log(f"    {aid}: {a}")
    scrape.log(f"[*] game fields fixed: {len(game_fixed)}")
    for aid, old, new in game_fixed:
        scrape.log(f"    {aid}: {old!r} -> {new!r}")
    scrape.log(f"[*] left empty (no verified byline): {len(author_empty)}")
    for aid, why in author_empty:
        scrape.log(f"    {aid}: {why}")


if __name__ == "__main__":
    main()
