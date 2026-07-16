from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from em_product.product import StandardProduct
from pydantic import ValidationError

from aliexpress_ds.html_utils import clean_product_description


def source_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("aliexpress.us") or host == "aliexpress.us":
        return "aliexpress.us"
    return "aliexpress.com"


def normalize_https_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def dig_result(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("result"), dict):
        return payload["result"]
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    nested = payload.get("aliexpress_ds_product_get_response")
    if isinstance(nested, dict) and isinstance(nested.get("result"), dict):
        return nested["result"]
    return payload


def _to_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        s = str(value).replace(",", "").strip()
        # AE often returns floor buckets like "500+", "10000+"
        if s.endswith("+"):
            s = s[:-1].strip()
        m = re.match(r"^-?\d+(?:\.\d+)?", s)
        if m:
            return int(float(m.group(0)))
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _sku_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    node = result.get("ae_item_sku_info_dtos")
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    if isinstance(node, dict):
        for key in ("ae_item_sku_info_d_t_o", "ae_item_sku_info_dto", "sku"):
            inner = node.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
            if isinstance(inner, dict):
                return [inner]
        if "sku_id" in node or "id" in node:
            return [node]
    return []


def _sku_properties(sku: dict[str, Any]) -> list[dict[str, Any]]:
    props = sku.get("ae_sku_property_dtos") or sku.get("ae_sku_property_dto")
    if isinstance(props, list):
        return [p for p in props if isinstance(p, dict)]
    if isinstance(props, dict):
        inner = (
            props.get("ae_sku_property_d_t_o")
            or props.get("ae_sku_property_dto")
            or props.get("ae_sku_property")
        )
        if isinstance(inner, list):
            return [p for p in inner if isinstance(p, dict)]
        if isinstance(inner, dict):
            return [inner]
    return []


def _images(result: dict[str, Any], skus: list[dict[str, Any]]) -> str:
    multimedia = result.get("ae_multimedia_info_dto") or {}
    urls: list[str] = []
    if isinstance(multimedia, dict):
        image_urls = multimedia.get("image_urls") or multimedia.get("images")
        if isinstance(image_urls, str):
            urls.extend(u.strip() for u in image_urls.split(";") if u.strip())
        elif isinstance(image_urls, list):
            urls.extend(str(u).strip() for u in image_urls if u)

    for sku in skus:
        for prop in _sku_properties(sku):
            img = prop.get("sku_image") or prop.get("sku_property_image_path")
            if img:
                urls.append(str(img).strip())

    # absolute https only, dedupe preserve order
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls:
        url = normalize_https_url(url)
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return ";".join(cleaned) if cleaned else "https://ae01.alicdn.com/placeholder.jpg"


def _videos(result: dict[str, Any]) -> str | None:
    multimedia = result.get("ae_multimedia_info_dto") or {}
    if not isinstance(multimedia, dict):
        return None
    videos = multimedia.get("ae_video_dtos") or multimedia.get("ae_video_dto")
    urls: list[str] = []
    items: list[Any]
    if isinstance(videos, list):
        items = videos
    elif isinstance(videos, dict):
        inner = videos.get("ae_video_d_t_o") or videos.get("ae_video_dto") or videos
        items = inner if isinstance(inner, list) else [inner]
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("media_url") or item.get("video_url") or item.get("url")
        if url:
            urls.append(normalize_https_url(str(url)))
    return ";".join(urls) if urls else None


def _specifications(result: dict[str, Any]) -> list[dict[str, str]] | None:
    props = result.get("ae_item_properties") or {}
    items: list[Any] = []
    if isinstance(props, list):
        items = props
    elif isinstance(props, dict):
        inner = props.get("ae_item_property") or props.get("ae_item_properties")
        if isinstance(inner, list):
            items = inner
        elif isinstance(inner, dict):
            items = [inner]
    specs: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("attr_name") or item.get("name") or "").strip()
        value = str(item.get("attr_value") or item.get("value") or "").strip()
        if name and value:
            specs.append({"name": name, "value": value})
    return specs or None


def _brand(specs: list[dict[str, str]] | None) -> str | None:
    if not specs:
        return None
    for spec in specs:
        if spec.get("name", "").lower() in {"brand name", "brand"}:
            return spec.get("value")
    return None


def _description(base: dict[str, Any]) -> str:
    html = clean_product_description(str(base.get("detail") or ""))
    if html:
        return html
    # fallback: mobile_detail text modules
    mobile = base.get("mobile_detail")
    if isinstance(mobile, str):
        try:
            mobile = json.loads(mobile)
        except json.JSONDecodeError:
            mobile = None
    texts: list[str] = []
    if isinstance(mobile, dict):
        for module in mobile.get("moduleList") or []:
            if not isinstance(module, dict):
                continue
            if module.get("type") == "text":
                content = ((module.get("data") or {}).get("content") or "").strip()
                if content:
                    texts.append(content)
            elif module.get("type") == "image":
                url = ((module.get("data") or {}).get("url") or "").strip()
                if url:
                    texts.append(f'<img src="{normalize_https_url(url)}"/>')
    if texts:
        return clean_product_description("<br/>".join(texts))
    title = str(base.get("subject") or "").strip()
    return title or "No description"


def _build_options_and_variants(
    product_id: str,
    currency: str,
    skus: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None, bool, float, int | None]:
    options_map: dict[str, dict[str, Any]] = {}
    variants: list[dict[str, Any]] = []
    prices: list[float] = []
    total_qty = 0

    for sku in skus:
        sku_attr = str(sku.get("sku_attr") or sku.get("id") or "")
        formatted_attr = ";".join(part.split("#")[0] for part in sku_attr.split(";") if part)
        price = _to_float(sku.get("offer_sale_price") or sku.get("sku_price"), 0.0) or 0.0
        qty = _to_int(sku.get("sku_available_stock") or sku.get("ipm_sku_stock"), 0) or 0
        if qty <= 0:
            # still keep zero-stock? alixq3 skips qty<=0. Follow same.
            continue

        option_values: list[dict[str, str]] = []
        variant_image = None
        for prop in _sku_properties(sku):
            option_id = str(prop.get("sku_property_id") or "").strip()
            option_name = str(prop.get("sku_property_name") or "").strip()
            option_value_id = str(prop.get("property_value_id") or "").strip()
            option_value = str(
                prop.get("property_value_definition_name")
                or prop.get("sku_property_value")
                or option_value_id
            ).strip()
            img = prop.get("sku_image")
            if img and not variant_image:
                variant_image = normalize_https_url(str(img))
            if option_id and option_name:
                options_map[option_id] = {"name": option_name, "id": option_id}
                option_values.append(
                    {
                        "option_id": option_id,
                        "option_value_id": option_value_id or option_value,
                        "option_name": option_name,
                        "option_value": option_value,
                    }
                )

        if price > 0:
            prices.append(price)
        total_qty += qty
        variants.append(
            {
                "sku": f"ALI_{product_id}_{formatted_attr or sku.get('sku_id') or 'default'}",
                "barcode": None,
                "variant_id": formatted_attr or str(sku.get("sku_id") or product_id),
                "price": price,
                "currency": currency,
                "available_qty": qty,
                "option_values": option_values,
                "images": variant_image,
            }
        )

    options = list(options_map.values()) or None
    if len(variants) <= 1:
        # single / zero sellable → default variant product
        available = variants[0]["available_qty"] if variants else None
        price = prices[0] if prices else 0.0
        return options, None, True, price, available

    return options, variants, False, (min(prices) if prices else 0.0), total_qty or None


def to_standard_product(
    raw_payload: dict[str, Any],
    *,
    url: str,
    source: str | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Convert aliexpress.ds.product.get payload → StandardProduct dict."""
    result = dig_result(raw_payload)
    base = result.get("ae_item_base_info_dto") or {}
    if not isinstance(base, dict):
        base = {}

    product_id = str(base.get("product_id") or result.get("product_id") or "").strip()
    if not product_id:
        # try from url
        m = re.search(r"/item/(?:[^/]+/)?(\d{10,20})", url)
        product_id = m.group(1) if m else "0"

    skus = _sku_list(result)
    # SKU offer_sale_price is in sku.currency_code (buyer/local, e.g. USD when
    # target_currency=USD). base.currency_code is often the seller/origin CNY.
    sku_currency = None
    for sku in skus:
        code = str(sku.get("currency_code") or "").strip()
        if code:
            sku_currency = code
            break
    currency = sku_currency or str(base.get("currency_code") or "USD")
    options, variants, has_only_default, price, available_qty = _build_options_and_variants(
        product_id, currency, skus
    )
    if price <= 0 and skus:
        price = min(
            (
                (_to_float(s.get("offer_sale_price") or s.get("sku_price"), 0.0) or 0.0)
                for s in skus
            ),
            default=0.0,
        )
        if available_qty is None:
            available_qty = sum(
                (_to_int(s.get("sku_available_stock"), 0) or 0) for s in skus
            ) or None

    images = _images(result, skus)
    specs = _specifications(result)
    title = str(base.get("subject") or base.get("product_title") or "").strip() or "ERROR"
    status = str(base.get("product_status_type") or "").lower()
    existence = status == "onselling"
    if title == "ERROR":
        existence = False

    logistics = result.get("logistics_info_dto") or {}
    shipping_days = _to_int(logistics.get("delivery_time"))

    now = datetime.now().replace(microsecond=0).isoformat()
    src = source or source_from_url(url)

    payload: dict[str, Any] = {
        "date": now,
        "url": normalize_https_url(url),
        "source": src,
        "product_id": product_id,
        "existence": existence and bool(images) and "placeholder" not in images,
        "title": title,
        "title_en": None,
        "description": _description(base),
        "summary": None,
        "sku": product_id,
        "upc": None,
        "brand": _brand(specs),
        "specifications": specs,
        "categories": None,
        "images": images,
        "videos": _videos(result),
        "price": price,
        "currency": currency,
        "available_qty": available_qty,
        "options": options,
        "variants": variants,
        "returnable": None,
        "reviews": _to_int(base.get("evaluation_count")),
        "rating": _to_float(base.get("avg_evaluation_rating"), default=None),
        "sold_count": _to_int(base.get("sales_count")),
        "shipping_fee": 0.0,
        "shipping_days_min": shipping_days,
        "shipping_days_max": shipping_days,
        "weight": None,
        "width": None,
        "height": None,
        "length": None,
        "has_only_default_variant": has_only_default,
        "created_at": now,
        "updated_at": now,
    }

    if validate:
        try:
            return StandardProduct(**payload).model_dump()
        except ValidationError:
            if not payload.get("description"):
                payload["description"] = payload["title"]
            payload["description"] = clean_product_description(str(payload["description"]))
            return StandardProduct(**payload).model_dump()

    return payload
