"""Editorial tags via an OpenAI-compatible chat API (tag system tier 3).

Classifies each postmortem's summary against a fixed editorial vocab
(outcome / production theme / business, including `port`) using any
OpenAI-compatible `/v1/chat/completions` endpoint — local (ollama, llama.cpp,
LM Studio) or hosted. Writes data/tags_llm.toml, which the frontend unions with
the deterministic/wiki tags from data/tags.toml. Keeping it a separate sidecar
means `just tags` (tiers 1-2) can regenerate without clobbering these, and vice
versa.

Config (env):
  TAGS_LLM_BASE_URL   default http://localhost:11434/v1   (ollama's OAI endpoint)
  TAGS_LLM_API_KEY    default ""  (sent as Bearer; local servers usually ignore it)
  TAGS_LLM_MODEL      default gemma3:12b

The pass is idempotent: ids already in the output are skipped unless --refresh,
and it flushes periodically so an interrupted run resumes cheaply.
"""
import json
import os
import re

from common import DATA as DATA_DIR, load_toml, log, SESSION

TAGS_LLM = DATA_DIR / "tags_llm.toml"

BASE_URL = os.environ.get("TAGS_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.environ.get("TAGS_LLM_API_KEY", "")
MODEL = os.environ.get("TAGS_LLM_MODEL", "gemma3:12b")

# Controlled vocab with one-line glosses. The model may only return these tags;
# anything else is dropped. `port` additionally carries a target-platform tag so
# it slots into the platform axis (overriding tags.py's distinctive-only rule).
OUTCOME = {
    "breakout-success": "an unexpectedly large commercial or critical hit",
    "commercial-flop": "sold poorly / lost money",
    "cult-classic": "modest sales but a devoted following or strong critical love",
    "mixed-reception": "divisive or middling reception",
}
THEME = {
    "crunch": "significant overtime / crunch described",
    "scope-creep": "scope grew well beyond the original plan",
    "pivot": "the project changed direction or genre significantly mid-development",
    "first-game": "the studio's or team's debut game",
    "port": "the postmortem is mainly about porting an existing game to a new platform",
    "engine-switch": "changed engine or core tech mid-project",
    "team-conflict": "notable internal team or management conflict",
    "long-dev": "unusually long development (many years)",
    "rushed": "shipped under heavy time pressure / unfinished",
}
BUSINESS = {
    "kickstarter": "crowdfunded (Kickstarter / IndieGoGo / similar)",
    "early-access": "released through Early Access",
    "self-published": "the developer self-published",
    "work-for-hire": "made under contract / work-for-hire",
}
PORT_PLATFORMS = ["pc", "console", "handheld", "mobile", "arcade", "web", "vr"]
VOCAB = set(OUTCOME) | set(THEME) | set(BUSINESS) | set(PORT_PLATFORMS)


def _vocab_block():
    lines = []
    for group, d in (("OUTCOME", OUTCOME), ("PRODUCTION", THEME), ("BUSINESS", BUSINESS)):
        lines.append(f"# {group}")
        lines += [f"- {tag}: {gloss}" for tag, gloss in d.items()]
    return "\n".join(lines)


SYSTEM = (
    "You label video-game postmortems with editorial tags. Given a postmortem's "
    "game, title, and summary, return ONLY a JSON object of the form "
    '{"tags": ["...", "..."]} using tags from the fixed list below. Apply a tag '
    "only when the text clearly supports it; return an empty list if none apply. "
    "Never invent tags outside the list.\n\n"
    + _vocab_block()
    + "\n\nSpecial rule for `port`: if the postmortem is mainly about porting an "
    "existing game to a new platform (e.g. \"we ported X to PS2\", \"bringing X to "
    "Switch\"), include \"port\" AND the target platform as one of: "
    + ", ".join(PORT_PLATFORMS) + "."
)

FEWSHOT = [
    ({"game": "Game A", "title": "Postmortem: Game A",
      "summary": "After a successful Kickstarter the two-person studio spent four "
                 "years on their first game, crunching hard near the end; it became "
                 "a surprise hit."},
     ["kickstarter", "first-game", "long-dev", "crunch", "breakout-success"]),
    ({"game": "Game B", "title": "Postmortem: porting Game B to PlayStation 2",
      "summary": "How we brought our PC engine to the PS2, the memory battles, and "
                 "what shipping the console version taught us."},
     ["port", "console"]),
    ({"game": "Game C", "title": "Postmortem: Game C",
      "summary": "A straightforward retrospective on our level design choices and "
                 "art direction."},
     []),
]


def _messages(article):
    msgs = [{"role": "system", "content": SYSTEM}]
    for ex, tags in FEWSHOT:
        msgs.append({"role": "user", "content": _user_prompt(ex)})
        msgs.append({"role": "assistant", "content": json.dumps({"tags": tags})})
    msgs.append({"role": "user", "content": _user_prompt(article)})
    return msgs


def _user_prompt(article):
    summary = (article.get("summary") or "")[:1500]
    return (f"Game: {article.get('game', '')}\n"
            f"Title: {article.get('title', '')}\n"
            f"Summary: {summary}\n\nJSON:")


def _extract_tags(content):
    """Pull {"tags":[...]} out of a model response, tolerant of stray prose or
    a <think> preamble; keep only valid-vocab tags."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    tags = data.get("tags", []) if isinstance(data, dict) else []
    seen, out = set(), []
    for t in tags:
        t = str(t).strip().lower()
        if t in VOCAB and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def classify(article, timeout=120):
    """Editorial tags for one article via the chat endpoint; [] on any failure."""
    body = {
        "model": MODEL,
        "messages": _messages(article),
        "temperature": 0,
        "max_tokens": 120,
    }
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    try:
        r = SESSION.post(f"{BASE_URL}/chat/completions", json=body,
                         headers=headers, timeout=timeout)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # network, HTTP, or malformed payload — skip this one
        log(f"  [!] LLM call failed: {e}")
        return None
    return _extract_tags(content)


def refresh_llm_tags(data_path, out_path=TAGS_LLM, limit=0, refresh=False, dry_run=False):
    """Classify each postmortem and write data/tags_llm.toml.

    Idempotent: skips ids already present unless refresh=True. dry_run prints the
    prompt for the first article and the endpoint config without calling out."""
    import tomli_w

    articles = load_toml(data_path).get("postmortem", [])
    if limit:
        articles = articles[:limit]

    if dry_run:
        log(f"[dry-run] endpoint={BASE_URL} model={MODEL} key={'set' if API_KEY else 'none'}")
        log("[dry-run] system+fewshot+user messages for the first article:")
        for m in _messages(articles[0]):
            log(f"--- {m['role']} ---\n{m['content']}")
        return 0

    prior = load_toml(out_path).get("editorial_tag", []) if out_path.exists() else []
    done = {row["id"]: row for row in prior}
    todo = [a for a in articles if refresh or a["id"] not in done]
    log(f"[*] {len(todo)} to classify ({len(done)} already done) via {MODEL} @ {BASE_URL}")

    def flush():
        rows = sorted(done.values(), key=lambda r: r["id"])
        out_path.write_bytes(tomli_w.dumps({"editorial_tag": rows}).encode())

    for i, art in enumerate(todo, 1):
        tags = classify(art)
        if tags is None:          # call failed; leave for a re-run, don't cache empty
            continue
        if tags:
            done[art["id"]] = {"id": art["id"], "tags": tags}
        elif art["id"] in done:   # refresh that now yields nothing -> drop stale row
            del done[art["id"]]
        if i % 20 == 0:
            flush()
            log(f"[*] classified {i}/{len(todo)}")
    flush()
    tagged = sum(1 for r in done.values() if r["tags"])
    log(f"[*] wrote editorial tags for {tagged} entries -> {out_path}")
    return tagged
