from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.url_parser import extract_product_id


class SkuInfo(BaseModel):
    sku_id: str | None = None
    sku_attr: Any = None
    sku_price: str | None = None
    offer_sale_price: str | None = None
    offer_bulk_sale_price: str | None = None
    currency_code: str | None = None
    sku_stock: bool | int | str | None = None
    ipm_sku_stock: int | str | None = None
    sku_available_stock: int | str | None = None


class ProductSummary(BaseModel):
    product_id: str
    title: str | None = None
    currency: str | None = None
    ship_to_country: str | None = None
    category_id: str | None = None
    product_status_type: str | None = None
    main_image: str | None = None
    images: list[str] = Field(default_factory=list)
    skus: list[SkuInfo] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class ProductService:
    API = "aliexpress.ds.product.get"

    def __init__(self, client: IopClient | None = None):
        self.client = client or IopClient()

    def get_by_url(
        self,
        url_or_id: str,
        *,
        ship_to_country: str = "US",
        target_currency: str = "USD",
        target_language: str = "EN",
        local_country: str | None = None,
        local_language: str | None = None,
    ) -> ProductSummary:
        product_id = extract_product_id(url_or_id)
        return self.get(
            product_id,
            ship_to_country=ship_to_country,
            target_currency=target_currency,
            target_language=target_language,
            local_country=local_country,
            local_language=local_language,
        )

    def get(
        self,
        product_id: str,
        *,
        ship_to_country: str = "US",
        target_currency: str = "USD",
        target_language: str = "EN",
        local_country: str | None = None,
        local_language: str | None = None,
    ) -> ProductSummary:
        params: dict[str, Any] = {
            "product_id": product_id,
            "ship_to_country": ship_to_country,
            "target_currency": target_currency,
            "target_language": target_language,
        }
        # Some DS docs / regions use local_* aliases; send when provided.
        if local_country:
            params["local_country"] = local_country
        if local_language:
            params["local_language"] = local_language

        payload = self.client.execute(self.API, params)
        result = _dig_result(payload)
        return _normalize(product_id, ship_to_country, result, payload)

    async def aget_by_url(
        self,
        url_or_id: str,
        *,
        ship_to_country: str = "US",
        target_currency: str = "USD",
        target_language: str = "EN",
        local_country: str | None = None,
        local_language: str | None = None,
    ) -> ProductSummary:
        product_id = extract_product_id(url_or_id)
        return await self.aget(
            product_id,
            ship_to_country=ship_to_country,
            target_currency=target_currency,
            target_language=target_language,
            local_country=local_country,
            local_language=local_language,
        )

    async def aget(
        self,
        product_id: str,
        *,
        ship_to_country: str = "US",
        target_currency: str = "USD",
        target_language: str = "EN",
        local_country: str | None = None,
        local_language: str | None = None,
    ) -> ProductSummary:
        params: dict[str, Any] = {
            "product_id": product_id,
            "ship_to_country": ship_to_country,
            "target_currency": target_currency,
            "target_language": target_language,
        }
        if local_country:
            params["local_country"] = local_country
        if local_language:
            params["local_language"] = local_language

        payload = await self.client.execute_async(self.API, params)
        result = _dig_result(payload)
        return _normalize(product_id, ship_to_country, result, payload)


def _dig_result(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("result"), dict):
        return payload["result"]
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _normalize(
    product_id: str,
    ship_to_country: str,
    result: dict[str, Any],
    raw_payload: dict[str, Any],
) -> ProductSummary:
    base = result.get("ae_item_base_info_dto") or result.get("ae_item_base_info") or {}
    if not isinstance(base, dict):
        base = {}

    multimedia = result.get("ae_multimedia_info_dto") or {}
    images: list[str] = []
    if isinstance(multimedia, dict):
        image_urls = multimedia.get("image_urls") or multimedia.get("images")
        if isinstance(image_urls, str):
            images = [u for u in image_urls.split(";") if u]
        elif isinstance(image_urls, list):
            images = [str(u) for u in image_urls]

    skus = [_sku(s) for s in _sku_list(result)]

    title = (
        base.get("subject")
        or base.get("product_title")
        or result.get("subject")
        or result.get("product_title")
    )

    return ProductSummary(
        product_id=str(base.get("product_id") or result.get("product_id") or product_id),
        title=title,
        currency=base.get("currency_code") or result.get("currency_code"),
        ship_to_country=ship_to_country,
        category_id=str(base.get("category_id")) if base.get("category_id") is not None else None,
        product_status_type=base.get("product_status_type"),
        main_image=images[0] if images else None,
        images=images,
        skus=skus,
        raw=raw_payload,
    )


def _sku_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        result.get("ae_item_sku_info_dtos"),
        result.get("ae_op_aeos_product_sku_dto_list"),
        result.get("aeop_ae_product_s_k_us"),
    ]
    for node in candidates:
        if node is None:
            continue
        if isinstance(node, list):
            return [x for x in node if isinstance(x, dict)]
        if isinstance(node, dict):
            for key in (
                "ae_item_sku_info_d_t_o",
                "ae_item_sku_info_dto",
                "aeop_ae_product_sku",
                "sku",
            ):
                inner = node.get(key)
                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]
                if isinstance(inner, dict):
                    return [inner]
            # Some payloads are already {sku_id: ...}
            if "sku_id" in node or "id" in node:
                return [node]
    return []


def _sku(data: dict[str, Any]) -> SkuInfo:
    return SkuInfo(
        sku_id=str(data.get("sku_id") or data.get("id") or "") or None,
        sku_attr=data.get("sku_attr")
        or data.get("ae_sku_property_dtos")
        or data.get("aeop_s_k_u_property_list"),
        sku_price=_as_str(data.get("sku_price")),
        offer_sale_price=_as_str(data.get("offer_sale_price")),
        offer_bulk_sale_price=_as_str(data.get("offer_bulk_sale_price")),
        currency_code=_as_str(data.get("currency_code")),
        sku_stock=data.get("sku_stock"),
        ipm_sku_stock=data.get("ipm_sku_stock"),
        sku_available_stock=data.get("sku_available_stock"),
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
