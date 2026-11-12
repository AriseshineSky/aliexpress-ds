from aliexpress_ds.cli import _auth_fatal


def test_auth_fatal_matches_underscore_access_token_message() -> None:
    # The exact message from token_store.resolve_access_token — regressed once
    # because the needle was only "access token" (with a space).
    message = (
        "No access_token: set REDIS_URL (Upstash) with a valid token, or "
        "ALIEXPRESS_ACCESS_TOKEN in .env. Authorize: "
        "https://aliexpress-oauth.onrender.com/oauth/authorize"
    )
    assert _auth_fatal(message)


def test_auth_fatal_matches_redis_token_unavailable() -> None:
    assert _auth_fatal("Redis token unavailable: No token in REDIS_URL.")


def test_auth_fatal_ignores_plain_product_errors() -> None:
    assert not _auth_fatal("The product is off shelf")
    assert not _auth_fatal("unsaleable product")
