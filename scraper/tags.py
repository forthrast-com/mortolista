"""Derive browse tags (era / platform / studio / business) into data/tags.toml.

Two tiers, both reproducible:
  1. deterministic — era from the publish date, plus a few confident
     studio/format cues from the existing category/title (no network);
  2. Wikipedia-category — reuses the game->wiki-page match the sales pass
     already made (data/wikipedia_game_sales.toml gives us the title) and maps
     the page's categories ("Windows games", "Game Boy games", "Indie games",
     "Kickstarter-funded video games", "2004 video games", …) onto tags.

Editorial/outcome tags (crunch, scope-creep, breakout-success, …) are a
separate follow-up via a local LLM — see docs/tags_plan.md. They'd merge into
this same sidecar.
"""
import re

from common import DATA as DATA_DIR, load_toml, log, WIKI_SALES
from wiki import wiki_page

TAGS = DATA_DIR / "tags.toml"


def era_tag(date_str):
    """'2004-12-01' -> '00s'. Decade bucket from the (possibly estimated) date."""
    m = re.match(r"(\d{4})", date_str or "")
    if not m:
        return None
    year = int(m.group(1))
    if year < 1980 or year > 2099:
        return None
    return f"{(year - year % 10) % 100:02d}s"


# Deterministic studio/format cues read off the catalogue text. Kept conservative
# — anything ambiguous is left to the Wikipedia-category tier.
DET_TEXT_RULES = [
    ("studio", "indie", re.compile(r"\bindie\b", re.I)),
    ("studio", "student", re.compile(r"\bstudent\b", re.I)),
    ("platform", "flash", re.compile(r"\bflash game\b|\badobe flash\b", re.I)),
    ("platform", "mobile", re.compile(r"\b(iphone|ipad|\bios\b|android|j2me|mobile game)\b", re.I)),
    ("business", "early-access", re.compile(r"\bearly access\b", re.I)),
    ("business", "kickstarter", re.compile(r"\bkickstarter\b", re.I)),
]


def deterministic_tags(article):
    """Studio/format tags derivable from the entry alone (no network).

    Era is handled separately in refresh_tags so the game's original release
    year (from Wikipedia) can win over the article's publish date for reprints.
    """
    tags = set()
    cat = (article.get("category") or "").lower()
    if "indie" in cat:
        tags.add("indie")
    if "student" in cat:
        tags.add("student")
    hay = " ".join(str(article.get(k, "")) for k in ("title", "game"))
    for _axis, tag, rx in DET_TEXT_RULES:
        if rx.search(hay):
            tags.add(tag)
    return tags


# Wikipedia category -> tag. Platform rules are ordered: the first that matches a
# given category wins for that category (so a handheld category isn't also read
# as a console). Across a game's many categories we still collect every distinct
# platform — a multi-platform game earns pc + console + handheld, as it should.
_HANDHELD = re.compile(
    r"\b(Game Boy|Game Gear|Nintendo DS|Nintendo 3DS|PlayStation Portable|"
    r"PlayStation Vita|Atari Lynx|WonderSwan|Neo Geo Pocket|N-Gage) games\b", re.I)
_CONSOLE = re.compile(
    r"\b(PlayStation|Xbox|GameCube|Wii U|Wii|Nintendo Switch|Nintendo 64|"
    r"Super Nintendo|Nintendo Entertainment System|Dreamcast|Sega (Genesis|Saturn|"
    r"Mega Drive|CD|32X)|TurboGrafx|Neo Geo) games\b", re.I)
_PC = re.compile(
    r"\b(Windows|Linux|macOS|Mac OS|Classic Mac OS|MS-DOS|DOS|Amiga|"
    r"Commodore 64|Atari ST|ZX Spectrum) games\b", re.I)
_MOBILE = re.compile(r"\b(Android|iOS|IOS|Windows Phone|BlackBerry|J2ME|Java platform) games\b", re.I)
_ARCADE = re.compile(r"\bArcade (video )?games\b", re.I)
_FLASH = re.compile(r"\b(Adobe Flash|Flash) games\b", re.I)
_WEB = re.compile(r"\bBrowser games\b", re.I)
_VR = re.compile(r"\b(Virtual reality|Oculus|PlayStation VR|SteamVR|Windows Mixed Reality) games\b", re.I)
PLATFORM_RULES = [  # ordered; first match per category wins
    ("handheld", _HANDHELD), ("console", _CONSOLE), ("pc", _PC), ("mobile", _MOBILE),
    ("arcade", _ARCADE), ("flash", _FLASH), ("web", _WEB), ("vr", _VR),
]
# Independent (non-platform) category rules — all that match are collected.
_YEAR = re.compile(r"\b((?:19|20)\d{2}) video games\b", re.I)
OTHER_RULES = [
    ("indie", re.compile(r"\bIndie (video )?games\b", re.I)),
    ("kickstarter", re.compile(r"\bKickstarter[- ]funded\b", re.I)),
    ("early-access", re.compile(r"\bEarly access (video )?games\b", re.I)),
]


def wiki_category_tags(categories):
    """Map a wiki page's category titles onto platform/studio/business tags."""
    titles = [c.get("title", "").replace("Category:", "") for c in (categories or [])]
    tags = set()
    for title in titles:
        for tag, rx in PLATFORM_RULES:
            if rx.search(title):
                tags.add(tag)
                break  # one platform per category
        for tag, rx in OTHER_RULES:
            if rx.search(title):
                tags.add(tag)
    return tags


def wiki_years(categories):
    """Release years from a wiki page's 'YYYY video games' categories."""
    out = set()
    for c in categories or []:
        m = _YEAR.search(c.get("title", ""))
        if m:
            out.add(int(m.group(1)))
    return sorted(out)


def refresh_tags(data_path, out_path=TAGS, limit=0):
    """Write data/tags.toml: deterministic + Wikipedia-category tags per entry."""
    import tomli_w

    articles = load_toml(data_path).get("postmortem", [])
    if limit:
        articles = articles[:limit]
    wiki_title = {row["id"]: row.get("wiki_title", "")
                  for row in load_toml(WIKI_SALES).get("wiki_game_sales", [])}

    page_cache = {}  # wiki_title -> categories (one fetch per distinct title)
    rows = []
    for i, art in enumerate(articles, 1):
        tags = deterministic_tags(art)
        cats = []
        title = wiki_title.get(art["id"], "")
        if title:
            if title not in page_cache:
                page = wiki_page(title)
                page_cache[title] = (page or {}).get("categories", [])
            cats = page_cache[title]
            tags |= wiki_category_tags(cats)
        # Single era: the game's earliest Wikipedia release year (so a classic
        # reprinted years later still reads as its own decade), else the article date.
        years = wiki_years(cats)
        era = era_tag(str(years[0])) if years else era_tag(art.get("date", ""))
        if era:
            tags.add(era)
        if tags:
            rows.append({"id": art["id"], "tags": sorted(tags)})
        if i % 25 == 0:
            log(f"[*] tagged {i}/{len(articles)}")
    out_path.write_bytes(tomli_w.dumps({"tag": rows}).encode())
    n_wiki = sum(1 for t in page_cache.values() if t)
    log(f"[*] wrote tags for {len(rows)} entries ({n_wiki} wiki-matched) -> {out_path}")
    return len(rows)
