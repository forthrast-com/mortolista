#!/usr/bin/env python3
"""Resolve gamedeveloper.com "Classic Postmortem" reprints back to their original
gamasutra /view/news/<id>/ (or /view/feature/) pages, and emit curated includes.

The migrated gamedeveloper.com page 403s scrapers, but its archived HTML still
references the old gamasutra path (e.g. /view/news/259479/Classic_Postmortem_...)
and hosts its heroes at db_area/images/news/<id>/. We track that reference,
prefer the gamasutra original (proper old-layout byline/date/hero), and fall back
to ingesting the gamedeveloper URL itself only when no original is found.

Usage:  python scraper/resolve_gamedev_originals.py        # prints include TOML
        python scraper/resolve_gamedev_originals.py --append   # appends to includes
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402

INC = scrape.CURATED_POSTMORTEMS
ORIG_RE = re.compile(r"/view/(?:news|feature)/(\d+)/([A-Za-z0-9_]+)", re.I)

# Tier B candidates (Eastshade omitted — already curated as blog 368294).
GAMEDEV_URLS = [
    "https://www.gamedeveloper.com/design/classic-postmortem-the-making-of-i-half-life-2-i-",
    "https://www.gamedeveloper.com/audio/classic-postmortem-double-fine-s-i-psychonauts-i-",
    "https://www.gamedeveloper.com/audio/classic-postmortem-i-silent-hill-4-the-room-i-",
    "https://www.gamedeveloper.com/audio/classic-postmortem-guitar-hero",
    "https://www.gamedeveloper.com/business/the-making-of-i-call-of-duty-4-modern-warfare-i-",
    "https://www.gamedeveloper.com/design/the-making-of-i-far-cry-2-i-",
    "https://www.gamedeveloper.com/business/classic-postmortem-how-maxis-avoided-sequel-itis-on-i-the-sims-2-i-",
    "https://www.gamedeveloper.com/design/classic-postmortem-firaxis-i-civilization-v-i-",
    "https://www.gamedeveloper.com/design/classic-postmortem-i-xcom-enemy-unknown-i-which-turns-5-today",
    "https://www.gamedeveloper.com/design/classic-postmortem-obsidian-s-i-knights-of-the-old-republic-ii-the-sith-lords-i-",
    "https://www.gamedeveloper.com/design/classic-postmortem-i-no-one-lives-forever-2-a-spy-in-harm-s-way-i-",
    "https://www.gamedeveloper.com/design/classic-postmortem-i-asheron-s-call-i-",
    "https://www.gamedeveloper.com/design/classic-postmortem-i-deadly-premonition-i-",
    "https://www.gamedeveloper.com/audio/classic-postmortem-people-can-fly-s-i-bulletstorm-i-",
    "https://www.gamedeveloper.com/production/postmortem-harmonix-s-dance-central-2010-",
    "https://www.gamedeveloper.com/design/classic-postmortem-telltale-games-the-walking-dead-2012",
    "https://www.gamedeveloper.com/design/classic-postmortem-nanaonhas-haunt-2012",
    "https://www.gamedeveloper.com/audio/postmortem-failbetter-games-i-sunless-sea-i-",
    "https://www.gamedeveloper.com/audio/postmortem-monolith-productions-i-middle-earth-shadow-of-mordor-i-",
    "https://www.gamedeveloper.com/audio/postmortem-technocrat-s-cyberpunk-adventure-game-i-technobabylon-i-",
    "https://www.gamedeveloper.com/business/postmortem-flippfly-s-i-race-the-sun-i-",
    "https://www.gamedeveloper.com/audio/xeodrifter-postmortem",
    "https://www.gamedeveloper.com/business/out-there-a-postmortem",
    "https://www.gamedeveloper.com/business/postmortem-leaving-lyndow",
    "https://www.gamedeveloper.com/business/postmortem-the-totalitarian-puzzle-platformer-i-black-the-fall-i-",
    "https://www.gamedeveloper.com/business/postmortem-building-i-the-turing-test-i-around-a-secret-mechanic",
    "https://www.gamedeveloper.com/business/postmortem-verdun-steam-early-access",
    "https://www.gamedeveloper.com/business/postmortem-i-offworld-trading-company-i-s-early-access-campaign",
    "https://www.gamedeveloper.com/business/indie-postmortem-sydney-hunter-and-the-curse-of-the-mayan-2019-",
]


def wayback_ts(url, tries=3):
    """A capture timestamp for a URL via the availability API (retries IA flak)."""
    import json
    for _ in range(tries):
        try:
            r = scrape.SESSION.get(
                "http://archive.org/wayback/available",
                params={"url": url}, timeout=30)
            snap = (r.json().get("archived_snapshots") or {}).get("closest")
            if snap and snap.get("available"):
                return snap["timestamp"]
        except Exception:
            pass
        time.sleep(2)
    return None


def resolve(url):
    """Return (gamasutra_url | gamedeveloper_url) for one candidate, or None."""
    ts = wayback_ts(url)
    if not ts:
        return None
    html = scrape.fetch_snapshot(ts, url)
    if not html:
        return None
    m = ORIG_RE.search(html)
    if m:
        aid, slug = m.group(1), m.group(2)
        return f"http://www.gamasutra.com/view/news/{aid}/{slug}.php"
    return url  # fall back to the gamedeveloper page itself


def main():
    have = re.findall(r'id = "([^"]+)"', INC.read_text())
    have = set(have)
    blocks = []
    for url in GAMEDEV_URLS:
        target = resolve(url)
        if not target:
            scrape.log(f"  [!] unresolved: {url}")
            continue
        parsed = scrape.parse_feature_url(target)
        if not parsed:
            scrape.log(f"  [!] unparseable target {target}")
            continue
        aid = parsed[0]
        if aid in have:
            scrape.log(f"  [=] {aid} already an include ({target})")
            continue
        have.add(aid)
        origin = "gamasutra /view/news original" if "gamasutra" in target else "gamedeveloper fallback"
        blocks.append(
            "[[include]]\n"
            f'id = "{aid}"\n'
            f'url = "{target}"\n'
            f'reason = "Tier B classic magazine postmortem; {origin} tracked from {url}"'
        )
        scrape.log(f"  [+] {aid} <- {url}  ({origin})")
        time.sleep(0.3)

    out = "\n\n".join(blocks)
    if "--append" in sys.argv and blocks:
        header = "\n\n# ---- Tier B: classic magazine postmortems via gamedeveloper->gamasutra (2026-06) ----\n\n"
        INC.write_text(INC.read_text().rstrip() + "\n" + header + out + "\n")
        scrape.log(f"[*] appended {len(blocks)} Tier B includes -> {INC}")
    else:
        print(out)


if __name__ == "__main__":
    main()
