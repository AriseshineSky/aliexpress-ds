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
    # First-seen only written on insert / if missing (upsert_url_docs preserves it).
    out["bestseller_first_seen_at"] = out.get("bestseller_first_seen_at") or synced_at
    out["updated_at"] = synced_at
    if not out.get("created_at"):
        out["created_at"] = synced_at
    if not out.get("scraped_at"):
        out["scraped_at"] = synced_at
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
        "bestseller_first_seen_at",
        "created_at",
        "updated_at",
        "scraped_at",
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


def _product_id_set(docs: list[dict[str, Any]]) -> set[str]:
    return {
        str(d.get("product_id") or "").strip()
        for d in docs
        if str(d.get("product_id") or "").strip()
    }


def build_ids_snapshot(docs: list[dict[str, Any]], *, day: str | None = None) -> dict[str, Any]:
    """Compact daily snapshot of bestseller product_ids for day-over-day diffs."""
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    ids = sorted(_product_id_set(docs))
    by_feed: dict[str, list[str]] = {}
    for d in docs:
        pid = str(d.get("product_id") or "").strip()
        if not pid:
            continue
        feed = str(d.get("bestseller_feed") or d.get("feed_name") or "unknown")
        by_feed.setdefault(feed, []).append(pid)
    for feed, pids in by_feed.items():
        by_feed[feed] = sorted(set(pids))
    return {
        "day": day,
        "generated_at": utc_now_iso(),
        "count": len(ids),
        "product_ids": ids,
        "by_feed": {k: {"count": len(v), "product_ids": v} for k, v in sorted(by_feed.items())},
    }


def write_ids_snapshot(
    docs: list[dict[str, Any]],
    reports_dir: Path,
    *,
    day: str | None = None,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    snap = build_ids_snapshot(docs, day=day)
    day = str(snap["day"])
    path = reports_dir / f"bestsellers_ids_{day}.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    (reports_dir / "bestsellers_ids_latest.json").write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return path


def load_ids_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def find_previous_ids_snapshot(reports_dir: Path, day: str) -> Path | None:
    """Find newest bestsellers_ids_YYYYMMDD.json strictly before ``day``."""
    if not reports_dir.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for p in reports_dir.glob("bestsellers_ids_????????.json"):
        stem = p.stem  # bestsellers_ids_YYYYMMDD
        parts = stem.rsplit("_", 1)
        if len(parts) != 2 or len(parts[1]) != 8 or not parts[1].isdigit():
            continue
        if parts[1] < day:
            candidates.append((parts[1], p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def diff_id_snapshots(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    cur_ids = set(str(x) for x in (current.get("product_ids") or []) if str(x).strip())
    prev_ids = set(str(x) for x in ((previous or {}).get("product_ids") or []) if str(x).strip())
    entered = sorted(cur_ids - prev_ids)
    exited = sorted(prev_ids - cur_ids)
    retained = sorted(cur_ids & prev_ids)
    return {
        "day": current.get("day"),
        "prev_day": (previous or {}).get("day"),
        "generated_at": utc_now_iso(),
        "current_count": len(cur_ids),
        "previous_count": len(prev_ids),
        "entered_count": len(entered),
        "exited_count": len(exited),
        "retained_count": len(retained),
        "net_change": len(cur_ids) - len(prev_ids),
        "entered_sample": entered[:50],
        "exited_sample": exited[:50],
        "entered": entered,
        "exited": exited,
    }


def render_changes_markdown(diff: dict[str, Any]) -> str:
    lines = [
        "# Bestsellers day-over-day changes",
        "",
        f"- day: `{diff.get('day')}` vs prev `{diff.get('prev_day') or 'none'}`",
        f"- generated_at: `{diff.get('generated_at')}`",
        f"- current_count: **{diff.get('current_count')}**",
        f"- previous_count: **{diff.get('previous_count')}**",
        f"- entered: **{diff.get('entered_count')}**",
        f"- exited: **{diff.get('exited_count')}**",
        f"- retained: **{diff.get('retained_count')}**",
        f"- net_change: **{diff.get('net_change')}**",
        "",
        "## Entered (sample ≤50)",
        "```",
        "\n".join(diff.get("entered_sample") or []) or "(none)",
        "```",
        "",
        "## Exited (sample ≤50)",
        "```",
        "\n".join(diff.get("exited_sample") or []) or "(none)",
        "```",
        "",
    ]
    return "\n".join(lines)


def write_changes_report(
    docs: list[dict[str, Any]],
    reports_dir: Path,
    *,
    day: str | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Write ids snapshot + day-over-day changes (json/md). Returns paths + diff."""
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    snap_path = write_ids_snapshot(docs, reports_dir, day=day)
    snap = load_ids_snapshot(snap_path) or {}
    prev_path = find_previous_ids_snapshot(reports_dir, day)
    prev = load_ids_snapshot(prev_path) if prev_path else None
    diff = diff_id_snapshots(snap, prev)
    if prev_path:
        diff["previous_snapshot"] = str(prev_path)
    json_path = reports_dir / f"bestsellers_changes_{day}.json"
    md_path = reports_dir / f"bestsellers_changes_{day}.md"
    # Drop full id lists from the markdown-facing json size? Keep full in json.
    json_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_changes_markdown(diff), encoding="utf-8")
    (reports_dir / "bestsellers_changes_latest.json").write_text(
        json_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (reports_dir / "bestsellers_changes_latest.md").write_text(
        md_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return snap_path, json_path, md_path, diff


def make_feed_service(client: IopClient | None = None) -> FeedService:
    return FeedService(client=client or IopClient())


def upsert_marked_batch(es: Elasticsearch, urls_index: str, docs: list[dict[str, Any]]) -> int:
    return upsert_url_docs(es, urls_index, docs)
