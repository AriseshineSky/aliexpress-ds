"""Unit tests for Redis app rate-limit resolution."""

from __future__ import annotations

from aliexpress_ds.app_registry import (
    DEFAULT_INTERVAL_NO_DAILY_LIMIT,
    SECONDS_PER_DAY,
    resolve_pace,
)


def test_resolve_pace_no_daily_defaults_to_2s() -> None:
    daily, interval, note = resolve_pace(daily_limit=None, min_interval_sec=None)
    assert daily == 0
    assert interval == DEFAULT_INTERVAL_NO_DAILY_LIMIT
    assert "2.0" in note or "no_daily" in note


def test_resolve_pace_zero_daily_unlimited() -> None:
    daily, interval, _ = resolve_pace(daily_limit=0, min_interval_sec=None)
    assert daily == 0
    assert interval == 2.0


def test_resolve_pace_calculates_from_daily_limit() -> None:
    daily, interval, note = resolve_pace(daily_limit=70000, min_interval_sec=None)
    assert daily == 70000
    assert abs(interval - SECONDS_PER_DAY / 70000) < 1e-9
    assert "daily_limit" in note


def test_resolve_pace_explicit_interval_overrides() -> None:
    daily, interval, note = resolve_pace(daily_limit=50000, min_interval_sec=1.5)
    assert daily == 50000
    assert interval == 1.5
    assert "explicit" in note


def test_resolve_pace_no_daily_with_explicit_interval() -> None:
    daily, interval, _ = resolve_pace(daily_limit=None, min_interval_sec=3.0)
    assert daily == 0
    assert interval == 3.0
