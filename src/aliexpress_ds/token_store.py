"""OAuth access_token from Upstash Redis, with automatic refresh."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from aliexpress_ds.config import Settings, get_settings

logger = logging.getLogger(__name__)

REDIS_TOKEN_KEY = "aliexpress:oauth:token"
# Refresh when access_token expires within this window (avoid mid-request failures).
REFRESH_SKEW = timedelta(hours=1)
# Re-check Redis at least this often even if local expiry is later (other process may refresh).
MEMORY_CACHE_MAX_AGE = timedelta(minutes=10)

_redis_clients: dict[str, Any] = {}
_redis_lock = threading.Lock()
# redis_url (or "__env__") → (cached_at_monotonic, token_dict)
_token_memory: dict[str, tuple[float, dict[str, Any]]] = {}
_token_memory_lock = threading.Lock()


def _redis_client(url: str):
    """Reuse one Redis connection per URL (avoids TLS handshake per API call)."""
    import redis

    with _redis_lock:
        client = _redis_clients.get(url)
        if client is not None:
            return client
        kwargs: dict[str, Any] = {
            "socket_timeout": 10,
            "decode_responses": True,
            "protocol": 2,
            "health_check_interval": 30,
        }
        if url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
        client = redis.from_url(url, **kwargs)
        _redis_clients[url] = client
        return client


def _cache_key(settings: Settings) -> str:
    url = (settings.redis_url or "").strip()
    return url or "__env__"


def _memory_get(settings: Settings) -> dict[str, Any] | None:
    key = _cache_key(settings)
    with _token_memory_lock:
        entry = _token_memory.get(key)
        if entry is None:
            return None
        cached_at, data = entry
    age = time.monotonic() - cached_at
    if age > MEMORY_CACHE_MAX_AGE.total_seconds():
        return None
    if access_token_expired(data):
        return None
    return data


def _memory_put(settings: Settings, data: dict[str, Any]) -> None:
    key = _cache_key(settings)
    with _token_memory_lock:
        _token_memory[key] = (time.monotonic(), dict(data))


def _memory_clear(settings: Settings | None = None) -> None:
    with _token_memory_lock:
        if settings is None:
            _token_memory.clear()
            return
        _token_memory.pop(_cache_key(settings), None)


def load_token_from_redis(settings: Settings | None = None) -> dict[str, Any] | None:
    """Load OAuth token JSON written by aliexpress-oauth / this package (Upstash)."""
    settings = settings or get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        return None

    client = _redis_client(url)
    raw = client.get(REDIS_TOKEN_KEY)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def save_token_to_redis(payload: dict[str, Any], settings: Settings | None = None) -> None:
    """Persist token JSON to Upstash (same key as aliexpress-oauth TokenStore)."""
    settings = settings or get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        raise ValueError("REDIS_URL is not set; cannot save refreshed token")

    body = dict(payload)
    body["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "id" not in body:
        body["id"] = "redis"

    ttl = _ttl_seconds(body)
    client = _redis_client(url)
    client.set(REDIS_TOKEN_KEY, json.dumps(body, ensure_ascii=False))
    if ttl > 0:
        client.expire(REDIS_TOKEN_KEY, ttl)
    _memory_put(settings, body)
    logger.info(
        "Saved token to Redis %s (expires_at=%s ttl=%ss)",
        REDIS_TOKEN_KEY,
        body.get("expires_at"),
        ttl,
    )


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ttl_seconds(payload: dict[str, Any]) -> int:
    refresh_at = _parse_time(payload.get("refresh_expires_at"))
    access_at = _parse_time(payload.get("expires_at"))
    deadline = max([t for t in (refresh_at, access_at) if t is not None], default=None)
    if deadline is None:
        return int(timedelta(days=30).total_seconds())
    return max(int((deadline - datetime.now(timezone.utc)).total_seconds()), 60)


def access_token_expired(data: dict[str, Any], *, skew: timedelta = REFRESH_SKEW) -> bool:
    expires_at = _parse_time(data.get("expires_at"))
    if expires_at is None:
        return False
    return expires_at <= datetime.now(timezone.utc) + skew


def refresh_token_expired(data: dict[str, Any]) -> bool:
    refresh_at = _parse_time(data.get("refresh_expires_at"))
    if refresh_at is None:
        return False
    return refresh_at <= datetime.now(timezone.utc)


def _sign(app_secret: str, api_name: str, params: dict[str, str]) -> str:
    p = {k: v for k, v in params.items() if k != "sign" and v is not None and str(v) != ""}
    basestring = f"{api_name}{''.join(f'{k}{p[k]}' for k in sorted(p))}"
    return (
        hmac.new(app_secret.encode("utf-8"), basestring.encode("utf-8"), hashlib.sha256)
        .hexdigest()
        .upper()
    )


def _auth_rest_base(settings: Settings) -> str:
    url = (settings.aliexpress_api_url or "").rstrip("/")
    if url.endswith("/sync"):
        return url[: -len("/sync")] + "/rest"
    if url.endswith("/rest"):
        return url
    return url + "/rest"


def refresh_access_token(
    refresh_token: str,
    *,
    settings: Settings | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call /auth/token/refresh and write the new token into REDIS_URL."""
    settings = settings or get_settings()
    settings.require_credentials()
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise ValueError("Missing refresh_token")

    api_name = "/auth/token/refresh"
    params: dict[str, str] = {
        "app_key": settings.aliexpress_app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "refresh_token": refresh_token,
    }
    params["sign"] = _sign(settings.aliexpress_app_secret, api_name, params)

    url = _auth_rest_base(settings) + api_name
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            url,
            content=urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        response.raise_for_status()
        body = response.json()

    if isinstance(body.get("error_response"), dict):
        err = body["error_response"]
        raise RuntimeError(err.get("msg") or err.get("sub_msg") or str(err))

    payload = body
    if isinstance(body.get("result"), dict) and body["result"].get("access_token"):
        payload = body["result"]
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise RuntimeError(f"Token refresh response missing access_token: {body}")

    now = datetime.now(timezone.utc)
    expires_in = int(payload.get("expires_in") or 0)
    refresh_expires_in = int(
        payload.get("refresh_expires_in") or payload.get("refresh_token_valid_time") or 0
    )
    # Some responses use absolute ms timestamp for refresh validity.
    raw_refresh_valid = payload.get("refresh_token_valid_time")
    refresh_expires_at: str | None = None
    if raw_refresh_valid is not None:
        try:
            n = int(raw_refresh_valid)
            if n > 1_000_000_000_000:
                refresh_expires_at = datetime.fromtimestamp(n / 1000.0, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
        except (TypeError, ValueError):
            pass
    if refresh_expires_at is None and refresh_expires_in > 0 and refresh_expires_in < 1_000_000_000:
        refresh_expires_at = (now + timedelta(seconds=refresh_expires_in)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    new_token: dict[str, Any] = {
        "id": (previous or {}).get("id") or "redis",
        "access_token": access,
        "refresh_token": payload.get("refresh_token")
        or (previous or {}).get("refresh_token")
        or refresh_token,
        "expires_at": (
            (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if expires_in > 0
            else (previous or {}).get("expires_at")
        ),
        "refresh_expires_at": refresh_expires_at or (previous or {}).get("refresh_expires_at"),
        "account": payload.get("account")
        or payload.get("user_nick")
        or (previous or {}).get("account"),
        "user_id": str(
            payload.get("user_id")
            or payload.get("seller_id")
            or payload.get("account_id")
            or (previous or {}).get("user_id")
            or ""
        )
        or None,
        "raw_response": body,
        "created_at": (previous or {}).get("created_at")
        or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_token_to_redis(new_token, settings)
    return new_token


def ensure_fresh_token(settings: Settings | None = None) -> dict[str, Any]:
    """Return usable token dict; refresh via API when access_token is near expiry.

    Uses an in-process memory cache so hot paths skip Upstash on every call.
    """
    settings = settings or get_settings()
    cached = _memory_get(settings)
    if cached is not None:
        return cached

    data = load_token_from_redis(settings)
    if not data or not str(data.get("access_token") or "").strip():
        raise ValueError(
            "No token in REDIS_URL. Authorize once via aliexpress-oauth "
            "(https://aliexpress-oauth.onrender.com/oauth/authorize)."
        )

    if not access_token_expired(data):
        _memory_put(settings, data)
        return data

    refresh = str(data.get("refresh_token") or "").strip()
    if not refresh:
        raise ValueError("access_token expired and no refresh_token in Redis — re-authorize")
    if refresh_token_expired(data):
        raise ValueError(
            "refresh_token expired — re-authorize at "
            "https://aliexpress-oauth.onrender.com/oauth/authorize"
        )

    logger.info("access_token expired/near-expiry; refreshing via /auth/token/refresh")
    return refresh_access_token(refresh, settings=settings, previous=data)


def resolve_access_token(settings: Settings | None = None) -> str:
    """Prefer Redis token (auto-refresh); fall back to ALIEXPRESS_ACCESS_TOKEN in .env."""
    settings = settings or get_settings()
    if (settings.redis_url or "").strip():
        try:
            data = ensure_fresh_token(settings)
            token = str(data.get("access_token") or "").strip()
            if token:
                logger.debug("Using access_token from Redis (%s)", REDIS_TOKEN_KEY)
                return token
        except ValueError as exc:
            logger.warning("Redis token unavailable: %s", exc)

    token = (settings.aliexpress_access_token or "").strip()
    if token and not token.startswith("your_"):
        return token
    raise ValueError(
        "No access_token: set REDIS_URL (Upstash) with a valid token, "
        "or ALIEXPRESS_ACCESS_TOKEN in .env. "
        "Authorize: https://aliexpress-oauth.onrender.com/oauth/authorize"
    )
