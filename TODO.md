# TODO / Roadmap

Gamasutra Postmortem Archive — a sortable 00s-style index of Gamasutra
postmortem features, linking out via the Wayback Machine.

## Phase 1 — automatable, in progress
- [x] CDX listing of postmortem feature URLs (~348 articles)
- [x] Per-article parse: title, game, authors, date, category, summary
- [x] Publish date: real byline date (old layout) or earliest Wayback
      capture as proxy (`date_estimated = true`)
- [x] HN points + comments via Algolia API
- [x] Wikipedia "notable author?" flag
- [x] Wayback capture count (backlinks proxy)
- [x] Full scrape run -> data/postmortems.toml
- [x] Static site (00s Gamasutra / Longreads aesthetic, dynamic sort/filter)
- [ ] Push to github.com/forthrast-com, enable GitHub Pages

## Phase 2 — curated / manual (deferred, columns present but empty)
- [ ] **Reddit upvotes** — best-effort via reddit search-by-URL; historical
      coverage is spotty (Pushshift dead). Field: `reddit_points`.
- [ ] **Game copies sold** — no reliable free API; curate manually for notable
      titles. Field: `copies_sold`.
- [ ] **Real backlink counts** — needs paid SEO API (Ahrefs/Moz). Currently
      proxied by `wayback_captures`. Consider OpenPageRank or Common Crawl.
- [ ] **Author significance nuance** — current flag is binary Wikipedia
      presence. Could weight by article length / specific curated list.

## Ideas / niceties
- [ ] Widen beyond URL-slug "postmortem" to catch postmortems whose slug
      lacks the word (e.g. "behind the scenes of ...").
- [ ] Pick the *best-rendered* snapshot per article for the Wayback link
      (some early captures are partial).
- [ ] Periodic refresh via GitHub Action (like the reference project).
- [x] Thumbnails (archived og:image).
- [x] Night mode toggle (persisted, defaults to OS preference).
