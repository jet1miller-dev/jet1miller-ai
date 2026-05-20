#!/usr/bin/env python3
"""Morning news digest v2: curated RSS, AI blurbs, Telegram HTML, sent history."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

from ai import try_refine

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "topics.yaml"
FEEDS_PATH = ROOT / "config" / "feeds.yaml"
HISTORY_PATH = ROOT / "data" / "sent_history.json"
TELEGRAM_MAX = 4000
FETCH_TIMEOUT = 25
USER_AGENT = "MorningDigest/2.0 (+https://github.com)"

# Telegram message groups (topic ids)
MESSAGE_GROUPS: list[tuple[str, list[str]]] = [
    ("Finance", ["finance_markets", "finance_personal"]),
    ("Property", ["property"]),
    ("Property development", ["property_dev"]),
    ("AI", ["ai"]),
    ("Australian politics", ["politics"]),
    ("UFC", ["ufc"]),
]


@dataclass
class Story:
    title: str
    link: str
    excerpt: str
    source: str
    published: datetime | None
    region: str
    topic_id: str = ""
    story_id: str = ""
    blurb: str = ""
    score: float = 0.0

    def __post_init__(self) -> None:
        if not self.story_id:
            self.story_id = story_hash(self.link, self.title)


def story_hash(link: str, title: str) -> str:
    key = normalize_url(link) or title
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict[str, Any]:
    return load_yaml(CONFIG_PATH)


def load_feed_catalog() -> dict[str, Any]:
    if FEEDS_PATH.exists():
        return load_yaml(FEEDS_PATH)
    return {}


def resolve_feed(ref: str, catalog: dict[str, Any]) -> tuple[str, str]:
    ref = ref.strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        label = urlparse(ref).netloc.replace("www.", "")
        return ref, label
    entry = catalog.get(ref) or {}
    if isinstance(entry, dict):
        return entry.get("url", ""), entry.get("label", ref)
    return "", ref


def google_news_rss(query: str, locale: str = "au") -> str:
    q = quote_plus(query)
    if locale == "au":
        return f"https://news.google.com/rss/search?q={q}&hl=en-AU&gl=AU&ceid=AU:en"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc"}
    q = [(k, v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items() if k not in drop]
    query = "&".join(f"{k}={v[0]}" for k, v in sorted(q)) if q else ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + (f"?{query}" if query else "")


def title_fingerprint(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:100]


def titles_similar(a: str, b: str) -> bool:
    na, nb = title_fingerprint(a), title_fingerprint(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) < len(nb) else (nb, na)
    return short in long and len(short) / len(long) > 0.55


def parse_published(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def format_age(published: datetime | None) -> str:
    if not published:
        return ""
    delta = datetime.now(timezone.utc) - published.astimezone(timezone.utc)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "just now"
    if hours < 48:
        return f" ({hours}h ago)"
    return f" ({hours // 24}d ago)"


def entry_to_story(
    entry: Any,
    region: str,
    excerpt_max: int,
    topic_id: str,
    source_override: str = "",
) -> Story | None:
    title = strip_html(entry.get("title") or "")
    link = entry.get("link") or ""
    if not title or not link:
        return None
    raw_excerpt = entry.get("summary") or entry.get("description") or ""
    excerpt = strip_html(raw_excerpt)
    if len(excerpt) > excerpt_max:
        excerpt = excerpt[: excerpt_max - 1].rsplit(" ", 1)[0] + "…"
    source = source_override
    if not source:
        src = entry.get("source")
        if isinstance(src, dict):
            source = src.get("title") or ""
    if not source:
        source = urlparse(link).netloc.replace("www.", "")
    return Story(
        title=title,
        link=link,
        excerpt=excerpt,
        source=source,
        published=parse_published(entry),
        region=region,
        topic_id=topic_id,
    )


def fetch_feed(url: str) -> list[Any]:
    if not url:
        return []
    feedparser.USER_AGENT = USER_AGENT
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.RequestException:
        parsed = feedparser.parse(url)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        return []
    return list(parsed.entries)


def is_blocked(story: Story, blocklist: list[str]) -> bool:
    hay = f"{story.title} {story.excerpt}".lower()
    return any(term.lower() in hay for term in blocklist)


def matches_any(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)


def dedupe_stories(stories: list[Story]) -> list[Story]:
    stories.sort(key=lambda s: s.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    kept: list[Story] = []
    for story in stories:
        if any(
            normalize_url(story.link) == normalize_url(k.link) or titles_similar(story.title, k.title)
            for k in kept
        ):
            continue
        kept.append(story)
    return kept


def load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return list(data.get("entries", []))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(entries: list[dict[str, Any]]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")


def prune_history(entries: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = []
    for e in entries:
        raw = e.get("sent_at", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= cutoff:
            kept.append(e)
    return kept


def in_history(story: Story, entries: list[dict[str, Any]]) -> bool:
    url = normalize_url(story.link)
    fp = title_fingerprint(story.title)
    for e in entries:
        if url and url == e.get("url"):
            return True
        if fp and fp == e.get("title_fp"):
            return True
        if titles_similar(story.title, e.get("title", "")):
            return True
    return False


def too_old(story: Story, max_age_hours: float) -> bool:
    if not story.published:
        return False
    age = datetime.now(timezone.utc) - story.published.astimezone(timezone.utc)
    return age > timedelta(hours=max_age_hours)


def collect_from_feed_refs(
    refs: list[str],
    catalog: dict[str, Any],
    region: str,
    excerpt_max: int,
    topic_id: str,
) -> list[Story]:
    stories: list[Story] = []
    for ref in refs:
        url, label = resolve_feed(ref, catalog)
        for entry in fetch_feed(url):
            story = entry_to_story(entry, region, excerpt_max, topic_id, source_override=label)
            if story:
                stories.append(story)
    return stories


def collect_from_queries(queries: list[str], locale: str, excerpt_max: int, topic_id: str) -> list[Story]:
    region = "au" if locale == "au" else "global"
    stories: list[Story] = []
    for query in queries:
        for entry in fetch_feed(google_news_rss(query, locale)):
            story = entry_to_story(entry, region, excerpt_max, topic_id)
            if story:
                stories.append(story)
    return stories


def apply_base_filters(
    stories: list[Story],
    blocklist: list[str],
    history: list[dict[str, Any]],
    max_age_hours: float,
) -> list[Story]:
    out: list[Story] = []
    for s in stories:
        if is_blocked(s, blocklist):
            continue
        if too_old(s, max_age_hours):
            continue
        if in_history(s, history):
            continue
        out.append(s)
    return dedupe_stories(out)


def ufc_passes(story: Story, topic: dict) -> bool:
    text = f"{story.title} {story.excerpt}"
    exclude = topic.get("exclude_keywords") or []
    include = topic.get("include_keywords") or []
    if exclude and matches_any(text, exclude):
        return False
    if include and not matches_any(text, include):
        return False
    if "ufc" not in text.lower():
        return False
    for kw in ("title fight", "championship", "main event", "ranked", "rankings"):
        if kw in text.lower():
            story.score += 2
    return True


def collect_regional_topic(
    topic: dict,
    catalog: dict[str, Any],
    defaults: dict[str, Any],
    blocklist: list[str],
    history: list[dict[str, Any]],
) -> list[Story]:
    excerpt_max = defaults.get("excerpt_max_chars", 220)
    max_age = defaults.get("max_age_hours", 36)
    au_max = defaults.get("au_max", 3)
    global_max = defaults.get("global_max", 2)
    tid = topic.get("id", "")

    au_feeds = topic.get("au_feeds") or topic.get("feeds") or []
    global_feeds = topic.get("global_feeds") or []
    au_queries = topic.get("au_queries") or []
    global_queries = topic.get("global_queries") or []

    au = collect_from_feed_refs(au_feeds, catalog, "au", excerpt_max, tid)
    gl = collect_from_feed_refs(global_feeds, catalog, "global", excerpt_max, tid)
    au = apply_base_filters(au, blocklist, history, max_age)
    gl = apply_base_filters(gl, blocklist, history, max_age)

    if len(au) < au_max and au_queries:
        extra = collect_from_queries(au_queries, "au", excerpt_max, tid)
        extra = apply_base_filters(extra, blocklist, history, max_age)
        au = dedupe_stories(au + extra)
    if len(gl) < global_max and global_queries:
        extra = collect_from_queries(global_queries, "global", excerpt_max, tid)
        extra = apply_base_filters(extra, blocklist, history, max_age)
        gl = dedupe_stories(gl + extra)

    au = au[:au_max]
    gl = gl[:global_max]
    return au + gl


def collect_simple_topic(
    topic: dict,
    catalog: dict[str, Any],
    defaults: dict[str, Any],
    blocklist: list[str],
    history: list[dict[str, Any]],
) -> list[Story]:
    excerpt_max = defaults.get("excerpt_max_chars", 220)
    max_age = defaults.get("max_age_hours", 36)
    max_items = topic.get("max_items", 5)
    tid = topic.get("id", "")

    feed_refs = topic.get("feeds") or []
    queries = topic.get("queries") or []

    stories = collect_from_feed_refs(feed_refs, catalog, "none", excerpt_max, tid)
    for loc in ("au", "global"):
        stories.extend(collect_from_feed_refs(topic.get(f"{loc}_feeds") or [], catalog, loc, excerpt_max, tid))
    stories = apply_base_filters(stories, blocklist, history, max_age)

    if len(stories) < max_items and queries:
        for loc in ("au", "global"):
            stories.extend(collect_from_queries(queries, loc, excerpt_max, tid))
        stories = apply_base_filters(stories, blocklist, history, max_age)

    stories = dedupe_stories(stories)

    if tid == "ufc":
        stories = [s for s in stories if ufc_passes(s, topic)]
        stories.sort(key=lambda s: (s.score, s.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    return stories[:max_items]


def collect_politics(
    topic: dict,
    catalog: dict[str, Any],
    defaults: dict[str, Any],
    blocklist: list[str],
    history: list[dict[str, Any]],
) -> list[Story]:
    excerpt_max = defaults.get("excerpt_max_chars", 220)
    max_age = defaults.get("max_age_hours", 36)
    tid = topic.get("id", "politics")
    all_stories: list[Story] = []

    for _key, sub in (topic.get("subsections") or {}).items():
        locale = sub.get("locale", "au")
        region = "global" if locale == "global" else "au"
        cap = sub.get("max", 2)
        feed_refs = sub.get("feeds") or []
        queries = sub.get("queries") or []

        batch = collect_from_feed_refs(feed_refs, catalog, region, excerpt_max, tid)
        batch = apply_base_filters(batch, blocklist, history, max_age)
        if len(batch) < cap and queries:
            batch = dedupe_stories(
                batch
                + apply_base_filters(
                    collect_from_queries(queries, locale, excerpt_max, tid),
                    blocklist,
                    history,
                    max_age,
                )
            )
        include_kw = sub.get("include_keywords")
        if include_kw:
            batch = [s for s in batch if matches_any(f"{s.title} {s.excerpt}", include_kw)]
        for s in batch[:cap]:
            s.region = _key
            all_stories.append(s)

    return all_stories


def collect_for_topic(
    topic: dict,
    catalog: dict[str, Any],
    defaults: dict[str, Any],
    blocklist: list[str],
    history: list[dict[str, Any]],
) -> list[Story]:
    if topic.get("subsections"):
        return collect_politics(topic, catalog, defaults, blocklist, history)
    if topic.get("regions"):
        return collect_regional_topic(topic, catalog, defaults, blocklist, history)
    return collect_simple_topic(topic, catalog, defaults, blocklist, history)


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_story_html(story: Story, link_label: str) -> str:
    age = format_age(story.published)
    body = story.blurb or story.excerpt
    lines = [f"• <b>{esc(story.title)}</b> — {esc(story.source)}{age}"]
    if body:
        lines.append(f"<i>{esc(body)}</i>")
    safe_url = esc(story.link, quote=True)
    lines.append(f'<a href="{safe_url}">{esc(link_label)}</a>')
    return "\n".join(lines)


def format_region_html(label: str, stories: list[Story], link_label: str) -> str:
    if not stories:
        return ""
    parts = [label] + [format_story_html(s, link_label) for s in stories]
    return "\n\n".join(parts)


def format_topic_block(
    topic: dict,
    stories: list[Story],
    defaults: dict[str, Any],
    link_label: str,
) -> str:
    if not stories:
        return ""
    au_max = defaults.get("au_max", 3)
    global_max = defaults.get("global_max", 2)

    if topic.get("subsections"):
        parts = [f"<b>{esc(topic['name'])}</b>"]
        by_region: dict[str, list[Story]] = {}
        for s in stories:
            by_region.setdefault(s.region, []).append(s)
        for _key, sub in topic["subsections"].items():
            label = sub.get("label", _key)
            prefix = "🌐" if sub.get("locale") == "global" else "🇦🇺"
            sub_stories = [s for s in stories if s.region == _key][: sub.get("max", 2)]
            block = format_region_html(f"{prefix} {label}", sub_stories, link_label)
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    if topic.get("regions"):
        parts = [f"<b>{esc(topic['name'])}</b>"]
        au = [s for s in stories if s.region == "au"][:au_max]
        gl = [s for s in stories if s.region == "global"][:global_max]
        unlabeled = [s for s in stories if s.region not in ("au", "global")]
        for s in unlabeled:
            (au if len(au) < au_max else gl).append(s)
        au, gl = au[:au_max], gl[:global_max]
        if au:
            parts.append(format_region_html("🇦🇺 Australia", au, link_label))
        if gl:
            parts.append(format_region_html("🌐 Global", gl, link_label))
        return "\n\n".join(parts)

    parts = [f"<b>{esc(topic['name'])}</b>"]
    parts.extend(format_story_html(s, link_label) for s in stories)
    return "\n\n".join(parts)


def split_long_html(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block[:limit]
    if current:
        chunks.append(current)
    total = len(chunks)
    if total <= 1:
        return chunks
    return [f"({i}/{total})\n{c}" for i, c in enumerate(chunks, 1)]


def send_telegram(token: str, chat_id: str, text: str, *, parse_mode: str | None = "HTML") -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", resp.text))


def send_error(token: str, chat_id: str, message: str) -> None:
    try:
        send_telegram(token, chat_id, f"⚠️ Morning digest failed\n\n{esc(message[:3500])}", parse_mode=None)
    except Exception:
        pass


def apply_ai_decisions(stories: list[Story], decisions: dict[str, dict[str, Any]]) -> list[Story]:
    if not decisions:
        return stories
    kept: list[Story] = []
    for s in stories:
        d = decisions.get(s.story_id, {})
        if d and not d.get("keep", True):
            continue
        if d.get("blurb"):
            s.blurb = d["blurb"]
        kept.append(s)
    return kept


def stories_to_candidates(stories: list[Story]) -> list[dict[str, Any]]:
    return [
        {
            "id": s.story_id,
            "topic": s.topic_id,
            "title": s.title,
            "excerpt": s.excerpt,
        }
        for s in stories
    ]


def record_sent(entries: list[dict[str, Any]], stories: list[Story]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    for s in stories:
        entries.append(
            {
                "url": normalize_url(s.link),
                "title": s.title,
                "title_fp": title_fingerprint(s.title),
                "sent_at": now,
            }
        )
    return entries


def run_digest() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    config = load_config()
    catalog = load_feed_catalog()
    defaults = config.get("defaults", {})
    blocklist = config.get("blocklist", [])
    link_label = defaults.get("link_label", "Read")
    history_days = defaults.get("history_days", 14)

    tz_name = os.environ.get("DIGEST_TIMEZONE") or config.get("schedule", {}).get("timezone", "Australia/Brisbane")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    intro = f"Morning digest — {now.strftime('%a')} {now.day} {now.strftime('%b %Y')} (Brisbane)"

    history = prune_history(load_history(), history_days)
    topics_by_id = {t["id"]: t for t in config.get("topics", [])}

  # Collect all stories per topic
    per_topic: dict[str, list[Story]] = {}
    for topic in config.get("topics", []):
        tid = topic.get("id", "")
        per_topic[tid] = collect_for_topic(topic, catalog, defaults, blocklist, history)

    pool = [s for stories in per_topic.values() for s in stories]
    if not pool:
        raise RuntimeError("No stories after filters. Check feeds, max_age_hours, or topics.yaml.")

    decisions, ai_ok, ai_err = try_refine(stories_to_candidates(pool), history)
    if ai_ok and decisions:
        for tid in per_topic:
            per_topic[tid] = apply_ai_decisions(per_topic[tid], decisions)
    else:
        for s in pool:
            if not s.blurb:
                s.blurb = ""

    if not ai_ok:
        intro += f"\n<i>(AI skipped: {esc(ai_err or 'unavailable')})</i>"

    sent_stories: list[Story] = []
    messages: list[str] = []

    for group_title, topic_ids in MESSAGE_GROUPS:
        blocks: list[str] = []
        for tid in topic_ids:
            topic = topics_by_id.get(tid)
            if not topic:
                continue
            stories = per_topic.get(tid, [])
            if not stories:
                continue
            block = format_topic_block(topic, stories, defaults, link_label)
            if block:
                blocks.append(block)
                sent_stories.extend(stories)
        if blocks:
            body = f"<b>{esc(group_title)}</b>\n\n" + "\n\n".join(blocks)
            messages.extend(split_long_html(body))

    if not messages:
        raise RuntimeError("Nothing to send after formatting.")

    send_telegram(token, chat_id, intro)
    for msg in messages:
        send_telegram(token, chat_id, msg)

    history = record_sent(history, sent_stories)
    save_history(prune_history(history, history_days))
    print(f"Sent intro + {len(messages)} topic message(s); {len(sent_stories)} stories; AI={'yes' if ai_ok else 'no'}.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)
    try:
        run_digest()
    except Exception as exc:
        send_error(token, chat_id, str(exc))
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
