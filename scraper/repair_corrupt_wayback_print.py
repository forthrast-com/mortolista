"""One-off repair for wayback_print URLs poisoned by a CDX 503 error page.

When the CDX endpoint 503'd, the old earliest/cdx helpers used the first line of
the HTML error body (`<html><body><h1>503 ...`) as a capture timestamp, baking it
into `web/<ts>/<url>` links. common.py is now hardened so this can't recur; this
script repairs the rows already written.

Crucially it is *three-state*: for each row it re-queries CDX for the print
variant and either restores the link from a real timestamp, blanks it when CDX
positively answers "no capture", or -- when CDX is unreachable/503 -- leaves the
row untouched. The same flakiness that caused the bug will hit us mid-run, and a
two-state "blank on empty" would discard real links during an outage. Re-runnable:
it targets rows still corrupt or already blanked, so repeated passes converge.
"""
import sys, time, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import WAYBACK_LINKS, load_toml, log, WAYBACK_TS_RE, SESSION  # noqa: E402
from links import print_url  # noqa: E402
import tomli_w  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POSTMORTEMS = ROOT / "data" / "postmortems.toml"
BACKUP = Path(sys.argv[1]) if len(sys.argv) > 1 else None  # pre-repair snapshot


def is_corrupt(url: str) -> bool:
    """True for a non-empty wayback_print whose /web/<ts>/ slug isn't 14 digits."""
    if not url:
        return False
    ts = url.split("/web/", 1)[-1].split("/", 1)[0]
    return not WAYBACK_TS_RE.match(ts)


def cdx_state(url: str, limit: int = 40):
    """('ok', caps) | ('absent', []) | ('unavailable', []).

    Distinguishes a definitive 200-with-no-rows from a 503/network failure -- the
    distinction the two-state cdx_captures throws away -- so the caller never
    blanks a link merely because Archive was sick when we asked."""
    q = ("http://web.archive.org/cdx/search/cdx?url=" + urllib.parse.quote(url, safe="")
         + "&output=text&fl=timestamp&filter=statuscode:200"
         + "&filter=mimetype:text/html&collapse=digest&limit=" + str(limit))
    try:
        r = SESSION.get(q, timeout=60)
    except Exception:
        return ("unavailable", [])
    if r.status_code != 200:
        return ("unavailable", [])
    toks = [ln.split()[0] for ln in r.text.splitlines() if ln.split()]
    caps = [t for t in toks if WAYBACK_TS_RE.match(t)]
    if caps:
        return ("ok", caps)
    # A clean 200 whose body still isn't timestamps (stray error page slipping
    # through with a 200) is safer treated as unavailable than as absent.
    return ("absent", []) if not toks else ("unavailable", [])


def resolve_print(print_target: str, tries: int = 6):
    """Retry through transient unavailability; only a positive 'ok'/'absent'
    short-circuits. Persistent unavailability after every retry stays unavailable."""
    state, caps = "unavailable", []
    for attempt in range(tries):
        state, caps = cdx_state(print_target)
        if state in ("ok", "absent"):
            return state, caps
        if attempt + 1 < tries:
            time.sleep(2.0 * (attempt + 1))
    return state, caps


def cdx_healthy() -> bool:
    state, _ = cdx_state("example.com", limit=1)
    return state in ("ok", "absent")


def main() -> int:
    if not cdx_healthy():
        log("[!] CDX is unreachable right now; aborting rather than risk blanking. "
            "Re-run when Archive is healthy.")
        return 1

    backup_corrupt = set()
    if BACKUP and BACKUP.exists():
        backup_corrupt = {r["id"] for r in load_toml(BACKUP).get("wayback_link", [])
                          if is_corrupt(r.get("wayback_print", ""))}

    orig_by_id = {a["id"]: a.get("original_url", "")
                  for a in load_toml(POSTMORTEMS).get("postmortem", [])}
    payload = load_toml(WAYBACK_LINKS)
    rows = payload.get("wayback_link", [])

    # Target rows still visibly corrupt, plus rows a prior pass blanked that the
    # backup proves were originally corrupt (i.e. the outage-blanked ones).
    targets = [r for r in rows
               if is_corrupt(r.get("wayback_print", ""))
               or (r["id"] in backup_corrupt and not r.get("wayback_print"))]
    log(f"[*] {len(targets)} rows to (re)resolve")

    restored, blanked, skipped = 0, 0, 0
    for i, row in enumerate(targets, 1):
        rid = row["id"]
        pu = print_url(orig_by_id.get(rid, ""))
        state, caps = resolve_print(pu) if pu else ("absent", [])
        if state == "ok":
            row["wayback_print"] = f"https://web.archive.org/web/{caps[0]}/{pu}"
            row["wayback_print_ok"] = True
            row["wayback_print_captures"] = len(caps)
            restored += 1
            log(f"[+] {i}/{len(targets)} {rid}: restored {caps[0]} ({len(caps)} caps)")
        elif state == "absent":
            row["wayback_print"] = ""
            row["wayback_print_ok"] = False
            row["wayback_print_captures"] = 0
            blanked += 1
            log(f"[-] {i}/{len(targets)} {rid}: no print capture; blanked")
        else:
            skipped += 1
            log(f"[~] {i}/{len(targets)} {rid}: CDX unavailable; left untouched")
        time.sleep(0.4)

    WAYBACK_LINKS.write_bytes(tomli_w.dumps(payload).encode())
    log(f"[*] done: {restored} restored, {blanked} blanked, {skipped} skipped "
        f"(unavailable), written to {WAYBACK_LINKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
