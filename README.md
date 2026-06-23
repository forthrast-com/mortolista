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
data/postmortems.toml  # the dataset (hand-editable TOML)
index.html, style.css, app.js  # static page, loads the TOML
TODO.md                # roadmap, incl. deferred/manual data axes
```

## Running the scraper
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install requests beautifulsoup4 lxml tomli-w
python scraper/scrape.py --sample 20      # quick sample
python scraper/scrape.py                  # full run -> data/postmortems.toml
```

Data source: Internet Archive CDX API + archived gamasutra.com pages.
Inspired by [Rich0664/Gamasutra-Archive](https://github.com/Rich0664/Gamasutra-Archive).
