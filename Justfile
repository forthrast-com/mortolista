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

reddit:
    python scraper/scrape.py --reddit-only

reddit-sample:
    python scraper/scrape.py --reddit-only --limit 5

reddit-metrics:
    python scraper/scrape.py --reddit-recompute

notable-authors:
    python scraper/scrape.py --notable-authors-only

wiki-sales:
    python scraper/scrape.py --wiki-sales-only

wiki-sales-sample:
    python scraper/scrape.py --wiki-sales-only --limit 10

sample:
    python scraper/scrape.py --sample 20

scrape:
    python scraper/scrape.py
