# Morning news digest

Personal morning digest delivered to **Telegram** at **7:00 AM Brisbane**, built from RSS and Google News. Runs on **GitHub Actions** (no Mac required).

## Sections

- Finance — Markets & macro (AU + Global)
- Finance — Personal (AU + Global)
- Property (AU + Global)
- Property development (AU + Global)
- AI (industry + dev tools)
- Australian politics (National, Queensland, major global only)
- UFC (max 2, ranked/title focus, no WMMA)

Each story: **headline + short excerpt + link**.

## One-time setup

### Telegram (done if you have token + chat ID)

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` — e.g. from `getUpdates` after messaging your bot

### GitHub secrets

Repo → **Settings → Secrets and variables → Actions** → New repository secret:

| Name | Value |
|------|--------|
| `TELEGRAM_BOT_TOKEN` | Your bot token |
| `TELEGRAM_CHAT_ID` | Your chat ID |

### Actions settings

- Allow all actions
- Workflow: read-only repo permissions
- Enable Actions on the repo

## Test run

1. Push this repo to GitHub.
2. Add secrets above.
3. **Actions → Morning digest → Run workflow**.
4. Check Telegram.

## Local test

```bash
cd news-digest
pip install -r requirements.txt
cp .env.example .env   # fill in token + chat ID
export $(grep -v '^#' .env | xargs)
python digest.py
```

## Schedule

- Cron: `0 21 * * *` UTC = **7:00 AM Australia/Brisbane**
- Private repos: runs may be delayed a few minutes.

## Tuning

Edit [`config/topics.yaml`](config/topics.yaml):

- `blocklist` — words that drop a story
- `au_queries` / `global_queries` — search terms per topic
- UFC `include_keywords` / `exclude_keywords`

## Failure alerts

- GitHub emails you when the workflow fails (enable in notification settings).
- The script also tries to send a Telegram error message.
