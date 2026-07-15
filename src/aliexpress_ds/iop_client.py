from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from aliexpress_ds.config import Settings, get_settings


class IopError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, body: Any = None):
        super().__init__(message)
        self.code = code
        self.body = body


class IopClient:
    """AliExpress IOP REST client.

    Signature (official):
      HMAC_SHA256(key=app_secret, data=api_path + sorted(key+value))
    """

    def __init__(self, settings: Settings | None = None, *, timeout: float = 30.0):
        self.settings = settings or get_settings()
        self.timeout = timeout

    def execute(self, method: str, api_params: dict[str, Any] | None = None) -> dict[str, Any]:
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
