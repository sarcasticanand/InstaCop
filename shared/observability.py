import logging

from shared.config import settings

logger = logging.getLogger(__name__)


def init_sentry(component: str) -> None:
    """No-op unless SENTRY_DSN is set. Call once at process start."""
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=component, traces_sample_rate=0.05)
        logger.info("sentry initialised for %s", component)
    except ImportError:
        logger.warning("SENTRY_DSN set but sentry-sdk not installed (pip install sentry-sdk)")
