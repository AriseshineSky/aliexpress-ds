from datetime import datetime, timezone

from aliexpress_ds.standard_mapper import to_standard_product

_PAYLOAD = {
    "result": {
        "ae_item_base_info_dto": {
            "product_id": 1005000000000001,
            "subject": "Test Widget",
            "product_status_type": "onselling",
            "category_id": 1,
        },
        "ae_multimedia_info_dto": {"image_urls": "https://ae01.alicdn.com/kf/x.jpg"},
        "ae_item_sku_info_dtos": {
            "ae_item_sku_info_d_t_o": [
                {
                    "sku_id": 1,
                    "sku_attr": "Color:Red",
                    "offer_sale_price": "9.99",
                    "sku_available_stock": 5,
                    "currency_code": "USD",
                }
            ]
        },
        "ae_store_info": {"store_id": 1, "store_name": "s"},
    }
}


def test_timestamps_are_timezone_aware_utc() -> None:
    """Regression: naive datetime.now() wrote CST wall-clock time as UTC.

    On a UTC-6 host every product looked 6h stale, so ``range: now-30m``
    queries (dashboards, freshness checks) returned 0.
    """
    product = to_standard_product(
        _PAYLOAD,
        url="https://www.aliexpress.us/item/1005000000000001.html",
        validate=False,
    )

    for field in ("date", "created_at", "updated_at"):
        raw = product[field]
        parsed = datetime.fromisoformat(raw)
        assert parsed.tzinfo is not None, f"{field} is naive: {raw}"
        assert parsed.utcoffset() == timezone.utc.utcoffset(None)
        assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 300