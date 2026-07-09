# InstaCop — Project Documentation

*Last updated: 2 July 2026. Status: **all 7 build phases complete, full stack running locally, bot live as [@capdetector_bot](https://t.me/capdetector_bot).***

---

## 1. What this product is

A trust-check service for Instagram-based sellers in India. A buyer who finds a shop on
Instagram checks it **before paying** and gets back a risk report built from two strictly
separated scores:

1. **Fraud Risk Score** — fully algorithmic. Counts how many known scam patterns the seller
   matches, weighted by how damning each pattern is. Always displayed as evidence
   ("matches 3 of 8 fraud patterns" + the specific findings), **never** as a verdict
   ("scammer"). This separation is a legal design constraint, not styling: we publish
   pattern analysis of public data, not accusations.
2. **Customer Experience Score** — aggregated from bot users' follow-up survey answers and
   `/report` submissions only (first-party, structured, tied to real check events — not
   scraped reviews). Captures the slow-delivery/poor-quality middle ground of
   real-but-mediocre sellers.

**The moat:** the follow-up loop. The bot knows who checked which seller and when. 12 days
later it asks "did you buy? how did it go?" — building a proprietary
check → decision → outcome dataset no competitor can scrape.

**Revenue model (built, not yet live):** sellers pay a monthly subscription (₹299 basic /
₹999 featured) for a verified page after KYC/GST checks. Verification is **revocable**:
2+ accepted fraud reports in 30 days auto-revokes and flips the public page regardless of
payment — credibility of the badge is the product.

---

## 2. System architecture

```
 INGESTION (cron)                     USER SURFACES
 ┌─────────────────────┐             ┌──────────────────┐   ┌─────────────────┐
 │ SerpAPI sweeps      │             │ Telegram bot     │   │ Next.js web     │
 │ Complaint sites     │             │ @capdetector_bot │   │ localhost:3002  │
 │ TG channels (blocked│             │ (aiogram 3,      │   │ SEO pages +     │
 │  on operator creds) │             │  long polling)   │   │ checker + proxy │
 │ Reddit (blocked on  │             └───────┬──────────┘   └────────┬────────┘
 │  Reddit approval)   │                     │ cached: serve         │ reads
 └─────────┬───────────┘                     │ cold: enqueue         ▼
           │ raw_mentions                    ▼               ┌─────────────────┐
           ▼                          ┌──────────────┐       │ FastAPI :8000   │
 ┌─────────────────────┐              │ Redis + RQ   │◀──────│ /api/sellers/*  │
 │ extract job (Gemini)│              │ queue:checks │       │ /admin/*        │
 │ mentions → sellers/ │              └──────┬───────┘       │ /api/verify/*   │
 │ payment_ids/reports │                     ▼               └─────────────────┘
 └─────────────────────┘              ┌──────────────┐                ▲
                                      │ RQ worker    │                │
           ┌──────────────────────────│ runs engine  │──── Postgres ──┘
           ▼                          └──────────────┘   (single source of truth)
 ┌─────────────────────┐
 │ RISK ENGINE          │  profile scrape (Apify) → 9 signals (parallel data:
 │ engine/check.py      │  image search, whois, comment classification) →
 │                      │  weighted scoring → LLM card synthesis → snapshot
 └─────────────────────┘
```

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 / Alembic · Postgres · Redis + RQ ·
aiogram 3 · Apify (Instagram data) · SerpAPI (Google Lens + discovery) · python-whois ·
**Gemini** (all LLM calls; Anthropic optional via config) · Next.js 15 (App Router) ·
Razorpay (test-mode-gated).

**Key architectural rules:**
- All Instagram access goes through one interface (`engine/ig_provider.py`). Apify today;
  screenshot-vision fallback built; swapping providers never touches signal code.
- All LLM access goes through one interface (`engine/llm.py`). Provider + model per
  call-site set by env vars (`MODEL_EXTRACT*`, `MODEL_SYNTH*`). Currently everything runs
  on `gemini-3.1-flash-lite` for cost; pricing table lives in one place in that file.
- Web pages never trigger paid checks from anonymous traffic — unknown sellers deep-link to
  the bot (`t.me/capdetector_bot?start=check_<handle>`). Cost control + bot adoption.

---

## 3. The 9 fraud signals (as implemented)

| # | Signal | Data source | Matches when | Weight |
|---|--------|-------------|--------------|--------|
| 1 | Young account, selling | Oldest-post date proxy + bio keywords (price, DM to order, COD, ₹…) | age ≤ 90d AND selling | **2** if ≤30d, else 1 |
| 2 | Stolen product photos | Up to 5 recent images → SerpAPI Google Lens; **early-exits at 2 hits**; excludes instagram.com and the seller's own site | ≥2 images found on external sites | **3** |
| 3 | No organic customer tags | Tagged-tab posts via Apify `mentions` (posts by *others* tagging the seller) | ≥60d account with <3 tags, OR tag burst within one week | 1 |
| 4 | Comment red flags | Comment **text** fetched from 3 most-commented own posts + 3 most-commented tagged posts; single Gemini classification call | comments disabled, OR own-post complaint ratio >10%, OR **≥2 complaints on tagged posts**, OR >70% bot-like | **3** if ratio ≥25% or ≥3 tagged complaints; 2 if complaint-driven; 1 if disabled-only |
| 5 | Suspicious bio website | whois domain age + page text → Gemini analysis (address? GST? returns? prepaid-only?) | domain <60d, OR ≥3 of 4 trust markers missing | **2** if domain <60d, else 1 |
| 6 | Prior fraud mentions | Own DB: reports with status `accepted` | ≥1 accepted report | **3** |
| 7 | Purchased followers | Engagement rate over last 12 posts | <0.3% with >10k followers | 1 |
| 8 | Off-platform funnel | Bio+captions → Gemini | DM/WhatsApp-only AND no checkout AND urgency, all three | 1 |
| 9 | Payment identity history | UPI/phone linked to seller, cross-checked against **all** sellers (the cross-handle catch) | that identity has ≥1 accepted report on any handle | **3** |

**Notes on signal design decisions made during calibration:**
- *Tagged posts are the ground truth* (user insight): comments there live on other
  accounts, so the seller can't delete them. Positive comments on tagged posts are ignored
  (usually praise for the influencer who posted); complaints ("never arrived", "cheap
  quality", "scam") are treated as brand-directed.
- *Recency:* comment timestamps are captured; complaints within the last 60 days are
  surfaced separately in evidence ("N complaint(s) posted within the last 60 days").
- Uncomputable signals (private account, no website, scrape failure) are excluded from the
  denominator — cards honestly say "checked 7 of 9".

## 4. Scoring

Weighted sum of matched signals (weights above):

| Weighted score | Band |
|---|---|
| ≥ 5 | 🔴 High risk |
| 2–4 | 🟡 Caution |
| 0–1 | 🟢 Low risk |
| <5 signals computable | ⚪ Insufficient info ("low information ≠ low risk") |

Rationale: one hard signal (weight 3) plus any strong corroboration crosses into High;
soft signals alone can't push a seller past Caution. Weights and thresholds live in
`engine/scoring.py` and are the **starting calibration** — to be tuned against the
operator's hand-labelled 50-handle set (spec §9.5).

**Legal guardrails (two layers):** the synthesis LLM prompt hard-bans verdict nouns, and a
deterministic regex (`engine/llm.py: BANNED_VERDICT_WORDS`) discards any output containing
scammer/fraudster/con artist/thief/criminal/cheat(er) — falling back to code-generated
evidence strings. "Fraud patterns" / "scam patterns" as category language is deliberately
allowed (spec-mandated phrasing).

## 5. Cost model (measured, not estimated)

| Item | Per cold check |
|---|---|
| Apify: profile scrape + mentions + 2× comment fetches | ~₹2.0–5.5 |
| SerpAPI Lens: ≤5 calls, usually 2 (early exit) | ~₹1.2–2.5 |
| Gemini: classification + analysis + synthesis (~5 calls) | ~₹0.05 (often free tier) |
| **Total measured across 16 live checks** | **₹2.5–7.2** (avg ₹3.25) |

Hard cap ₹30/check enforced in code (`engine/cost.py`); signals skip gracefully past the
cap. Cached checks cost ~₹0. Spec budget was ₹25 — we run at ~13% of it.

## 6. What was live-tested (real money, real data)

- **16 cold checks** across your 7 handles (several re-runs during calibration):
  final bands — 5 High (tethrifts, thriftykeralaft, gapstore_._, dotmen.india,
  shiromani.india), 2 Caution (shoezo_online, trendfactory_clothing). Notable catches:
  a 2-day-old account selling with stolen photos; a seller with a 50% complaint ratio
  visible in both own-post and tagged-post comments.
- **Ingestion:** 114 raw mentions from live SerpAPI sweeps (incl. 14+ Reddit threads via
  Google), all processed → **38 sellers, 5 payment identities, 29 reports** currently in DB.
  One of your test handles (shiromani.india) independently surfaced in public scam threads.
- **Verification lifecycle:** apply → admin approve → 🛡 on page → 2 accepted reports →
  auto-revoke flipped the page publicly. Full cycle demonstrated.
- **Web:** home checker (live card render), SEO seller pages, sitemap (8 URLs), 404→bot
  deep-link flow, disclaimer footer — verified in browser.
- **Queue/rate limits:** RQ round trip, 6th cold check and 21st group check correctly
  blocked, cache states (fresh serve / stale banner+refresh / cold enqueue).

**Bugs caught by live data (all fixed):** apify-client 3.x breaking API change; Apify
`details` returning profile objects → false "comments disabled" on every seller; seller's
own website counted as a stolen-photo source; tagged-posts originally read from the wrong
data source (seller's own tags, not the Tagged tab); comment text requiring dedicated
fetches; rate limit checked after enqueue (spend leak); Gemini free-tier 429s (backoff added).

## 7. How to test right now (from your phone)

Everything is already running. Open Telegram → **@capdetector_bot**:

1. `/start` → intro
2. Send `tethrifts` (or any checked handle) → **instant cached card**
3. Send a handle we've never checked → ack in <2s → early signal (~60–90s) → full card
   (~2–4 min). Watch the RQ worker do the work.
4. `/report` → pick the seller → choose what happened → send any payment screenshot
   (UPI/phone gets vision-extracted into `payment_identities`) → one-line note → logged.
5. **Follow-up loop is set to 2 minutes for testing** (`FOLLOWUP_DELAY_MINUTES=2` in .env):
   ~2 min after a cold check completes, the bot asks "did you buy?" with 5 buttons; your
   answer lands in the Customer Experience score and on the web page.
   **Set it to 17280 (12 days) before real users touch it.**
6. Web: http://localhost:3002 — paste a handle; http://localhost:3002/tethrifts — SEO page.
7. Add the bot to a group → `/check handle` or @capdetector_bot mention works, 20/day cap.

Local process map: FastAPI :8000 · Next.js :3002 · bot (polling) · `rq worker checks` ·
follow-up scheduler — all running in background shells. Restart commands in README.md.

## 8. Current dataset

38 sellers · 23 risk snapshots · 114 raw mentions (100% processed) · 29 reports
(unreviewed — triage them at `GET /admin/metrics` / `GET /admin/reports` with
`X-Admin-Token`) · 5 payment identities · 2 recorded checks (bot-era; engine-era checks
predate the checks-row wiring).

## 9. External dependencies & their status

| Dependency | Status | Notes |
|---|---|---|
| Apify (Instagram) | ✅ live | #1 platform risk per spec; screenshot-vision fallback built |
| SerpAPI (Lens + discovery) | ✅ live | free tier 250 searches/mo — watch this first, Lens eats it |
| Gemini API | ✅ live (free tier) | 15 req/min limit handled with backoff; paid tier removes it |
| Telegram bot | ✅ live | @capdetector_bot, polling; switch to webhook for prod |
| Anthropic | optional | wired but unused; flip `MODEL_SYNTH_PROVIDER=anthropic` any time |
| Reddit API | ❌ blocked upstream | self-serve keys ended (Responsible Builder Policy). Apply via Reddit Help → "Developer Platform & Accessing Reddit Data"; SerpAPI `site:reddit.com` queries are the compliant substitute meanwhile |
| Telethon channels | ⏳ operator | needs my.telegram.org creds + curated `config/telegram_channels.txt` |
| Meta Ad Library | ⏳ operator | identity verification takes days — start now; sweep code ready |
| Razorpay | ⏳ operator | flow works without keys (dev mode); needs sole-prop + PAN when real |
| WhoisXML | optional | fallback for flaky .in whois |

## 10. Known limitations & calibration agenda

1. **Stolen-photos dominates.** It matched all 7 test handles and carries weight 3. For
   thrift/reseller shops, product photos appearing on eBay/retail sites is partly inherent
   to the niche. **Next calibration step: run known-legit sellers (original photography)
   and confirm they land Low; if legit resellers land High, discount marketplace-listing
   matches or require retail-catalog domains.**
2. Signals 6/9 are live but empty until you accept reports in admin — triage the 29 pending.
3. Account age is a proxy (oldest post) — gameable by buying aged accounts; that's why
   9 signals exist.
4. Web app uses plain CSS, not Tailwind (spec deviation for build speed).
5. Bot runs long-polling; production wants webhook mode + Railway/Render deployment.
6. Engagement-rate math can't see reel views; very video-heavy sellers may under-trigger
   signal 7.
7. `.env` currently holds real keys — never commit it; rotate keys before adding
   collaborators. (Also: the keys were pasted in this chat; rotating them at deploy time
   is cheap insurance.)

## 11. Deployment plan (when ready)

- **API + workers + bot:** Railway or Render — 4 processes (`uvicorn`, `rq worker checks`,
  `python -m bot.main` in webhook mode, `python -m workers.scheduler`) + cron entries from
  README.md. Upstash for Redis, Supabase for Postgres.
- **Web:** Vercel; set `API_URL`, `NEXT_PUBLIC_BOT_USERNAME`, `NEXT_PUBLIC_SITE_URL`.
- **Before public web traffic** (spec §9.4): grievance@ mailbox on the real domain, final
  T&C/disclaimer copy, and the ₹5–10k lawyer consult.
- Migrations: `alembic upgrade head` on deploy. Backups: daily `pg_dump` (README).

## 12. Operator to-do list (only you can do these)

1. **Now:** triage the 29 reports in admin (activates signals 6/9); set
   `FOLLOWUP_DELAY_MINUTES=17280` after testing; spot-check bot cards against real profiles.
2. **This week:** Meta identity verification (lead time!); Reddit access application;
   my.telegram.org creds + join 10–20 scam-alert channels into the config file;
   hand-collect the 100–200 known-scam handles for signal-6 validation.
3. **Pre-launch:** domain purchase, grievance mailbox, lawyer consult, key rotation,
   deploy per §11.
4. **Growth (spec §9.8):** bot into 5 buy/sell groups, 1 scam-breakdown reel/week,
   reply with bot link in viral scam-callout threads.
5. **Phase 6 trigger:** DM 30–50 clean sellers from your green-card results; free verified
   link for first 25; their willingness to pay ₹299/mo decides if Razorpay goes live.
