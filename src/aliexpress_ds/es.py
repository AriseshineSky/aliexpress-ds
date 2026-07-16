from __future__ import annotations

from typing import Any, Iterator

from elasticsearch import Elasticsearch

from aliexpress_ds.config import Settings, get_settings


def make_es_client(settings: Settings | None = None) -> Elasticsearch:
    settings = settings or get_settings()
    if not settings.es_host:
        raise ValueError("ES_HOST is not set in .env")
    return Elasticsearch(
        hosts=[{"host": settings.es_host, "port": settings.es_port, "scheme": "http"}],
        basic_auth=(settings.es_user, settings.es_password),
        request_timeout=60,
    )


def scroll_sources(
    es: Elasticsearch,
    index: str,
    *,
    source_fields: list[str],
    page_size: int = 2000,
    keep_alive: str = "10m",
    query: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Scroll docs. keep_alive must cover time to finish consuming each page.

    Prefer materializing quickly (see ``list_missing_urls``) when callers pause
    between yields for slow API work — otherwise scroll context may expire.
    """
    resp = es.search(
        index=index,
        size=page_size,
        scroll=keep_alive,
        _source=source_fields,
        query=query or {"match_all": {}},
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                src = hit.get("_source") or {}
                src["_id"] = hit.get("_id")
                yield src
            resp = es.scroll(scroll_id=scroll_id, scroll=keep_alive)
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass


def urls_quality_query(
    *,
    max_price: float | None = None,
    min_rating: float | None = None,
    min_reviews: int | None = None,
    min_sold_count: int | None = None,
) -> dict[str, Any]:
    """ES bool query for listing quality gates on the urls index."""
    must: list[dict[str, Any]] = [{"exists": {"field": "product_id"}}]
    if max_price is not None and max_price > 0:
        must.append({"range": {"price": {"gt": 0, "lt": float(max_price)}}})
    if min_rating is not None:
        must.append({"range": {"rating": {"gte": float(min_rating)}}})
    if min_reviews is not None:
        must.append({"range": {"reviews": {"gte": int(min_reviews)}}})
    if min_sold_count is not None:
        must.append({"range": {"sold_count": {"gte": int(min_sold_count)}}})
    return {"bool": {"must": must}}


def load_existing_product_ids(es: Elasticsearch, products_index: str) -> set[str]:
    ids: set[str] = set()
    for doc in scroll_sources(es, products_index, source_fields=["product_id", "sku", "url"]):
        pid = str(doc.get("product_id") or doc.get("sku") or "").strip()
        if pid:
            ids.add(pid)
        # also parse from _id like aliexpress.us_3256...
        doc_id = str(doc.get("_id") or "")
        if "_" in doc_id:
            tail = doc_id.rsplit("_", 1)[-1]
            if tail.isdigit():
                ids.add(tail)
    return ids


def list_missing_urls(
    es: Elasticsearch,
    *,
    urls_index: str,
    products_index: str,
    existing_ids: set[str] | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    min_reviews: int | None = None,
    min_sold_count: int | None = None,
    quality_filter: bool = False,
) -> list[dict[str, Any]]:
    """Load pending URLs into memory so scroll finishes before slow API calls.

    When ``quality_filter`` is True, apply listing gates on urls-index fields
    (price / rating / reviews / sold_count) via ES query.
    """
    existing = existing_ids if existing_ids is not None else load_existing_product_ids(es, products_index)
    query: dict[str, Any] | None = None
    if quality_filter:
        query = urls_quality_query(
            max_price=max_price,
            min_rating=min_rating,
            min_reviews=min_reviews,
            min_sold_count=min_sold_count,
        )
    missing: list[dict[str, Any]] = []
    for doc in scroll_sources(
        es,
        urls_index,
        source_fields=[
            "product_id",
            "url",
            "source",
            "title",
            "category",
            "price",
            "rating",
            "reviews",
            "sold_count",
        ],
        keep_alive="10m",
        query=query,
    ):
        pid = str(doc.get("product_id") or "").strip()
        if not pid or pid in existing:
            continue
        url = doc.get("url") or f"https://www.aliexpress.us/item/{pid}.html"
        missing.append(
            {
                "product_id": pid,
                "url": url,
                "source": doc.get("source"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "price": doc.get("price"),
                "rating": doc.get("rating"),
                "reviews": doc.get("reviews"),
                "sold_count": doc.get("sold_count"),
                "es_id": doc.get("_id"),
            }
        )
    return missing


def iter_missing_urls(
    es: Elasticsearch,
    *,
    urls_index: str,
    products_index: str,
    existing_ids: set[str] | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    min_reviews: int | None = None,
    min_sold_count: int | None = None,
    quality_filter: bool = False,
) -> Iterator[dict[str, Any]]:
    yield from list_missing_urls(
        es,
        urls_index=urls_index,
        products_index=products_index,
        existing_ids=existing_ids,
        max_price=max_price,
        min_rating=min_rating,
        min_reviews=min_reviews,
        min_sold_count=min_sold_count,
        quality_filter=quality_filter,
    )


def upsert_url_docs(
    es: Elasticsearch,
    urls_index: str,
    docs: list[dict[str, Any]],
) -> int:
    """Bulk index URL seed docs. Doc id = {source}_{product_id}."""
    if not docs:
        return 0
    actions: list[dict[str, Any]] = []
    for doc in docs:
        pid = str(doc.get("product_id") or "").strip()
        if not pid:
            continue
        source = str(doc.get("source") or "aliexpress.us").strip()
        doc_id = f"{source}_{pid}"
        body = dict(doc)
        body.pop("raw_feed", None)
        actions.append({"index": {"_index": urls_index, "_id": doc_id}})
        actions.append(body)
    if not actions:
        return 0
    resp = es.bulk(operations=actions, refresh=False)
    if resp.get("errors"):
        # Count successes; ignore per-item conflicts for seed URLs
        ok = 0
        for item in resp.get("items") or []:
            idx = item.get("index") or {}
            if idx.get("status") in (200, 201):
                ok += 1
        return ok
    return len(actions) // 2


def upsert_standard_products(
    es: Elasticsearch,
    products_index: str,
    products: list[dict[str, Any]],
) -> int:
    """Bulk index StandardProduct docs. Doc id = {source}_{product_id}."""
    if not products:
        return 0
    actions: list[dict[str, Any]] = []
    for product in products:
        pid = str(product.get("product_id") or "").strip()
        if not pid:
            continue
        source = str(product.get("source") or "aliexpress.us").strip()
        doc_id = f"{source}_{pid}"
        actions.append({"index": {"_index": products_index, "_id": doc_id}})
        actions.append(product)
    if not actions:
        return 0
    resp = es.bulk(operations=actions, refresh=False)
    if resp.get("errors"):
        ok = 0
        for item in resp.get("items") or []:
            idx = item.get("index") or {}
            if idx.get("status") in (200, 201):
                ok += 1
        return ok
    return len(actions) // 2


def upsert_docs(
    es: Elasticsearch,
    index: str,
    docs: list[dict[str, Any]],
    *,
    id_field: str,
) -> int:
    """Generic bulk upsert; document _id = docs[id_field]."""
    if not docs:
        return 0
    actions: list[dict[str, Any]] = []
    for doc in docs:
        doc_id = str(doc.get(id_field) or "").strip()
        if not doc_id:
            continue
        actions.append({"index": {"_index": index, "_id": doc_id}})
        actions.append(doc)
    if not actions:
        return 0
    resp = es.bulk(operations=actions, refresh="wait_for")
    if resp.get("errors"):
        ok = 0
        for item in resp.get("items") or []:
            idx = item.get("index") or {}
            if idx.get("status") in (200, 201):
                ok += 1
        return ok
    return len(actions) // 2


def upsert_crawl_category_seeds(
    es: Elasticsearch,
    index: str,
    docs: list[dict[str, Any]],
    *,
    id_field: str = "name",
) -> int:
    """Partial-update crawl seeds; preserve claim/crawl progress fields.

    New docs get crawl_status=pending. Existing docs keep crawl_status,
    claimed_*, listing_total, crawled_* etc.
    """
    if not docs:
        return 0
    ok = 0
    for doc in docs:
        doc_id = str(doc.get(id_field) or "").strip()
        if not doc_id:
            continue
        seed = dict(doc)
        upsert = dict(seed)
        upsert.setdefault("crawl_status", "pending")
        try:
            es.update(
                index=index,
                id=doc_id,
                body={"doc": seed, "upsert": upsert},
                refresh=False,
            )
            ok += 1
        except Exception:
            continue
    try:
        es.indices.refresh(index=index)
    except Exception:
        pass
    return ok


def ensure_index(es: Elasticsearch, index: str) -> None:
    if not es.indices.exists(index=index):
        es.indices.create(index=index)


def load_crawl_categories(
    es: Elasticsearch,
    index: str,
    *,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    """Load calp crawl seeds for aliexpress-link-crawler."""
    must: list[dict[str, Any]] = []
    if enabled_only:
        must.append({"term": {"enabled": True}})
    query: dict[str, Any] = {"match_all": {}} if not must else {"bool": {"must": must}}
    resp = es.search(
        index=index,
        size=500,
        query=query,
        sort=[{"priority": {"order": "asc", "unmapped_type": "long"}}],
    )
    rows: list[dict[str, Any]] = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        if src.get("name") and src.get("url"):
            rows.append(src)
    return rows
