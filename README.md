# InstaCop

Trust-check service for Instagram sellers in India. A buyer pastes a shop's
link and gets a fraud-pattern risk report: 9 algorithmic signals, weighted
scoring, plus a customer-experience score built from follow-up surveys.

## Monorepo layout

| Dir | What | Run |
|---|---|---|
| `engine/` | Risk engine: 9 signals, scoring, card synthesis | `python -m engine.check <handle>` |
| `bot/` | Telegram bot (aiogram 3) | `python -m bot.main` |
| `workers/` | RQ jobs + ingestion + Ad Library sweep | see below |
| `api/` | FastAPI: seller pages API, admin, verification | `uvicorn app.main:app` (from `api/`, PYTHONPATH=..) |
| `web/` | Next.js: SEO seller pages + checker | `npm run dev` (from `web/`) |
| `shared/` | models, config, cache policy, schemas | — |

## Local dev

```bash
# infra (either docker or brew services)
docker compose up -d postgres redis      # or: brew services start postgresql@14 redis

# migrate
cd api && alembic upgrade head

# processes (each in its own terminal, from repo root, venv active)
cd api && PYTHONPATH=..:. uvicorn app.main:app --port 8000
python -m bot.main                       # needs TELEGRAM_BOT_TOKEN
rq worker checks                         # executes cold checks
cd web && npm run dev                    # http://localhost:3000
```

## Cron jobs (wire into Railway/Render cron or system crontab)

| Schedule | Command | What |
|---|---|---|
| daily 06:00 | `python -m workers.ingestion.reddit daily` | fresh scam mentions |
| weekly Sun | `python -m workers.ingestion.complaint_sites` | complaint sites |
| daily 06:30 | `python -m workers.ingestion.telegram_channels` | TG scam-alert channels |
| weekly Mon | `python -m workers.ingestion.serpapi_sweep` | discovery queries |
| hourly | `python -m workers.ingestion.extract 100` | mentions → entities |
| daily 07:00 | `python -m workers.adlibrary` | pre-scan advertisers (needs Meta token) |
| every 15 min | enqueue `workers.jobs.send_due_followups` | the follow-up loop (the moat) |
| daily 08:00 | `python -c "from app.verification import auto_revoke_job; auto_revoke_job()"` | revoke bad verified sellers |

## Backups

`pg_dump "$DATABASE_URL" | gzip > backup-$(date +%F).sql.gz` daily; keep 14.
(Supabase paid tiers include PITR — this is the belt-and-suspenders copy.)

## Admin

All under `/admin/*` with header `X-Admin-Token: $ADMIN_TOKEN`:
- `GET /admin/metrics` — checks/day, cache hit rate, follow-up response rate, avg cost
- `GET /admin/reports` — unreviewed reports queue
- `POST /admin/reports/{id}/accept|reject` — accepted reports activate signals 6/9 + trigger re-check
- `POST /admin/verify/{handle}/approve|revoke` — verified-seller lifecycle

## Keys still needed for full operation

- `TELEGRAM_BOT_TOKEN` (@BotFather) — bot live testing
- `REDDIT_CLIENT_ID/SECRET` — **blocked upstream**: Reddit ended self-serve API keys (Responsible Builder Policy, 2025). Apply for access via support.reddithelp.com → "Developer Platform & Accessing Reddit Data" (non-commercial form pre-revenue). Until approved, the SerpAPI sweep's `site:reddit.com` queries are the compliant substitute — `workers/ingestion/reddit.py` activates automatically once creds exist.
- `TELETHON_API_ID/HASH/SESSION` (my.telegram.org) + `config/telegram_channels.txt` — channel ingestion
- `META_AD_LIBRARY_TOKEN` (developers.facebook.com, has days of lead time) — pre-scan
- `RAZORPAY_KEY_ID/SECRET` — real subscriptions (flow works without them in dev)
- `ADMIN_TOKEN` — set a long random string
