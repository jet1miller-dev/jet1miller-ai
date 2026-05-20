# Morning news digest (v2)

Telegram digest at **7:00 AM Brisbane** via GitHub Actions.

**v2 features:** curated RSS feeds, HTML **Read** links, 36h freshness, 14-day duplicate memory, optional OpenAI blurbs, one Telegram message per topic group.

## What you need on GitHub

| Secret | Required? |
|--------|-----------|
| `TELEGRAM_BOT_TOKEN` | Yes |
| `TELEGRAM_CHAT_ID` | Yes |
| `OPENAI_API_KEY` | Optional (blurbs + smarter dedup; works without) |

### Fix OpenAI “quota” / AI skipped

1. Log in at [platform.openai.com](https://platform.openai.com).
2. **Settings → Billing** (or [account/billing](https://platform.openai.com/account/billing)).
3. Add a **payment method** and **credits** (or enable pay-as-you-go).
4. Create an API key at [API keys](https://platform.openai.com/api-keys) if needed.
5. GitHub repo → **Settings → Secrets → Actions** → set or update `OPENAI_API_KEY` (starts with `sk-`).
6. **Actions → Morning digest → Run workflow** — intro should not say “AI skipped”.

Typical cost for this digest: about **one small request per day** (`gpt-4o-mini`), usually cents per month.

## Test

**Actions → Morning digest → Run workflow**

## Customising feeds and topics

### 1. Named feeds (`config/feeds.yaml`)

Add a nickname and URL:

```yaml
my_favourite_site:
  url: https://example.com/rss.xml
  label: Example News
```

### 2. Use feeds in a topic (`config/topics.yaml`)

**By nickname:**

```yaml
au_feeds:
  - abc_business
  - my_favourite_site
```

**Or paste a full URL** (no `feeds.yaml` entry needed):

```yaml
au_feeds:
  - https://www.example.com/feed.xml
```

### 3. AU vs global

For topics with `regions: [au, global]`:

- `au_feeds` / `au_queries` — Australia first
- `global_feeds` / `global_queries` — international
- **Google News queries** run only if feeds don’t fill the section (`au_max` / `global_max`)

### 4. Simple topics (AI, UFC)

```yaml
- id: ai
  max_items: 5
  feeds:
    - verge_ai
  queries:
    - "AI startups"   # fallback only
```

### 5. Defaults (`defaults` in topics.yaml)

| Key | Meaning |
|-----|---------|
| `max_age_hours` | Drop stories older than this (default 36) |
| `history_days` | Don’t resend same story within N days (default 14) |
| `au_max` / `global_max` | Max items per region |
| `link_label` | Telegram link text (default `Read`) |
| `blocklist` | Words that drop a story |

### 6. After editing

Push to GitHub (or commit on `main`). Next workflow run uses the new config.

## Files

| File | Purpose |
|------|---------|
| `config/topics.yaml` | Sections, feeds, queries, limits |
| `config/feeds.yaml` | Named RSS catalog |
| `data/sent_history.json` | Auto-updated “already sent” log |
| `digest.py` | Main script |
| `ai.py` | OpenAI batch step |

## Local run

```bash
cd news-digest
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in secrets
set -a && source .env && set +a
.venv/bin/python digest.py
```

## Schedule

Cron `0 21 * * *` UTC = 7:00 AM Brisbane. Private repos may run a few minutes late.
