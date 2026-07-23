"""Redis product_id task queue (separate from OAuth token Redis)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

from aliexpress_ds.config import Settings, get_settings

log = logging.getLogger(__name__)

# Cap wait between Redis reconnect retries (seconds).
_REDIS_RETRY_MAX_DELAY = 60.0


def _redis_client_kwargs(url: str) -> dict[str, Any]:
    """Shared redis-py kwargs. protocol=2 for Redis <6 (no HELLO).

    socket_timeout=None so BRPOP's server-side wait is not cut short by the
    client socket deadline (which previously surfaced as TimeoutError and
    crashed queue-worker under systemd Restart=always).
    """
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "protocol": 2,
        "socket_timeout": None,
        "socket_connect_timeout": 10,
        "retry_on_timeout": True,
        "health_check_interval": 30,
    }
    if url.startswith("rediss://"):
        import ssl

        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
    return kwargs


def _redis_client(url: str):
    try:
        import redis as redis_lib
    except ImportError as exc:
        raise RuntimeError("redis package missing; run: uv add redis") from exc

    return redis_lib.from_url(url, **_redis_client_kwargs(url))


def _is_redis_transient(exc: BaseException) -> bool:
    """True for timeouts / disconnects that should wait-and-retry."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError
    except ImportError:
        return False
    return isinstance(exc, (RedisTimeoutError, RedisConnectionError))


def _log_redis_retry(op: str, exc: BaseException, delay: float) -> None:
    msg = f"Redis {op} failed ({type(exc).__name__}: {exc}); retry in {delay:.0f}s"
    log.warning(msg)
    print(msg, file=sys.stderr, flush=True)


class ProductQueue:
    """LPUSH / BRPOP product jobs + optional seen-set for dedupe."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        url = (self.settings.redis_queue_url or "").strip()
        if not url:
            raise ValueError("REDIS_QUEUE_URL is not set")
        self._url = url
        self.client = _redis_client(url)
        self.queue_key = self.settings.redis_queue_key
        self.seen_key = self.settings.redis_queue_seen_key
        self.brpop_timeout = max(1, int(self.settings.redis_queue_brpop_timeout))

    def _reconnect(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        self.client = _redis_client(self._url)

    def ping(self) -> str:
        return str(self.client.ping())

    def length(self) -> int:
        delay = 1.0
        while True:
            try:
                return int(self.client.llen(self.queue_key))
            except Exception as exc:
                if not _is_redis_transient(exc):
                    raise
                _log_redis_retry("LLEN", exc, delay)
                self._reconnect()
                time.sleep(delay)
                delay = min(_REDIS_RETRY_MAX_DELAY, delay * 2)

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

    def clear(self, *, clear_seen: bool = True) -> dict[str, int]:
        """Delete pending queue list and optionally the seen set. Returns sizes removed."""
        qlen = int(self.client.llen(self.queue_key) or 0)
        seen = int(self.client.scard(self.seen_key) or 0)
        self.client.delete(self.queue_key)
        if clear_seen:
            self.client.delete(self.seen_key)
        return {"queue_removed": qlen, "seen_removed": seen if clear_seen else 0}

    def enqueue_many(
        self,
        items: list[dict[str, Any]],
        *,
        force: bool = False,
        batch_size: int = 1000,
        priority: bool = False,
    ) -> tuple[int, int]:
        """Enqueue list of {product_id, url?, source?}. Returns (new, skipped).

        Uses Redis pipelines so mass re-queue (100k+) finishes in minutes.

        priority=True uses RPUSH so BRPOP picks these jobs before older LPUSH
        backlog (worker uses BRPOP = pop from right).
        """
        new = 0
        skipped = 0
        batch_size = max(100, int(batch_size))
        buf: list[tuple[str, str]] = []  # (pid, payload)
        push_cmd = "rpush" if priority else "lpush"

        def _flush(chunk: list[tuple[str, str]]) -> None:
            nonlocal new, skipped
            if not chunk:
                return
            pids = [pid for pid, _ in chunk]
            if force:
                pipe = self.client.pipeline(transaction=False)
                for pid in pids:
                    pipe.sadd(self.seen_key, pid)
                pipe.execute()
                pipe = self.client.pipeline(transaction=False)
                for _pid, payload in chunk:
                    getattr(pipe, push_cmd)(self.queue_key, payload)
                pipe.execute()
                new += len(chunk)
                return

            # Non-force: only enqueue ids newly added to seen
            pipe = self.client.pipeline(transaction=False)
            for pid in pids:
                pipe.sadd(self.seen_key, pid)
            sadd_flags = pipe.execute()
            pipe = self.client.pipeline(transaction=False)
            pushed = 0
            for (pid, payload), flag in zip(chunk, sadd_flags, strict=True):
                if int(flag or 0) == 1:
                    getattr(pipe, push_cmd)(self.queue_key, payload)
                    pushed += 1
                else:
                    skipped += 1
            if pushed:
                pipe.execute()
                new += pushed
            else:
                # avoid executing empty pipeline
                pass

        for item in items:
            pid = str(item.get("product_id") or "").strip()
            if not pid:
                skipped += 1
                continue
            extra = {
                k: v
                for k, v in item.items()
                if k not in ("product_id", "url", "source") and v is not None
            } or None
            payload = self._encode_job(
                pid,
                url=item.get("url"),
                source=item.get("source"),
                extra=extra,
            )
            buf.append((pid, payload))
            if len(buf) >= batch_size:
                _flush(buf)
                buf = []
        _flush(buf)
        return new, skipped

    def blocking_pop(self, timeout: int | None = None) -> dict[str, Any] | None:
        """BRPOP one job. Returns decoded job dict or None on empty-queue timeout.

        Transient Redis timeouts/disconnects wait-and-retry (reconnect) instead
        of raising — queue-worker must stay up under flaky network.
        """
        to = self.brpop_timeout if timeout is None else max(1, int(timeout))
        delay = 1.0
        while True:
            try:
                result = self.client.brpop(self.queue_key, timeout=to)
                if not result:
                    return None
                _key, raw = result
                return self.decode_job(raw)
            except Exception as exc:
                if not _is_redis_transient(exc):
                    raise
                _log_redis_retry("BRPOP", exc, delay)
                self._reconnect()
                time.sleep(delay)
                delay = min(_REDIS_RETRY_MAX_DELAY, delay * 2)

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
        self._url = url
        self._kwargs = _redis_client_kwargs(url)
        self._redis_async = redis_async
        self.client = redis_async.from_url(url, **self._kwargs)
        self.queue_key = self.settings.redis_queue_key
        self.seen_key = self.settings.redis_queue_seen_key
        self.brpop_timeout = max(1, int(self.settings.redis_queue_brpop_timeout))

    async def _reconnect(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass
        self.client = self._redis_async.from_url(self._url, **self._kwargs)

    async def length(self) -> int:
        delay = 1.0
        while True:
            try:
                return int(await self.client.llen(self.queue_key))
            except Exception as exc:
                if not _is_redis_transient(exc):
                    raise
                _log_redis_retry("LLEN", exc, delay)
                await self._reconnect()
                await asyncio.sleep(delay)
                delay = min(_REDIS_RETRY_MAX_DELAY, delay * 2)

    async def blocking_pop(self, timeout: int | None = None) -> dict[str, Any] | None:
        """BRPOP one job. Returns decoded job or None on empty-queue timeout.

        Transient Redis timeouts/disconnects wait-and-retry (reconnect) instead
        of raising — keeps systemd queue-worker from crash-looping.
        """
        to = self.brpop_timeout if timeout is None else max(1, int(timeout))
        delay = 1.0
        while True:
            try:
                result = await self.client.brpop(self.queue_key, timeout=to)
                if not result:
                    return None
                _key, raw = result
                return ProductQueue.decode_job(raw)
            except Exception as exc:
                if not _is_redis_transient(exc):
                    raise
                _log_redis_retry("BRPOP", exc, delay)
                await self._reconnect()
                await asyncio.sleep(delay)
                delay = min(_REDIS_RETRY_MAX_DELAY, delay * 2)

    async def requeue(self, job: dict[str, Any]) -> None:
        pid = str(job.get("product_id") or "").strip()
        if not pid:
            return
        delay = 1.0
        payload = ProductQueue._encode_job(
            pid,
            url=job.get("url"),
            source=job.get("source"),
            extra={
                k: v
                for k, v in job.items()
                if k not in ("product_id", "url", "source") and v is not None
            }
            or None,
        )
        while True:
            try:
                await self.client.lpush(self.queue_key, payload)
                return
            except Exception as exc:
                if not _is_redis_transient(exc):
                    raise
                _log_redis_retry("LPUSH", exc, delay)
                await self._reconnect()
                await asyncio.sleep(delay)
                delay = min(_REDIS_RETRY_MAX_DELAY, delay * 2)

    async def aclose(self) -> None:
        await self.client.aclose()


def get_async_product_queue(settings: Settings | None = None) -> AsyncProductQueue:
    return AsyncProductQueue(settings)
