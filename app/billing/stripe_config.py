"""Resolved Stripe credentials and helpers (test/live mode)."""

from __future__ import annotations

import logging
import warnings
from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

StripeMode = Literal["test", "live"]

_UNSAFE_HOST_FRAGMENTS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "trycloudflare.com",
    "ngrok.io",
    "ngrok-free.app",
    "loca.lt",
)


def mask_secret(value: Optional[str], *, keep: int = 4) -> str:
    raw = (value or "").strip()
    if not raw:
        return "(empty)"
    if len(raw) <= keep * 2:
        return "***"
    return f"{raw[:keep]}…{raw[-keep:]}"


def _first_nonempty(*values: str) -> str:
    for v in values:
        if (v or "").strip():
            return v.strip()
    return ""


class StripeRuntime:
    """Active Stripe credentials selected by STRIPE_MODE (+ legacy fallbacks)."""

    def __init__(self) -> None:
        self.mode: StripeMode = self._resolve_mode()
        self.legacy_used = False
        self.webhook_secret_source = "NONE"
        self.webhook_secret_is_legacy = False
        self.secret_key = ""
        self.publishable_key = ""
        self.webhook_secret = ""
        self.price_starter = ""
        self.price_professional = ""
        self.price_business = ""
        self._load_keys()

    def _resolve_mode(self) -> StripeMode:
        raw = (settings.STRIPE_MODE or "").strip().lower()
        if raw in ("test", "live"):
            return raw  # type: ignore[return-value]
        # Default: production → live, everything else → test
        if settings.app_env == "production":
            return "live"
        return "test"

    def _pick_secret(self, preferred_name: str, preferred: str, legacy_name: str, legacy: str) -> str:
        pref = (preferred or "").strip()
        leg = (legacy or "").strip()
        if pref:
            self.webhook_secret_source = preferred_name
            self.webhook_secret_is_legacy = False
            return pref
        if leg:
            self.webhook_secret_source = legacy_name
            self.webhook_secret_is_legacy = True
            return leg
        self.webhook_secret_source = "NONE"
        self.webhook_secret_is_legacy = False
        return ""

    def _load_keys(self) -> None:
        if self.mode == "live":
            self.secret_key = _first_nonempty(settings.STRIPE_LIVE_SECRET_KEY, settings.STRIPE_SECRET_KEY)
            self.publishable_key = _first_nonempty(
                settings.STRIPE_LIVE_PUBLISHABLE_KEY, settings.STRIPE_PUBLISHABLE_KEY
            )
            self.webhook_secret = self._pick_secret(
                "STRIPE_LIVE_WEBHOOK_SECRET",
                settings.STRIPE_LIVE_WEBHOOK_SECRET,
                "STRIPE_WEBHOOK_SECRET",
                settings.STRIPE_WEBHOOK_SECRET,
            )
            self.price_starter = _first_nonempty(
                settings.STRIPE_LIVE_PRICE_STARTER, settings.STRIPE_PRICE_STARTER
            )
            self.price_professional = _first_nonempty(
                settings.STRIPE_LIVE_PRICE_PROFESSIONAL, settings.STRIPE_PRICE_PROFESSIONAL
            )
            self.price_business = _first_nonempty(
                settings.STRIPE_LIVE_PRICE_BUSINESS, settings.STRIPE_PRICE_BUSINESS
            )
        else:
            self.secret_key = _first_nonempty(settings.STRIPE_TEST_SECRET_KEY, settings.STRIPE_SECRET_KEY)
            self.publishable_key = _first_nonempty(
                settings.STRIPE_TEST_PUBLISHABLE_KEY, settings.STRIPE_PUBLISHABLE_KEY
            )
            self.webhook_secret = self._pick_secret(
                "STRIPE_TEST_WEBHOOK_SECRET",
                settings.STRIPE_TEST_WEBHOOK_SECRET,
                "STRIPE_WEBHOOK_SECRET",
                settings.STRIPE_WEBHOOK_SECRET,
            )
            self.price_starter = _first_nonempty(
                settings.STRIPE_TEST_PRICE_STARTER, settings.STRIPE_PRICE_STARTER
            )
            self.price_professional = _first_nonempty(
                settings.STRIPE_TEST_PRICE_PROFESSIONAL, settings.STRIPE_PRICE_PROFESSIONAL
            )
            self.price_business = _first_nonempty(
                settings.STRIPE_TEST_PRICE_BUSINESS, settings.STRIPE_PRICE_BUSINESS
            )

        # Legacy single-slot vars
        if settings.STRIPE_SECRET_KEY and not (
            settings.STRIPE_TEST_SECRET_KEY or settings.STRIPE_LIVE_SECRET_KEY
        ):
            self.legacy_used = True
        if settings.STRIPE_PRICE_STARTER and not (
            settings.STRIPE_TEST_PRICE_STARTER or settings.STRIPE_LIVE_PRICE_STARTER
        ):
            self.legacy_used = True
        if settings.STRIPE_WEBHOOK_SECRET and not (
            settings.STRIPE_TEST_WEBHOOK_SECRET or settings.STRIPE_LIVE_WEBHOOK_SECRET
        ):
            self.legacy_used = True

    def price_for_plan(self, plan_key: str) -> Optional[str]:
        mapping = {
            "starter": self.price_starter,
            "professional": self.price_professional,
            "business": self.price_business,
        }
        return mapping.get(plan_key) or None

    def plan_from_price(self, price_id: Optional[str]) -> Optional[str]:
        if not price_id:
            return None
        pid = price_id.strip()
        for key in ("starter", "professional", "business"):
            if self.price_for_plan(key) == pid:
                return key
        return None

    def assert_price_matches_mode(self, price_id: str) -> None:
        """Soft check: live mode should use live-looking prices only via configured IDs."""
        if not price_id:
            raise ValueError("Price ID is empty")
        configured = {self.price_starter, self.price_professional, self.price_business}
        if price_id not in configured:
            raise ValueError("Price ID is not configured for the active Stripe mode")


@lru_cache
def get_stripe_runtime() -> StripeRuntime:
    return StripeRuntime()


def clear_stripe_runtime_cache() -> None:
    get_stripe_runtime.cache_clear()


def _url_is_unsafe_for_production(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return True
    if parsed.scheme != "https":
        return True
    return any(frag in host for frag in _UNSAFE_HOST_FRAGMENTS)


def validate_stripe_configuration(*, for_startup: bool = True) -> list[str]:
    """Return list of configuration errors (empty if OK)."""
    errors: list[str] = []
    rt = get_stripe_runtime()
    env = settings.app_env

    if rt.legacy_used and for_startup:
        warnings.warn(
            "Deprecated Stripe env vars (STRIPE_SECRET_KEY / STRIPE_PRICE_* / STRIPE_WEBHOOK_SECRET) "
            "are in use. Migrate to STRIPE_TEST_* / STRIPE_LIVE_* and STRIPE_MODE.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "Deprecated legacy Stripe env vars in use (mode=%s secret=%s). "
            "Prefer STRIPE_TEST_* / STRIPE_LIVE_*.",
            rt.mode,
            mask_secret(rt.secret_key),
        )

    if for_startup and rt.mode == "test" and rt.webhook_secret_is_legacy:
        logger.warning(
            "Stripe test mode is verifying webhooks with legacy %s (masked=%s). "
            "Prefer STRIPE_TEST_WEBHOOK_SECRET from the Dashboard endpoint that points at "
            "the current PUBLIC_BASE_URL / Cloudflare tunnel + /api/billing/webhook. "
            "Full API restart is required after .env changes (get_stripe_runtime is cached).",
            rt.webhook_secret_source,
            mask_secret(rt.webhook_secret, keep=6),
        )

    # Production must be live
    if env == "production" and rt.mode != "live":
        errors.append("STRIPE_MODE must be 'live' when APP_ENV=production")

    # Staging defaults to test unless STRIPE_MODE=live explicitly set
    if env == "staging" and (settings.STRIPE_MODE or "").strip().lower() == "live":
        # allowed if explicitly live
        pass

    secret = rt.secret_key
    if secret:
        if rt.mode == "test" and not secret.startswith("sk_test_"):
            errors.append("STRIPE_MODE=test requires a secret key with prefix sk_test_")
        if rt.mode == "live" and not secret.startswith("sk_live_"):
            errors.append("STRIPE_MODE=live requires a secret key with prefix sk_live_")

    # Dev must not use live keys unless override
    if env in ("dev", "test") and secret.startswith("sk_live_"):
        if not settings.STRIPE_ALLOW_LIVE_IN_DEV:
            errors.append(
                "Live Stripe secret key rejected in development/test. "
                "Set STRIPE_ALLOW_LIVE_IN_DEV=true only for explicit break-glass."
            )

    pub = rt.publishable_key
    if pub:
        if rt.mode == "test" and not pub.startswith("pk_test_"):
            errors.append("Test mode publishable key must start with pk_test_")
        if rt.mode == "live" and not pub.startswith("pk_live_"):
            errors.append("Live mode publishable key must start with pk_live_")

    # Production URL checks when Stripe is configured
    stripe_configured = bool(secret and rt.webhook_secret)
    if env == "production" and stripe_configured:
        for name, url in (
            ("STRIPE_SUCCESS_URL", settings.STRIPE_SUCCESS_URL),
            ("STRIPE_CANCEL_URL", settings.STRIPE_CANCEL_URL),
            ("STRIPE_PORTAL_RETURN_URL", settings.STRIPE_PORTAL_RETURN_URL),
        ):
            u = (url or "").strip()
            if not u:
                errors.append(f"{name} is required in production when Stripe is configured")
            elif _url_is_unsafe_for_production(u):
                errors.append(
                    f"{name} must be HTTPS and must not use localhost/tunnels in production"
                )

        fe = (settings.FRONTEND_BASE_URL or "").strip()
        if fe and _url_is_unsafe_for_production(fe):
            errors.append("FRONTEND_BASE_URL must be a public HTTPS origin in production")

        if not rt.price_starter or not rt.price_professional or not rt.price_business:
            errors.append("Live Stripe Price IDs for starter/professional/business are required in production")

    return errors

