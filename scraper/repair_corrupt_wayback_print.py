"""One-off repair for wayback_print URLs poisoned by a CDX 503 error page.

When the CDX endpoint 503'd, the old earliest/cdx helpers used the first line of
the HTML error body (`<html><body><h1>503 ...`) as a capture timestamp, baking it
into `web/<ts>/<url>` links. common.py is now hardened so this can't recur; this
script repairs the rows already written. For each corrupt row it re-queries CDX
for the print variant (with retries, since the same flakiness that caused the bug
will hit us here) and rewrites the link from a real timestamp -- or blanks it when
the print page genuinely isn't archived.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (WAYBACK_LINKS, load_toml, cdx_captures, log,  # noqa: E402
                    WAYBACK_TS_RE)
from links import print_url  # noqa: E402
import tomli_w  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POSTMORTEMS = ROOT / "data" / "postmortems.toml"


def is_corrupt(url: str) -> bool:
    """True for a non-empty wayback_print whose /web/<ts>/ slug isn't 14 digits."""
    if not url:
        return False
    after = url.split("/web/", 1)[-1]
    ts = after.split("/", 1)[0]
    return not WAYBACK_TS_RE.match(ts)


def resolve_print(print_target: str, tries: int = 6) -> list[str]:
    """cdx_captures with retries: only an empty result after every retry means
    'genuinely absent'. The hardened cdx_captures returns [] on a 503, so without
    retries a flake would read as absence and blank a real link."""
    for attempt in range(tries):
        caps = cdx_captures(print_target)
        if caps:
            return caps
        if attempt + 1 < tries:
            time.sleep(2.0 * (attempt + 1))
    return []


def main() -> int:
    orig_by_id = {a["id"]: a.get("original_url", "")
                  for a in load_toml(POSTMORTEMS).get("postmortem", [])}
    payload = load_toml(WAYBACK_LINKS)
    rows = payload.get("wayback_link", [])

    corrupt = [r for r in rows if is_corrupt(r.get("wayback_print", ""))]
    log(f"[*] {len(corrupt)} corrupt wayback_print rows to repair")

    restored, blanked = 0, 0
    for i, row in enumerate(corrupt, 1):
        rid = row.get("id", "")
        original = orig_by_id.get(rid, "")
        pu = print_url(original) if original else ""
        caps = resolve_print(pu) if pu else []
        if caps:
            row["wayback_print"] = f"https://web.archive.org/web/{caps[0]}/{pu}"
            row["wayback_print_ok"] = True
            row["wayback_print_captures"] = len(caps)
            restored += 1
            log(f"[+] {i}/{len(corrupt)} {rid}: restored {caps[0]} ({len(caps)} caps)")
        else:
            row["wayback_print"] = ""
            row["wayback_print_ok"] = False
            row["wayback_print_captures"] = 0
            blanked += 1
            log(f"[-] {i}/{len(corrupt)} {rid}: no print capture; blanked")
        time.sleep(0.4)

    WAYBACK_LINKS.write_bytes(tomli_w.dumps(payload).encode())
    log(f"[*] done: {restored} restored, {blanked} blanked, written to {WAYBACK_LINKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
