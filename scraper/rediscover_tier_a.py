#!/usr/bin/env python3
"""Rediscover renderable captures for the Tier A /blogs/ entries that the
liveness probe flagged STUB (captures exist but the first 6 don't render) or
DEAD (zero CDX hits — possibly just transient CDX flak on the exact URL).

Strategy, per flagged id:
  1. Build host/protocol variants of the include URL (www <-> non-www,
     http <-> https) — the Archive often holds the post under one host shape
     but not the one we curated.
  2. For each variant, pull the full CDX list (limit 40) and walk it BOTH ways:
     oldest->newest then newest->oldest, fetching each capture until one renders
     as a real post. The liveness probe only tries the oldest 6, so a STUB there
     can still have a good render deeper in the list or at the recent end.
  3. Report a proposed fix (variant URL + verified ts) per id. Writes nothing to
     the dataset — this is the "report" half of auto-rediscover + report.

Read-only. Run AFTER the wayback sidecar finishes so the two don't fight over
the CDX endpoint.
"""
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape  # noqa: E402

# The 10 the Tier A sweep flagged. id -> curated include URL.
TARGETS = {
    "372977": "https://www.gamasutra.com/blogs/AmirHFassihi/20201102/372977/Postmortem_Children_of_Morta.php",
    "234086": "http://www.gamasutra.com/blogs/SergeyTitov/20150113/234086/Postmortem_Launching_The_War_Z.php",
    "341148": "http://www.gamasutra.com/blogs/TonyHua/20190421/341148/Postmortem_Skirmish_Line.php",
    "254610": "http://gamasutra.com/blogs/CarletonDiLeo/20150925/254610/Wordsum_Postmortem__The_10_year_journey_of_a_solo_game_developer.php",
    "337113": "http://www.gamasutra.com/blogs/YannickElahee/20190221/337113/Robothorium_Post_Mortem.php",
    "330427": "http://www.gamasutra.com/blogs/ConstantinBacioiu/20181112/330427/Post_Mortem__1_year_later_I_managed_to_ship_6000_copies_to_stay_in_business.php",
    "360226": "https://www.gamasutra.com/blogs/DingDong/20200327/360226/Postmortem_A_Rationally_Designed_Funny_Game__The_making_of_biped_in_hindsight.php",
    "294276": "http://www.gamasutra.com/blogs/SergiuBucur/20170322/294276/PostMortem_Gorescript_Classic.php",
    "199358": "http://gamasutra.com/blogs/MatthewKlingensmith/20130912/199358/DwarfCorps_Kickstarter_Postmortem.php",
    "216965": "http://www.gamasutra.com/blogs/KarlInglott/20140506/216965/903m__From_University_project_to_Steam_released_game.php",
}

DELAY = 1.0  # polite gap between CDX/snapshot calls


def url_variants(url):
    """www<->non-www x http<->https, original first, deduped."""
    p = urllib.parse.urlparse(url)
    host = p.netloc
    bare = host[4:] if host.startswith("www.") else host
    hosts = [host, bare, "www." + bare]
    out, seen = [], set()
    for scheme in (p.scheme, "http", "https"):
        for h in hosts:
            v = urllib.parse.urlunparse((scheme, h, p.path, "", "", ""))
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def walk_for_render(variant, tss):
    """Fetch captures oldest->newest then newest->oldest; return first ts whose
    snapshot renders as a real blog post, else None."""
    order = list(tss) + list(reversed(tss))
    tried = set()
    for ts in order:
        if ts in tried:
            continue
        tried.add(ts)
        html = scrape.fetch_snapshot(ts, variant)
        time.sleep(DELAY)
        if scrape.capture_renders(html, is_blog=True):
            return ts
    return None


def rediscover(aid, url):
    result = {"id": aid, "url": url, "status": "STILL-DEAD",
              "variant": None, "ts": None, "variants": []}
    for variant in url_variants(url):
        tss = scrape.cdx_captures(variant)
        time.sleep(DELAY)
        result["variants"].append((variant, len(tss)))
        if not tss:
            continue
        if result["status"] == "STILL-DEAD":
            result["status"] = "STILL-STUB"  # captures exist somewhere
        ts = walk_for_render(variant, tss)
        if ts:
            result.update(status="RECOVERED", variant=variant, ts=ts)
            return result
    return result


def main():
    rows = []
    for aid, url in TARGETS.items():
        scrape.log(f">> {aid}  {url}")
        r = rediscover(aid, url)
        for v, n in r["variants"]:
            scrape.log(f"     cdx={n:<3} {v}")
        scrape.log(f"   [{r['status']}] variant={r['variant']} ts={r['ts']}")
        rows.append(r)

    rec = [r for r in rows if r["status"] == "RECOVERED"]
    scrape.log(f"\n[*] {len(rec)}/{len(rows)} recovered")
    scrape.log("\n=== proposed fixes (review before editing includes) ===")
    for r in rec:
        same = r["variant"] == r["url"]
        note = "deeper-capture only (no URL change)" if same else "swap URL"
        scrape.log(f"  {r['id']}  {note}")
        scrape.log(f"      was: {r['url']}")
        scrape.log(f"      use: {r['variant']}  (ts {r['ts']})")
    still = [r for r in rows if r["status"] != "RECOVERED"]
    if still:
        scrape.log("\n=== still unresolved ===")
        for r in still:
            scrape.log(f"  {r['id']}  {r['status']}  {r['url']}")


if __name__ == "__main__":
    main()
