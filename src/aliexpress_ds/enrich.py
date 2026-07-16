"""Post-process StandardProduct with freight + categories."""

from __future__ import annotations

import logging
from typing import Any

from aliexpress_ds.category_lookup import resolve_categories
from aliexpress_ds.freight import fetch_shipping_fee
from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.standard_mapper import dig_result, _sku_list

logger = logging.getLogger(__name__)


def _urls_category_fallback(product_id: str, source: str | None = None) -> str | None:
    """Look up urls-index breadcrumb when job/item has no category."""
    pid = str(product_id or "").strip()
    if not pid:
        return None
    try:
        from aliexpress_ds.config import get_settings
        from aliexpress_ds.es import make_es_client

        settings = get_settings()
        settings.require_es()
        es = make_es_client(settings)
        sources = []
        if source:
            sources.append(str(source).strip())
        sources.extend(["aliexpress.us", "aliexpress.com"])
        seen: set[str] = set()
        for src in sources:
            if not src or src in seen:
                continue
            seen.add(src)
            try:
                doc = es.get(index=settings.es_urls_index, id=f"{src}_{pid}")
            except Exception:
                continue
            cat = (doc.get("_source") or {}).get("category")
            if cat:
                return str(cat)
    except Exception as exc:
        logger.debug("urls category fallback failed for %s: %s", pid, exc)
    return None


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
    fallback = item.get("category") or item.get("categories")
    if not fallback:
        pid = str(product.get("product_id") or base.get("product_id") or "").strip()
        fallback = _urls_category_fallback(pid, item.get("source") or product.get("source"))
    categories = resolve_categories(
        category_id=category_id,
        fallback_category=fallback,
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
