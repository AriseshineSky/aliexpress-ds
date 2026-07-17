from __future__ import annotations

import logging
import threading
from typing import Any, Iterator

from elasticsearch import Elasticsearch

from aliexpress_ds.config import Settings, get_settings

logger = logging.getLogger(__name__)


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
    """Bulk upsert URL seed docs via update+doc_as_upsert. Doc id = {source}_{product_id}."""
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
        body.pop("raw_search", None)
        actions.append({"update": {"_index": urls_index, "_id": doc_id}})
        actions.append({"doc": body, "doc_as_upsert": True})
    if not actions:
        return 0
    resp = es.bulk(operations=actions, refresh=False)
    if resp.get("errors"):
        # Count successes; surface first mapper/error so silent 0 isn't mysterious
        ok = 0
        first_err: str | None = None
        for item in resp.get("items") or []:
            upd = item.get("update") or item.get("index") or {}
            if upd.get("status") in (200, 201):
                ok += 1
                continue
            if first_err is None:
                err = upd.get("error") or {}
                first_err = (
                    f"{err.get('type')}: {err.get('reason')}"
                    if isinstance(err, dict)
                    else str(err)
                )
        if ok == 0 and first_err:
            raise RuntimeError(f"ES urls bulk failed (0/{len(actions)//2}): {first_err}")
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


class EsProductBuffer:
    """Background bulk buffer so ES I/O stays off the API hot path.

    Products are queued in-memory; a daemon thread flushes when ``max_size`` is
    reached or ``flush_interval_sec`` elapses. Call ``close()`` on shutdown.
    """

    def __init__(
        self,
        es: Elasticsearch,
        products_index: str,
        *,
        max_size: int = 25,
        flush_interval_sec: float = 2.0,
    ):
        self.es = es
        self.products_index = products_index
        self.max_size = max(1, int(max_size))
        self.flush_interval_sec = max(0.2, float(flush_interval_sec))
        self._buf: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._indexed = 0
        self._errors = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="es-product-buffer",
            daemon=True,
        )
        self._thread.start()

    @property
    def indexed(self) -> int:
        return self._indexed

    @property
    def errors(self) -> int:
        return self._errors

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._buf)

    def add(self, product: dict[str, Any]) -> None:
        if not isinstance(product, dict):
            return
        should_flush = False
        with self._lock:
            self._buf.append(product)
            should_flush = len(self._buf) >= self.max_size
        if should_flush:
            self._wake.set()

    def flush(self) -> int:
        """Synchronously flush the current buffer. Returns docs indexed this call."""
        with self._lock:
            batch = self._buf
            self._buf = []
        if not batch:
            return 0
        return self._write(batch)

    def close(self) -> int:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=max(5.0, self.flush_interval_sec + 3.0))
        return self.flush()

    def _write(self, batch: list[dict[str, Any]]) -> int:
        try:
            n = upsert_standard_products(self.es, self.products_index, batch)
            self._indexed += n
            return n
        except Exception as exc:
            self._errors += 1
            logger.warning(
                "ES bulk flush failed (%s docs): %s",
                len(batch),
                exc,
            )
            return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.flush_interval_sec)
            self._wake.clear()
            with self._lock:
                if not self._buf:
                    continue
                batch = self._buf
                self._buf = []
            if batch:
                self._write(batch)


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
