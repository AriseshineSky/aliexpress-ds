from __future__ import annotations

from typing import Any

from aliexpress_ds.iop_client import IopClient


class FeedService:
    """AliExpress DS recommend feed + feedname listing (product discovery)."""

    FEED_GET = "aliexpress.ds.recommend.feed.get"
    FEEDNAME_GET = "aliexpress.ds.feedname.get"
    CATEGORY_GET = "aliexpress.ds.category.get"

    def __init__(self, client: IopClient | None = None):
        self.client = client or IopClient()

    def list_feed_names(self, **extra: Any) -> dict[str, Any]:
        return self.client.execute(self.FEEDNAME_GET, extra or None)

    def list_categories(self, **extra: Any) -> dict[str, Any]:
        return self.client.execute(self.CATEGORY_GET, extra or None)

    def recommend(
        self,
        *,
        feed_name: str = "DS bestseller",
        category_id: str | None = None,
        country: str = "US",
        target_currency: str = "USD",
        target_language: str = "EN",
        page_no: int = 1,
        page_size: int = 50,
        sort: str | None = "volumeDesc",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "feed_name": feed_name,
            "country": country,
            "target_currency": target_currency,
            "target_language": target_language,
            "page_no": page_no,
            "page_size": min(max(int(page_size), 1), 50),
        }
        if category_id:
            params["category_id"] = str(category_id)
        if sort:
            params["sort"] = sort
        return self.client.execute(self.FEED_GET, params)


def extract_products(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize recommend.feed.get products into a flat list."""
    root = payload.get("result") or payload.get("resp_result") or payload
    if isinstance(root, dict) and isinstance(root.get("result"), dict):
        root = root["result"]
    if not isinstance(root, dict):
        return []

    products = (
        root.get("products")
        or root.get("products_list")
        or root.get("product")
        or root.get("ae_product_list")
    )
    if isinstance(products, dict):
        for key in (
            "traffic_product_d_t_o",
            "traffic_product_dto",
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
            # Still a dict with no recognized list key
            if "product_id" in products or "productId" in products:
                products = [products]
            else:
                products = []
    if not isinstance(products, list):
        return []
    return [p for p in products if isinstance(p, dict)]


def product_to_url_doc(product: dict[str, Any], *, source: str = "aliexpress.us") -> dict[str, Any] | None:
    pid = str(
        product.get("product_id")
        or product.get("productId")
        or product.get("item_id")
        or ""
    ).strip()
    if not pid:
        return None
    url = (
        product.get("product_detail_url")
        or product.get("detail_url")
        or product.get("product_url")
        or f"https://www.aliexpress.us/item/{pid}.html"
    )
    return {
        "product_id": pid,
        "url": url,
        "source": source,
        "title": product.get("product_title") or product.get("subject") or product.get("title"),
        "category": product.get("category_id") or product.get("second_level_category_id"),
        "price": product.get("target_sale_price") or product.get("sale_price") or product.get("price"),
        "raw_feed": product,
    }
