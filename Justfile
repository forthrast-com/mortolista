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

# Full data refresh. Phase A (sequential, Internet Archive): resolve the Tier B
# reprints back to their gamasutra originals, then ingest every curated include.
# Phase B fans out — reddit (Arctic Shift), HN (Algolia), archive.is mirrors, and
# the Tier A liveness sweep each hit a different service and write a different
# sidecar, so they run in parallel. Per-step logs land in scraper/logs/.
refresh-all:
    #!/usr/bin/env bash
    set -uo pipefail
    mkdir -p scraper/logs
    echo ">> phase A: Tier B resolve -> ingest (Internet Archive, sequential)"
    python scraper/resolve_gamedev_originals.py --append 2>&1 | tee scraper/logs/tier-b-resolve.log
    python scraper/add_curated_blogs.py 2>&1 | tee scraper/logs/ingest.log
    echo ">> phase B: enrichment fan-out (reddit | hn | archive.is | tier-a)"
    python scraper/scrape.py --reddit-only         > scraper/logs/reddit.log     2>&1 & p_reddit=$!
    python scraper/scrape.py --refresh-hn-metrics   > scraper/logs/hn.log         2>&1 & p_hn=$!
    python scraper/scrape.py --archive-mirrors-only > scraper/logs/archive-is.log 2>&1 & p_ais=$!
    python scraper/tier_a_liveness.py               > scraper/logs/tier-a-live.log 2>&1 & p_tiera=$!
    wait $p_reddit; r_reddit=$?
    wait $p_hn;     r_hn=$?
    wait $p_ais;    r_ais=$?
    wait $p_tiera;  r_tiera=$?
    echo ">> phase B exit codes: reddit=$r_reddit hn=$r_hn archive.is=$r_ais tier-a=$r_tiera"
    tail -n 3 scraper/logs/reddit.log scraper/logs/hn.log scraper/logs/archive-is.log scraper/logs/tier-a-live.log
    if [ $((r_reddit + r_hn + r_ais + r_tiera)) -eq 0 ]; then
        echo ">> refresh-all: OK"
    else
        echo ">> refresh-all: some phase-B steps failed (see scraper/logs/)"
    fi

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
