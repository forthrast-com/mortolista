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

sample:
    python scraper/scrape.py --sample 20

scrape:
    python scraper/scrape.py
