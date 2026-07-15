from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))


class RateLimiter:
    """AliExpress Open Platform call pacing.

    Official (Test / Formal Test Environment):
      - 5,000 API calls per app per day
      - Additional QPS / App Call Limited bans (ApiCallLimits)

    See:
      https://open.alitrip.com/docs/doc.htm?articleId=108105&docType=1
      https://developer.alibaba.com/docs/doc.htm?articleId=108869&docType=1
    """

    def __init__(
        self,
        *,
        min_interval_sec: float = 1.0,
        daily_limit: int = 5000,
        state_path: Path | None = None,
    ):
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.daily_limit = max(0, int(daily_limit))
        self.state_path = state_path or Path("data/rate_limit_state.json")
        self._lock = threading.Lock()
        self._last_call_at = 0.0
        self._load()

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

    def wait_turn(self) -> None:
        """Block until allowed to make the next API call. Raises if daily quota exhausted."""
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._count = 0

            if self.daily_limit > 0 and self._count >= self.daily_limit:
                raise RuntimeError(
                    f"Daily API quota exhausted ({self.daily_limit}/day Beijing time). "
                    "Test apps are limited to 5,000 calls/day per official docs. "
                    "Resume after 00:00 GMT+8, or release the app and raise quota in Console."
                )

            now = time.monotonic()
            wait = self.min_interval_sec - (now - self._last_call_at)
            if wait > 0:
                time.sleep(wait)

            self._last_call_at = time.monotonic()
            self._count += 1
            self._save()

    def ensure_min_count(self, count: int) -> None:
        """Raise today's counter if prior process already used calls (e.g. JSONL rows)."""
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._count = 0
            if count > self._count:
                self._count = count
                self._save()

    def penalize(self, seconds: float) -> None:
        """Extra cooldown after ApiCallLimits / App Call Limited."""
        seconds = max(0.0, float(seconds))
        if seconds <= 0:
            return
        with self._lock:
            time.sleep(seconds)
            self._last_call_at = time.monotonic()
