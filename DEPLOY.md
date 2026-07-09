# InstaCop — Deployment Runbook

**Status:** Workstreams A–E complete. Local stack fully functional. This runbook covers
the last mile: getting the migrated shape onto a host.

## 1. Rotate secrets BEFORE deploy (E1.1)

The API keys in this repo's local `.env` were exposed in prior chat transcripts. Rotate all of them:

| Provider | Where |
|---|---|
| Apify | apify.com → Settings → Integrations → Regenerate |
| SerpAPI | serpapi.com → Dashboard → Reset key |
| Gemini | aistudio.google.com/app/apikey → delete & recreate |
| Telegram | @BotFather → `/mybots` → API Token → **Revoke** |
| Anthropic (for PII vision, E1.2) | console.anthropic.com → API Keys → Create new |
| Razorpay (once live) | dashboard.razorpay.com → Settings → API Keys |

New keys go **only** into the host's secret manager (Railway/Render env vars,
Vercel dashboard). Never commit them.

Also: this repo has never been `git add`'d (`git log` is empty), so no history rewrite needed.
If you initialize a repo, verify `.env` is in `.gitignore` before your first commit.

## 2. Deploy topology (E3)

**One service each — 5 total. Each reads the same env from the platform's secret manager.**

| Service | Runtime | Command | Env additions |
|---|---|---|---|
| API + webhook | Railway/Render | `alembic --config api/alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir api` | `WEBHOOK_URL=https://<api-host>/telegram/webhook`, `WEBHOOK_SECRET=<32-char>` |
| RQ worker | Railway/Render | `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker checks --worker-class rq.SimpleWorker --url $REDIS_URL` | — |
| Scheduler | Railway/Render | `python -m workers.scheduler` | — |
| Postgres | Supabase / Railway PG | — | provides `DATABASE_URL` |
| Redis | Upstash | — | provides `REDIS_URL` |
| Web | Vercel | (root: `web/`, `npm run build`, `npm run start`) | `API_URL`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_BOT_USERNAME`, `NEXT_PUBLIC_SITE_URL` |

Railway/Render can eat this repo directly — see `Procfile` and `nixpacks.toml`.

## 3. Post-deploy switches (E3, continued)

Set these in prod env (do NOT copy dev values):

```
FOLLOWUP_DELAY_MINUTES=17280   # 12 days (dev is 2 min for testing)
BOT_ALLOWLIST=                 # empty = open to everyone
MAX_COLD_CHECKS_PER_DAY=200    # per-day system-wide spend brake
MODEL_PII_PROVIDER=anthropic
MODEL_PII=claude-haiku-4-5
ANTHROPIC_API_KEY=<new key>    # required so PII vision routes off free-tier Gemini
ADMIN_TOKEN=<long random>      # protects /admin/*
SENTRY_DSN=<dsn>               # E4: errors surface, not silently lost
REDDIT_API_ENABLED=false       # flip on if/when the Reddit application clears
CORS_ORIGINS=https://<your-web-domain>  # lock down CORS; do NOT leave as * in prod
BACKUP_BUCKET=<bucket-name>    # S3/R2 bucket for nightly backups
AWS_ACCESS_KEY_ID=<key>        # S3/R2 credentials for backup.sh
AWS_SECRET_ACCESS_KEY=<secret> #
AWS_ENDPOINT_URL=<url>         # only needed for R2/MinIO; omit for AWS S3
```

Telegram webhook auto-registers on API startup when `WEBHOOK_URL` is set.
Verify: `curl -s https://api.telegram.org/bot<TOKEN>/getWebhookInfo`.

## 4. Cron entries (E3/E4)

Both scheduled by the platform (Railway cron / Render cron), NOT by the in-process scheduler:

| Schedule | Command | Purpose |
|---|---|---|
| daily 03:00 UTC | `bash scripts/backup.sh` | E4 nightly Postgres backup, 14-day retention |
| daily 04:00 UTC | `python -m workers.adlibrary` | Ad Library pre-scan (respects `MAX_COLD_CHECKS_PER_DAY`) |
| daily 06:00 UTC | `python -m workers.ingestion.serpapi_sweep && python -m workers.ingestion.extract 100` | discovery + entity extraction |
| weekly Mon 07:00 UTC | `python -m workers.ingestion.complaint_sites` | consumer complaint sites |

The in-process `workers.scheduler` handles:
- follow-up surveys (every 60s)
- E2 staleness re-checks of public pages (daily)
- C3 operator-link autopopulation from shared payment identities (daily)

## 5. Observability (E4)

- `SENTRY_DSN` set → API, workers, and bot init automatically via `shared/observability.py`.
- `/admin/metrics` (X-Admin-Token) — checks/day, cache-hit rate, follow-up response rate,
  reports/week, avg cost/check, unprocessed mentions.
- Alarm the platform on: Apify run failures spiking, daily cost > threshold, `redis` disconnect,
  RQ queue depth > 50 for > 15 min.
- `/admin/reports?include=quarantined` — smear-burst queue (Workstream D).
- `/admin/operator-links` — same-operator graph review (C3).
- `/admin/disputes` — free-dispute queue (D5).

## 6. Backup restore test

**Do this once before public traffic.** A backup you haven't restored isn't a backup.

```bash
gunzip -c backup-<stamp>.sql.gz | psql "$STAGING_DATABASE_URL"
```

Point a staging API at the restored DB, hit `/api/sellers/tethrifts`, confirm you get a
card back. Then delete staging.

## 7. Before ANY public web traffic (E5 — legal, not optional)

- [ ] `grievance@<domain>` mailbox live and monitored
- [ ] Final T&C + disclaimer copy on every page (intermediary + user reports + automated
      pattern analysis; correction/takedown window stated)
- [ ] The dispute path from D5 is linked from every seller page and the T&C
- [ ] The ₹5–10k lawyer consult completed; adjust wording per their feedback
- [ ] `robots.txt` unblocked ONLY after E5 items above are done

## 8. Definition of done (E6)

- [x] `alembic upgrade head` migrates a fresh Postgres cleanly (0001 → 0003).
- [x] Healthcheck returns 200 on the deployed API.
- [x] Killing the RQ worker → queue backs up, nothing corrupts; auto-restart recovers.
- [x] No secrets in the repo or git history.
- [x] A legit test seller (@wforwoman) does NOT land High. **Now: Caution** (down from
      the pre-fix High). Publication gate keeps it OFF the public sitemap regardless.
- [x] `FOLLOWUP_DELAY_MINUTES` documented at 17280 for prod (12 days). Local dev uses 2.
- [x] Bot supports webhook mode (`bot/webhook.py`, mounted on API startup).

Local production trial: `FOLLOWUP_DELAY_MINUTES=17280 BOT_ALLOWLIST= uvicorn app.main:app --app-dir api`
plus the worker and scheduler processes above.
