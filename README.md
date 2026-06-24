# Gamasutra Postmortem Archive

**Live at [postmortem.forthrast.com](https://postmortem.forthrast.com)**
(deployed to GitHub Pages via `.github/workflows/pages.yml`).

A sortable index of classic **Gamasutra postmortem features**, preserved and
linked via the **Wayback Machine**. Styled after the late-90s/00s Gamasutra
and Longreads — dense, readable, restrained — with modern client-side
sorting and filtering.

Gamasutra rebranded to GameDeveloper.com and many old `/view/feature/...`
links rotted. This project indexes the postmortems and points each entry at a
good Internet Archive snapshot.

## Sort axes
- **Balanced (default)** — a blended score across the signals below. Each
  metric only covers a slice of the catalogue (HN points ~16%, copies sold
  ~37%, captures ~98%), so any single sort leaves most entries tied at zero.
  The balanced score maps each entry to its percentile *among entries with a
  signal* on each axis, then takes a weighted average — so an entry rises when
  it stands out on any axis, and further when it stands out on several.
- Date (real publish date, or earliest Wayback capture as a proxy)
- Title / Game / Author / Category
- Hacker News points & comments
- Wayback capture count (a rough "how linked / re-crawled" proxy)
- Notable author (Wikipedia presence)
- *(Phase 2, curated:* Reddit upvotes, copies sold *)*

## Layout
```
scraper/scrape.py      # Wayback CDX + per-article parse + HN/Wikipedia enrich
data/postmortems.toml          # the dataset (hand-editable TOML)
data/postmortem_url_includes.toml  # curated postmortem-canon URLs missed by slug search
data/hn_gamasutra_posts.toml   # cached HN stories whose URLs mention gamasutra.com
data/hn_postmortem_audit.toml  # local HN/postmortem URL audit + review candidates
index.html, style.css, app.js  # static page, loads the TOML
TODO.md                # roadmap, incl. deferred/manual data axes
```

## Development
This repo is Nix-first. With direnv/nix-direnv installed:

```bash
direnv allow
just
```

Or enter manually:

```bash
nix develop
```

Useful commands:

```bash
just serve    # static site at http://localhost:8000
just hn         # refresh data/hn_gamasutra_posts.toml
just hn-audit   # local audit of HN links vs known postmortem URLs
just hn-metrics # local-only recompute of HN sums/thread links in data/postmortems.toml
just check-links # slow network pass for link availability fields
just sample     # quick scraper run
just scrape     # full Wayback scrape -> data/postmortems.toml
just check      # flake checks
```

Data source: Internet Archive CDX API + archived gamasutra.com pages + cached Hacker News submissions in `data/hn_gamasutra_posts.toml`.
Inspired by [Rich0664/Gamasutra-Archive](https://github.com/Rich0664/Gamasutra-Archive).
