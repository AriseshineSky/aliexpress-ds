"""Redis product_id task queue (separate from OAuth token Redis)."""

from __future__ import annotations

import json
from typing import Any

from aliexpress_ds.config import Settings, get_settings


def _redis_client(url: str):
    try:
        import redis as redis_lib
    except ImportError as exc:
        raise RuntimeError("redis package missing; run: uv add redis") from exc

    kwargs: dict[str, Any] = {"decode_responses": True, "protocol": 2}
    if url.startswith("rediss://"):
        import ssl

        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
    return redis_lib.from_url(url, **kwargs)


class ProductQueue:
    """LPUSH / BRPOP product jobs + optional seen-set for dedupe."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        url = (self.settings.redis_queue_url or "").strip()
        if not url:
            raise ValueError("REDIS_QUEUE_URL is not set")
        self.client = _redis_client(url)
        self.queue_key = self.settings.redis_queue_key
        self.seen_key = self.settings.redis_queue_seen_key
        self.brpop_timeout = max(1, int(self.settings.redis_queue_brpop_timeout))

    def ping(self) -> str:
        return str(self.client.ping())

    def length(self) -> int:
        return int(self.client.llen(self.queue_key))

    def seen_count(self) -> int:
        return int(self.client.scard(self.seen_key))

    def is_seen(self, product_id: str) -> bool:
        return bool(self.client.sismember(self.seen_key, product_id))

    def mark_seen(self, product_id: str) -> None:
        self.client.sadd(self.seen_key, product_id)

    def enqueue(
        self,
        product_id: str,
        *,
        url: str | None = None,
        source: str | None = None,
        force: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Enqueue one job. Returns True if newly queued (or force-requeued)."""
        pid = str(product_id or "").strip()
        if not pid:
            return False
        if not force and self.client.sadd(self.seen_key, pid) != 1:
            return False
        if force:
            self.client.sadd(self.seen_key, pid)
            # Drop any stale copies so force puts a fresh job at the head.
            self.client.lrem(self.queue_key, 0, self._encode_legacy_pid(pid))

        payload = self._encode_job(pid, url=url, source=source, extra=extra)
        if force:
            # Remove matching JSON payloads that contain this product_id (best-effort).
            # Cheap path: only rem exact payload string we are about to push.
            self.client.lrem(self.queue_key, 0, payload)
        self.client.lpush(self.queue_key, payload)
        return True

    def enqueue_many(
        self,
        items: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> tuple[int, int]:
        """Enqueue list of {product_id, url?, source?}. Returns (new, skipped)."""
        new = 0
        skipped = 0
        for item in items:
            pid = str(item.get("product_id") or "").strip()
            if not pid:
                skipped += 1
                continue
            ok = self.enqueue(
                pid,
                url=item.get("url"),
                source=item.get("source"),
                force=force,
                extra={
                    k: v
                    for k, v in item.items()
                    if k not in ("product_id", "url", "source") and v is not None
                }
                or None,
            )
            if ok:
                new += 1
            else:
                skipped += 1
        return new, skipped

    def blocking_pop(self, timeout: int | None = None) -> dict[str, Any] | None:
        """BRPOP one job. Returns decoded job dict or None on timeout."""
        to = self.brpop_timeout if timeout is None else max(1, int(timeout))
        result = self.client.brpop(self.queue_key, timeout=to)
        if not result:
            return None
        _key, raw = result
        return self.decode_job(raw)

    def requeue(self, job: dict[str, Any]) -> None:
        pid = str(job.get("product_id") or "").strip()
        if not pid:
            return
        self.client.lpush(
            self.queue_key,
            self._encode_job(
                pid,
                url=job.get("url"),
                source=job.get("source"),
                extra={
                    k: v
                    for k, v in job.items()
                    if k not in ("product_id", "url", "source") and v is not None
                }
                or None,
            ),
        )

    @staticmethod
    def _encode_legacy_pid(product_id: str) -> str:
        return product_id

    @staticmethod
    def _encode_job(
        product_id: str,
        *,
        url: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"product_id": product_id}
        if url:
            body["url"] = url
        if source:
            body["source"] = source
        if extra:
            body.update(extra)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def decode_job(raw: str | bytes) -> dict[str, Any]:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if not text:
            return {"product_id": ""}
        if text[0] == "{":
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return {"product_id": text}
            if isinstance(obj, dict):
                pid = str(obj.get("product_id") or "").strip()
                if not pid and obj.get("url"):
                    from aliexpress_ds.url_parser import extract_product_id

                    try:
                        pid = extract_product_id(str(obj["url"]))
                    except ValueError:
                        pid = ""
                obj["product_id"] = pid
                return obj
        # Plain product_id string
        return {"product_id": text}


def get_product_queue(settings: Settings | None = None) -> ProductQueue:
    return ProductQueue(settings)


class AsyncProductQueue:
    """redis.asyncio BRPOP/LPUSH — no thread pool for queue waits."""

    def __init__(self, settings: Settings | None = None):
        import redis.asyncio as redis_async

        self.settings = settings or get_settings()
        url = (self.settings.redis_queue_url or "").strip()
        if not url:
            raise ValueError("REDIS_QUEUE_URL is not set")
        kwargs: dict[str, Any] = {"decode_responses": True, "protocol": 2}
        if url.startswith("rediss://"):
            import ssl

            kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
        self.client = redis_async.from_url(url, **kwargs)
        self.queue_key = self.settings.redis_queue_key
        self.seen_key = self.settings.redis_queue_seen_key
        self.brpop_timeout = max(1, int(self.settings.redis_queue_brpop_timeout))

    async def length(self) -> int:
        return int(await self.client.llen(self.queue_key))

    async def blocking_pop(self, timeout: int | None = None) -> dict[str, Any] | None:
        to = self.brpop_timeout if timeout is None else max(1, int(timeout))
        result = await self.client.brpop(self.queue_key, timeout=to)
        if not result:
            return None
        _key, raw = result
        return ProductQueue.decode_job(raw)

    async def requeue(self, job: dict[str, Any]) -> None:
        pid = str(job.get("product_id") or "").strip()
        if not pid:
            return
        await self.client.lpush(
            self.queue_key,
            ProductQueue._encode_job(
                pid,
                url=job.get("url"),
                source=job.get("source"),
                extra={
                    k: v
                    for k, v in job.items()
                    if k not in ("product_id", "url", "source") and v is not None
                }
                or None,
            ),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def get_async_product_queue(settings: Settings | None = None) -> AsyncProductQueue:
    return AsyncProductQueue(settings)
