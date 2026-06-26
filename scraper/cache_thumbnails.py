#!/usr/bin/env python3
"""Mirror remote (Wayback) thumbnails into the repo so the static site serves
them itself instead of hotlinking a slow, prunable archive.

Permanent cache: an id whose thumbnail already points at data/thumbs/ — or whose
local file already exists — is skipped, so each image is fetched at most once.
A successful fetch is optimised (resized + re-encoded to webp when Pillow is
present) and the toml is rewritten to the local path; a failure leaves the
remote URL in place to retry on the next run.

Runs in the mirror-thumbnails GitHub Action (pip install pillow tomli-w) or
locally: nix-shell -p "python3.withPackages(p:[p.pillow p.tomli-w])" \\
  --run "python scraper/cache_thumbnails.py".

The remote is web.archive.org, which tolerates only a small burst before it
soft-blocks the runner's IP for a few minutes. So this is deliberately gentle:
--delay paces requests, fetch() backs off (honouring Retry-After) on 429/503/
connection resets, and the first sustained block raises RateLimited to stop the
run early — we keep what we mirrored and let the next scheduled run resume once
the block has lifted, rather than thrashing and marking the rest failed.

Flags: --limit N (mirror at most N new images this run), --delay S (seconds to
wait between fetches), --retries N (backoff attempts before a block stops the
run), --dry-run (fetch and optimise to validate, but write nothing).
"""
import io
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import tomllib
import tomli_w

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
THUMBS = DATA / "thumbs"
LOCAL_PREFIX = "data/thumbs/"
MAX_WIDTH = 240          # thumbnails render at 64–80px; 240 covers retina
WEBP_QUALITY = 80
# tomls whose entries carry a `thumbnail` to localise, and their array key
TARGETS = {"postmortems.toml": "postmortem", "blog_curation.toml": "entry"}
CT_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
          "image/gif": "gif", "image/webp": "webp"}
# HTTP statuses that mean "you're going too fast", not "this image is broken".
RATE_LIMIT_CODES = {429, 503}
RETRY_AFTER_CAP = 120    # never honour a Retry-After longer than this; bail instead


class RateLimited(Exception):
    """The archive is blocking us — stop the run and resume next time."""


def log(*a):
    print(*a, flush=True)


def is_remote(url):
    return url.startswith("http")


def count_remote():
    """How many thumbnails still point at a remote URL (i.e. not yet mirrored)."""
    n = 0
    for fname, key in TARGETS.items():
        path = DATA / fname
        if not path.exists():
            continue
        for r in tomllib.loads(path.read_text()).get(key, []):
            if is_remote((r.get("thumbnail") or "").strip()):
                n += 1
    return n


def emit_outputs(**kv):
    """Expose run results to the workflow (chain/stop decision) via step outputs."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def _safe_url(url):
    """Percent-encode chars urllib rejects (archived filenames carry spaces &c.)
    without double-encoding escapes already present."""
    return urllib.parse.quote(url, safe="/:?#[]@!$&'()*+,;=~%")


def _retry_after(headers):
    raw = headers.get("Retry-After") if headers else None
    try:
        return min(int(raw), RETRY_AFTER_CAP) if raw else None
    except ValueError:
        return None


def fetch(url, retries=3, delay=5.0):
    """Fetch a thumbnail, backing off on rate-limit/transient errors.

    Returns (bytes, content_type). Raises RateLimited if the archive keeps
    blocking us after `retries` backoffs (the caller stops the run); re-raises
    other HTTPErrors (e.g. 404) as a per-image failure.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(_safe_url(url), headers={"User-Agent": "mortolista-thumb-mirror"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code not in RATE_LIMIT_CODES:
                raise                       # a broken image, not a block
            last = e
            if attempt < retries:
                wait = _retry_after(e.headers) or delay * (2 ** attempt)
                log(f"    HTTP {e.code}; backing off {wait:.0f}s ({attempt + 1}/{retries})")
                time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e                        # reset/timeout: a soft block looks like this
            if attempt < retries:
                wait = delay * (2 ** attempt)
                log(f"    {type(e).__name__}: {e}; backing off {wait:.0f}s ({attempt + 1}/{retries})")
                time.sleep(wait)
    raise RateLimited(f"still blocked after {retries} retries: {last}")


def ext_from(content_type, url):
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CT_EXT:
        return CT_EXT[ct]
    tail = url.split("?")[0].rsplit(".", 1)
    if len(tail) == 2 and tail[1].lower() in ("jpg", "jpeg", "png", "gif", "webp"):
        return "jpg" if tail[1].lower() == "jpeg" else tail[1].lower()
    return "jpg"


def optimise(raw):
    """(bytes, ext): resize + webp when Pillow is present, else pass through."""
    if not HAVE_PIL:
        return raw, None
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGB")
    if im.width > MAX_WIDTH:
        im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)))
    out = io.BytesIO()
    im.save(out, "WEBP", quality=WEBP_QUALITY, method=6)
    return out.getvalue(), "webp"


def parse_args():
    a = sys.argv

    def opt(name, default, cast):
        return cast(a[a.index(name) + 1]) if name in a else default

    return {
        "limit": opt("--limit", 0, int),
        "delay": opt("--delay", 0.0, float),
        "retries": opt("--retries", 3, int),
        "dry": "--dry-run" in a,
    }


def main():
    args = parse_args()
    limit, delay, dry = args["limit"], args["delay"], args["dry"]
    THUMBS.mkdir(exist_ok=True)
    # id -> already-mirrored file on disk (the permanent cache)
    on_disk = {p.stem: p for p in THUMBS.glob("*.*")}
    failed = set()
    stats = {"linked": 0, "mirrored": 0, "failed": 0, "skipped_local": 0}
    blocked = False        # set once the archive rate-limits us; stops the run

    for fname, key in TARGETS.items():
        if blocked:
            break
        path = DATA / fname
        if not path.exists():
            continue
        rows = tomllib.loads(path.read_text()).get(key, [])
        changed = False
        for r in rows:
            url = (r.get("thumbnail") or "").strip()
            aid = str(r.get("id") or "")
            if not url or not aid or url.startswith(LOCAL_PREFIX):
                if url.startswith(LOCAL_PREFIX):
                    stats["skipped_local"] += 1
                continue
            if not is_remote(url):
                continue
            # Already mirrored (this or a previous run)? Just relink, never refetch.
            if aid in on_disk:
                rel = LOCAL_PREFIX + on_disk[aid].name
                if r["thumbnail"] != rel:
                    r["thumbnail"] = rel
                    changed = True
                stats["linked"] += 1
                continue
            if blocked or aid in failed or (limit and stats["mirrored"] >= limit):
                continue
            # pace ourselves so we don't trip the archive's burst limit
            if delay and stats["mirrored"]:
                time.sleep(delay)
            try:
                raw, ct = fetch(url, retries=args["retries"], delay=delay or 5.0)
                data, ext = optimise(raw)
                ext = ext or ext_from(ct, url)
                outp = THUMBS / f"{aid}.{ext}"
                if not dry:
                    outp.write_bytes(data)
                on_disk[aid] = outp
                r["thumbnail"] = LOCAL_PREFIX + outp.name
                changed = True
                stats["mirrored"] += 1
                log(f"  mirrored {aid}  {len(data) // 1024}KB {ext}  <- {url[:64]}")
            except RateLimited as e:
                blocked = True
                log(f"  [!] rate-limited: {e}")
                log("      stopping this run; the next scheduled run will resume.")
                break
            except Exception as e:
                failed.add(aid)
                stats["failed"] += 1
                log(f"  FAILED {aid}: {e}")
        if changed and not dry:
            path.write_bytes(tomli_w.dumps({key: rows}).encode())

    remaining = 0 if dry else count_remote()
    log(f"[*] thumbnails: {stats['mirrored']} mirrored, {stats['linked']} relinked, "
        f"{stats['skipped_local']} already local, {stats['failed']} failed, "
        f"{remaining} remote remaining"
        + ("  (stopped early: rate-limited)" if blocked else "")
        + ("  (dry run — nothing written)" if dry else ""))
    if not HAVE_PIL:
        log("[!] Pillow not installed — stored images unoptimised (raw bytes)")
    # the workflow chains another run only when this one made progress and more
    # remains, and only if we weren't blocked — so 404s can't spin forever.
    emit_outputs(mirrored=stats["mirrored"], remaining=remaining,
                 blocked=str(blocked).lower())


if __name__ == "__main__":
    main()
