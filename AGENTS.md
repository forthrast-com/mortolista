# Mortolista project notes

## Purpose

- Static, old-web-style catalogue of Gamasutra/Game Developer postmortem features.
- `index.html`, `style.css`, and `app.js` are the shipped page; catalogue data lives in `data/postmortems.toml`; article-scoped HN metrics live separately in `data/hn_postmortem_threads.toml`.
- `scraper/scrape.py` rebuilds/enriches the TOML from Wayback, HN, Wikipedia, and curated includes.

## Local workflow

- Always enter the Nix dev shell for repo commands: use `nix develop -c <command>` from automation, or `nix develop` interactively. Do not rely on bare host Python/Just/node tooling.
- Use `just serve` for the static site on port 8000, or inspect the existing `mortolista-server` tmux session before starting another server.
- Use `just check` after code changes when practical; it runs `nix flake check`.
- Use `just sample` for scraper smoke tests and `just scrape` only when a full refresh is intentional.
- The scraper touches network APIs and can be slow; prefer narrow edits plus a sample run unless the user asks for a full rebuild.

## Data/link invariants

- If an archived `?print=1` full-text URL exists, the page headline/thumbnail should use it as the primary link.
- The mirror line should still expose:
  - the normal Wayback snapshot,
  - the original Gamasutra URL, even when likely dead,
  - a live `gamedeveloper.com` URL, even when migrated formatting is broken,
  - archive.today as a fallback.
- Keep curated non-`postmortem` canon in `data/postmortem_url_includes.toml`; keep rationale short and reviewable.
- Do not hand-edit generated `data/postmortems.toml` unless the task explicitly calls for a surgical data fix.

## Style

- This is a tiny no-build frontend: plain JS modules, CSS, TOML fetched client-side.
- Match the vintage Gamasutra-ish presentation; avoid framework creep.
- Keep JS helpers small and data-driven. Escape dynamic HTML with `esc()`.
- Prefer preserving broken-but-useful historical links over hiding them.
