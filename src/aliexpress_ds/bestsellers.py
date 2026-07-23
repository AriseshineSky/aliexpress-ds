"""Daily bestsellers sync / mark / missing-enqueue / distribution report."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from elasticsearch import Elasticsearch

from aliexpress_ds.es import scroll_sources, upsert_url_docs
from aliexpress_ds.feed import (
    DEFAULT_BESTSELLER_FEEDS,
    PRIORITY_FEED_CATEGORY_IDS,
    FeedService,
    extract_feed_meta,
    extract_products,
    product_to_url_doc,
)
from aliexpress_ds.iop_client import IopClient
from aliexpress_ds.standard_mapper import _to_float, _to_int

BESTSELLER_TAG = "bestseller"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_bestseller_jobs(
    *,
    feeds: Iterable[str] | None = None,
    category_ids: Iterable[str] | None = None,
    use_priority_categories: bool = True,
) -> list[tuple[str, str | None]]:
    """(feed_name, category_id|None) jobs for daily sync."""
    feed_list = [f.strip() for f in (feeds or DEFAULT_BESTSELLER_FEEDS) if f and str(f).strip()]
    if category_ids:
        cats: list[str | None] = [str(c).strip() for c in category_ids if str(c).strip()]
    elif use_priority_categories:
        cats = list(dict.fromkeys(PRIORITY_FEED_CATEGORY_IDS.values()))
    else:
        cats = [None]

    jobs: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for fname in feed_list:
        is_droplo = "droplo" in fname.lower() or fname.startswith("AEB_")
        if is_droplo:
            for cid in cats:
                key = (fname, cid)
                if key not in seen:
                    seen.add(key)
                    jobs.append(key)
        else:
            key = (fname, None)
            if key not in seen:
                seen.add(key)
                jobs.append(key)
    return jobs


def mark_bestseller_doc(
    doc: dict[str, Any],
    *,
    feed_name: str,
    category_id: str | None,
    synced_at: str,
) -> dict[str, Any]:
    """Attach bestseller tags/markers for ES urls index."""
    out = dict(doc)
    tags = out.get("tags")
    tag_list: list[str] = []
    if isinstance(tags, list):
        tag_list = [str(t) for t in tags if t]
    elif isinstance(tags, str) and tags.strip():
        tag_list = [tags.strip()]
    if BESTSELLER_TAG not in tag_list:
        tag_list.append(BESTSELLER_TAG)
    out["tags"] = tag_list
    out["is_bestseller"] = True
    out["bestseller"] = True
    out["bestseller_feed"] = feed_name
    out["bestseller_synced_at"] = synced_at
    if category_id:
        out["bestseller_category_id"] = str(category_id)
    out["discovery"] = out.get("discovery") or "recommend_feed"
    return out


def sync_bestsellers_from_feed(
    service: FeedService,
    *,
    jobs: list[tuple[str, str | None]],
    pages_per_job: int = 200,
    page_size: int = 50,
    sort: str = "volumeDesc",
    country: str = "US",
    on_batch: Any | None = None,
) -> dict[str, Any]:
    """Pull feed pages, yield/upsert marked url docs. Returns stats + docs buffer via on_batch.

    on_batch(docs) is called periodically (caller upserts to ES).
    """
    synced_at = utc_now_iso()
    stats = {
        "jobs": len(jobs),
        "api_calls": 0,
        "products_seen": 0,
        "unique_ids": 0,
        "synced_at": synced_at,
    }
    global_seen: set[str] = set()
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        if on_batch is not None:
            on_batch(buffer)
        buffer = []

    for fname, cid in jobs:
        for page in range(1, max(int(pages_per_job), 1) + 1):
            payload = service.recommend(
                feed_name=fname,
                category_id=cid,
                country=country,
                page_no=page,
                page_size=page_size,
                sort=sort,
            )
            stats["api_calls"] += 1
            products = extract_products(payload)
            meta = extract_feed_meta(payload)
            if not products:
                break
            for product in products:
                doc = product_to_url_doc(product, feed_name=fname, category_id=cid)
                if not doc:
                    continue
                stats["products_seen"] += 1
                pid = doc["product_id"]
                if pid in global_seen:
                    continue
                global_seen.add(pid)
                stats["unique_ids"] += 1
                marked = mark_bestseller_doc(
                    doc, feed_name=fname, category_id=cid, synced_at=synced_at
                )
                # Keep ES payload smaller
                slim = {k: v for k, v in marked.items() if k != "raw_feed"}
                buffer.append(slim)
                if len(buffer) >= 100:
                    flush()
            if meta.get("is_finished"):
                break
        flush()

    flush()
    stats["unique_ids"] = len(global_seen)
    return stats


def scroll_bestseller_urls(
    es: Elasticsearch,
    urls_index: str,
    *,
    source_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    fields = source_fields or [
        "product_id",
        "url",
        "source",
        "price",
        "rating",
        "reviews",
        "sold_count",
        "category",
        "bestseller_feed",
        "bestseller_synced_at",
        "is_bestseller",
        "tags",
    ]
    query = {
        "bool": {
            "should": [
                {"term": {"is_bestseller": True}},
                {"term": {"bestseller": True}},
                {"term": {"tags.keyword": BESTSELLER_TAG}},
                {"term": {"tags": BESTSELLER_TAG}},
                {"term": {"discovery.keyword": "recommend_feed"}},
                {"term": {"discovery": "recommend_feed"}},
            ],
            "minimum_should_match": 1,
        }
    }
    return list(
        scroll_sources(es, urls_index, source_fields=fields, page_size=2000, query=query)
    )


def partition_fetched(
    bestsellers: list[dict[str, Any]],
    existing_product_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (missing_from_products, already_fetched)."""
    missing: list[dict[str, Any]] = []
    fetched: list[dict[str, Any]] = []
    for doc in bestsellers:
        pid = str(doc.get("product_id") or "").strip()
        if not pid:
            continue
        if pid in existing_product_ids:
            fetched.append(doc)
        else:
            missing.append(doc)
    return missing, fetched


def _bucket_counts(values: list[float | int | None], edges: list[float]) -> list[tuple[str, int]]:
    """Histogram: (-inf, e0), [e0,e1), ... [e_last, +inf)."""
    labels: list[str] = []
    counts = [0] * (len(edges) + 1)
    labels.append(f"<{edges[0]:g}")
    for i in range(len(edges) - 1):
        labels.append(f"{edges[i]:g}–{edges[i+1]:g}")
    labels.append(f"≥{edges[-1]:g}")
    for v in values:
        if v is None:
            continue
        placed = False
        for i, edge in enumerate(edges):
            if float(v) < float(edge):
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return list(zip(labels, counts, strict=True))


def distribution_report(
    docs: list[dict[str, Any]],
    *,
    missing_count: int,
    fetched_count: int,
    enqueued: int,
    synced_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build rating / reviews / sold_count distribution for bestseller urls."""
    ratings: list[float | None] = []
    reviews: list[int | None] = []
    solds: list[int | None] = []
    missing_rating = missing_reviews = missing_sold = 0
    for d in docs:
        r = _to_float(d.get("rating"), default=None)
        rev = _to_int(d.get("reviews"))
        sold = _to_int(d.get("sold_count"))
        ratings.append(r)
        reviews.append(rev)
        solds.append(sold)
        if r is None:
            missing_rating += 1
        if rev is None:
            missing_reviews += 1
        if sold is None:
            missing_sold += 1

    present_ratings = [float(x) for x in ratings if x is not None]
    present_reviews = [int(x) for x in reviews if x is not None]
    present_solds = [int(x) for x in solds if x is not None]

    def _pct(n: int, total: int) -> float:
        return round(100.0 * n / total, 2) if total else 0.0

    total = len(docs)
    report = {
        "generated_at": utc_now_iso(),
        "total_bestsellers": total,
        "fetched_in_products": fetched_count,
        "missing_from_products": missing_count,
        "enqueued_missing": enqueued,
        "sync": synced_stats or {},
        "field_coverage": {
            "rating_present": total - missing_rating,
            "rating_missing": missing_rating,
            "rating_present_pct": _pct(total - missing_rating, total),
            "reviews_present": total - missing_reviews,
            "reviews_missing": missing_reviews,
            "reviews_present_pct": _pct(total - missing_reviews, total),
            "sold_count_present": total - missing_sold,
            "sold_count_missing": missing_sold,
            "sold_count_present_pct": _pct(total - missing_sold, total),
        },
        "rating": {
            "count": len(present_ratings),
            "buckets": [
                {"label": lab, "count": c}
                for lab, c in _bucket_counts(present_ratings, [3.0, 4.0, 4.4, 4.6, 4.8, 5.0])
            ],
            "gte_4_4": sum(1 for x in present_ratings if x >= 4.4),
            "gte_4_6": sum(1 for x in present_ratings if x >= 4.6),
        },
        "reviews": {
            "count": len(present_reviews),
            "buckets": [
                {"label": lab, "count": c}
                for lab, c in _bucket_counts(
                    present_reviews, [1, 10, 50, 100, 300, 500, 1000, 5000]
                )
            ],
            "gte_300": sum(1 for x in present_reviews if x >= 300),
            "gte_1000": sum(1 for x in present_reviews if x >= 1000),
        },
        "sold_count": {
            "count": len(present_solds),
            "buckets": [
                {"label": lab, "count": c}
                for lab, c in _bucket_counts(
                    present_solds, [10, 50, 100, 500, 1000, 5000, 10000]
                )
            ],
            "gte_500": sum(1 for x in present_solds if x >= 500),
            "gte_1000": sum(1 for x in present_solds if x >= 1000),
        },
        "quality_gate_approx": {
            "note": "urls-index fields only; feed often lacks reviews",
            "price_lt_100_rating_gte_4_4_sold_gte_500": sum(
                1
                for d in docs
                if (_to_float(d.get("price"), None) is not None)
                and float(d["price"]) < 100
                and (_to_float(d.get("rating"), None) or 0) >= 4.4
                and (_to_int(d.get("sold_count")) or 0) >= 500
            ),
            "plus_reviews_gte_300": sum(
                1
                for d in docs
                if (_to_float(d.get("price"), None) is not None)
                and float(d["price"]) < 100
                and (_to_float(d.get("rating"), None) or 0) >= 4.4
                and (_to_int(d.get("sold_count")) or 0) >= 500
                and (_to_int(d.get("reviews")) or 0) >= 300
            ),
        },
        "by_feed": dict(Counter(str(d.get("bestseller_feed") or "unknown") for d in docs)),
    }
    return report


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Bestsellers daily report",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- total_bestsellers: **{report.get('total_bestsellers')}**",
        f"- fetched_in_products: **{report.get('fetched_in_products')}**",
        f"- missing_from_products: **{report.get('missing_from_products')}**",
        f"- enqueued_missing (priority): **{report.get('enqueued_missing')}**",
        "",
        "## Sync",
        "```json",
        json.dumps(report.get("sync") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Field coverage (urls index)",
        "```json",
        json.dumps(report.get("field_coverage") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Rating distribution",
        "```json",
        json.dumps(report.get("rating") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Reviews distribution",
        "```json",
        json.dumps(report.get("reviews") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Sold_count distribution",
        "```json",
        json.dumps(report.get("sold_count") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Quality gate (approx, urls fields)",
        "```json",
        json.dumps(report.get("quality_gate_approx") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## By feed",
        "```json",
        json.dumps(report.get("by_feed") or {}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = reports_dir / f"bestsellers_{day}.json"
    md_path = reports_dir / f"bestsellers_{day}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report_markdown(report), encoding="utf-8")
    # also latest aliases
    (reports_dir / "bestsellers_latest.json").write_text(
        json_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (reports_dir / "bestsellers_latest.md").write_text(
        md_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return json_path, md_path


def make_feed_service(client: IopClient | None = None) -> FeedService:
    return FeedService(client=client or IopClient())


def upsert_marked_batch(es: Elasticsearch, urls_index: str, docs: list[dict[str, Any]]) -> int:
    return upsert_url_docs(es, urls_index, docs)
