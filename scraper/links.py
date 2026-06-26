"""Link liveness: archive.today mirrors, Wayback links, gamedeveloper.com.

Split out of scrape.py; see that module for the CLI.
"""
import calendar, html as htmllib, json, re, sys, time, urllib.parse, unicodedata
from pathlib import Path
from datetime import datetime, timezone
from difflib import SequenceMatcher
import requests
import tomli_w
from common import ARCHIVE_MIRRORS, CHECK_HEAD, GAMEDEV_LIVE, SESSION, WAYBACK_LINKS, ascii_fold, cdx_captures, earliest_good_ts, http_exists, load_toml, log, wayback_available
from parse import canonical_slug, canonical_title
from hn import strip_hn_fields

def archive_today_url(original_url, host="archive.is"):
    return f"https://{host}/newest/{urllib.parse.quote(original_url, safe='')}"
def print_url(original_url):
    if not original_url:
        return ""
    sep = "&" if "?" in original_url else "?"
    return original_url if re.search(r"[?&]print=1(?:[#&]|$)", original_url) else original_url + sep + "print=1"
def wayback_print_capture(original_url, tries=2):
    """Earliest real 200 capture of the *print* variant, found by its own CDX
    lookup. The catalogue used to fabricate the print link from the base
    capture's timestamp, which read as live even when the ?print=1 page was never
    archived; this verifies the print page on its own terms. Returns a capture
    timestamp, or None when the print variant genuinely isn't archived.

    A flaky CDX returns empty just like a genuine absence, so retry a couple of
    times -- callers never cache a None, but this keeps a sick Archive from
    spuriously dropping print links that do exist."""
    pu = print_url(original_url)
    if not pu:
        return None
    for attempt in range(tries):
        ts = earliest_good_ts(pu)
        if ts:
            return ts
        if attempt + 1 < tries:
            time.sleep(1.5)
    return None
ARCHIVE_TODAY_HOSTS = ("archive.ph", "archive.today", "archive.is", "archive.vn")
def is_archive_today_host(netloc):
    return any(h in netloc.lower() for h in ARCHIVE_TODAY_HOSTS)
def archive_today_status(url, timeout=25):
    """Three-state liveness for an archive.is /newest/ mirror URL.

    archive.is 429s automated checks, so a plain boolean can't tell "no snapshot"
    from "we got rate-limited". Distinguish:
      - "ok":      /newest/ redirected to a real snapshot permalink (dated path,
                   no longer /newest/) on an archive.* host -> a capture exists.
      - "absent":  archive.is answered but kept us on the /newest/ submission page
                   -> it has nothing archived for this URL.
      - "unknown": 429 / timeout / network flak -> we genuinely can't tell; the
                   caller should preserve whatever it knew before rather than
                   flipping a known-good mirror to dead.
    """
    if not url:
        return "absent"
    try:
        r = SESSION.get(url, headers=CHECK_HEAD, timeout=timeout,
                        allow_redirects=True, stream=True)
        status, final = r.status_code, r.url
        r.close()
    except requests.RequestException:
        return "unknown"
    parsed = urllib.parse.urlparse(final)
    # URL *shape* is the reliable signal, not the final status code: when a
    # capture exists archive.is redirects /newest/ to a dated permalink
    # (/20351231.../ or a shortcode) -- even if that final hop then 429s us, the
    # redirect already proved the mirror exists. Only a still-on-/newest/ landing
    # or a 404 means there's genuinely nothing archived.
    if is_archive_today_host(parsed.netloc) and parsed.path.strip("/") and "/newest/" not in parsed.path:
        return "ok"
    if status == 404:
        return "absent"
    if status == 429 or status >= 500:
        return "unknown"
    if 200 <= status < 400:
        return "absent"  # answered, but never left the /newest/ submission form
    return "unknown"
def archive_today_available(url):
    return archive_today_status(url) == "ok"
def original_available(url):
    ok, final = http_exists(url, allow_redirect=True, timeout=20)
    if not ok:
        return False
    parsed = urllib.parse.urlparse(final)
    if "gamedeveloper.com" in parsed.netloc.lower() and parsed.path.rstrip("/") in ("", "/", "/latest-news"):
        return False
    return True
_gamedev_sitemap_cache = {}
def gamedev_archive_sitemap_urls(article):
    date = article.get("date", "")
    if not re.match(r"\d{4}-\d{2}-\d{2}$", date):
        return []
    year = int(date[:4])
    month = calendar.month_name[int(date[5:7])].lower()
    return [f"https://www.gamedeveloper.com/article/archive/{year}/{month}.xml"]
def gamedev_live_candidates(article):
    candidates = []
    for sitemap_url in gamedev_archive_sitemap_urls(article):
        if sitemap_url not in _gamedev_sitemap_cache:
            try:
                r = SESSION.get(sitemap_url, headers=CHECK_HEAD, timeout=30)
                _gamedev_sitemap_cache[sitemap_url] = r.text if r.status_code == 200 else ""
            except requests.RequestException:
                _gamedev_sitemap_cache[sitemap_url] = ""
        for href in re.findall(r"<loc>(.*?)</loc>", _gamedev_sitemap_cache[sitemap_url], re.I):
            href = htmllib.unescape(href).strip()
            if "gamedeveloper.com" not in href:
                continue
            text = urllib.parse.unquote(urllib.parse.urlparse(href).path.rsplit("/", 1)[-1])
            text = re.sub(r"[-_]+", " ", text)
            candidates.append((href, text))
    return candidates
def find_live_gamedeveloper_url(article):
    title = canonical_title(article)
    slug = canonical_slug(article)
    best = (0.0, "")
    seen = set()
    for href, text in gamedev_live_candidates(article):
        if href in seen or "/keyword/" in href or "/search" in href:
            continue
        seen.add(href)
        text_key = re.sub(r"\W+", " ", ascii_fold(text).lower()).strip()
        score = SequenceMatcher(None, title, text_key).ratio() if title else 0
        if slug:
            score = max(score, SequenceMatcher(None, slug, text_key).ratio())
        if score > best[0]:
            best = (score, href)
    if best[0] < 0.62:
        return ""
    # GameDeveloper redirects missing articles to section indexes with HTTP 200.
    # Sitemap URLs are the canonical source here; do a cheap sanity check without
    # following those soft-404 redirects away from the candidate path.
    ok, final = http_exists(best[1], allow_redirect=False, timeout=20)
    if not ok:
        return ""
    path = urllib.parse.urlparse(best[1]).path.rstrip("/")
    if path in ("", "/", "/business", "/design", "/programming", "/latest-news"):
        return ""
    return best[1]
def check_article_links(article):
    article["wayback_ok"] = wayback_available(article.get("wayback", ""))
    # Verify the print capture on its own terms (the wayback_links sidecar is the
    # canonical home for this; kept consistent here so --check-links doesn't
    # diverge or lean on a fabricated base-ts print URL).
    ts = wayback_print_capture(article.get("original_url", ""))
    article["wayback_print"] = (
        f"https://web.archive.org/web/{ts}/{print_url(article.get('original_url',''))}" if ts else "")
    article["wayback_print_ok"] = bool(ts)
    article["original_ok"] = original_available(article.get("original_url", ""))
    return article
def _mirror_ok(status, prior, key):
    """Resolve a three-state archive.is probe to a stored boolean. On "unknown"
    (429/flak) keep the prior value if we had one; otherwise stay False — the
    frontend still offers the bare /newest/ link, so we lose no fallback, we just
    don't *claim* a capture we never confirmed."""
    if status == "ok":
        return True
    if status == "absent":
        return False
    return bool(prior.get(key, False)) if prior else False
def archive_mirror_row(article, prior=None, recheck=False):
    """One archive.is mirror row, with a sticky per-URL cache.

    archive.is is the touchiest provider here (it 429s automated checks), and a
    mirror's existence doesn't change between runs -- so once a URL resolves to a
    definitive "ok"/"absent" we reuse that verdict instead of re-requesting it.
    We only (re)probe when forced (recheck), when there's no prior verdict, or
    when the prior was inconclusive ("unknown", i.e. a 429/flak last time). The
    returned row carries a transient "_probed" flag so the caller throttles only
    on requests it actually made.
    """
    prior = prior or {}
    original = article.get("original_url", "")
    az = archive_today_url(original)
    az_print = archive_today_url(print_url(original))

    def resolve(target, url_key, st_key, ok_key):
        cached = (not recheck and prior.get(url_key) == target
                  and prior.get(st_key) in ("ok", "absent"))
        if cached:
            return prior[st_key], bool(prior.get(ok_key, False)), False
        st = archive_today_status(target)
        return st, _mirror_ok(st, prior, ok_key), True

    st, ok, probed = resolve(az, "archive_today", "archive_today_state", "archive_today_ok")
    if az_print == az:
        st_print, ok_print, probed_print = st, ok, False
    else:
        st_print, ok_print, probed_print = resolve(
            az_print, "archive_today_print", "archive_today_print_state", "archive_today_print_ok")
    return {
        "id": article["id"],
        "archive_today": az,
        "archive_today_ok": ok,
        "archive_today_state": st,
        "archive_today_print": az_print,
        "archive_today_print_ok": ok_print,
        "archive_today_print_state": st_print,
        "_probed": probed or probed_print,
    }
def gamedev_live_row(article):
    live = find_live_gamedeveloper_url(article)
    return {"id": article["id"], "live_url": live, "live_ok": bool(live)}
def wayback_link_row(article, prior=None, recheck=False):
    """Wayback liveness sidecar row: the canonical capture and the print variant
    checked *independently*, so the frontend reflects each on its own terms.

    The Internet Archive is touchy/flaky, and a capture that exists doesn't
    vanish, so a confirmed-live verdict is sticky -- reused from the prior sidecar
    instead of re-requested. We never cache a negative (a flaked lookup or a
    not-yet-archived print page should retry next run). recheck busts the cache.
    The row carries a transient "_probed" flag so the caller throttles only on
    requests it actually made.
    """
    prior = prior or {}
    original = article.get("original_url", "")
    probed = False

    # Canonical capture count + liveness. Features already carry a count from the
    # bulk CDX sweep; the curated entries (blogs/Tier B) were never swept and sit
    # at zero, leaving them invisible on the captures axis. Reuse an existing or
    # cached count and only hit CDX to fill a genuine zero. captures>0 == live.
    cached_caps = prior.get("wayback_captures") if not recheck else None
    if cached_caps is None:
        cached_caps = article.get("wayback_captures", 0) or 0
    if cached_caps > 0:
        wb_caps = cached_caps
    else:
        wb_caps = len(cdx_captures(original)) if original else 0
        probed = True
    wb_ok = wb_caps > 0

    # Print variant, counted by its own CDX (sticky once confirmed live).
    if not recheck and prior.get("wayback_print_ok") and prior.get("wayback_print"):
        wp, wp_ok, wp_caps = prior["wayback_print"], True, prior.get("wayback_print_captures", 0)
    else:
        pcaps = cdx_captures(print_url(original)) if original else []
        wp = f"https://web.archive.org/web/{pcaps[0]}/{print_url(original)}" if pcaps else ""
        wp_ok = bool(pcaps)
        wp_caps = len(pcaps)
        probed = True

    return {
        "id": article["id"],
        "wayback_captures": wb_caps,
        "wayback_print_captures": wp_caps,
        "wayback_ok": wb_ok,
        "wayback_print": wp,
        "wayback_print_ok": wp_ok,
        "_probed": probed,
    }
def refresh_link_checks(data_path):
    """Refresh core Wayback/original link availability fields only."""
    data_path = Path(data_path)
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    for i, article in enumerate(articles, 1):
        strip_hn_fields(article)
        check_article_links(article)
        if i % 25 == 0:
            log(f"[*] checked links {i}/{len(articles)}")
    data_path.write_bytes(tomli_w.dumps(payload).encode())
    return len(articles)
def refresh_archive_mirrors(data_path, out_path=ARCHIVE_MIRRORS, limit=0, delay=4.0,
                            recheck=False):
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    if limit:
        articles = articles[:limit]
    prior = {}
    if Path(out_path).exists():  # absent on a first-ever run
        prior = {r.get("id"): r for r in load_toml(out_path).get("archive_mirror", [])}
    rows, live, probed = [], 0, 0
    for i, article in enumerate(articles, 1):
        row = archive_mirror_row(article, prior=prior.get(article["id"]), recheck=recheck)
        hit = row.pop("_probed", True)  # transient; never persist it
        rows.append(row)
        live += int(row["archive_today_ok"])
        if hit:
            probed += 1
            time.sleep(delay)  # only throttle on requests we actually made
        if i % 25 == 0:
            log(f"[*] archive.is {i}/{len(articles)} ({live} live, {probed} probed, "
                f"{i - probed} reused)")
    Path(out_path).write_bytes(tomli_w.dumps({"archive_mirror": rows}).encode())
    log(f"[*] archive.is: {live}/{len(rows)} confirmed-live mirrors "
        f"({probed} probed, {len(rows) - probed} reused from cache)")
    return len(rows)
def refresh_wayback_links(data_path, out_path=WAYBACK_LINKS, limit=0, delay=0.2,
                          recheck=False):
    """Wayback liveness sidecar: canonical + print capture checked separately."""
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    if limit:
        articles = articles[:limit]
    prior = {}
    if Path(out_path).exists():  # absent on a first-ever run
        prior = {r.get("id"): r for r in load_toml(out_path).get("wayback_link", [])}
    rows, base_live, print_live, probed = [], 0, 0, 0
    for i, article in enumerate(articles, 1):
        row = wayback_link_row(article, prior=prior.get(article["id"]), recheck=recheck)
        hit = row.pop("_probed", True)  # transient; never persist it
        rows.append(row)
        base_live += int(row["wayback_ok"])
        print_live += int(row["wayback_print_ok"])
        if hit:
            probed += 1
            time.sleep(delay)  # IA is touchy; stay gentle on the requests we make
        if i % 25 == 0:
            log(f"[*] wayback {i}/{len(articles)} ({base_live} canonical, "
                f"{print_live} print, {probed} probed)")
    Path(out_path).write_bytes(tomli_w.dumps({"wayback_link": rows}).encode())
    log(f"[*] wayback: {base_live}/{len(rows)} canonical live, "
        f"{print_live}/{len(rows)} print live ({probed} probed, "
        f"{len(rows) - probed} reused from cache)")
    return len(rows)
def refresh_gamedev_live(data_path, out_path=GAMEDEV_LIVE):
    payload = load_toml(data_path)
    articles = payload.get("postmortem", [])
    rows = []
    total_failures = 0
    for i, article in enumerate(articles, 1):
        rows.append(gamedev_live_row(article))
        if i % 25 == 0:
            log(f"[*] found live URLs {i}/{len(articles)}")
    Path(out_path).write_bytes(tomli_w.dumps({"gamedeveloper_live": rows}).encode())
    return len(rows)
