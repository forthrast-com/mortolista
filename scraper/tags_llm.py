"""Editorial tags + studio extraction via an OpenAI-compatible chat API (tier 3).

Pass 1 (per entry, `--tags-llm-only`): classify each postmortem's summary against
a fixed editorial vocab (outcome / production theme / business, incl. `port`) and
extract a few free-text facts — the developer `studio`, the `engine`/tech, and an
approximate `team_size`. Writes data/tags_llm.toml.

Pass 2 (per *unique* studio, `--studios-only`): tally the studio names, keep the
ones at/above a notability threshold (STUDIO_MIN_COUNT, default 2), and classify
each *once* into aaa/indie/solo/hobbyist/student. Writes data/tags_studios.toml.

Both run against either an OpenAI-compatible `/v1/chat/completions` endpoint —
local (ollama, llama.cpp, LM Studio) or hosted (e.g. cocore.dev) — or, with
`TAGS_LLM_PROVIDER=anthropic`, the native Claude Messages API. Separate sidecars
so `just tags` (tiers 1-2) regenerates without clobbering these; the frontend
unions them. Requests are issued concurrently (TAGS_LLM_CONCURRENCY).

Config (env):
  TAGS_LLM_PROVIDER     default "openai" ("openai" = /v1/chat/completions; "anthropic" = Claude Messages API)
  TAGS_LLM_BASE_URL     default http://localhost:11434/v1  (openai provider only)
  TAGS_LLM_API_KEY      default ""  (openai: sent as Bearer; anthropic: overrides ANTHROPIC_API_KEY when set)
  TAGS_LLM_MODEL        default gemma3:12b (openai) / claude-opus-4-8 (anthropic)
  TAGS_LLM_MAX_TOKENS   default 512 (openai) / 2048 (anthropic — room for thinking)
  TAGS_LLM_THINKING     default off (openai) / on (anthropic) — adaptive thinking
  TAGS_LLM_CONCURRENCY  default 6   (parallel in-flight requests)
  STUDIO_MIN_COUNT      default 2   (pass-2 notability threshold)

Idempotent: pass 1 skips ids already present unless refresh; both flush
periodically so an interrupted run resumes cheaply.
"""
import json
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import DATA as DATA_DIR, load_toml, log, SESSION

TAGS_LLM = DATA_DIR / "tags_llm.toml"
TAGS_STUDIOS = DATA_DIR / "tags_studios.toml"

PROVIDER = os.environ.get("TAGS_LLM_PROVIDER", "openai").strip().lower()
BASE_URL = os.environ.get("TAGS_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.environ.get("TAGS_LLM_API_KEY", "")
MODEL = os.environ.get("TAGS_LLM_MODEL", "claude-opus-4-8" if PROVIDER == "anthropic" else "gemma3:12b")
# Anthropic needs headroom for adaptive thinking; the local default stays tight.
MAX_TOKENS = int(os.environ.get("TAGS_LLM_MAX_TOKENS", "2048" if PROVIDER == "anthropic" else "512"))
# Adaptive thinking is a Claude 4.6+ feature; Haiku 4.5 rejects it with a 400, so
# default it off there. Explicit TAGS_LLM_THINKING always wins.
_thinking_default = "on" if (PROVIDER == "anthropic" and "haiku" not in MODEL.lower()) else "off"
THINKING = os.environ.get("TAGS_LLM_THINKING", _thinking_default).strip().lower() not in ("", "0", "off", "false", "no")
CONCURRENCY = max(1, int(os.environ.get("TAGS_LLM_CONCURRENCY", "6")))
STUDIO_MIN_COUNT = max(1, int(os.environ.get("STUDIO_MIN_COUNT", "2")))
# Where requests actually go, for logging.
TARGET = "api.anthropic.com" if PROVIDER == "anthropic" else BASE_URL

# Controlled vocab. Mostly process/factual tags. Commercial-outcome tags
# (breakout / commercial-failure) are included but gated hard in the prompt: apply
# ONLY when the summary explicitly states market performance, never inferred from
# development troubles (a weaker model conflates "what went wrong in dev" — which
# every postmortem dwells on — with commercial failure). `port` also carries a
# target-platform tag so it slots into the platform axis.
THEME = {
    "crunch": "significant overtime, crunch, long hours, or a death march described",
    "scope-creep": "scope or feature set grew well beyond the original plan",
    "pivot": "the project changed direction or genre significantly mid-development",
    "first-game": "the studio's or team's debut game",
    "port": "the postmortem is mainly about porting an existing game to a new platform",
    "engine-switch": "changed engine or core tech mid-project",
    "team-conflict": "notable internal team or management conflict",
    "long-dev": "unusually long development (many years)",
    "rushed": "shipped under heavy time pressure / unfinished",
    "meta": "RARE — a meta-article ABOUT postmortems or the publication itself: a "
            "roundup of multiple postmortems, a piece on Game Developer magazine, or "
            "an essay on the postmortem format/terminology. A postmortem of any actual "
            "product is NEVER meta — a game, tool, middleware, course, or "
            "crowdfunding campaign all get tagged normally, never meta",
}
BUSINESS = {
    "kickstarter": "crowdfunded (Kickstarter / IndieGoGo / similar)",
    "early-access": "released through Early Access",
    "self-published": "the developer self-published",
    "work-for-hire": "made under contract / work-for-hire",
}
OUTCOME = {
    "breakout": "the summary EXPLICITLY describes a standout, better-than-expected hit — a surprise success, or a debut / new IP that punched above its weight; NOT routine strong sales of a sequel or an entry in an already-successful series",
    "commercial-failure": "the summary EXPLICITLY says the game sold poorly, lost money, or failed commercially",
}
PLATFORM = {
    "pc": "PC / Mac / Linux desktop",
    "console": "home console (PlayStation, Xbox, GameCube, Wii, Dreamcast, N64, …)",
    "handheld": "handheld console (DS/3DS, PSP/Vita, Game Boy, …)",
    "mobile": "phone or tablet (iOS, Android, J2ME, WAP)",
    "arcade": "arcade machine",
    "web": "browser / Flash",
    "vr": "virtual reality",
}
PORT_PLATFORMS = list(PLATFORM)
VOCAB = set(THEME) | set(BUSINESS) | set(OUTCOME) | set(PLATFORM)
STUDIO_CLASSES = {"aaa", "indie", "solo", "hobbyist", "student"}


def _vocab_block():
    lines = []
    for group, d in (("PRODUCTION", THEME), ("BUSINESS", BUSINESS), ("OUTCOME", OUTCOME), ("PLATFORM", PLATFORM)):
        lines.append(f"# {group}")
        lines += [f"- {tag}: {gloss}" for tag, gloss in d.items()]
    return "\n".join(lines)


SYSTEM = (
    "You label video-game postmortems. Given a postmortem's game, title, and "
    "summary, return ONLY a JSON object:\n"
    '{"tags": [...], "studio": "<developer name or null>", '
    '"engine": "<engine/tech or null>", "team_size": <int or null>}\n\n'
    "tags: apply only those from the fixed list below that the text clearly "
    "supports (empty list if none); never invent tags.\n"
    "studio: the developer's canonical name. It is usually a possessive in the "
    "title or game (\"Bungie's Myth\" -> Bungie, \"Valve's Design Process\" -> "
    "Valve, \"Looking Glass' Thief\" -> Looking Glass); use that. If the game is "
    "well-known, infer its developer (Half-Life -> Valve, SimCity -> Maxis). Use "
    "the same form every time; null only if genuinely unclear.\n"
    "engine: the engine or core tech if named (e.g. \"Unreal\", \"Unity\", "
    "\"id Tech\", \"custom\", \"Flash\"), else null.\n"
    "team_size: approximate number of people if stated, else null.\n\n"
    + _vocab_block()
    + "\n\nCommercial-outcome tags (breakout, commercial-failure) are special: apply "
    "them ONLY when the summary EXPLICITLY states how the game performed in the "
    "market (e.g. \"sold over a million copies\", \"became a surprise hit\", \"was a "
    "commercial failure\", \"flopped\", \"never recouped its budget\"). NEVER infer "
    "a commercial outcome from development difficulties — every postmortem dwells on "
    "what went wrong while making the game, and that says nothing about how it sold. "
    "Reserve `breakout` for genuine standout successes — a surprise hit, a debut, or "
    "new IP that clearly exceeded expectations; do NOT tag `breakout` for a sequel or "
    "a new installment of an already-popular series just because it sold well.\n\n"
    "Special rule for `port`: if the postmortem is mainly about porting an "
    "existing game to a new platform, include \"port\" AND the target platform.\n\n"
    "PLATFORM: also include the game's primary platform from the PLATFORM list when "
    "the title/summary states it or it is well known — e.g. \"DS\"/\"3DS\"/\"Game Boy\" "
    "-> handheld; \"PS2\"/\"PS3\"/\"Xbox\"/\"GameCube\"/\"Dreamcast\"/\"Wii\"/\"N64\" -> "
    "console; \"iPhone\"/\"Android\"/\"WAP\"/\"J2ME\" -> mobile; arcade -> arcade; "
    "browser/Flash -> web. PC is a real lead platform too — a PC-first game (most "
    "90s–2000s desktop titles, and every Valve game such as Half-Life) gets \"pc\". "
    "Give the lead platform (two at most); omit only for a broadly multi-platform "
    "release with no clear lead."
)

FEWSHOT = [
    ({"game": "Game A", "title": "Postmortem: Game A",
      "summary": "After a successful Kickstarter the two-person studio Tiny Boat "
                 "spent four years on their first game in Unity, crunching hard near "
                 "the end; it became a surprise hit."},
     {"tags": ["kickstarter", "first-game", "long-dev", "crunch", "breakout"],
      "studio": "Tiny Boat", "engine": "Unity", "team_size": 2}),
    ({"game": "Game B", "title": "Postmortem: porting Game B to PlayStation 2",
      "summary": "How Acme brought our PC engine to the PS2, the memory battles, and "
                 "what shipping the console version taught us."},
     {"tags": ["port", "console"], "studio": "Acme", "engine": "custom", "team_size": None}),
    ({"game": "Game C", "title": "Postmortem: Game C",
      "summary": "A straightforward retrospective on our level design choices."},
     {"tags": [], "studio": None, "engine": None, "team_size": None}),
    ({"game": "Game D", "title": "Postmortem: Game D",
      "summary": "A gruelling three-year project: we rewrote the engine twice, scope "
                 "ballooned far beyond plan, and the team crunched for the final six "
                 "months just to ship."},
     {"tags": ["long-dev", "engine-switch", "scope-creep", "crunch"],
      "studio": None, "engine": None, "team_size": None}),
    ({"game": "Game E II", "title": "Postmortem: Game E II",
      "summary": "The sequel to our hit debut, Game E II sold over a million copies in "
                 "its first months — a strong follow-up the established fanbase "
                 "expected after the original's success."},
     {"tags": [], "studio": None, "engine": None, "team_size": None}),
    ({"game": "Game F", "title": "Postmortem: Game F (Nintendo DS)",
      "summary": "Bringing our quirky rhythm game to the DS, and what the dual screen "
                 "and stylus let us design around."},
     {"tags": ["handheld"], "studio": None, "engine": None, "team_size": None}),
    ({"game": "", "title": "A Roundup of the Year's Best Postmortems",
      "summary": "We gather highlights from a dozen postmortems and what they share — "
                 "an article about the postmortem form itself, not any one game."},
     {"tags": ["meta"], "studio": None, "engine": None, "team_size": None}),
]


def _user_prompt(article):
    summary = (article.get("summary") or "")[:1500]
    return (f"Game: {article.get('game', '')}\n"
            f"Title: {article.get('title', '')}\n"
            f"Summary: {summary}\n\nJSON:")


def _messages(article):
    msgs = [{"role": "system", "content": SYSTEM}]
    for ex, out in FEWSHOT:
        msgs.append({"role": "user", "content": _user_prompt(ex)})
        msgs.append({"role": "assistant", "content": json.dumps(out)})
    msgs.append({"role": "user", "content": _user_prompt(article)})
    return msgs


_ANTHROPIC_CLIENT = None
_ANTHROPIC_LOCK = threading.Lock()


def _anthropic_client():
    """Lazily build a thread-safe Anthropic client (api_key from TAGS_LLM_API_KEY
    if set, else the ANTHROPIC_API_KEY env var the SDK reads by default)."""
    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT is None:
        with _ANTHROPIC_LOCK:
            if _ANTHROPIC_CLIENT is None:
                import anthropic
                _ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=API_KEY) if API_KEY else anthropic.Anthropic()
    return _ANTHROPIC_CLIENT


def _chat_openai(messages, timeout=120):
    """One OpenAI-compatible chat completion; message content or None on failure."""
    body = {"model": MODEL, "messages": messages, "temperature": 0, "max_tokens": MAX_TOKENS}
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    try:
        r = SESSION.post(f"{BASE_URL}/chat/completions", json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  [!] LLM call failed: {e}")
        return None


def _chat_anthropic(messages, timeout=120):
    """Same turn against the Claude Messages API. Anthropic carries the system
    prompt out-of-band, so hoist any system messages into the `system` field and
    leave the user/assistant few-shot turns in `messages`."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    kwargs = {"model": MODEL, "max_tokens": MAX_TOKENS, "messages": convo}
    if system:
        kwargs["system"] = system
    if THINKING:
        kwargs["thinking"] = {"type": "adaptive"}
    try:
        resp = _anthropic_client().with_options(timeout=timeout).messages.create(**kwargs)
        return "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:
        log(f"  [!] LLM call failed: {e}")
        return None


def _chat(messages, timeout=120):
    """One classification turn; returns the text content or None on any failure."""
    if PROVIDER == "anthropic":
        return _chat_anthropic(messages, timeout)
    return _chat_openai(messages, timeout)


def _last_json(content):
    """Last flat {...} object in a response, tolerant of <think>/harmony preambles."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    for blob in reversed(re.findall(r"\{[^{}]*\}", content, re.S)):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean_str(v):
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v if v and v.lower() not in ("null", "none", "n/a", "unknown", "") else None


def _clean_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v)
        return int(m.group(0)) if m else None
    return None


def classify(article, timeout=120):
    """Per-entry fields dict {tags, studio, engine, team_size}, or None on failure."""
    content = _chat(_messages(article), timeout)
    if content is None:
        return None
    data = _last_json(content) or {}
    seen, tags = set(), []
    for t in (data.get("tags") or []):
        t = str(t).strip().lower()
        if t in VOCAB and t not in seen:
            seen.add(t)
            tags.append(t)
    return {"tags": tags, "studio": _clean_str(data.get("studio")),
            "engine": _clean_str(data.get("engine")), "team_size": _clean_int(data.get("team_size"))}


def _row(aid, fields):
    """Compact tags_llm.toml row — drop empty/None fields."""
    row = {"id": aid}
    if fields["tags"]:
        row["tags"] = fields["tags"]
    for k in ("studio", "engine"):
        if fields.get(k):
            row[k] = fields[k]
    if fields.get("team_size") is not None:
        row["team_size"] = fields["team_size"]
    return row


def refresh_llm_tags(data_path, out_path=TAGS_LLM, limit=0, refresh=False, dry_run=False):
    """Pass 1: classify + extract per entry, concurrently, into data/tags_llm.toml."""
    import tomli_w

    articles = load_toml(data_path).get("postmortem", [])
    if limit:
        articles = articles[:limit]

    if dry_run:
        key_set = bool(API_KEY) or (PROVIDER == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"))
        log(f"[dry-run] provider={PROVIDER} endpoint={TARGET} model={MODEL} thinking={THINKING} "
            f"concurrency={CONCURRENCY} key={'set' if key_set else 'none'}")
        for m in _messages(articles[0]):
            log(f"--- {m['role']} ---\n{m['content']}")
        return 0

    prior = load_toml(out_path).get("editorial_tag", []) if out_path.exists() else []
    done = {r["id"]: r for r in prior}
    todo = [a for a in articles if refresh or a["id"] not in done]
    log(f"[*] {len(todo)} to classify ({len(done)} done) via {MODEL} @ {TARGET} x{CONCURRENCY}")

    lock = threading.Lock()

    def flush():
        rows = sorted(done.values(), key=lambda r: r["id"])
        out_path.write_bytes(tomli_w.dumps({"editorial_tag": rows}).encode())

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(classify, a): a for a in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            aid = futs[fut]["id"]
            fields = fut.result()
            if fields is None:
                continue  # failed; leave for a re-run
            with lock:
                row = _row(aid, fields)
                if len(row) > 1:          # has at least one extracted field
                    done[aid] = row
                elif aid in done:         # refresh now yields nothing -> drop stale
                    del done[aid]
                if i % 20 == 0:
                    flush()
                    log(f"[*] classified {i}/{len(todo)}")
    flush()
    log(f"[*] wrote {len(done)} rows -> {out_path}")
    return len(done)


# ----------------------------------------------------------- pass 2: studios
def _norm_studio(name):
    """Normalised key for grouping studio-name variants ("Foo" == "Foo Studios")."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    s = re.sub(r"\b(studios?|inc|llc|ltd|gmbh|games|interactive|entertainment|"
               r"corp|corporation|company|co|software|productions?|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def classify_studio(name, timeout=60):
    """One studio -> a class in STUDIO_CLASSES, or None."""
    msgs = [
        {"role": "system", "content":
            "Classify a video-game developer into exactly one class: "
            "aaa (large/major studio or publisher-owned), indie (small independent), "
            "solo (one person), hobbyist (amateur/non-commercial), student. "
            'Reply ONLY {"class": "<one of: aaa, indie, solo, hobbyist, student, unknown>"}.'},
        {"role": "user", "content": f'Developer: "{name}"\nJSON:'},
    ]
    content = _chat(msgs, timeout)
    if content is None:
        return None
    cls = _clean_str((_last_json(content) or {}).get("class"))
    return cls.lower() if cls and cls.lower() in STUDIO_CLASSES else None


def refresh_studio_classes(tags_path=TAGS_LLM, out_path=TAGS_STUDIOS, threshold=None):
    """Pass 2: tally studios from pass 1, classify the notable ones once each."""
    import tomli_w

    threshold = STUDIO_MIN_COUNT if threshold is None else threshold
    rows = load_toml(tags_path).get("editorial_tag", []) if tags_path.exists() else []
    # group name variants; keep the most common display form + member ids per group
    groups = defaultdict(list)  # norm -> [(id, name), ...]
    for r in rows:
        if r.get("studio"):
            groups[_norm_studio(r["studio"])].append((r["id"], r["studio"]))
    groups.pop("", None)

    notable = []  # (display_name, [ids])
    for members in groups.values():
        if len(members) >= threshold:
            display = Counter(n for _, n in members).most_common(1)[0][0]
            notable.append((display, [i for i, _ in members]))
    log(f"[*] {len(notable)} studios at/above threshold {threshold}; classifying once each")

    out = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(classify_studio, name): (name, ids) for name, ids in notable}
        for fut in as_completed(futs):
            name, ids = futs[fut]
            out.append({"name": name, "count": len(ids), "class": fut.result() or "unknown", "ids": ids})
    out.sort(key=lambda r: (-r["count"], r["name"]))
    out_path.write_bytes(tomli_w.dumps({"studio": out}).encode())
    log(f"[*] wrote {len(out)} studios -> {out_path}")
    return len(out)
