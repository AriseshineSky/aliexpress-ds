from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from aliexpress_ds.config import Settings, get_settings
from aliexpress_ds.rate_limit import (
    DailyQuotaExhausted,
    RateLimiter,
    is_transient_transport,
    parse_flow_control,
    retry_sleep_seconds,
)

logger = logging.getLogger(__name__)


class IopError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, body: Any = None):
        super().__init__(message)
        self.code = code
        self.body = body


class IopClient:
    """AliExpress IOP REST client with official flow-control retry.

    Signature (official):
      HMAC_SHA256(key=app_secret, data=api_path + sorted(key+value))

    Retries:
      - Flow control (code 7 / ApiCallLimits / limited-by-*): wait ban seconds, retry
      - App daily quota: do not retry; raise DailyQuotaExhausted
      - Transient HTTP/transport: exponential backoff

    Reuses sync ``httpx.Client`` and/or ``httpx.AsyncClient`` (keepalive).
    Prefer ``execute_async`` from asyncio workers that share one RateLimiter.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
        max_retries: int | None = None,
    ):
        self.settings = settings or get_settings()
        self.timeout = timeout
        self.limiter = limiter
        self.max_retries = (
            int(max_retries)
            if max_retries is not None
            else int(getattr(self.settings, "aliexpress_max_retries", 6) or 6)
        )
        self._http: httpx.Client | None = None
        self._http_async: httpx.AsyncClient | None = None
        self._http_lock = threading.Lock()

    def _http_client(self) -> httpx.Client:
        client = self._http
        if client is not None and not client.is_closed:
            return client
        with self._http_lock:
            client = self._http
            if client is None or client.is_closed:
                self._http = httpx.Client(
                    timeout=self.timeout,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                        keepalive_expiry=30.0,
                    ),
                )
            return self._http

    def _http_async_client(self) -> httpx.AsyncClient:
        client = self._http_async
        if client is not None and not client.is_closed:
            return client
        with self._http_lock:
            client = self._http_async
            if client is None or client.is_closed:
                self._http_async = httpx.AsyncClient(
                    timeout=self.timeout,
                    limits=httpx.Limits(
                        max_connections=40,
                        max_keepalive_connections=20,
                        keepalive_expiry=30.0,
                    ),
                )
            return self._http_async

    def close(self) -> None:
        with self._http_lock:
            if self._http is not None and not self._http.is_closed:
                self._http.close()
            self._http = None

    async def aclose(self) -> None:
        with self._http_lock:
            async_client = self._http_async
            self._http_async = None
            sync_client = self._http
            self._http = None
        if async_client is not None and not async_client.is_closed:
            await async_client.aclose()
        if sync_client is not None and not sync_client.is_closed:
            sync_client.close()

    def __enter__(self) -> IopClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> IopClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def execute(self, method: str, api_params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_exc: BaseException | None = None
        attempts = max(1, self.max_retries + 1)

        for attempt in range(attempts):
            if self.limiter is not None:
                self.limiter.wait_turn()

            try:
                payload = self._execute_once(method, api_params)
                if self.limiter is not None:
                    self.limiter.note_success()
                return payload
            except DailyQuotaExhausted:
                raise
            except IopError as exc:
                last_exc = exc
                fc = parse_flow_control(exc)
                if fc is None:
                    raise
                if fc.stop_for_day or fc.kind.value == "app_daily":
                    if self.limiter is not None:
                        self.limiter.apply_flow_control(fc)
                    raise DailyQuotaExhausted(
                        f"Platform AppKey daily quota hit ({fc.sub_code or fc.kind.value}). "
                        "Resume after 00:00 GMT+8."
                    ) from exc
                if attempt >= attempts - 1:
                    raise
                logger.warning(
                    "IOP flow-control on %s attempt=%s/%s: %s — cooling %.1fs",
                    method,
                    attempt + 1,
                    attempts,
                    fc.kind.value,
                    fc.cooldown_sec,
                )
                if self.limiter is not None:
                    self.limiter.apply_flow_control(fc)
                else:
                    time.sleep(fc.cooldown_sec)
                continue
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                last_exc = exc
                if not is_transient_transport(exc) or attempt >= attempts - 1:
                    raise IopError(f"HTTP/transport error: {exc}", code="transport") from exc
                delay = retry_sleep_seconds(attempt)
                logger.warning(
                    "IOP transport error on %s attempt=%s/%s: %s — backoff %.1fs",
                    method,
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
                continue

        assert last_exc is not None
        raise last_exc

    async def execute_async(
        self, method: str, api_params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        import asyncio

        last_exc: BaseException | None = None
        attempts = max(1, self.max_retries + 1)

        for attempt in range(attempts):
            if self.limiter is not None:
                await self.limiter.wait_turn_async()

            try:
                payload = await self._execute_once_async(method, api_params)
                if self.limiter is not None:
                    self.limiter.note_success()
                return payload
            except DailyQuotaExhausted:
                raise
            except IopError as exc:
                last_exc = exc
                fc = parse_flow_control(exc)
                if fc is None:
                    raise
                if fc.stop_for_day or fc.kind.value == "app_daily":
                    if self.limiter is not None:
                        await self.limiter.apply_flow_control_async(fc)
                    raise DailyQuotaExhausted(
                        f"Platform AppKey daily quota hit ({fc.sub_code or fc.kind.value}). "
                        "Resume after 00:00 GMT+8."
                    ) from exc
                if attempt >= attempts - 1:
                    raise
                logger.warning(
                    "IOP flow-control on %s attempt=%s/%s: %s — cooling %.1fs",
                    method,
                    attempt + 1,
                    attempts,
                    fc.kind.value,
                    fc.cooldown_sec,
                )
                if self.limiter is not None:
                    await self.limiter.apply_flow_control_async(fc)
                else:
                    await asyncio.sleep(fc.cooldown_sec)
                continue
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                last_exc = exc
                if not is_transient_transport(exc) or attempt >= attempts - 1:
                    raise IopError(f"HTTP/transport error: {exc}", code="transport") from exc
                delay = retry_sleep_seconds(attempt)
                logger.warning(
                    "IOP transport error on %s attempt=%s/%s: %s — backoff %.1fs",
                    method,
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

        assert last_exc is not None
        raise last_exc

    def _build_request(
        self, method: str, api_params: dict[str, Any] | None = None
    ) -> tuple[str, dict[str, str]]:
        settings = self.settings
        settings.require_credentials()

        from aliexpress_ds.token_store import resolve_access_token

        access_token = resolve_access_token(settings)

        api_name = method if method.startswith("/") else f"/{method}"
        if "." in method and not method.startswith("/"):
            api_name_for_sign = method
            use_path_style = False
        else:
            api_name_for_sign = api_name
            use_path_style = True

        params: dict[str, str] = {
            "app_key": settings.aliexpress_app_key,
            "timestamp": str(int(time.time() * 1000)),
            "sign_method": "sha256",
            "simplify": "true",
            "access_token": access_token,
            "session": access_token,
        }
        for key, value in (api_params or {}).items():
            if value is None:
                continue
            params[str(key)] = str(value)

        params["sign"] = self._sign(params, api_name_for_sign)

        if use_path_style:
            url = settings.aliexpress_api_url.rstrip("/")
            if url.endswith("/sync") and api_name.startswith("/auth/"):
                url = url[: -len("/sync")] + "/rest" + api_name
            elif url.endswith("/rest"):
                url = url + api_name
            else:
                url = url.rstrip("/") + api_name
            body = params
        else:
            url = settings.aliexpress_api_url
            body = dict(params)
            body["method"] = method
            body.pop("sign", None)
            body["sign"] = self._sign(body, api_name="")

        return url, body

    def _execute_once(
        self, method: str, api_params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url, body = self._build_request(method, api_params)
        response = self._http_client().post(
            url,
            content=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        response.raise_for_status()
        return self._unwrap(response.json(), method)

    async def _execute_once_async(
        self, method: str, api_params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url, body = self._build_request(method, api_params)
        response = await self._http_async_client().post(
            url,
            content=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        response.raise_for_status()
        return self._unwrap(response.json(), method)

    def _sign(self, params: dict[str, str], api_name: str) -> str:
        p = {k: v for k, v in params.items() if k != "sign" and v is not None and str(v) != ""}
        if "method" in p and "/" in str(p.get("method", "")):
            api_name = str(p.pop("method"))
        basestring = f"{api_name}{''.join(f'{k}{p[k]}' for k in sorted(p))}"
        return (
            hmac.new(
                self.settings.aliexpress_app_secret.encode("utf-8"),
                basestring.encode("utf-8"),
                hashlib.sha256,
            )
            .hexdigest()
            .upper()
        )

    def _unwrap(self, body: dict[str, Any], method: str) -> dict[str, Any]:
        if "error_response" in body:
            err = body["error_response"]
            raise IopError(
                err.get("msg") or err.get("sub_msg") or "AliExpress error",
                code=str(err.get("code") or err.get("sub_code") or ""),
                body=body,
            )

        response_key = f"{method.replace('.', '_')}_response"
        payload = body.get(response_key, body)

        code = str(payload.get("code") or payload.get("rsp_code") or "0")
        if code not in {"0", "200", ""}:
            raise IopError(
                payload.get("message") or payload.get("rsp_msg") or "API business error",
                code=code,
                body=body,
            )
        return payload if isinstance(payload, dict) else {"result": payload}

    @staticmethod
    def dump(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)
