"""Post-process StandardProduct with freight + categories."""

from __future__ import annotations

from typing import Any

from aliexpress_ds.category_lookup import resolve_categories
from aliexpress_ds.freight import fetch_shipping_fee
from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.standard_mapper import dig_result, _sku_list


def enrich_product(
    product: dict[str, Any],
    *,
    raw_payload: dict[str, Any],
    item: dict[str, Any] | None = None,
    ship_to_country: str = "US",
    fetch_shipping: bool = True,
    client: IopClient | None = None,
) -> dict[str, Any]:
    """Fill shipping_fee and categories on an already-mapped product dict."""
    item = item or {}
    result = dig_result(raw_payload)
    base = result.get("ae_item_base_info_dto") or {}
    if not isinstance(base, dict):
        base = {}

    category_id = str(base.get("category_id") or "").strip() or None
    categories = resolve_categories(
        category_id=category_id,
        fallback_category=item.get("category"),
    )
    if categories:
        product["categories"] = categories

    if fetch_shipping:
        pid = str(product.get("product_id") or base.get("product_id") or "").strip()
        skus = _sku_list(result)
        fee = fetch_shipping_fee(
            product_id=pid,
            skus=skus,
            ship_to_country=ship_to_country,
            client=client,
        )
        if fee is not None:
            product["shipping_fee"] = fee

    return product
