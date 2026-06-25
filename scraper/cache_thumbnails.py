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

Flags: --limit N (mirror at most N new images this run), --dry-run (fetch and
optimise to validate, but write nothing).
"""
import io
import sys
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


def log(*a):
    print(*a, flush=True)


def is_remote(url):
    return url.startswith("http")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mortolista-thumb-mirror"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read(), r.headers.get("Content-Type", "")


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
    limit, dry = 0, False
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--dry-run" in sys.argv:
        dry = True
    return limit, dry


def main():
    limit, dry = parse_args()
    THUMBS.mkdir(exist_ok=True)
    # id -> already-mirrored file on disk (the permanent cache)
    on_disk = {p.stem: p for p in THUMBS.glob("*.*")}
    failed = set()
    stats = {"linked": 0, "mirrored": 0, "failed": 0, "skipped_local": 0}

    for fname, key in TARGETS.items():
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
            if aid in failed or (limit and stats["mirrored"] >= limit):
                continue
            try:
                raw, ct = fetch(url)
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
            except Exception as e:
                failed.add(aid)
                stats["failed"] += 1
                log(f"  FAILED {aid}: {e}")
        if changed and not dry:
            path.write_bytes(tomli_w.dumps({key: rows}).encode())

    log(f"[*] thumbnails: {stats['mirrored']} mirrored, {stats['linked']} relinked, "
        f"{stats['skipped_local']} already local, {stats['failed']} failed"
        + ("  (dry run — nothing written)" if dry else ""))
    if not HAVE_PIL:
        log("[!] Pillow not installed — stored images unoptimised (raw bytes)")


if __name__ == "__main__":
    main()
