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
- [x] Push to github.com/forthrast-com, enable GitHub Pages
      (deployed via `.github/workflows/pages.yml` to
      https://postmortem.forthrast.com)

## Phase 2 — curated / manual (deferred, columns present but empty)
- [x] **Reddit upvotes** — bulk-harvested from the Arctic Shift archive
      (`api/posts/search?url=…gamasutra.com`), cached to
      `data/reddit_gamasutra_posts.toml`, matched to articles by feature
      id/path, and written to `data/reddit_postmortem_threads.toml`
      (`reddit_score_sum`, `reddit_comments_sum`, `reddit_submissions`,
      `reddit_threads`). Run `just reddit`.
- [ ] **Game copies sold** — no reliable free API; curate manually for notable
      titles. Field: `copies_sold`.
- [ ] **Real backlink counts** — needs paid SEO API (Ahrefs/Moz). Currently
      proxied by `wayback_captures`. Consider OpenPageRank or Common Crawl.
- [ ] **Author significance nuance** — current flag is binary Wikipedia
      presence. Could weight by article length / specific curated list.
- [ ] **Tags** — richer browse axes beyond the single `category` Type field:
      platform/era, studio/business, editorial themes. Stored in a
      `data/tags.toml` sidecar (regenerated `postmortems.toml` can't hold them),
      rendered as clickable badges. Deterministic + Wikipedia-infobox tiers
      first; editorial/outcome tags via a **local** LLM classifier (ollama /
      llama.cpp, strict controlled vocab) — design in `docs/tags_plan.md`.

## Ideas / niceties
- [x] Intro "where to start" links: Game Developer's "10 seminal game
      postmortems" + Microsoft Research's "What Went Right and What Went Wrong"
      (an analysis of 155 Gamasutra postmortems — i.e. this very corpus).
- [x] Multi-part series: ingest /blogs/-shaped postmortems (Octodad Pt 1–3,
      "How much do indie PC devs make" Pt 1/8) via curated includes; render
      with series/part_no/part_total/part_label fields. **Parts of one series now
      collapse into a single grouped card** (`groupSeries`/`mergeSeries` in app.js;
      a lone curated part stays a normal entry). Blogs are schematically unlike
      features — og:image-less, body heroes on third-party hosts — so the scraper
      grows them their own path (verified oldest capture + im_-wrapped body image
      + curated thumbnail override).
- [x] Widen beyond URL-slug "postmortem": Tier A (44 /blogs/ from the HN/Reddit
      sweep) curated in. **Tier B** classic magazine reprints (29) + the **Round 2**
      gamedeveloper.com canon batch resolved via the migrated gamedeveloper.com page
      (`scraper/resolve_gamedev_originals.py`, now with a `--round2` mode and
      news/feature path handling); heroes sit in db_area/images/news/<id>/. Dataset 289.
- [ ] **Video / GDC postmortems (Tier C): deferred on purpose.** This is where the
      existing `category`/type field earns its keep — add a `format` (article|video)
      or a `video` category and render it distinctly, rather than pretending a GDC
      talk is a written article. Candidates listed in
      `docs/candidate_missing_gamasutra_gamedev_postmortems.md`.
- [ ] Pick the *best-rendered* snapshot per article for the Wayback link
      (some early captures are partial).
- [ ] Periodic refresh via GitHub Action (like the reference project).
- [x] Thumbnails (archived og:image).
- [x] Night mode toggle (persisted, defaults to OS preference).
