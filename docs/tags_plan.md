# Tags — design plan

Goal: move beyond the single `category` "Type" axis to a richer, browseable tag
system. `category` stays as-is (the Type badge + dropdown); tags are additive.

Agreed dimensions (2026-06-26): **platform/era**, **studio/business**,
**editorial themes**. Genre deliberately dropped for now. LLM-sourced tags are a
TODO to be done with a **local model**, not a hosted API.

## Storage — a sidecar, not an inline field

Tags live in `data/tags.toml`, keyed by article id, **not** in
`data/postmortems.toml` (which the pipeline regenerates and would wipe). This
matches every other enrichment (reddit / archive.is / wayback / gamedev-live),
which are sidecars merged client-side (`app.js` ~line 111) and server-side via
`merge_sidecar_rows`.

```toml
[[tag]]
id = "259479"
tags = ["00s", "pc", "console", "aaa", "breakout-success"]
# optional provenance so we can re-derive/trust each tag independently
[tag.source]
era = "date"
platform = "wiki"
studio = "category"
outcome = "llm-local"
```

## Controlled vocabularies (validate against these; drop unknowns)

- **era** (from `date`): `90s` `00s` `10s` `20s`
- **platform/format**: `pc` `console` `handheld` `mobile` `arcade` `flash` `web`
  `vr`
- **studio (size)**: `aaa` `indie` `solo` `student` `hobbyist`
- **business**: `kickstarter` `early-access` `self-published` `work-for-hire`
- **outcome**: `breakout-success` `commercial-flop` `mixed` `cult-classic`
- **process/theme**: `crunch` `scope-creep` `pivot` `first-game` `port`
  `engine-switch` `team-conflict` `long-dev` `rushed`

## Status

Tiers 1–2 **shipped** (2026-06-26): `scraper/tags.py` + `just tags` /
`--tags-only` write `data/tags.toml`; the frontend renders colour-coded chips
(era/platform/studio/business) and folds every tag into the one **Type
dropdown** (grouped: Type / Era / Platform / Studio / Business — single-select;
a chip click drives the same control). 289/289 tagged, 203 wiki-matched.

Platform is **distinctive-only**: a single-platform game keeps its one platform
(Half-Life → `pc`, an arcade/mobile-only title → that), but a port-everywhere
game (Super Meat Boy) earns no platform tag — being multiplatform says nothing.

Tier 3 (local-LLM editorial tags) remains the open follow-up below.

## Sourcing tiers

1. **Deterministic (build first, free, reproducible):**
   - `era` from `date`.
   - `platform/format`: `flash`/`web`/`arcade` from URL + era heuristics;
     `mobile` from title/slug cues.
   - `indie` from the existing `Indie Postmortem` category; `student` likewise.
2. **Wikipedia-derived (reuse existing plumbing):** the sales pass already
   fetches the wiki game page (`find_wiki_game_page` / `wiki_page` in
   `scraper/wiki.py`). Extend it to read the infobox `platforms` (and developer,
   for `aaa` vs `indie` heuristics) in the same request — near-zero marginal
   cost, covers the wiki-matched slice.
3. **LLM, local model (TODO — see below):** the editorial/business tags
   (`outcome`, `crunch`, `scope-creep`, `kickstarter`, `early-access`, …) that no
   infobox carries. Classify each entry from its `summary` (+ page-1 body via
   `fetch_snapshot`) against the fixed vocab above.

## Pipeline

- New module `scraper/tags.py` + a `--tags-only` CLI mode on `scrape.py`
  (mirrors the other sidecar refreshers) writing `data/tags.toml`.
- Tiers 1–2 run there. Tier 3 is a separate, optional pass that merges its
  output into the same sidecar so deterministic tags don't depend on the model
  being available.

## Frontend (`app.js` / `style.css`)

- Load `data/tags.toml` as a sidecar, merge by id (the `app.js:111` pattern).
- Render tags as small clickable badges near the title (vintage styling, not a
  second dropdown). Click a tag → filter the table by it; support clearing /
  combining active tags.

## TODO — local-model editorial classifier

Implement tier 3 with a **local** instruct model (nix-first: ollama or
llama.cpp; candidates `qwen2.5:7b-instruct`, `llama3.1:8b`). Requirements:
- Strict JSON output constrained to the controlled vocab; temperature 0;
  validate every returned tag against the allowed set and silently drop unknowns.
- Few-shot prompt with 3–4 hand-tagged exemplars from this corpus.
- Idempotent + cached per id (like the reddit-probe cache) so re-runs are cheap
  and don't re-hit the model.
- Write provenance `source.* = "llm-local"` so these are distinguishable from
  deterministic/wiki tags and can be re-generated independently.
