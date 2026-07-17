from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg://instacop:instacop@localhost:5432/instacop"
    REDIS_URL: str = "redis://localhost:6379/0"

    TELEGRAM_BOT_TOKEN: str = ""
    TELETHON_API_ID: str = ""
    TELETHON_API_HASH: str = ""
    TELETHON_SESSION: str = ""

    APIFY_TOKEN: str = ""
    SERPAPI_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Provider is swappable per call-site: "anthropic" or "gemini". Both extraction
    # and synthesis default to Gemini (cheapest) -- ANTHROPIC_API_KEY is optional,
    # only read if you flip either provider to "anthropic".
    MODEL_EXTRACT_PROVIDER: str = "gemini"
    MODEL_EXTRACT: str = "gemini-3.1-flash-lite"
    MODEL_SYNTH_PROVIDER: str = "gemini"
    MODEL_SYNTH: str = "gemini-3.1-flash-lite"

    # PII-bearing calls (payment screenshots: UPI ids, phones, names, amounts)
    # must NOT run on free-tier Gemini (training-permitted terms). Default to
    # Anthropic; if the key is absent in dev, code falls back with a loud warning.
    MODEL_PII_PROVIDER: str = "gemini"
    MODEL_PII: str = "gemini-3.1-flash-lite"

    MAX_COLD_CHECKS_PER_DAY: int = 200
    BOT_ALLOWLIST: str = ""  # comma-separated Telegram user ids; empty = open
    ADMIN_USER_IDS: str = ""  # comma-separated Telegram user ids exempt from rate limits (operators)

    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = "instacop-ingestion/0.1"
    REDDIT_API_ENABLED: bool = False  # flips PRAW path on once Reddit approves access
    BRAND_REVIEW_STALENESS_DAYS: int = 30

    META_AD_LIBRARY_TOKEN: str = ""
    ADMIN_TOKEN: str = ""  # protects admin endpoints; set a long random string

    # 12 days in prod (spec); set to 2 for a 2-minute live test of the loop
    FOLLOWUP_DELAY_MINUTES: int = 12 * 24 * 60
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    WHOISXML_KEY: str = ""
    SENTRY_DSN: str = ""
    BASE_URL: str = "http://localhost:8000"

    # Prod bot mode: set WEBHOOK_URL=https://api.<domain>/telegram/webhook (and
    # WEBHOOK_SECRET to a long random string) to run the bot in webhook mode
    # via the API process. Empty = long polling (dev/laptop).
    WEBHOOK_URL: str = ""
    WEBHOOK_SECRET: str = ""

    # Comma-separated origins for CORS. In prod, set to your web domain,
    # e.g. "https://instacop.shop". Defaults to wildcard (dev only).
    # "hikerapi" (paid, ~$0.001/request, reliable), "instaloader" (free but
    # Instagram bans the login account) or "apify" (paid, credit-billed).
    IG_PROVIDER: str = "instaloader"
    HIKERAPI_TOKEN: str = ""  # dashboard.hikerapi.com access key
    # Instagram session cookies for instaloader. Anonymous scraping is now
    # fully blocked by Instagram (403), so IG_SESSION_ID is effectively
    # required. Copy sessionid (and ideally csrftoken) from browser DevTools
    # after logging in: Application > Cookies > instagram.com.
    IG_SESSION_ID: str = ""
    IG_CSRF_TOKEN: str = ""  # optional; bootstrapped automatically when empty

    # Instagram DM bot (Meta Instagram Messaging API). All three come from the
    # Meta app: access token for the connected IG professional account, the
    # webhook verify token you choose, and the app secret for payload
    # signature checks. Empty = Instagram DM surface disabled.
    IG_DM_ACCESS_TOKEN: str = ""
    IG_DM_VERIFY_TOKEN: str = ""
    IG_DM_APP_SECRET: str = ""

    CORS_ORIGINS: str = "*"


settings = Settings()
