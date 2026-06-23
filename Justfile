default:
    @just --list

check:
    nix flake check

serve:
    python -m http.server 8000

sample:
    python scraper/scrape.py --sample 20

scrape:
    python scraper/scrape.py
