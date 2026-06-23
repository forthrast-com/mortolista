default:
    @just --list

check:
    nix flake check

serve:
    python -m http.server 8000

hn:
    python scraper/scrape.py --hn-only

sample:
    python scraper/scrape.py --sample 20

scrape:
    python scraper/scrape.py
