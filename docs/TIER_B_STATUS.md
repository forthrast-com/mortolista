# Tier B (classic magazine postmortems) — DONE 2026-06-26

Status: **complete.** All 29 Tier B candidates are ingested, and the Round 2 batch
(`ROUND2_gamedev_postmortem_candidate_additions.md`) followed via the resolver's new
`--round2` mode. Dataset is 289 entries. The verification bug below is fixed; what
remains is the irreducible IA flakiness, handled by re-running the idempotent steps.

The notes below are kept as the method record.

## How it actually went

- The resolver's `original_archived` now retries across `www.`/bare hosts **and**
  `/view/news/` **+** `/view/feature/` path kinds (older classics like Baldur's Gate II
  131493 and Weapon of Choice 132292 are features, not news).
- IA still flakes non-deterministically, so the loop is: run
  `resolve_gamedev_originals.py [--round2] --append` a few times (idempotent), hand-fix
  any marquee stragglers, then `add_curated_blogs.py` until everything parses.
- HL2 (259479) and Psychonauts (251220) were hand-written when the resolver fell back
  to the gd page under flak — both originals are confirmed archived.
- Weapon of Choice (132292) had been wrongly merged as an alt_id of Explosionade
  (6250) via the shared truncated slug `postmortem_mommys_best_games_`; un-merged so it
  stands as its own entry.

## What these are

The Tier B candidates (Half-Life 2, Psychonauts, XCOM, …) live today on
gamedeveloper.com as "Classic Postmortem" reprints, but were originally gamasutra
**`/view/news/<id>/`** articles (non-"postmortem" slugs, which is why the feature
sweep missed them). Heroes sit at `db_area/images/news/<id>/`.

## Method ("redirect tracking")

The migrated gamedeveloper.com page (live 403s scrapers — fetch the Wayback
capture) still embeds the original gamasutra path, e.g. for HL2:
`/view/news/259479/Classic_Postmortem_The_making_of_HalfLife_2`. Track that, prefer
the gamasutra original (proper old-layout byline/date/hero), fall back to ingesting
the gamedeveloper page only when no original survives.

## Code already landed (kept on main)

- `scraper/scrape.py`: `parse_feature_url` accepts `/view/news/`; `IMG_RE` matches
  `db_area/images/news/<id>/`; gamedeveloper.com fallback branch with JSON-LD
  `datePublished`/author extraction and `<i>`-tag stripping (`strip_tags`).
- `scraper/resolve_gamedev_originals.py`: the resolver (the 29 candidate URLs,
  Eastshade deduped against blog 368294). Run `--append` to write includes.

## ⚠️ Known bug to fix first

The resolver verifies the gamasutra original with `cdx_captures(gama, limit=1)`
before preferring it. **That CDX call flaked on a 503-ing Archive and wrongly sent
HL2 to the gamedeveloper fallback** — but the original *does* exist:
`https://web.archive.org/web/20151117155135/http://gamasutra.com/view/news/259479/Classic_Postmortem_The_making_of_HalfLife_2.php`

Likely the other "gd fallback" rows below are also false negatives. Fix options:
- make the verification robust (retries; try both `www.` and bare host; or use the
  availability API), **or**
- prefer the gamasutra `/view/news/<id>` whenever the embedded path exists and let
  `parse_article`/`verified_good_ts` decide if it's archived, falling back only on
  a real parse failure.

## Resolution results (29 candidates)

Resolved to gamasutra `/view/news/<id>` (trust these ids):

| game | id |
|---|---|
| Call of Duty 4 | 258315 |
| The Sims 2 | 305424 |
| Civilization V | 306040 |
| XCOM: Enemy Unknown | 307191 |
| KOTOR II | 310658 |
| No One Lives Forever 2 | 131478 |
| Asheron's Call | 257659 |
| Deadly Premonition | 252418 |
| Sunless Sea | 237657 |
| Shadow of Mordor | 234421 |
| Technobabylon | 248567 |
| Race the Sun | 264127 |
| Black The Fall | 307674 |
| The Turing Test | 308654 |
| Offworld Trading Company | 274240 |

Fell back to gamedeveloper (RE-CHECK — likely have gamasutra originals too):
Half-Life 2 (**confirmed 259479**), Silent Hill 4, Far Cry 2, Bulletstorm,
Dance Central, Haunt, Xeodrifter, Out There, Leaving Lyndow, Verdun, Sydney Hunter.

Unresolved (IA flak on the availability API — just re-run):
Psychonauts (**known 251220**), Guitar Hero, Telltale's The Walking Dead.

## To resume

1. Fix the resolver verification bug above.
2. `python scraper/resolve_gamedev_originals.py --append` (when IA is healthy).
3. `python scraper/add_curated_blogs.py` to ingest, then `--reddit-recompute` +
   `--refresh-hn-metrics`.
4. Marquee titles with no auto thumbnail (gd-fallback entries lack og:image) can
   take a curated `thumbnail = "<url>"` on their include.

See memory: tier-b-gamasutra-news-resolution.
