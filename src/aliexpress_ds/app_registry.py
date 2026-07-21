"""AppKey / Secret registry in Upstash Redis (same as aliexpress-oauth AppRegistry).

Hash key: ``aliexpress:oauth:apps``
Field: app_key
Value: JSON ``{"app_secret": "...", "label": "..."}``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aliexpress_ds.config import Settings

logger = logging.getLogger(__name__)

REDIS_APPS_KEY = "aliexpress:oauth:apps"
# Re-read Redis periodically so console secret updates apply without restart.
_MEMORY_MAX_AGE = 300.0

_cache_lock = threading.Lock()
# cache_key → (monotonic_ts, app_secret, label)
_secret_cache: dict[str, tuple[float, str, str | None]] = {}


def _cache_key(settings: Settings) -> str:
    url = (settings.redis_url or "").strip()
    app = (settings.aliexpress_app_key or "").strip()
    return f"{url}|{app}"


def load_app_from_redis(
    app_key: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Return ``{app_key, app_secret, label}`` from Redis hash, or None."""
    from aliexpress_ds.config import get_settings

    settings = settings or get_settings()
    url = (settings.redis_url or "").strip()
    key = (app_key or "").strip()
    if not url or not key or key.startswith("your_"):
        return None

    from aliexpress_ds.token_store import _redis_client

    client = _redis_client(url)
    raw = client.hget(REDIS_APPS_KEY, key)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in Redis %s field %s", REDIS_APPS_KEY, key)
        return None
    if not isinstance(data, dict):
        return None
    secret = str(data.get("app_secret") or data.get("secret") or "").strip()
    if not secret:
        return None
    label = str(data.get("label") or "").strip() or None
    return {"app_key": key, "app_secret": secret, "label": label}


def ensure_app_credentials(settings: Settings | None = None) -> Settings:
    """Fill ``aliexpress_app_secret`` from Redis when present (preferred over .env).

    ``ALIEXPRESS_APP_KEY`` in .env selects which Redis hash field to use.
    Token remains in ``aliexpress:oauth:token:{app_key}`` (see token_store).
    """
    from aliexpress_ds.config import get_settings

    settings = settings or get_settings()
    app_key = (settings.aliexpress_app_key or "").strip()
    url = (settings.redis_url or "").strip()
    if not app_key or app_key.startswith("your_") or not url:
        return settings

    ck = _cache_key(settings)
    now = time.monotonic()
    with _cache_lock:
        hit = _secret_cache.get(ck)
        if hit is not None:
            cached_at, secret, _label = hit
            if now - cached_at <= _MEMORY_MAX_AGE and secret:
                settings.aliexpress_app_secret = secret
                return settings

    entry = load_app_from_redis(app_key, settings=settings)
    if entry and entry.get("app_secret"):
        secret = str(entry["app_secret"])
        label = entry.get("label")
        settings.aliexpress_app_secret = secret
        with _cache_lock:
            _secret_cache[ck] = (now, secret, label if isinstance(label, str) else None)
        logger.debug(
            "Loaded app_secret from Redis %s[%s] label=%s",
            REDIS_APPS_KEY,
            app_key,
            label,
        )
        return settings

    # Keep .env secret as fallback for apps not yet in the Redis registry.
    return settings


def clear_app_credential_cache() -> None:
    with _cache_lock:
        _secret_cache.clear()
