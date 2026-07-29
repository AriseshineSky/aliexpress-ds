"""AppKey / Secret registry in Upstash Redis (same as aliexpress-oauth AppRegistry).

Hash key: ``aliexpress:oauth:apps``
Field: app_key
Value: JSON::

    {
      "app_secret": "...",
      "label": "...",
      "daily_limit": 70000,          # optional pacing basis; never a local cap
      "min_interval_sec": 1.0        # optional only when daily_limit is unset
    }
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
# Re-read Redis periodically so console secret / limit updates apply without restart.
_MEMORY_MAX_AGE = 300.0

SECONDS_PER_DAY = 86400.0
# When Redis has no daily_limit for this AppKey: pace at 1s.
DEFAULT_INTERVAL_NO_DAILY_LIMIT = 1.0
# Never pace slower than 1 request/sec (interval > 1s → clamp to 1s).
MAX_PACE_INTERVAL_SEC = 1.0

_cache_lock = threading.Lock()
# cache_key → (monotonic_ts, entry_dict)
_entry_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(settings: Settings) -> str:
    url = (settings.redis_url or "").strip()
    app = (settings.aliexpress_app_key or "").strip()
    return f"{url}|{app}"


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_pace_interval(interval: float, note: str) -> tuple[float, str]:
    """Ensure pacing is never slower than 1 request/sec."""
    if interval > MAX_PACE_INTERVAL_SEC:
        return (
            MAX_PACE_INTERVAL_SEC,
            f"{note}; capped→{MAX_PACE_INTERVAL_SEC:g}s (min 1req/s)",
        )
    return interval, note


def resolve_pace(
    *,
    daily_limit: int | None,
    min_interval_sec: float | None,
) -> tuple[int, float, str]:
    """Return ``(daily_limit, min_interval_sec, source_note)``.

    - ``daily_limit`` None/≤0 → interval defaults to 1s
      (or explicit ``min_interval_sec`` if set).
    - ``daily_limit`` >0 → interval = 86400/daily_limit. It is a pacing basis
      only and never stops local requests. Explicit interval is ignored.
    - Any resolved interval slower than 1 req/s is capped at 1.0s.
    """
    lim = int(daily_limit or 0)
    if lim <= 0:
        if min_interval_sec is not None and min_interval_sec > 0:
            interval, note = _clamp_pace_interval(
                float(min_interval_sec), "no_daily_limit+explicit_interval"
            )
            return 0, interval, note
        return 0, DEFAULT_INTERVAL_NO_DAILY_LIMIT, "no_daily_limit→1.0s"

    calculated = SECONDS_PER_DAY / float(lim)
    interval, note = _clamp_pace_interval(
        calculated, f"daily_limit→{SECONDS_PER_DAY:g}/{lim}"
    )
    if min_interval_sec is not None and min_interval_sec > 0:
        note = f"{note}; explicit_interval_ignored"
    return lim, interval, note


def load_app_from_redis(
    app_key: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Return app registry entry from Redis hash, or None.

    Keys: ``app_key``, ``app_secret``, ``label``, optional ``daily_limit``,
    ``min_interval_sec``.
    """
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
    out: dict[str, Any] = {"app_key": key, "app_secret": secret, "label": label}
    daily = _parse_optional_int(data.get("daily_limit"))
    if daily is not None:
        out["daily_limit"] = daily
    interval = _parse_optional_float(data.get("min_interval_sec"))
    if interval is not None:
        out["min_interval_sec"] = interval
    return out


def update_app_limits(
    app_key: str,
    *,
    daily_limit: int | None = None,
    min_interval_sec: float | None = None,
    clear_min_interval: bool = False,
    app_secret: str | None = None,
    label: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Merge rate-limit fields into Redis app registry (create if secret available).

    Pass ``daily_limit=0`` to clear the pacing basis in Redis.
    Pass ``clear_min_interval=True`` to remove explicit interval (use calculated).
    If the AppKey is missing, ``app_secret`` (or ``settings.aliexpress_app_secret`` /
    env) is required to create the hash field.
    """
    from aliexpress_ds.config import get_settings

    settings = settings or get_settings()
    url = (settings.redis_url or "").strip()
    key = (app_key or "").strip()
    if not url:
        raise ValueError("REDIS_URL missing")
    if not key or key.startswith("your_"):
        raise ValueError("app_key required")

    from aliexpress_ds.token_store import _redis_client

    client = _redis_client(url)
    raw = client.hget(REDIS_APPS_KEY, key)
    data: dict[str, Any]
    if raw:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON for AppKey {key}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Invalid registry value for AppKey {key}")
        data = parsed
    else:
        secret = (app_secret or settings.aliexpress_app_secret or "").strip()
        if not secret or secret.startswith("your_"):
            raise ValueError(
                f"AppKey {key} not in Redis {REDIS_APPS_KEY}; "
                "pass --app-secret or set ALIEXPRESS_APP_SECRET to create it"
            )
        data = {"app_secret": secret}
        if label:
            data["label"] = label

    if app_secret and not str(app_secret).startswith("your_"):
        data["app_secret"] = str(app_secret).strip()
    if label:
        data["label"] = label

    if daily_limit is not None:
        if daily_limit <= 0:
            data.pop("daily_limit", None)
        else:
            data["daily_limit"] = int(daily_limit)
    if clear_min_interval:
        data.pop("min_interval_sec", None)
    elif min_interval_sec is not None:
        if min_interval_sec <= 0:
            data.pop("min_interval_sec", None)
        else:
            data["min_interval_sec"] = float(min_interval_sec)

    if not str(data.get("app_secret") or data.get("secret") or "").strip():
        raise ValueError(f"AppKey {key} registry entry missing app_secret")

    client.hset(REDIS_APPS_KEY, key, json.dumps(data, ensure_ascii=False))
    clear_app_credential_cache()
    loaded = load_app_from_redis(key, settings=settings)
    if loaded:
        return loaded
    return {
        "app_key": key,
        **{
            k: data[k]
            for k in ("app_secret", "label", "daily_limit", "min_interval_sec")
            if k in data
        },
    }


def ensure_app_credentials(settings: Settings | None = None) -> Settings:
    """Fill secret + rate limits from Redis when present.

    ``ALIEXPRESS_APP_KEY`` in .env selects which Redis hash field to use.
    Pacing: Redis ``daily_limit`` preferred; missing → 1s.
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
    entry: dict[str, Any] | None = None
    with _cache_lock:
        hit = _entry_cache.get(ck)
        if hit is not None:
            cached_at, cached_entry = hit
            if now - cached_at <= _MEMORY_MAX_AGE and cached_entry.get("app_secret"):
                entry = cached_entry

    if entry is None:
        entry = load_app_from_redis(app_key, settings=settings)
        if entry and entry.get("app_secret"):
            with _cache_lock:
                _entry_cache[ck] = (now, entry)
            logger.debug(
                "Loaded app from Redis %s[%s] label=%s daily_limit=%s",
                REDIS_APPS_KEY,
                app_key,
                entry.get("label"),
                entry.get("daily_limit"),
            )

    if not entry or not entry.get("app_secret"):
        # Keep .env secret as fallback for apps not yet in the Redis registry.
        return settings

    settings.aliexpress_app_secret = str(entry["app_secret"])
    apply_redis_rate_limits(settings, entry)
    return settings


def apply_redis_rate_limits(settings: Settings, entry: dict[str, Any]) -> Settings:
    """Overwrite settings daily/interval from a Redis app entry."""
    daily_raw = entry.get("daily_limit")
    interval_raw = entry.get("min_interval_sec")
    daily, interval, note = resolve_pace(
        daily_limit=_parse_optional_int(daily_raw),
        min_interval_sec=_parse_optional_float(interval_raw),
    )
    settings.aliexpress_daily_limit = daily
    settings.aliexpress_min_interval_sec = interval
    logger.info(
        "AppKey %s rate limits from Redis: daily_limit=%s interval=%.4fs (%s)",
        entry.get("app_key") or settings.aliexpress_app_key,
        daily or "∞",
        interval,
        note,
    )
    return settings


def clear_app_credential_cache() -> None:
    with _cache_lock:
        _entry_cache.clear()
