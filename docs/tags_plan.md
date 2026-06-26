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
dropdown** (grouped: Type / Era / Platform / Studio / Business, plus a Notable-authors
option — single-select; a chip click drives the same control). Chips sit above
the title alongside the type badge. 289/289 tagged, 203 wiki-matched.

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
3. **LLM via an OpenAI-compatible API (built — see below):** the
   editorial/business tags (`outcome`, `crunch`, `scope-creep`, `port`, …) that no
   infobox carries. Classifies each entry from its `summary` against the fixed
   vocab above.

## Pipeline

- `scraper/tags.py` + `--tags-only` (`just tags`) writes `data/tags.toml` —
  tiers 1–2.
- `scraper/tags_llm.py` + `--tags-llm-only` (`just tags-llm`) writes
  `data/tags_llm.toml` — tier 3. Separate sidecar so either can regenerate
  without clobbering the other; the frontend unions them.

## Frontend (`app.js` / `style.css`)

- Load `data/tags.toml` + `data/tags_llm.toml`, union tags by id.
- Render tags as colour-coded chips above the title, and fold every tag into the
  one grouped Type dropdown (single-select; a chip click drives it).

## Tier 3 — editorial classifier (built)

`scraper/tags_llm.py` classifies each postmortem's summary against the fixed
vocab via any **OpenAI-compatible** `/v1/chat/completions` endpoint — local
(ollama, llama.cpp, LM Studio) or hosted. Run `just tags-llm`; configure with:
- `TAGS_LLM_BASE_URL` (default `http://localhost:11434/v1`, ollama's OAI endpoint)
- `TAGS_LLM_API_KEY` (sent as Bearer; local servers usually ignore it)
- `TAGS_LLM_MODEL` (default `gemma3:12b`)

Properties: strict JSON constrained to the vocab (temperature 0; every returned
tag validated against the allowed set, unknowns dropped); 3 few-shot exemplars;
idempotent (skips ids already in the output unless `--tags-llm-refresh`) with
periodic flush so an interrupted run resumes cheaply. The whole-sidecar *is* the
provenance (everything in `data/tags_llm.toml` is LLM-sourced). `port` emits the
target platform too. Preview the prompt with `--tags-llm-dry-run`.

Remaining: run a full pass with your chosen model and spot-check / tune the vocab
and prompt — a 12B local model gets the obvious cases right but is imperfect on
subtler ones (e.g. `first-game`). Optionally feed page-1 body text (via
`fetch_snapshot`) for richer signal than the summary alone.
- **`port`**: tag when the postmortem's *subject* is porting an existing game to
  a new platform ("we ported X to PS2", "bringing X to Switch") — not a passing
  mention of a later port. This is exactly the judgement deterministic/wiki tiers
  can't make (the word "port" is a substring minefield, and Wikipedia platform
  categories don't say which release the article is about), so it lives here. When
  `port` fires, prefer the *target* platform tag too (e.g. PS2 → `console`),
  overriding the distinctive-only platform rule for this entry.
