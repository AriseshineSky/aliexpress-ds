# frozen_string_literal equivalent
from __future__ import annotations

import json
import logging
from typing import Any

from aliexpress_ds.config import Settings, get_settings

logger = logging.getLogger(__name__)

REDIS_TOKEN_KEY = "aliexpress:oauth:token"


def load_token_from_redis(settings: Settings | None = None) -> dict[str, Any] | None:
    """Load OAuth token JSON written by aliexpress-oauth (Upstash)."""
    settings = settings or get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        return None

    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("redis package missing; run: uv add redis") from exc

    kwargs: dict[str, Any] = {"socket_timeout": 5}
    if url.startswith("rediss://"):
        import ssl

        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE

    client = redis.from_url(url, **kwargs)
    raw = client.get(REDIS_TOKEN_KEY)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def resolve_access_token(settings: Settings | None = None) -> str:
    """Prefer Redis token; fall back to ALIEXPRESS_ACCESS_TOKEN in .env."""
    settings = settings or get_settings()
    from_redis = load_token_from_redis(settings)
    if from_redis:
        token = str(from_redis.get("access_token") or "").strip()
        if token:
            logger.info("Using access_token from Redis (%s)", REDIS_TOKEN_KEY)
            return token

    token = (settings.aliexpress_access_token or "").strip()
    if token and not token.startswith("your_"):
        return token
    raise ValueError(
        "No access_token: set REDIS_URL (Upstash) or ALIEXPRESS_ACCESS_TOKEN in .env"
    )
