from aliexpress_ds.fetch_errors import (
    CLASS_BAD_ID,
    CLASS_NOT_FOUND,
    CLASS_SHIP_PROHIBITED,
    CLASS_UNSALEABLE,
    classify_ds_error,
    is_permanent_ds_error,
    is_transient_ds_error,
)


def test_classify_ds_error_classes() -> None:
    assert classify_ds_error("All SKU Unsaleable") == CLASS_UNSALEABLE
    assert classify_ds_error("ITEM_ID_NOT_FOUND") == CLASS_NOT_FOUND
    assert classify_ds_error("SHIP_TO_COUNTRY_PROHIBITED") == CLASS_SHIP_PROHIBITED
    assert classify_ds_error("Cannot extract product_id from: 'https://x'") == CLASS_BAD_ID
    assert classify_ds_error("System Error") is None


def test_permanent_ds_errors() -> None:
    assert is_permanent_ds_error("All SKU Unsaleable")
    assert is_permanent_ds_error("ITEM_ID_NOT_FOUND")
    assert is_permanent_ds_error("SHIP_TO_COUNTRY_PROHIBITED")
    assert is_permanent_ds_error("Cannot extract product_id from: 'https://x'")
    assert not is_permanent_ds_error("System Error")
    assert not is_permanent_ds_error("AppApiCallLimit ban will last 12 seconds")


def test_transient_ds_errors() -> None:
    assert is_transient_ds_error("System Error")
    assert is_transient_ds_error("timeout talking to gateway")
    assert not is_transient_ds_error("All SKU Unsaleable")
