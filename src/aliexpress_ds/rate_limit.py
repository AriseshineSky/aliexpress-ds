"""AliExpress Open Platform call pacing + flow-control parsing.

Use AliExpress Open Service docs/console (not Taobao TOP 流量包 FAQ):

- Console: https://openservice.aliexpress.com/app/index.htm
- Docs: https://openservice.aliexpress.com/doc/doc.htm (DropShippers API Developer)
- DS product.get current limiting (community):
  https://openservice.aliexpress.com/dada/community/index.htm?#/article-detail/1901

Limit kinds we handle from API **error** bodies (success responses have no quota fields):

1. Per AppKey daily quota (Beijing GMT+8) — match Console package; local
   ``ALIEXPRESS_DAILY_LIMIT`` is client-side only.
   Sub-code: ``accesscontrol.limited-by-app-access-count``

2. Per-API QPS / minute caps. Often includes
   ``This ban will last for N more seconds`` / ``ApiCallLimits``.
   Sub-code: ``accesscontrol.limited-by-api-access-count``

3. Per AppKey+API frequency — common in practice as ``AppApiCallLimit``
   (``The frequency of app access to the api exceeds the limit``).
   Sub-code: ``accesscontrol.limited-by-app-api-access-count``

Wait the ban window from the error message; do not hammer while banned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8))

# Conservative defaults: Online apps rarely publish a fixed QPS for DS APIs.
# Proactive pacing stays under typical soft caps; response-driven backoff handles the rest.
DEFAULT_MIN_INTERVAL_SEC = 1.0  # ~1 QPS; safer against AppApiCallLimit
DEFAULT_DAILY_LIMIT = 5000  # Formal Test Environment
MAX_ADAPTIVE_INTERVAL_SEC = 8.0
MIN_ADAPTIVE_INTERVAL_SEC = 0.2


class FlowKind(str, Enum):
    APP_DAILY = "app_daily"  # stop until next Beijing day
    API_QPS = "api_qps"  # wait ban seconds
    APP_API = "app_api"  # wait ban seconds (often unreleased apps)
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FlowControl:
    kind: FlowKind
    cooldown_sec: float
    stop_for_day: bool = False
    sub_code: str = ""
    message: str = ""


class DailyQuotaExhausted(RuntimeError):
    """Local or platform daily AppKey quota exhausted (Beijing day)."""


def seconds_until_beijing_midnight() -> float:
    now = datetime.now(BEIJING)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def _exc_blob(exc: BaseException) -> str:
    parts = [str(exc)]
    code = getattr(exc, "code", None)
    if code is not None:
        parts.append(str(code))
    body = getattr(exc, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(body))
    return "\n".join(parts).lower()


def parse_ban_seconds(blob: str, *, default: float = 1.0) -> float:
    """Extract ban window from official messages; default 1s (ApiCallLimits FAQ)."""
    patterns = (
        r"last(?:s)?\s+for\s+(\d+)\s+more\s+seconds",
        r"ban will last\s+(\d+)\s+seconds",
        r"last\s+(\d+)\s+seconds",
        r"retry after\s+(\d+)",
        r"wait\s+(\d+)\s+seconds",
    )
    for pat in patterns:
        m = re.search(pat, blob, flags=re.I)
        if m:
            return float(m.group(1)) + 1.0  # +1s safety margin
    return max(default, 1.0)


def parse_flow_control(exc: BaseException) -> FlowControl | None:
    """Return FlowControl if ``exc`` is an Open Platform rate/flow limit error."""
    blob = _exc_blob(exc)
    code = str(getattr(exc, "code", "") or "").strip()
    code_l = code.lower()
    msg = str(exc)[:500]

    markers = (
        "apicalllimits",
        "appapicalllimit",
        "app call limited",
        "call limited",
        "accesscontrol.limited",
        "limited-by-app",
        "limited-by-api",
        "ban will last",
        "flowlimit",
        "frequency of app access",
        "api access frequency exceeds",
        "frequency",
    )
    code_hit = code_l in {
        "7",
        "apicalllimits",
        "app call limited",
        "appapicalllimit",
    }
    if not code_hit and not any(m in blob for m in markers):
        return None

    ban = parse_ban_seconds(blob, default=1.0)

    if "limited-by-app-access-count" in blob:
        return FlowControl(
            FlowKind.APP_DAILY,
            cooldown_sec=seconds_until_beijing_midnight(),
            stop_for_day=True,
            sub_code="accesscontrol.limited-by-app-access-count",
            message=msg,
        )
    # App+API frequency (common Online): code AppApiCallLimit
    # msg e.g. "The frequency of app access to the api exceeds the limit.
    # This ban will last N seconds"
    if (
        code_l == "appapicalllimit"
        or "limited-by-app-api-access-count" in blob
        or "frequency of app access to the api" in blob
    ):
        return FlowControl(
            FlowKind.APP_API,
            cooldown_sec=max(ban, 1.0),
            sub_code=code or "AppApiCallLimit",
            message=msg,
        )
    if "limited-by-api-access-count" in blob or "apicalllimits" in blob:
        return FlowControl(
            FlowKind.API_QPS,
            cooldown_sec=max(ban, 1.0),
            sub_code=code or "accesscontrol.limited-by-api-access-count",
            message=msg,
        )
    if "app call limited" in blob or code == "7":
        return FlowControl(
            FlowKind.UNKNOWN,
            cooldown_sec=max(ban, 60.0 if ban <= 1.0 else ban),
            sub_code=code or "7",
            message=msg,
        )
    return FlowControl(
        FlowKind.UNKNOWN, cooldown_sec=max(ban, 1.0), sub_code=code, message=msg
    )


class RateLimiter:
    """Proactive pacing + adaptive slowdown + global ban cooldown.

    Sync and async callers share the same counters. Sleeps never hold the lock,
    so concurrent asyncio tasks can pipeline while still pacing starts.
    """

    def __init__(
        self,
        *,
        min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        state_path: Path | None = None,
    ):
        self.base_interval_sec = max(0.0, float(min_interval_sec))
        self.min_interval_sec = self.base_interval_sec
        self._effective_interval = self.base_interval_sec
        self.daily_limit = max(0, int(daily_limit))
        self.state_path = state_path or Path("data/rate_limit_state.json")
        self.platform_state_path = self.state_path.parent / "platform_rate_limit.json"
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None
        self._last_call_at = 0.0
        self._cooldown_until = 0.0  # monotonic
        self._load()

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def _today(self) -> str:
        return datetime.now(BEIJING).strftime("%Y-%m-%d")

    def _load(self) -> None:
        self._day = self._today()
        self._count = 0
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if data.get("day") == self._day:
                    self._count = int(data.get("count") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"day": self._day, "count": self._count}, ensure_ascii=False),
            encoding="utf-8",
        )

    @property
    def remaining_today(self) -> int | None:
        if self.daily_limit <= 0:
            return None
        return max(0, self.daily_limit - self._count)

    @property
    def effective_interval(self) -> float:
        return self._effective_interval

    def _prepare_turn_unlocked(self) -> float:
        """Under lock: roll day / check quota. Return seconds to sleep, or 0 to claim.

        Caller must claim with ``_claim_turn_unlocked`` when return value is 0.
        """
        today = self._today()
        if today != self._day:
            self._day = today
            self._count = 0
            self._effective_interval = self.base_interval_sec

        if self.daily_limit > 0 and self._count >= self.daily_limit:
            raise DailyQuotaExhausted(
                f"Local daily API quota exhausted ({self.daily_limit}/day Beijing time). "
                "Resume after 00:00 GMT+8, or raise ALIEXPRESS_DAILY_LIMIT to match Console."
            )

        now = time.monotonic()
        cool_wait = self._cooldown_until - now
        gap_wait = self._effective_interval - (now - self._last_call_at)
        return max(0.0, cool_wait, gap_wait)

    def _claim_turn_unlocked(self) -> None:
        self._last_call_at = time.monotonic()
        self._count += 1
        self._save()

    def wait_turn(self) -> None:
        """Block until allowed to make the next API call."""
        while True:
            with self._lock:
                sleep_for = self._prepare_turn_unlocked()
                if sleep_for <= 0:
                    self._claim_turn_unlocked()
                    return
            time.sleep(sleep_for)

    async def wait_turn_async(self) -> None:
        """Async variant — shared pacing across concurrent product tasks."""
        while True:
            async with self._get_async_lock():
                # Also take threading lock so sync+async never interleave counters.
                with self._lock:
                    sleep_for = self._prepare_turn_unlocked()
                    if sleep_for <= 0:
                        self._claim_turn_unlocked()
                        return
            await asyncio.sleep(sleep_for)

    def ensure_min_count(self, count: int) -> None:
        """Raise today's counter if prior process already used calls."""
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._count = 0
            if count > self._count:
                self._count = count
                self._save()

    def note_success(self) -> None:
        """Gradually recover toward base interval after healthy calls."""
        with self._lock:
            if self._effective_interval <= self.base_interval_sec:
                self._effective_interval = self.base_interval_sec
                return
            recovered = self._effective_interval * 0.9
            self._effective_interval = max(self.base_interval_sec, recovered)

    def apply_flow_control(self, fc: FlowControl) -> None:
        """Sleep for ban window and tighten pacing (official response-driven backoff)."""
        cool = self._arm_flow_control(fc)
        if cool > 0:
            time.sleep(cool)
            with self._lock:
                self._last_call_at = time.monotonic()

    async def apply_flow_control_async(self, fc: FlowControl) -> None:
        cool = self._arm_flow_control(fc)
        if cool > 0:
            await asyncio.sleep(cool)
            async with self._get_async_lock():
                with self._lock:
                    self._last_call_at = time.monotonic()

    def _save_platform_signal(self, fc: FlowControl, cool: float) -> None:
        """Persist last platform-reported limit (only present on error responses)."""
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "beijing_day": datetime.now(BEIJING).strftime("%Y-%m-%d"),
            "kind": fc.kind.value,
            "sub_code": fc.sub_code or "",
            "ban_seconds": cool,
            "stop_for_day": fc.stop_for_day,
            "message": (fc.message or "")[:500],
            "note": (
                "Successful AE API responses do not include remaining daily quota. "
                "This file only records the last flow-control error body."
            ),
        }
        try:
            self.platform_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.platform_state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Could not write platform rate signal: %s", exc)

    def _arm_flow_control(self, fc: FlowControl) -> float:
        cool = max(0.0, float(fc.cooldown_sec))
        with self._lock:
            if fc.stop_for_day or fc.kind is FlowKind.APP_DAILY:
                cool = max(cool, seconds_until_beijing_midnight())
            until = time.monotonic() + cool
            if until > self._cooldown_until:
                self._cooldown_until = until
            bumped = max(self._effective_interval * 1.5, self.base_interval_sec * 2.0, 1.0)
            self._effective_interval = min(MAX_ADAPTIVE_INTERVAL_SEC, bumped)
            # Hard App+API bans (minutes+) mean proactive pacing was too aggressive —
            # raise the floor so we do not re-trigger immediately after the cool-down.
            if cool >= 60.0 and fc.kind in {FlowKind.APP_API, FlowKind.API_QPS, FlowKind.UNKNOWN}:
                new_base = min(
                    MAX_ADAPTIVE_INTERVAL_SEC,
                    max(self.base_interval_sec * 1.5, self.base_interval_sec + 0.1, 1.0),
                )
                if new_base > self.base_interval_sec:
                    logger.warning(
                        "Hard ban %.0fs — raising base interval %.2fs→%.2fs for this process",
                        cool,
                        self.base_interval_sec,
                        new_base,
                    )
                    self.base_interval_sec = new_base
                    self.min_interval_sec = new_base
                    self._effective_interval = max(self._effective_interval, new_base)
            self._save_platform_signal(fc, cool)
            logger.warning(
                "Flow control kind=%s sub=%s sleep=%.1fs interval→%.2fs base=%.2fs",
                fc.kind.value,
                fc.sub_code or "-",
                cool,
                self._effective_interval,
                self.base_interval_sec,
            )
        return cool

    def penalize(self, seconds: float) -> None:
        """Backward-compatible cooldown helper."""
        self.apply_flow_control(
            FlowControl(FlowKind.UNKNOWN, cooldown_sec=max(0.0, float(seconds)))
        )


def is_transient_transport(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return True
    if "connect" in name or "transport" in name:
        return True
    markers = ("temporarily unavailable", "connection reset", "broken pipe", "503", "502", "504")
    return any(m in msg for m in markers)


def retry_sleep_seconds(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff for transient transport errors: 1, 2, 4, …"""
    return min(cap, base * (2**max(0, attempt)))


def describe_limits(*, daily_limit: int, min_interval: float) -> dict[str, Any]:
    return {
        "daily_limit": daily_limit or "unlimited(local)",
        "min_interval_sec": min_interval,
        "approx_qps": (1.0 / min_interval) if min_interval > 0 else "unlimited",
        "beijing_day": datetime.now(BEIJING).strftime("%Y-%m-%d"),
        "docs": {
            # AliExpress Open Service (not Taobao TOP 管理证书 FAQ)
            "console": "https://openservice.aliexpress.com/app/index.htm",
            "docs_home": "https://openservice.aliexpress.com/doc/doc.htm",
            "ds_product_get_current_limiting": (
                "https://openservice.aliexpress.com/dada/community/index.htm?#/article-detail/1901"
            ),
            "support": "https://openservice.aliexpress.com/support/index.htm",
            # Historical AE English note (hosted on alitrip domain)
            "access_count": "https://open.alitrip.com/docs/doc.htm?articleId=108426&docType=1",
        },
    }
