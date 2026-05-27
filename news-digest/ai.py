"""OpenRouter/Claude: relevance scoring, keep/drop filtering, and why-it-matters blurbs."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI

_MODEL = "google/gemini-2.5-flash-lite"
_BASE_URL = "https://openrouter.ai/api/v1"

_SYSTEM = """\
You curate a morning news digest for a reader in Brisbane, Australia. Their profile:
- Property investor/developer focused on Brisbane, SEQ, and Queensland broadly
- Follows Australian macro: RBA decisions, CPI, ASX, AUD, federal budget, cost of living
- Tracks AI only for new model releases and major product announcements — not opinion pieces, think-tanks, or general AI commentary
- Follows Australian federal and Queensland state politics; global politics only for major events (wars, elections, sanctions, invasions)
- Follows men's UFC main card fights, title fights, and ranked fighter news — not women's divisions, weigh-ins, or USADA

Respond with JSON only, no markdown fences:
{"decisions":[{"id":"...","keep":true|false,"score":7,"blurb":"..."}]}

Scoring rules (1–10):
- 9–10: Major, directly relevant news (RBA rate call, new frontier AI model launch, major SEQ DA/planning approval, UFC title fight confirmed)
- 7–8: Solid relevant story worth reading
- 5–6: On-topic but not urgent
- 1–4: Marginally related or off-topic — set keep=false

Other rules:
- Set keep=false for any story you score below 5
- Set keep=false for duplicates of RECENT_SENT (same story or same event reworded)
- Set keep=false for clickbait, listicles, opinion pieces, podcasts, or live blogs
- blurb = one sentence, max 30 words, plain English: what happened and why it matters to this reader. No markdown.
- Include every candidate id in decisions."""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object in model response")
    return json.loads(match.group())


def refine_stories(
    candidates: list[dict[str, Any]],
    recent_sent: list[dict[str, str]],
    *,
    api_key: str,
    model: str = _MODEL,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """
    Returns mapping story_id -> {keep: bool, score: float, blurb: str}, and whether AI succeeded.
    """
    if not candidates:
        return {}, True

    recent_lines = [f"- {r.get('title', '')[:120]}" for r in recent_sent[-40:]]
    recent_block = "\n".join(recent_lines) if recent_lines else "(none)"

    payload = [
        {
            "id": c["id"],
            "topic": c["topic"],
            "title": c["title"][:200],
            "excerpt": (c.get("excerpt") or "")[:300],
        }
        for c in candidates
    ]

    user = (
        f"RECENT_SENT:\n{recent_block}\n\n"
        f"CANDIDATES:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    client = OpenAI(
        base_url=_BASE_URL,
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/jet1miller-dev/jet1miller-ai",
            "X-Title": "Morning Digest",
        },
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or ""
    data = _extract_json(raw)
    out: dict[str, dict[str, Any]] = {}
    for row in data.get("decisions", []):
        sid = row.get("id")
        if not sid:
            continue
        out[sid] = {
            "keep": bool(row.get("keep", True)),
            "score": float(row.get("score", 5)),
            "blurb": (row.get("blurb") or "").strip(),
        }
    return out, True


def friendly_error(exc: BaseException) -> str:
    """Short message for Telegram when AI is skipped."""
    raw = str(exc)
    lower = raw.lower()
    if "insufficient_quota" in lower or "exceeded" in lower or "402" in raw:
        return "OpenRouter quota — add credits at openrouter.ai/credits (digest sent without AI blurbs)"
    if "invalid_api_key" in lower or "incorrect api key" in lower or "401" in raw:
        return "Invalid OpenRouter API key — check OPENROUTER_API_KEY in GitHub Secrets"
    if "rate_limit" in lower or "429" in raw:
        return "OpenRouter rate limit — digest sent without AI blurbs"
    if "not a valid model" in lower or "unknown model" in lower:
        return f"OpenRouter model ID invalid ({_MODEL}) — check openrouter.ai/models (digest sent without AI blurbs)"
    if len(raw) > 120:
        return raw[:117] + "…"
    return raw or "unknown error"


def try_refine(
    candidates: list[dict[str, Any]],
    recent_sent: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
    """Returns (decisions, ai_ok, error_message)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return {}, False, "OPENROUTER_API_KEY not set in GitHub Secrets"
    try:
        decisions, _ = refine_stories(candidates, recent_sent, api_key=api_key)
        return decisions, True, None
    except Exception as exc:
        return {}, False, friendly_error(exc)
