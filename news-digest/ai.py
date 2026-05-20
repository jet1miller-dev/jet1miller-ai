"""OpenAI batch: dedupe against recent sends and write one-line blurbs."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI


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
    model: str = "gpt-4o-mini",
) -> tuple[dict[str, dict[str, Any]], bool]:
    """
    Returns mapping story_id -> {keep: bool, blurb: str}, and whether AI succeeded.
  """
    if not candidates:
        return {}, True

    recent_lines = [
        f"- {r.get('title', '')[:120]}" for r in recent_sent[-40:]
    ]
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

    system = (
        "You curate a morning news digest for a reader in Brisbane, Australia. "
        "Respond with JSON only, no markdown fences:\n"
        '{"decisions":[{"id":"...","keep":true|false,"blurb":"..."}]}\n'
        "Rules:\n"
        "- keep=false for duplicates of RECENT_SENT (same story, reworded headline, or same event).\n"
        "- keep=false for clickbait, listicles, celebrity fluff, or off-topic items.\n"
        "- keep=true for substantive news; blurb = one plain English sentence (max 28 words) "
        "explaining what happened. No markdown in blurb.\n"
        "- Include every candidate id in decisions."
    )
    user = f"RECENT_SENT:\n{recent_block}\n\nCANDIDATES:\n{json.dumps(payload, ensure_ascii=False)}"

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
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
            "blurb": (row.get("blurb") or "").strip(),
        }
    return out, True


def try_refine(
    candidates: list[dict[str, Any]],
    recent_sent: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
    """Returns (decisions, ai_ok, error_message)."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}, False, "OPENAI_API_KEY not set"
    try:
        decisions, _ = refine_stories(candidates, recent_sent, api_key=api_key)
        return decisions, True, None
    except Exception as exc:
        return {}, False, str(exc)
