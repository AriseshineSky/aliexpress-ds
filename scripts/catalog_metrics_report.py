#!/usr/bin/env python3
"""Catalog metrics report: rating/reviews/sold distributions, export thresholds, categories.

Run on mongo VPS (near ES) for speed::

    cd /home/Admin/aliexpress-ds
    set -a && source .env && set +a
    .venv/bin/python scripts/catalog_metrics_report.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repo src is importable when run as scripts/*.py
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from aliexpress_ds.category_blacklist import (  # noqa: E402
    load_blacklist_keywords,
    text_hits_blacklist,
)
from aliexpress_ds.config import get_settings  # noqa: E402
from aliexpress_ds.es import make_es_client, scroll_sources  # noqa: E402


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 2) if total else 0.0


def _round_pct_vals(vals: dict[str, float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k, v in vals.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out[k] = None
        else:
            out[k] = round(float(v), 4)
    return out


def percentile_table(aggs: dict[str, Any]) -> dict[str, Any]:
    values = aggs.get("values") or {}
    # ES returns keys like "50.0"
    mapped = {
        "min": values.get("0.0"),
        "p25": values.get("25.0"),
        "p50": values.get("50.0"),
        "p75": values.get("75.0"),
        "p90": values.get("90.0"),
        "p99": values.get("99.0"),
        "max": values.get("100.0"),
    }
    return _round_pct_vals(mapped)


def range_bucket_counts(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"label": b["key"], "count": b["doc_count"]} for b in buckets]


def field_coverage(es, index: str, field: str, total: int) -> dict[str, Any]:
    present = es.count(index=index, query={"exists": {"field": field}})["count"]
    return {
        "present": present,
        "missing": total - present,
        "present_pct": _pct(present, total),
    }


def metric_aggs(
    es,
    index: str,
    *,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body_query = query or {"match_all": {}}
    resp = es.search(
        index=index,
        size=0,
        query=body_query,
        aggs={
            "rating_pct": {
                "percentiles": {
                    "field": "rating",
                    "percents": [0, 25, 50, 75, 90, 99, 100],
                }
            },
            "reviews_pct": {
                "percentiles": {
                    "field": "reviews",
                    "percents": [0, 25, 50, 75, 90, 99, 100],
                }
            },
            "sold_pct": {
                "percentiles": {
                    "field": "sold_count",
                    "percents": [0, 25, 50, 75, 90, 99, 100],
                }
            },
            "rating_stats": {"stats": {"field": "rating"}},
            "reviews_stats": {"stats": {"field": "reviews"}},
            "sold_stats": {"stats": {"field": "sold_count"}},
            "rating_ranges": {
                "range": {
                    "field": "rating",
                    "ranges": [
                        {"key": "<3", "to": 3.0},
                        {"key": "3–4", "from": 3.0, "to": 4.0},
                        {"key": "4–4.4", "from": 4.0, "to": 4.4},
                        {"key": "4.4–4.6", "from": 4.4, "to": 4.6},
                        {"key": "4.6–4.8", "from": 4.6, "to": 4.8},
                        {"key": "4.8–5", "from": 4.8, "to": 5.0},
                        {"key": "≥5", "from": 5.0},
                    ],
                }
            },
            "reviews_ranges": {
                "range": {
                    "field": "reviews",
                    "ranges": [
                        {"key": "<1", "to": 1},
                        {"key": "1–10", "from": 1, "to": 10},
                        {"key": "10–50", "from": 10, "to": 50},
                        {"key": "50–100", "from": 50, "to": 100},
                        {"key": "100–200", "from": 100, "to": 200},
                        {"key": "200–300", "from": 200, "to": 300},
                        {"key": "300–500", "from": 300, "to": 500},
                        {"key": "500–1000", "from": 500, "to": 1000},
                        {"key": "1000–5000", "from": 1000, "to": 5000},
                        {"key": "≥5000", "from": 5000},
                    ],
                }
            },
            "sold_ranges": {
                "range": {
                    "field": "sold_count",
                    "ranges": [
                        {"key": "<10", "to": 10},
                        {"key": "10–50", "from": 10, "to": 50},
                        {"key": "50–100", "from": 50, "to": 100},
                        {"key": "100–500", "from": 100, "to": 500},
                        {"key": "500–1000", "from": 500, "to": 1000},
                        {"key": "1000–5000", "from": 1000, "to": 5000},
                        {"key": "5000–10000", "from": 5000, "to": 10000},
                        {"key": "≥10000", "from": 10000},
                    ],
                }
            },
            "rating_gte_4_4": {"filter": {"range": {"rating": {"gte": 4.4}}}},
            "rating_gte_4_6": {"filter": {"range": {"rating": {"gte": 4.6}}}},
            "reviews_gte_100": {"filter": {"range": {"reviews": {"gte": 100}}}},
            "reviews_gte_200": {"filter": {"range": {"reviews": {"gte": 200}}}},
            "reviews_gte_300": {"filter": {"range": {"reviews": {"gte": 300}}}},
            "reviews_gte_1000": {"filter": {"range": {"reviews": {"gte": 1000}}}},
            "sold_gte_500": {"filter": {"range": {"sold_count": {"gte": 500}}}},
            "sold_gte_1000": {"filter": {"range": {"sold_count": {"gte": 1000}}}},
        },
    )
    a = resp["aggregations"]
    matched = resp["hits"]["total"]["value"] if isinstance(resp["hits"]["total"], dict) else resp["hits"]["total"]
    return {
        "matched": matched,
        "coverage": {
            "rating": field_coverage(es, index, "rating", matched) if query is None else None,
            "reviews": field_coverage(es, index, "reviews", matched) if query is None else None,
            "sold_count": field_coverage(es, index, "sold_count", matched) if query is None else None,
        },
        "rating": {
            "stats": {
                "count": int(a["rating_stats"]["count"]),
                "min": a["rating_stats"]["min"],
                "max": a["rating_stats"]["max"],
                "mean": round(a["rating_stats"]["avg"] or 0, 4) if a["rating_stats"]["count"] else None,
            },
            "percentiles": percentile_table(a["rating_pct"]),
            "buckets": range_bucket_counts(a["rating_ranges"]["buckets"]),
            "gte_4_4": a["rating_gte_4_4"]["doc_count"],
            "gte_4_6": a["rating_gte_4_6"]["doc_count"],
        },
        "reviews": {
            "stats": {
                "count": int(a["reviews_stats"]["count"]),
                "min": a["reviews_stats"]["min"],
                "max": a["reviews_stats"]["max"],
                "mean": round(a["reviews_stats"]["avg"] or 0, 4) if a["reviews_stats"]["count"] else None,
            },
            "percentiles": percentile_table(a["reviews_pct"]),
            "buckets": range_bucket_counts(a["reviews_ranges"]["buckets"]),
            "gte_100": a["reviews_gte_100"]["doc_count"],
            "gte_200": a["reviews_gte_200"]["doc_count"],
            "gte_300": a["reviews_gte_300"]["doc_count"],
            "gte_1000": a["reviews_gte_1000"]["doc_count"],
        },
        "sold_count": {
            "stats": {
                "count": int(a["sold_stats"]["count"]),
                "min": a["sold_stats"]["min"],
                "max": a["sold_stats"]["max"],
                "mean": round(a["sold_stats"]["avg"] or 0, 4) if a["sold_stats"]["count"] else None,
            },
            "percentiles": percentile_table(a["sold_pct"]),
            "buckets": range_bucket_counts(a["sold_ranges"]["buckets"]),
            "gte_500": a["sold_gte_500"]["doc_count"],
            "gte_1000": a["sold_gte_1000"]["doc_count"],
        },
    }


def bestseller_query() -> dict[str, Any]:
    return {
        "bool": {
            "should": [
                {"term": {"is_bestseller": True}},
                {"term": {"bestseller": True}},
                {"term": {"tags.keyword": "bestseller"}},
                {"term": {"tags": "bestseller"}},
            ],
            "minimum_should_match": 1,
        }
    }


def load_bestseller_ids(es, urls_index: str) -> tuple[list[str], Counter, int]:
    """Return (doc_ids like aliexpress.us_XXX, by_feed, total)."""
    ids: list[str] = []
    feeds: Counter = Counter()
    for src in scroll_sources(
        es,
        urls_index,
        source_fields=["product_id", "bestseller_feed", "is_bestseller"],
        page_size=3000,
        query=bestseller_query(),
    ):
        pid = str(src.get("product_id") or "").strip()
        if not pid:
            continue
        ids.append(f"aliexpress.us_{pid}")
        feeds[str(src.get("bestseller_feed") or "unknown")] += 1
    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq, feeds, len(uniq)


def ids_exist_in_products(es, products_index: str, doc_ids: list[str]) -> list[str]:
    """Return subset of doc_ids that exist in products index."""
    existing: list[str] = []
    batch = 5000
    for i in range(0, len(doc_ids), batch):
        chunk = doc_ids[i : i + batch]
        resp = es.mget(index=products_index, ids=chunk, _source=False)
        for doc in resp.get("docs", []):
            if doc.get("found"):
                existing.append(doc["_id"])
    return existing


def metrics_for_ids(es, products_index: str, doc_ids: list[str]) -> dict[str, Any]:
    """Run metric aggregations filtered to document ids (chunked + merge manually for thresholds).

    For large id sets, use terms/_id filter in chunks and merge counts/buckets; percentiles
    from a single ids query when size allows.
    """
    if not doc_ids:
        return {"matched": 0}
    # ES terms/_id can take large lists; chunk if needed and use one pass with bool should
    # Prefer single query with ids filter (supports large lists better than terms).
    # Cap per query to avoid huge payloads.
    if len(doc_ids) <= 20000:
        return metric_aggs(
            es,
            products_index,
            query={"ids": {"values": doc_ids}},
        )

    # Chunked: collect raw values via scroll for accurate percentiles
    ratings: list[float] = []
    reviews: list[int] = []
    solds: list[int] = []
    batch = 5000
    for i in range(0, len(doc_ids), batch):
        chunk = doc_ids[i : i + batch]
        for src in scroll_sources(
            es,
            products_index,
            source_fields=["rating", "reviews", "sold_count"],
            page_size=2000,
            query={"ids": {"values": chunk}},
        ):
            if src.get("rating") is not None:
                try:
                    ratings.append(float(src["rating"]))
                except (TypeError, ValueError):
                    pass
            if src.get("reviews") is not None:
                try:
                    reviews.append(int(src["reviews"]))
                except (TypeError, ValueError):
                    pass
            if src.get("sold_count") is not None:
                try:
                    solds.append(int(src["sold_count"]))
                except (TypeError, ValueError):
                    pass
    return _metrics_from_lists(ratings, reviews, solds)


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def _bucket(vals: list[float], edges: list[float], labels: list[str]) -> list[dict[str, Any]]:
    counts = [0] * len(labels)
    for v in vals:
        placed = False
        for i, edge in enumerate(edges):
            if v < edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return [{"label": lab, "count": c} for lab, c in zip(labels, counts, strict=True)]


def _metrics_from_lists(
    ratings: list[float], reviews: list[int], solds: list[int]
) -> dict[str, Any]:
    rs = sorted(ratings)
    revs = sorted(float(x) for x in reviews)
    ss = sorted(float(x) for x in solds)

    def pct_block(vals: list[float]) -> dict[str, float | None]:
        return {
            "min": _percentile(vals, 0),
            "p25": _percentile(vals, 25),
            "p50": _percentile(vals, 50),
            "p75": _percentile(vals, 75),
            "p90": _percentile(vals, 90),
            "p99": _percentile(vals, 99),
            "max": _percentile(vals, 100),
        }

    return {
        "matched": max(len(ratings), len(reviews), len(solds)),
        "rating": {
            "stats": {
                "count": len(ratings),
                "min": min(ratings) if ratings else None,
                "max": max(ratings) if ratings else None,
                "mean": round(sum(ratings) / len(ratings), 4) if ratings else None,
            },
            "percentiles": pct_block(rs),
            "buckets": _bucket(
                ratings,
                [3.0, 4.0, 4.4, 4.6, 4.8, 5.0],
                ["<3", "3–4", "4–4.4", "4.4–4.6", "4.6–4.8", "4.8–5", "≥5"],
            ),
            "gte_4_4": sum(1 for x in ratings if x >= 4.4),
            "gte_4_6": sum(1 for x in ratings if x >= 4.6),
        },
        "reviews": {
            "stats": {
                "count": len(reviews),
                "min": min(reviews) if reviews else None,
                "max": max(reviews) if reviews else None,
                "mean": round(sum(reviews) / len(reviews), 4) if reviews else None,
            },
            "percentiles": pct_block(revs),
            "buckets": _bucket(
                [float(x) for x in reviews],
                [1, 10, 50, 100, 200, 300, 500, 1000, 5000],
                [
                    "<1",
                    "1–10",
                    "10–50",
                    "50–100",
                    "100–200",
                    "200–300",
                    "300–500",
                    "500–1000",
                    "1000–5000",
                    "≥5000",
                ],
            ),
            "gte_100": sum(1 for x in reviews if x >= 100),
            "gte_200": sum(1 for x in reviews if x >= 200),
            "gte_300": sum(1 for x in reviews if x >= 300),
            "gte_1000": sum(1 for x in reviews if x >= 1000),
        },
        "sold_count": {
            "stats": {
                "count": len(solds),
                "min": min(solds) if solds else None,
                "max": max(solds) if solds else None,
                "mean": round(sum(solds) / len(solds), 4) if solds else None,
            },
            "percentiles": pct_block(ss),
            "buckets": _bucket(
                [float(x) for x in solds],
                [10, 50, 100, 500, 1000, 5000, 10000],
                [
                    "<10",
                    "10–50",
                    "50–100",
                    "100–500",
                    "500–1000",
                    "1000–5000",
                    "5000–10000",
                    "≥10000",
                ],
            ),
            "gte_500": sum(1 for x in solds if x >= 500),
            "gte_1000": sum(1 for x in solds if x >= 1000),
        },
    }


def top_level_category(category: str) -> str:
    """Normalize url category to top-level bucket."""
    c = (category or "").strip()
    if not c:
        return "(empty)"
    # Formats: "US / Beauty & Health > ..." or "Home & Garden > ..." or "Men's Clothing"
    if c.startswith("US /"):
        c = c[4:].strip()
    if ">" in c:
        c = c.split(">", 1)[0].strip()
    return c or "(empty)"


def analyze_categories(
    es,
    urls_index: str,
    products_index: str,
    *,
    blacklist_file: str = "",
) -> dict[str, Any]:
    print("Loading product id set …", flush=True)
    product_ids: set[str] = set()
    for src in scroll_sources(
        es,
        products_index,
        source_fields=["product_id"],
        page_size=5000,
    ):
        pid = str(src.get("product_id") or "").strip()
        if pid:
            product_ids.add(pid)
    print(f"  products with product_id: {len(product_ids)}", flush=True)

    keywords = load_blacklist_keywords(
        blacklist_file=blacklist_file,
        env_keywords=os.environ.get("ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS", ""),
    )
    print(f"  blacklist keywords: {len(keywords)}", flush=True)

    urls_by_cat: Counter = Counter()
    products_by_cat: Counter = Counter()
    urls_by_top: Counter = Counter()
    products_by_top: Counter = Counter()
    blacklist_urls = 0
    blacklist_products = 0
    blacklist_by_keyword: Counter = Counter()
    blacklist_products_by_keyword: Counter = Counter()
    blacklist_by_cat: Counter = Counter()
    urls_with_cat = 0
    products_with_cat_via_urls = 0

    print("Scrolling urls for category distribution …", flush=True)
    n = 0
    for src in scroll_sources(
        es,
        urls_index,
        source_fields=["product_id", "category"],
        page_size=5000,
    ):
        n += 1
        if n % 200000 == 0:
            print(f"  scrolled urls: {n}", flush=True)
        cat = str(src.get("category") or "").strip()
        if not cat:
            continue
        urls_with_cat += 1
        top = top_level_category(cat)
        urls_by_cat[cat] += 1
        urls_by_top[top] += 1

        hit = text_hits_blacklist(cat, keywords)
        if hit:
            blacklist_urls += 1
            blacklist_by_keyword[hit] += 1
            blacklist_by_cat[cat] += 1

        pid = str(src.get("product_id") or "").strip()
        if pid and pid in product_ids:
            products_with_cat_via_urls += 1
            products_by_cat[cat] += 1
            products_by_top[top] += 1
            if hit:
                blacklist_products += 1
                blacklist_products_by_keyword[hit] += 1

    # Also products.categories field (low coverage)
    print("Aggregating products.categories.keyword (sparse) …", flush=True)
    cats_field_exists = es.count(
        index=products_index, query={"exists": {"field": "categories"}}
    )["count"]
    cats_field_top = es.search(
        index=products_index,
        size=0,
        query={"exists": {"field": "categories"}},
        aggs={"top": {"terms": {"field": "categories.keyword", "size": 50}}},
    )
    products_categories_field = {
        "present": cats_field_exists,
        "top": [
            {"category": b["key"], "count": b["doc_count"]}
            for b in cats_field_top["aggregations"]["top"]["buckets"]
        ],
    }

    def top_n(counter: Counter, n: int = 40) -> list[dict[str, Any]]:
        return [{"category": k, "count": v} for k, v in counter.most_common(n)]

    return {
        "urls_total_scrolled": n,
        "urls_with_category": urls_with_cat,
        "products_joined_via_urls_category": products_with_cat_via_urls,
        "blacklist": {
            "keywords": list(keywords),
            "urls_hit": blacklist_urls,
            "urls_hit_pct": _pct(blacklist_urls, urls_with_cat),
            "products_hit": blacklist_products,
            "products_hit_pct": _pct(blacklist_products, products_with_cat_via_urls),
            "by_keyword_urls": dict(blacklist_by_keyword.most_common()),
            "by_keyword_products": dict(blacklist_products_by_keyword.most_common()),
            "top_blacklisted_categories_urls": top_n(blacklist_by_cat, 30),
        },
        "urls_by_category_top": top_n(urls_by_cat, 40),
        "products_by_category_top": top_n(products_by_cat, 40),
        "urls_by_top_level": top_n(urls_by_top, 40),
        "products_by_top_level": top_n(products_by_top, 40),
        "products_categories_field": products_categories_field,
        "unique_url_categories": len(urls_by_cat),
        "unique_product_categories": len(products_by_cat),
        "unique_url_top_levels": len(urls_by_top),
        "unique_product_top_levels": len(products_by_top),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Catalog metrics report")
    a("")
    a(f"- generated_at: `{report['generated_at']}`")
    a(f"- host: `{report.get('host', '')}`")
    a(
        f"- indices: `{report['products_index']}` (products), "
        f"`{report['urls_index']}` (urls)"
    )
    a("")

    # Overview
    ov = report["overview"]
    a("## 1. 总览")
    a("")
    a("| 指标 | 数量 |")
    a("|------|------|")
    a(f"| products 全库 | **{ov['products_total']}** |")
    a(f"| urls 全库 | **{ov['urls_total']}** |")
    a(f"| bestsellers（urls 标记） | **{ov['bestsellers_urls']}** |")
    a(
        f"| bestsellers 已抓进 products | **{ov['bestsellers_in_products']}** "
        f"（{_pct(ov['bestsellers_in_products'], ov['bestsellers_urls'])}%） |"
    )
    a(f"| bestsellers 尚未抓取 | **{ov['bestsellers_missing']}** |")
    a("")

    # Export thresholds
    ex = report["export_thresholds"]
    a("## 2. 可导出数量（reviews 门槛）")
    a("")
    a("以 **products 索引** `reviews` 字段为准。")
    a("")
    a("| 范围 | 全库 products | bestsellers（已抓） |")
    a("|------|---------------|---------------------|")
    a(
        f"| reviews ≥ 100 | **{ex['all_products']['reviews_gte_100']}** | "
        f"**{ex['bestsellers_fetched']['reviews_gte_100']}** |"
    )
    a(
        f"| reviews ≥ 200 | **{ex['all_products']['reviews_gte_200']}** | "
        f"**{ex['bestsellers_fetched']['reviews_gte_200']}** |"
    )
    a(
        f"| reviews ≥ 300 | {ex['all_products']['reviews_gte_300']} | "
        f"{ex['bestsellers_fetched']['reviews_gte_300']} |"
    )
    a("")
    a(
        f"全库占比：≥100 = {_pct(ex['all_products']['reviews_gte_100'], ov['products_total'])}%；"
        f" ≥200 = {_pct(ex['all_products']['reviews_gte_200'], ov['products_total'])}%。"
    )
    a("")

    def section_metrics(title: str, m: dict[str, Any]) -> None:
        a(f"## {title}")
        a("")
        if not m or not m.get("matched"):
            a("_无数据_")
            a("")
            return
        cov = m.get("coverage") or {}
        if cov and cov.get("rating"):
            a("### 字段覆盖")
            a("")
            a("| 字段 | 有值 | 缺失 | 覆盖率 |")
            a("|------|------|------|--------|")
            for field in ("rating", "reviews", "sold_count"):
                c = cov.get(field) or {}
                a(
                    f"| {field} | {c.get('present')} | {c.get('missing')} | "
                    f"{c.get('present_pct')}% |"
                )
            a("")

        for key, label in (
            ("rating", "Rating"),
            ("reviews", "Reviews"),
            ("sold_count", "Sold count"),
        ):
            block = m[key]
            st = block["stats"]
            pct = block["percentiles"]
            a(f"### {label}")
            a("")
            a(
                f"n={st['count']}，mean={st.get('mean')}，"
                f"min={st.get('min')}，max={st.get('max')}"
            )
            a("")
            a("| min | p25 | p50 | p75 | p90 | p99 | max |")
            a("|-----|-----|-----|-----|-----|-----|-----|")
            a(
                f"| {pct.get('min')} | {pct.get('p25')} | {pct.get('p50')} | "
                f"{pct.get('p75')} | {pct.get('p90')} | {pct.get('p99')} | "
                f"{pct.get('max')} |"
            )
            a("")
            a("| 区间 | 数量 |")
            a("|------|------|")
            for b in block["buckets"]:
                a(f"| {b['label']} | {b['count']} |")
            a("")
            if key == "rating":
                a(f"- `gte_4_4`: **{block.get('gte_4_4')}**")
                a(f"- `gte_4_6`: **{block.get('gte_4_6')}**")
            elif key == "reviews":
                a(f"- `gte_100`: **{block.get('gte_100')}**")
                a(f"- `gte_200`: **{block.get('gte_200')}**")
                a(f"- `gte_300`: **{block.get('gte_300')}**")
                a(f"- `gte_1000`: **{block.get('gte_1000')}**")
            else:
                a(f"- `gte_500`: **{block.get('gte_500')}**")
                a(f"- `gte_1000`: **{block.get('gte_1000')}**")
            a("")

    section_metrics("3. Bestsellers（已抓进 products）分布", report["bestsellers_products"])
    a("### By feed（urls 标记）")
    a("")
    a("```json")
    a(json.dumps(report["bestsellers_by_feed"], indent=2, ensure_ascii=False))
    a("```")
    a("")

    section_metrics("4. 全库 products 分布", report["all_products"])

    # Blacklist
    cat = report["categories"]
    bl = cat["blacklist"]
    a("## 5. Blacklist category 产品数量")
    a("")
    a(
        "匹配规则：urls.`category` 对 `config/category_blacklist.yaml` 关键词 "
        "（与 enqueue-es 相同）。已抓产品 = urls 有 category 且 product_id 在 products 中。"
    )
    a("")
    a("| 指标 | 数量 |")
    a("|------|------|")
    a(f"| urls 命中 blacklist | **{bl['urls_hit']}**（{bl['urls_hit_pct']}% of urls w/ category） |")
    a(
        f"| 已抓 products 命中 blacklist | **{bl['products_hit']}**"
        f"（{bl['products_hit_pct']}% of joined） |"
    )
    a("")
    a("### 按命中关键词（urls）")
    a("")
    a("| keyword | urls | products |")
    a("|---------|------|----------|")
    all_kw = set(bl["by_keyword_urls"]) | set(bl["by_keyword_products"])
    for kw in sorted(all_kw, key=lambda k: -bl["by_keyword_urls"].get(k, 0)):
        a(
            f"| {kw} | {bl['by_keyword_urls'].get(kw, 0)} | "
            f"{bl['by_keyword_products'].get(kw, 0)} |"
        )
    a("")
    a("### Top blacklisted categories（urls）")
    a("")
    a("| category | count |")
    a("|----------|-------|")
    for row in bl["top_blacklisted_categories_urls"][:20]:
        a(f"| {row['category']} | {row['count']} |")
    a("")

    # Category distribution
    a("## 6. Category 产品数量分布")
    a("")
    a(
        f"- urls 有 category: **{cat['urls_with_category']}** "
        f"（unique leaf categories: {cat['unique_url_categories']}）"
    )
    a(
        f"- 已抓 products 经 urls.category 关联: **{cat['products_joined_via_urls_category']}** "
        f"（unique: {cat['unique_product_categories']}）"
    )
    a(
        f"- products.`categories` 字段有值（稀疏）: "
        f"**{cat['products_categories_field']['present']}**"
    )
    a("")

    a("### Top-level category（已抓 products）")
    a("")
    a("| top-level | count |")
    a("|-----------|-------|")
    for row in cat["products_by_top_level"][:30]:
        a(f"| {row['category']} | {row['count']} |")
    a("")

    a("### Top-level category（urls 全量）")
    a("")
    a("| top-level | count |")
    a("|-----------|-------|")
    for row in cat["urls_by_top_level"][:30]:
        a(f"| {row['category']} | {row['count']} |")
    a("")

    a("### Leaf category Top 30（已抓 products）")
    a("")
    a("| category | count |")
    a("|----------|-------|")
    for row in cat["products_by_category_top"][:30]:
        a(f"| {row['category']} | {row['count']} |")
    a("")

    a("## 7. 结论（简要）")
    a("")
    bp = report["bestsellers_products"]
    ap = report["all_products"]
    if bp and bp.get("reviews"):
        a(
            f"- Bestsellers 已抓评分中位 **{bp['rating']['percentiles'].get('p50')}**，"
            f"reviews 中位 **{bp['reviews']['percentiles'].get('p50')}**，"
            f"sold 中位 **{bp['sold_count']['percentiles'].get('p50')}**。"
        )
    if ap and ap.get("reviews"):
        a(
            f"- 全库 products reviews 中位 **{ap['reviews']['percentiles'].get('p50')}**，"
            f"≥100 可导出 **{ex['all_products']['reviews_gte_100']}**，"
            f"≥200 可导出 **{ex['all_products']['reviews_gte_200']}**。"
        )
    a(
        f"- Blacklist 已抓产品 **{bl['products_hit']}** / "
        f"urls **{bl['urls_hit']}**。"
    )
    a("")
    return "\n".join(lines)


def main() -> int:
    import socket

    settings = get_settings()
    es = make_es_client(settings)
    products_index = settings.es_products_index
    urls_index = settings.es_urls_index

    print(f"ES products={products_index} urls={urls_index}", flush=True)
    products_total = es.count(index=products_index)["count"]
    urls_total = es.count(index=urls_index)["count"]
    print(f"counts products={products_total} urls={urls_total}", flush=True)

    print("Aggregating all products metrics …", flush=True)
    all_products = metric_aggs(es, products_index)
    # fill coverage properly
    all_products["coverage"] = {
        "rating": field_coverage(es, products_index, "rating", products_total),
        "reviews": field_coverage(es, products_index, "reviews", products_total),
        "sold_count": field_coverage(es, products_index, "sold_count", products_total),
    }
    all_products["matched"] = products_total

    print("Loading bestsellers from urls …", flush=True)
    bs_ids, by_feed, bs_total = load_bestseller_ids(es, urls_index)
    print(f"  bestsellers unique: {bs_total}", flush=True)
    print("Intersecting with products …", flush=True)
    bs_in_products = ids_exist_in_products(es, products_index, bs_ids)
    print(f"  bestsellers in products: {len(bs_in_products)}", flush=True)
    print("Aggregating bestsellers products metrics …", flush=True)
    bestsellers_products = metrics_for_ids(es, products_index, bs_in_products)

    print("Analyzing categories + blacklist …", flush=True)
    categories = analyze_categories(
        es,
        urls_index,
        products_index,
        blacklist_file=settings.enqueue_category_blacklist_file or "",
    )

    report: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "host": socket.gethostname(),
        "products_index": products_index,
        "urls_index": urls_index,
        "overview": {
            "products_total": products_total,
            "urls_total": urls_total,
            "bestsellers_urls": bs_total,
            "bestsellers_in_products": len(bs_in_products),
            "bestsellers_missing": bs_total - len(bs_in_products),
        },
        "bestsellers_by_feed": dict(by_feed.most_common()),
        "export_thresholds": {
            "all_products": {
                "reviews_gte_100": all_products["reviews"]["gte_100"],
                "reviews_gte_200": all_products["reviews"]["gte_200"],
                "reviews_gte_300": all_products["reviews"]["gte_300"],
            },
            "bestsellers_fetched": {
                "reviews_gte_100": bestsellers_products.get("reviews", {}).get("gte_100", 0),
                "reviews_gte_200": bestsellers_products.get("reviews", {}).get("gte_200", 0),
                "reviews_gte_300": bestsellers_products.get("reviews", {}).get("gte_300", 0),
            },
        },
        "bestsellers_products": bestsellers_products,
        "all_products": all_products,
        "categories": categories,
    }

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    reports_dir = _ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = _ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    md = render_markdown(report)
    json_text = json.dumps(report, indent=2, ensure_ascii=False)

    for directory in (reports_dir, docs_dir):
        # docs only gets the markdown/json canonical names
        pass

    (reports_dir / f"catalog_metrics_report_{day}.json").write_text(json_text, encoding="utf-8")
    (reports_dir / f"catalog_metrics_report_{day}.md").write_text(md, encoding="utf-8")
    (reports_dir / "catalog_metrics_report_latest.json").write_text(json_text, encoding="utf-8")
    (reports_dir / "catalog_metrics_report_latest.md").write_text(md, encoding="utf-8")
    (docs_dir / "catalog_metrics_report.md").write_text(md, encoding="utf-8")

    print(md[:2000])
    print(f"\nWrote reports/catalog_metrics_report_{day}.md and docs/catalog_metrics_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
