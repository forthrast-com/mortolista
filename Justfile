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
# Both steps are idempotent (resolve skips ids already present; ingest skips
# entries already in the dataset), and re-running is exactly how entries that
# flaked on a sick Archive get picked up next time.
# Phase B fans out — reddit (Arctic Shift), HN (Algolia), archive.is mirrors, wiki
# sales (Wikipedia), and the Tier A liveness sweep each hit a different service and
# write a different sidecar, so they run in parallel. Caching is per-request inside
# each step (archive.is reuses resolved-mirror verdicts; reddit skips already-probed
# URLs), so re-runs don't re-hit the touchy archive providers. Per-step logs land in
# scraper/logs/.
refresh-all:
    #!/usr/bin/env bash
    set -uo pipefail
    mkdir -p scraper/logs

    echo ">> phase A: Tier B resolve -> ingest (Internet Archive, sequential)"
    python scraper/resolve_gamedev_originals.py --append 2>&1 | tee scraper/logs/tier-b-resolve.log
    python scraper/add_curated_blogs.py 2>&1 | tee scraper/logs/ingest.log

    echo ">> phase B: enrichment fan-out (reddit | hn | archive.is | wiki | tier-a)"
    pids=""; names=""
    launch() {  # launch <name> <cmd...>
        local name="$1"; shift
        echo "   [run]  $name"
        "$@" > "scraper/logs/$name.log" 2>&1 &
        pids="$pids $!"; names="$names $name"
    }
    launch reddit     python scraper/scrape.py --reddit-only
    launch hn         python scraper/scrape.py --refresh-hn-metrics
    launch archive-is python scraper/scrape.py --archive-mirrors-only
    launch wiki       python scraper/scrape.py --wiki-sales-only
    launch tier-a     python scraper/tier_a_liveness.py

    set -- $pids; rc=0
    for name in $names; do
        wait "$1"; s=$?; shift
        echo "   $name exit=$s"; rc=$((rc + s))
    done
    [ "$rc" -eq 0 ] && echo ">> refresh-all: OK" || echo ">> refresh-all: some phase-B steps failed (see scraper/logs/)"

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
