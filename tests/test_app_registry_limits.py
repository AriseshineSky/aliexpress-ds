"""Unit tests for Redis app rate-limit resolution."""

from __future__ import annotations

from aliexpress_ds.app_registry import (
    DEFAULT_INTERVAL_NO_DAILY_LIMIT,
    MAX_PACE_INTERVAL_SEC,
    SECONDS_PER_DAY,
    resolve_pace,
)


def test_resolve_pace_no_daily_defaults_to_1s() -> None:
    daily, interval, note = resolve_pace(daily_limit=None, min_interval_sec=None)
    assert daily == 0
    assert interval == DEFAULT_INTERVAL_NO_DAILY_LIMIT
    assert interval == 1.0
    assert "1.0" in note or "no_daily" in note


def test_resolve_pace_zero_daily_unlimited() -> None:
    daily, interval, _ = resolve_pace(daily_limit=0, min_interval_sec=None)
    assert daily == 0
    assert interval == 1.0


def test_resolve_pace_caps_slow_calculated_interval() -> None:
    """70000/day → ~1.23s calculated; slower than 1req/s → capped to 1s."""
    daily, interval, note = resolve_pace(daily_limit=70000, min_interval_sec=None)
    assert daily == 70000
    assert abs(SECONDS_PER_DAY / 70000 - 1.234285714) < 1e-6
    assert interval == MAX_PACE_INTERVAL_SEC
    assert "capped" in note


def test_resolve_pace_keeps_fast_calculated_interval() -> None:
    """High daily_limit → sub-1s pace is kept (faster than 1req/s)."""
    daily, interval, note = resolve_pace(daily_limit=200000, min_interval_sec=None)
    assert daily == 200000
    expected = SECONDS_PER_DAY / 200000
    assert abs(interval - expected) < 1e-9
    assert "capped" not in note


def test_resolve_pace_daily_limit_ignores_slower_explicit_interval() -> None:
    daily, interval, note = resolve_pace(daily_limit=50000, min_interval_sec=1.5)
    assert daily == 50000
    assert interval == MAX_PACE_INTERVAL_SEC
    assert "explicit_interval_ignored" in note
    assert "capped" in note


def test_resolve_pace_daily_limit_ignores_faster_explicit_interval() -> None:
    daily, interval, note = resolve_pace(daily_limit=50000, min_interval_sec=0.5)
    assert daily == 50000
    assert interval == MAX_PACE_INTERVAL_SEC
    assert "explicit_interval_ignored" in note
    assert "capped" in note


def test_resolve_pace_no_daily_with_explicit_interval_capped() -> None:
    daily, interval, note = resolve_pace(daily_limit=None, min_interval_sec=3.0)
    assert daily == 0
    assert interval == MAX_PACE_INTERVAL_SEC
    assert "capped" in note
