"""AliExpress DS recommend feed discovery (aliexpress.ds.recommend.feed.get).

Unlike text.search (keyword), feeds are platform-curated pools such as
bestsellers / new arrivals. Call with optional category_id and paginate until
is_finished — useful to fill product_ids that keyword/crawl missed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.standard_mapper import _to_float, _to_int

# Known DS bestseller promo names (from feedname.get). Prefer these over the
# dead placeholder "DS bestseller".
DEFAULT_BESTSELLER_FEEDS: tuple[str, ...] = (
    "AEB_Droplo_BestsellersItems_20241016",
    "DS_Sports&Outdoors_bestsellers",
    "DS_ConsumerElectronics_bestsellers",
    "DS_Automobile&Accessories_bestsellers",
    "DS_Home&Kitchen_bestsellers",
    "DS_Beauty_bestsellers",
    "DS_ElectronicComponents_bestsellers",
)

# Priority crawl L1 display name → DS category_id (for Droplo feed + category_id).
PRIORITY_FEED_CATEGORY_IDS: dict[str, str] = {
    "Automotive": "34",
    "Toys & Games": "26",  # Toys & Hobbies (better than Mother & Kids)
    "Beauty & Health": "66",
    "Jewelry & Accessories": "36",
    "Hair Extensions & Wigs": "200165144",
    "Pet Supplies": "100006664",
    "Cell Phones & Accessories": "509",
    "Electronics": "44",
    "Patio, Lawn & Garden": "15",
    "Tools & Home Improvement": "13",
    "Sports & Outdoors": "18",
    "Arts, Crafts & Sewing": "200003937",
    "Office & School Supplies": "21",
}


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
        feed_name: str = "AEB_Droplo_BestsellersItems_20241016",
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


def parse_feed_promos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten feedname.get into [{name, product_num, desc}, ...]."""
    out: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            name = obj.get("promo_name") or obj.get("feed_name")
            if name:
                out.append(
                    {
                        "name": str(name).strip(),
                        "product_num": _to_int(obj.get("product_num")),
                        "desc": obj.get("promo_desc") or obj.get("desc"),
                    }
                )
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    # de-dupe by name, keep max product_num
    by_name: dict[str, dict[str, Any]] = {}
    for row in out:
        n = row["name"]
        if not n:
            continue
        prev = by_name.get(n)
        if prev is None or (row.get("product_num") or 0) >= (prev.get("product_num") or 0):
            by_name[n] = row
    rows = list(by_name.values())
    rows.sort(key=lambda r: (-(r.get("product_num") or 0), r["name"]))
    return rows


def extract_feed_meta(payload: dict[str, Any]) -> dict[str, Any]:
    root = payload.get("result") or payload.get("resp_result") or payload
    if isinstance(root, dict) and isinstance(root.get("result"), dict):
        # sometimes nested
        if "total_record_count" in root["result"] or "products" in root["result"]:
            root = root["result"]
    if not isinstance(root, dict):
        return {}
    return {
        "total_record_count": _to_int(root.get("total_record_count")),
        "current_record_count": _to_int(root.get("current_record_count")),
        "is_finished": bool(root.get("is_finished")),
    }


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


def _normalize_item_url(url: str, *, source: str = "aliexpress.us") -> str:
    text = (url or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    if "?" in text:
        text = text.split("?", 1)[0]
    if "aliexpress.com/item/" in text and "aliexpress.us" not in text:
        if source == "aliexpress.us":
            text = text.replace("://www.aliexpress.com/", "://www.aliexpress.us/")
            text = text.replace("://aliexpress.com/", "://www.aliexpress.us/")
    # strip fragment
    parts = urlsplit(text)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _rating_from_evaluate_rate(raw: Any) -> float | None:
    """Convert evaluate_rate like '95.8%' → ~4.79 on a 0–5 scale."""
    if raw is None:
        return None
    s = str(raw).strip().replace("%", "")
    val = _to_float(s, default=None)
    if val is None:
        return None
    if val > 5.0:
        # percent-like positive feedback rate
        return round(val / 20.0, 2)
    return val


def product_to_url_doc(
    product: dict[str, Any],
    *,
    source: str = "aliexpress.us",
    feed_name: str | None = None,
    category_id: str | None = None,
) -> dict[str, Any] | None:
    pid = str(
        product.get("product_id")
        or product.get("productId")
        or product.get("item_id")
        or ""
    ).strip()
    if not pid:
        return None
    raw_url = (
        product.get("product_detail_url")
        or product.get("detail_url")
        or product.get("product_url")
        or ""
    )
    url = _normalize_item_url(str(raw_url), source=source) or (
        f"https://www.aliexpress.us/item/{pid}.html"
    )
    l1 = str(product.get("first_level_category_name") or "").strip()
    l2 = str(product.get("second_level_category_name") or "").strip()
    path_parts = [p for p in (l1, l2) if p]
    category_path = " > ".join(path_parts) if path_parts else None
    cat = (
        category_path
        or category_id
        or product.get("second_level_category_id")
        or product.get("first_level_category_id")
        or product.get("category_id")
    )
    price = _to_float(
        product.get("target_sale_price")
        or product.get("sale_price")
        or product.get("price"),
        default=None,
    )
    rating = _rating_from_evaluate_rate(
        product.get("evaluate_rate") or product.get("evaluateRate")
    )
    sold = _to_int(
        product.get("lastest_volume")
        or product.get("latest_volume")
        or product.get("volume")
        or product.get("sold_count")
    )
    return {
        "product_id": pid,
        "url": url,
        "source": source,
        "title": product.get("product_title") or product.get("subject") or product.get("title"),
        "category": cat,
        "price": price,
        "rating": rating,
        "sold_count": sold,
        "discovery": "recommend_feed",
        "feed_name": feed_name,
        "feed_category_id": category_id
        or str(product.get("first_level_category_id") or "").strip()
        or None,
        "raw_feed": product,
    }
