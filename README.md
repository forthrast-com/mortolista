# Gamasutra Postmortem Archive

A sortable index of classic **Gamasutra postmortem features**, preserved and
linked via the **Wayback Machine**. Styled after the late-90s/00s Gamasutra
and Longreads — dense, readable, restrained — with modern client-side
sorting and filtering.

Gamasutra rebranded to GameDeveloper.com and many old `/view/feature/...`
links rotted. This project indexes the postmortems and points each entry at a
good Internet Archive snapshot.

## Sort axes
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
data/hn_gamasutra_posts.toml   # cached HN stories whose URLs mention gamasutra.com
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
just hn       # refresh data/hn_gamasutra_posts.toml
just sample   # quick scraper run
just scrape   # full run -> data/postmortems.toml
just check    # flake checks
```

Data source: Internet Archive CDX API + archived gamasutra.com pages + cached Hacker News submissions in `data/hn_gamasutra_posts.toml`.
Inspired by [Rich0664/Gamasutra-Archive](https://github.com/Rich0664/Gamasutra-Archive).
