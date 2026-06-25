default:
    @just --list

check:
    nix flake check

serve:
    python -m http.server 8000

hn:
    python scraper/scrape.py --hn-only

hn-audit:
    python scraper/scrape.py --hn-audit

hn-metrics:
    python scraper/scrape.py --refresh-hn-metrics

check-links:
    python scraper/scrape.py --no-enrich --check-links

archive-mirrors:
    python scraper/scrape.py --archive-mirrors-only

gamedev-live:
    python scraper/scrape.py --gamedev-live-only

# Liveness probe over the curated /blogs/ (Tier A) includes. Append `--limit N`
# for a quick sample; bare run sweeps them all (slow when the Archive flaks).
tier-a-live *ARGS:
    python scraper/tier_a_liveness.py {{ARGS}}

reddit:
    python scraper/scrape.py --reddit-only

reddit-sample:
    python scraper/scrape.py --reddit-only --limit 5

reddit-metrics:
    python scraper/scrape.py --reddit-recompute

notable-authors:
    python scraper/scrape.py --notable-authors-only

author-bios:
    python scraper/scrape.py --author-bios-only

author-bios-sample:
    python scraper/scrape.py --author-bios-only --limit 10

wiki-sales:
    python scraper/scrape.py --wiki-sales-only

wiki-sales-sample:
    python scraper/scrape.py --wiki-sales-only --limit 10

sample:
    python scraper/scrape.py --sample 20

scrape:
    python scraper/scrape.py
