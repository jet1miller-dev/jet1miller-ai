#!/usr/bin/env python3
"""Morning news digest: fetch RSS, format, send to Telegram."""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "topics.yaml"
TELEGRAM_MAX = 4000
FETCH_TIMEOUT = 25
USER_AGENT = "MorningDigest/1.0 (personal; +https://github.com)"


@dataclass
class Story:
    title: str
    link: str
    excerpt: str
    source: str
    published: datetime | None
    region: str  # au | global | none

    score: float = 0.0


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc"}
    q = [(k, v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items() if k not in drop]
    query = "&".join(f"{k}={v[0]}" for k, v in sorted(q)) if q else ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + (f"?{query}" if query else "")


def titles_similar(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    na, nb = norm(a), norm(b)
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


def format_age(published: datetime | None, tz: ZoneInfo) -> str:
    if not published:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - published.astimezone(timezone.utc)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def entry_to_story(entry: Any, region: str, excerpt_max: int) -> Story | None:
    title = strip_html(entry.get("title") or "")
    link = entry.get("link") or ""
    if not title or not link:
        return None
    raw_excerpt = entry.get("summary") or entry.get("description") or ""
    excerpt = strip_html(raw_excerpt)
    if len(excerpt) > excerpt_max:
        excerpt = excerpt[: excerpt_max - 1].rsplit(" ", 1)[0] + "…"
    source = entry.get("source", {}).get("title") if isinstance(entry.get("source"), dict) else ""
    if not source:
        source = urlparse(link).netloc.replace("www.", "")
    return Story(
        title=title,
        link=link,
        excerpt=excerpt,
        source=source,
        published=parse_published(entry),
        region=region,
    )


def fetch_feed(url: str) -> list[Any]:
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


def ufc_passes(story: Story, topic: dict) -> bool:
    text = f"{story.title} {story.excerpt}"
    include = topic.get("include_keywords") or []
    exclude = topic.get("exclude_keywords") or []
    if exclude and matches_any(text, exclude):
        return False
    if include and not matches_any(text, include):
        return False
    if "ufc" not in text.lower():
        return False
    # Prefer ranked / title stories
    boost = 0
    for kw in ("title fight", "championship", "main event", "ranked", "rankings"):
        if kw in text.lower():
            boost += 2
    story.score += boost
    return True


def collect_from_queries(queries: list[str], locale: str, excerpt_max: int) -> list[Story]:
    stories: list[Story] = []
    for query in queries:
        url = google_news_rss(query, locale)
        for entry in fetch_feed(url):
            story = entry_to_story(entry, "au" if locale == "au" else "global", excerpt_max)
            if story:
                stories.append(story)
    return stories


def collect_topic_stories(
    topic: dict,
    defaults: dict,
    blocklist: list[str],
) -> list[Story]:
    excerpt_max = defaults.get("excerpt_max_chars", 220)
    stories: list[Story] = []

    for feed_url in topic.get("feeds") or []:
        for entry in fetch_feed(feed_url):
            story = entry_to_story(entry, "none", excerpt_max)
            if story:
                stories.append(story)

    for query in topic.get("queries") or []:
        for loc in ("au", "global"):
            stories.extend(collect_from_queries([query], loc, excerpt_max))

    if topic.get("regions"):
        for query in topic.get("au_queries") or []:
            stories.extend(collect_from_queries([query], "au", excerpt_max))
        for query in topic.get("global_queries") or []:
            stories.extend(collect_from_queries([query], "global", excerpt_max))

    stories = dedupe_stories(stories)
    stories = [s for s in stories if not is_blocked(s, blocklist)]

    if topic.get("id") == "ufc":
        stories = [s for s in stories if ufc_passes(s, topic)]
        stories.sort(key=lambda s: (s.score, s.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return stories[: topic.get("max_items", 2)]

    return stories


def collect_subsection(
    sub: dict,
    defaults: dict,
    blocklist: list[str],
) -> list[Story]:
    excerpt_max = defaults.get("excerpt_max_chars", 220)
    locale = sub.get("locale", "au")
    stories: list[Story] = []
    for query in sub.get("queries") or []:
        stories.extend(collect_from_queries([query], locale, excerpt_max))
    stories = dedupe_stories(stories)
    stories = [s for s in stories if not is_blocked(s, blocklist)]
    include_kw = sub.get("include_keywords")
    if include_kw:
        stories = [s for s in stories if matches_any(f"{s.title} {s.excerpt}", include_kw)]
    return stories[: sub.get("max", 2)]


def split_au_global(stories: list[Story], au_max: int, global_max: int) -> tuple[list[Story], list[Story]]:
    au = [s for s in stories if s.region == "au"][:au_max]
    global_ = [s for s in stories if s.region == "global"][:global_max]
    # If one bucket is thin, backfill from unlabeled by taking newest
    if len(au) < au_max or len(global_) < global_max:
        used = {normalize_url(s.link) for s in au + global_}
        rest = [s for s in stories if normalize_url(s.link) not in used]
        for s in rest:
            if len(au) < au_max and s.region in ("au", "none"):
                s.region = "au"
                au.append(s)
            elif len(global_) < global_max:
                s.region = "global"
                global_.append(s)
            if len(au) >= au_max and len(global_) >= global_max:
                break
    return au, global_


def format_story(story: Story, tz: ZoneInfo) -> str:
    age = format_age(story.published, tz)
    age_bit = f" ({age})" if age else ""
    lines = [f"• {story.title} — {story.source}{age_bit}"]
    if story.excerpt:
        lines.append(f"  {story.excerpt}")
    lines.append(f"  {story.link}")
    return "\n".join(lines)


def format_region_block(label: str, stories: list[Story], tz: ZoneInfo) -> str:
    if not stories:
        return ""
    lines = [label]
    for s in stories:
        lines.append(format_story(s, tz))
    return "\n".join(lines)


def build_digest(config: dict) -> str:
    defaults = config.get("defaults", {})
    blocklist = config.get("blocklist", [])
    au_max = defaults.get("au_max", 3)
    global_max = defaults.get("global_max", 2)
    tz_name = os.environ.get("DIGEST_TIMEZONE") or config.get("schedule", {}).get("timezone", "Australia/Brisbane")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    header = f"Morning digest — {now.strftime('%a')} {now.day} {now.strftime('%b %Y')} (Brisbane)\n"
    parts = [header]

    for topic in config.get("topics", []):
        tid = topic.get("id", "")

        if topic.get("subsections"):
            parts.append(f"\n{topic['name']}")
            for _key, sub in topic["subsections"].items():
                label = sub.get("label", _key)
                prefix = "🌐" if sub.get("locale") == "global" else "🇦🇺"
                stories = collect_subsection(sub, defaults, blocklist)
                block = format_region_block(f"{prefix} {label}", stories, tz)
                if block:
                    parts.append("\n" + block)
            continue

        if tid == "ufc":
            stories = collect_topic_stories(topic, defaults, blocklist)[: topic.get("max_items", 2)]
            if stories:
                parts.append(f"\n{topic['name']}")
                for s in stories:
                    parts.append(format_story(s, tz))
            continue

        if topic.get("queries") and not topic.get("regions"):
            stories = collect_topic_stories(topic, defaults, blocklist)[: topic.get("max_items", 5)]
            if stories:
                parts.append(f"\n{topic['name']}")
                for s in stories:
                    parts.append(format_story(s, tz))
            continue

        if topic.get("regions"):
            all_stories = collect_topic_stories(topic, defaults, blocklist)
            au, global_ = split_au_global(all_stories, au_max, global_max)
            if au or global_:
                parts.append(f"\n{topic['name']}")
                au_block = format_region_block("🇦🇺 Australia", au, tz)
                gl_block = format_region_block("🌐 Global", global_, tz)
                if au_block:
                    parts.append("\n" + au_block)
                if gl_block:
                    parts.append("\n" + gl_block)

    body = "\n".join(parts).strip()
    if len(body) <= len(header.strip()) + 5:
        raise RuntimeError("Digest is empty — no stories matched filters. Check feeds or topics.yaml.")
    return body


def split_messages(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line[:limit]
    if current:
        chunks.append(current)
    total = len(chunks)
    if total <= 1:
        return chunks
    return [f"({i}/{total})\n{c}" if not c.startswith("(") else c for i, c in enumerate(chunks, 1)]


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", resp.text))


def send_error(token: str, chat_id: str, message: str) -> None:
    try:
        send_telegram(token, chat_id, f"⚠️ Morning digest failed\n\n{message[:3500]}")
    except Exception:
        pass


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    try:
        config = load_config()
        digest = build_digest(config)
        for chunk in split_messages(digest):
            send_telegram(token, chat_id, chunk)
        print("Digest sent successfully.")
    except Exception as exc:
        send_error(token, chat_id, str(exc))
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
