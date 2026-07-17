"""AliExpress DS keyword search (aliexpress.ds.text.search).

Complements link-crawler URL scrape and recommend.feed discovery: given a
category / selection and keywords, returns product_ids for the urls index.
"""

from __future__ import annotations

import json
from typing import Any

from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.standard_mapper import _to_float, _to_int


class TextService:
    """Dropshipping text search — product discovery by keyword."""

    API = "aliexpress.ds.text.search"

    def __init__(self, client: IopClient | None = None):
        self.client = client or IopClient()

    def text_search(
        self,
        *,
        key_word: str,
        category_id: str | None = None,
        country_code: str = "US",
        local: str = "en_US",
        currency: str = "USD",
        page_index: int = 1,
        page_size: int = 20,
        sort_by: str | None = "orders,desc",
        selection_name: str | None = None,
        search_extend: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "keyWord": key_word,
            "local": local,
            "countryCode": country_code,
            "currency": currency,
            "pageIndex": max(1, int(page_index)),
            "pageSize": min(max(int(page_size), 1), 50),
        }
        if category_id:
            params["categoryId"] = str(category_id)
        if sort_by:
            params["sortBy"] = sort_by
        if selection_name:
            params["selectionName"] = selection_name
        if search_extend:
            params["searchExtend"] = json.dumps(search_extend, ensure_ascii=False)
        return self.client.execute(self.API, params)


def extract_search_products(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (products, meta) from text.search response.

    meta includes totalCount / pageIndex / pageSize when present.
    """
    root: Any = payload
    # Unwrap gateway envelopes
    for _ in range(4):
        if not isinstance(root, dict):
            break
        if isinstance(root.get("data"), dict) and (
            "products" in root["data"] or "totalCount" in root["data"]
        ):
            root = root["data"]
            break
        nxt = (
            root.get("aliexpress_ds_text_search_response")
            or root.get("result")
            or root.get("resp_result")
            or root.get("data")
        )
        if isinstance(nxt, dict):
            root = nxt
            continue
        break

    if not isinstance(root, dict):
        return [], {}

    # Sometimes code/data sit on same level after unwrap
    if isinstance(root.get("data"), dict) and "products" in root["data"]:
        root = root["data"]

    products = root.get("products") or root.get("product_list") or []
    if isinstance(products, dict):
        for key in (
            "selection_search_product",
            "traffic_product_d_t_o",
            "product",
            "products",
            "ae_item",
            "item",
        ):
            inner = products.get(key)
            if isinstance(inner, list):
                products = inner
                break
            if isinstance(inner, dict):
                products = [inner]
                break
        else:
            products = []
    if not isinstance(products, list):
        products = []

    meta = {
        "total_count": root.get("totalCount") or root.get("total_count"),
        "page_index": root.get("pageIndex") or root.get("page_index"),
        "page_size": root.get("pageSize") or root.get("page_size"),
    }
    return [p for p in products if isinstance(p, dict)], meta


def search_product_to_url_doc(
    product: dict[str, Any],
    *,
    source: str = "aliexpress.us",
    keyword: str | None = None,
    category_id: str | None = None,
    category_path: str | None = None,
) -> dict[str, Any] | None:
    pid = str(
        product.get("itemId")
        or product.get("item_id")
        or product.get("product_id")
        or product.get("productId")
        or ""
    ).strip()
    if not pid:
        return None
    url = (
        product.get("itemUrl")
        or product.get("item_url")
        or product.get("product_detail_url")
        or f"https://www.aliexpress.us/item/{pid}.html"
    )
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    # Prefer .us host for our urls index
    if "aliexpress.com/item/" in url and "aliexpress.us" not in url:
        url = url.replace("://www.aliexpress.com/", "://www.aliexpress.us/")
    # Strip tracking junk for stable seeds
    if "?" in url:
        url = url.split("?", 1)[0]
    price = _to_float(
        product.get("targetSalePrice")
        or product.get("salePrice")
        or product.get("sale_price")
        or product.get("originMinPrice"),
        default=None,
    )
    # score is often 0–5; evaluateRate may be "98.0%" style
    rating_raw = product.get("score") or product.get("evaluateRate")
    rating: float | None = None
    if rating_raw is not None and str(rating_raw).strip() != "":
        s = str(rating_raw).strip().replace("%", "")
        rating = _to_float(s, default=None)
        if rating is not None and rating > 5.0:
            # convert percent-like to 0–5 scale used by urls index
            rating = round(rating / 20.0, 2)
    return {
        "product_id": pid,
        "url": url,
        "source": source,
        "title": product.get("title") or product.get("product_title"),
        "category": category_path or category_id or product.get("cateId") or product.get("cate_id"),
        "price": price,
        "rating": rating,
        "sold_count": _to_int(product.get("orders") or product.get("sold_count")),
        "discovery": "text_search",
        "search_keyword": keyword,
        "raw_search": product,
    }
