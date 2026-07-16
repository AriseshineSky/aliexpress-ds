"""Shipping fee via aliexpress.logistics.buyer.freight.calculate."""

from __future__ import annotations

import json
import logging
from typing import Any

from aliexpress_ds.iop_client import IopClient, IopError
from aliexpress_ds.rate_limit import DailyQuotaExhausted

logger = logging.getLogger(__name__)


def _freight_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not isinstance(result, dict):
        return []
    node = result.get("aeop_freight_calculate_result_for_buyer_d_t_o_list")
    if isinstance(node, dict):
        inner = node.get("aeop_freight_calculate_result_for_buyer_dto")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            return [inner]
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    return []


def _pick_sku_for_freight(skus: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not skus:
        return None
    for sku in skus:
        qty = sku.get("sku_available_stock") or sku.get("ipm_sku_stock")
        try:
            if qty is not None and int(qty) > 0:
                return sku
        except (TypeError, ValueError):
            continue
    return skus[0]


def fetch_shipping_fee(
    *,
    product_id: str,
    skus: list[dict[str, Any]],
    ship_to_country: str = "US",
    send_goods_country_code: str = "CN",
    client: IopClient | None = None,
) -> float | None:
    """Return cheapest freight.amount (USD) for the first in-stock SKU, or None."""
    sku = _pick_sku_for_freight(skus)
    if not sku:
        return None

    sku_id = str(sku.get("sku_id") or sku.get("id") or "").strip()
    if not sku_id:
        return None

    price = sku.get("offer_sale_price") or sku.get("sku_price")
    if price is None:
        return None

    currency = str(sku.get("currency_code") or "USD").strip() or "USD"
    dto = {
        "product_id": int(str(product_id)),
        "sku_id": sku_id,
        "country_code": ship_to_country,
        "send_goods_country_code": send_goods_country_code,
        "product_num": 1,
        "price": str(price),
        "price_currency": currency,
    }

    client = client or IopClient()
    try:
        resp = client.execute(
            "aliexpress.logistics.buyer.freight.calculate",
            {
                "param_aeop_freight_calculate_for_buyer_d_t_o": json.dumps(
                    dto, separators=(",", ":")
                )
            },
        )
    except DailyQuotaExhausted:
        raise
    except (IopError, ValueError, RuntimeError) as exc:
        logger.warning("freight.calculate failed for %s: %s", product_id, exc)
        return None

    options = _freight_list(resp)
    amounts: list[float] = []
    for opt in options:
        if int(opt.get("error_code") or 0) != 0:
            continue
        freight = opt.get("freight")
        if not isinstance(freight, dict):
            continue
        try:
            amount = float(freight.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount >= 0:
            amounts.append(amount)

    if not amounts:
        return None
    return min(amounts)
