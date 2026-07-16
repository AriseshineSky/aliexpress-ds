from __future__ import annotations

import hashlib
import hmac
import json
import logging
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

    def _execute_once(
        self, method: str, api_params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        settings = self.settings
        settings.require_credentials()

        from aliexpress_ds.token_store import resolve_access_token

        access_token = resolve_access_token(settings)

        api_name = method if method.startswith("/") else f"/{method}"
        # Business APIs like aliexpress.ds.product.get stay without leading slash in some gateways;
        # for path-style (/auth/...) keep leading slash in the signature string.
        if "." in method and not method.startswith("/"):
            api_name_for_sign = method  # TOP-style method name in signature for sync gateway
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
            # Prefer /rest for auth; product calls usually use /sync with method param.
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
            # re-sign including method for TOP, or path prepend without method in payload
            # Official OP path APIs put method in URL; TOP puts method in body and
            # signature basestring is sorted params WITHOUT prepending (TOP MD5 style).
            # For DS on /sync with dotted method, AE SDK prepends nothing if method
            # has no "/" — uses HMAC of sorted params only (ae_sdk logic).
            body.pop("sign", None)
            body["sign"] = self._sign(body, api_name="")

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                url,
                content=urlencode(body),
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
            response.raise_for_status()
            payload = response.json()

        return self._unwrap(payload, method)

    def _sign(self, params: dict[str, str], api_name: str) -> str:
        p = {k: v for k, v in params.items() if k != "sign" and v is not None and str(v) != ""}
        if "method" in p and "/" in str(p.get("method", "")):
            # OP style already in method field — prepend and exclude from pairs
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
