from aliexpress_ds.rate_limit import RateLimiter


def test_daily_limit_is_pacing_metadata_not_local_stop(tmp_path) -> None:
    limiter = RateLimiter(
        min_interval_sec=0,
        daily_limit=2,
        state_path=tmp_path / "rate.json",
    )

    limiter.wait_turn()
    limiter.wait_turn()
    limiter.wait_turn()

    assert limiter.calls_today == 3
    assert limiter.remaining_today is None
