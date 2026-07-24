#!/usr/bin/env python3
"""Bestsellers + catalog report with blacklist-excluded export thresholds.

Run on mongo VPS::

    cd /home/Admin/aliexpress-ds && set -a && source .env && set +a
    .venv/bin/python scripts/bestsellers_export_report.py
"""

from __future__ import annotations

import json
import math
import os
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _round(v: float | None, nd: int = 2) -> float | None:
    if v is None:
        return None
    return round(float(v), nd)


def pct_row(vals: list[float]) -> dict[str, float | None]:
    s = sorted(vals)
    return {
        "n": len(s),
        "p25": _round(_percentile(s, 25)),
        "p50": _round(_percentile(s, 50)),
        "p75": _round(_percentile(s, 75)),
        "p90": _round(_percentile(s, 90)),
        "mean": _round(sum(s) / len(s), 2) if s else None,
        "min": _round(min(s), 2) if s else None,
        "max": _round(max(s), 2) if s else None,
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


def load_url_meta(es, urls_index: str, keywords: tuple[str, ...]) -> dict[str, Any]:
    """Scroll urls → product_id maps for category / bestseller / blacklist."""
    category_by_pid: dict[str, str] = {}
    blacklisted_pids: set[str] = set()
    bestseller_pids: set[str] = set()
    blacklist_hit_kw: Counter = Counter()
    blacklist_cat: Counter = Counter()
    n = 0

    print("Scrolling urls for category / bestseller / blacklist …", flush=True)
    for src in scroll_sources(
        es,
        urls_index,
        source_fields=[
            "product_id",
            "category",
            "is_bestseller",
            "bestseller",
            "tags",
            "bestseller_feed",
        ],
        page_size=5000,
    ):
        n += 1
        if n % 200000 == 0:
            print(f"  urls scrolled: {n}", flush=True)
        pid = str(src.get("product_id") or "").strip()
        if not pid:
            continue
        cat = str(src.get("category") or "").strip()
        if cat and pid not in category_by_pid:
            category_by_pid[pid] = cat

        tags = src.get("tags")
        is_bs = bool(src.get("is_bestseller") or src.get("bestseller"))
        if isinstance(tags, list):
            is_bs = is_bs or ("bestseller" in tags)
        elif tags == "bestseller":
            is_bs = True
        if is_bs:
            bestseller_pids.add(pid)

        if cat:
            hit = text_hits_blacklist(cat, keywords)
            if hit:
                blacklisted_pids.add(pid)
                blacklist_hit_kw[hit] += 1
                blacklist_cat[cat] += 1

    print(
        f"  urls={n} pids_with_cat={len(category_by_pid)} "
        f"bestsellers={len(bestseller_pids)} blacklisted_pids={len(blacklisted_pids)}",
        flush=True,
    )
    return {
        "urls_scrolled": n,
        "category_by_pid": category_by_pid,
        "blacklisted_pids": blacklisted_pids,
        "bestseller_pids": bestseller_pids,
        "blacklist_hit_kw": blacklist_hit_kw,
        "blacklist_cat": blacklist_cat,
    }


def load_product_metrics(
    es,
    products_index: str,
    *,
    want_pids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Scroll products metrics. If want_pids set, only keep those (else all)."""
    out: dict[str, dict[str, Any]] = {}
    n = 0
    print("Scrolling products metrics …", flush=True)
    for src in scroll_sources(
        es,
        products_index,
        source_fields=["product_id", "rating", "reviews", "sold_count", "price"],
        page_size=5000,
    ):
        n += 1
        if n % 100000 == 0:
            print(f"  products scrolled: {n}", flush=True)
        pid = str(src.get("product_id") or "").strip()
        if not pid:
            continue
        if want_pids is not None and pid not in want_pids:
            continue
        out[pid] = {
            "rating": src.get("rating"),
            "reviews": src.get("reviews"),
            "sold_count": src.get("sold_count"),
            "price": src.get("price"),
        }
    print(f"  products kept: {len(out)} (scanned {n})", flush=True)
    return out


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def analyze_cohort(
    metrics: dict[str, dict[str, Any]],
    pids: set[str],
    *,
    blacklisted_pids: set[str],
    exclude_blacklist: bool,
) -> dict[str, Any]:
    ratings: list[float] = []
    reviews: list[int] = []
    solds: list[int] = []
    kept = 0
    skipped_bl = 0
    missing_metrics = 0

    for pid in pids:
        if exclude_blacklist and pid in blacklisted_pids:
            skipped_bl += 1
            continue
        m = metrics.get(pid)
        if not m:
            missing_metrics += 1
            continue
        kept += 1
        r = to_float(m.get("rating"))
        rev = to_int(m.get("reviews"))
        sold = to_int(m.get("sold_count"))
        if r is not None:
            ratings.append(r)
        if rev is not None:
            reviews.append(rev)
        if sold is not None:
            solds.append(sold)

    def gte(vals: list[float] | list[int], thr: float) -> int:
        return sum(1 for x in vals if x >= thr)

    return {
        "exclude_blacklist": exclude_blacklist,
        "cohort_pids": len(pids),
        "skipped_blacklist": skipped_bl,
        "missing_from_products": missing_metrics,
        "kept_in_products": kept,
        "rating": {
            **pct_row(ratings),
            "gte_4_4": gte(ratings, 4.4),
            "gte_4_6": gte(ratings, 4.6),
        },
        "reviews": {
            **pct_row([float(x) for x in reviews]),
            "gte_100": gte(reviews, 100),
            "gte_200": gte(reviews, 200),
            "gte_300": gte(reviews, 300),
            "gte_1000": gte(reviews, 1000),
        },
        "sold_count": {
            **pct_row([float(x) for x in solds]),
            "gte_500": gte(solds, 500),
            "gte_1000": gte(solds, 1000),
        },
    }


def analyze_all_products_export(
    metrics: dict[str, dict[str, Any]],
    blacklisted_pids: set[str],
) -> dict[str, Any]:
    """Export thresholds for full catalog, with/without blacklist."""
    raw = {"gte_100": 0, "gte_200": 0, "gte_300": 0, "total_with_reviews": 0}
    clean = {"gte_100": 0, "gte_200": 0, "gte_300": 0, "total_with_reviews": 0, "excluded_blacklist": 0}

    for pid, m in metrics.items():
        rev = to_int(m.get("reviews"))
        if rev is None:
            continue
        raw["total_with_reviews"] += 1
        if rev >= 100:
            raw["gte_100"] += 1
        if rev >= 200:
            raw["gte_200"] += 1
        if rev >= 300:
            raw["gte_300"] += 1

        if pid in blacklisted_pids:
            clean["excluded_blacklist"] += 1
            continue
        clean["total_with_reviews"] += 1
        if rev >= 100:
            clean["gte_100"] += 1
        if rev >= 200:
            clean["gte_200"] += 1
        if rev >= 300:
            clean["gte_300"] += 1

    return {
        "raw_no_blacklist_filter": raw,
        "excluding_clothing_adult": clean,
        "products_total": len(metrics),
    }


def tightness(share_pct: float) -> str:
    if share_pct >= 80:
        return "很松"
    if share_pct >= 40:
        return "偏松"
    if share_pct >= 15:
        return "中等"
    if share_pct >= 5:
        return "偏紧"
    return "极紧"


def render_md(report: dict[str, Any]) -> str:
    a: list[str] = []
    p = a.append
    bs = report["bestsellers_excluding_blacklist"]
    bs_raw = report["bestsellers_raw"]
    ex = report["export_thresholds"]
    bl = report["blacklist"]
    n_bs = bs["kept_in_products"] or 1

    p("# Bestsellers 指标完整报告（已剔除服装 / 成人用品）")
    p("")
    p(f"- generated_at: `{report['generated_at']}`")
    p(f"- host: `{report['host']}`")
    p(f"- products index: `{report['products_index']}`")
    p(f"- urls index: `{report['urls_index']}`")
    p(
        "- blacklist: `config/category_blacklist.yaml` "
        "（clothing / adult，与 enqueue-es 相同）"
    )
    p("")
    p("## 0. 先回答：≥100 / ≥200 是否已去服装和成人？")
    p("")
    p("**上一版报告里的 89,022 / 49,288 是全库、未剔 blacklist。**")
    p("")
    p("本报告给出 **剔除服装 + 成人用品之后** 的可导出数量：")
    p("")
    p("| 门槛 | 全库 products（已剔 blacklist） | bestsellers 已抓（已剔 blacklist） |")
    p("|------|--------------------------------|-------------------------------------|")
    p(
        f"| reviews ≥ 100 | **{ex['all_excluding_blacklist']['gte_100']}**"
        f"（{_pct(ex['all_excluding_blacklist']['gte_100'], report['products_total'])}%） | "
        f"**{bs['reviews']['gte_100']}** |"
    )
    p(
        f"| reviews ≥ 200 | **{ex['all_excluding_blacklist']['gte_200']}**"
        f"（{_pct(ex['all_excluding_blacklist']['gte_200'], report['products_total'])}%） | "
        f"**{bs['reviews']['gte_200']}** |"
    )
    p("")
    p("对照（未剔 blacklist，便于核对）：")
    p("")
    p("| 门槛 | 全库 raw | bestsellers raw |")
    p("|------|----------|-----------------|")
    p(
        f"| ≥100 | {ex['all_raw']['gte_100']} | {bs_raw['reviews']['gte_100']} |"
    )
    p(
        f"| ≥200 | {ex['all_raw']['gte_200']} | {bs_raw['reviews']['gte_200']} |"
    )
    p("")
    p(
        f"全库因 blacklist 排除的有 reviews 的产品：**"
        f"{ex['all_excluding_blacklist']['excluded_blacklist']}**；"
        f"bestsellers 已抓因 blacklist 排除：**{bs['skipped_blacklist']}**。"
    )
    p("")

    p("## 1. 总览")
    p("")
    p("| 指标 | 数量 |")
    p("|------|------|")
    p(f"| products 全库 | **{report['products_total']}** |")
    p(f"| bestsellers（urls 标记） | **{report['bestsellers_urls']}** |")
    p(
        f"| bestsellers 已抓进 products | **{bs_raw['kept_in_products']}** "
        f"（{_pct(bs_raw['kept_in_products'], report['bestsellers_urls'])}%） |"
    )
    p(
        f"| 其中 blacklist（服装/成人） | **{bs['skipped_blacklist']}** |"
    )
    p(
        f"| bestsellers 已抓且非 blacklist（分析主样本） | **{bs['kept_in_products']}** |"
    )
    p("")

    p("## 2. 百分位怎么读")
    p("")
    p("把商品按该字段从小到大排：")
    p("")
    p("- **p50** = 中位数（一半商品 ≥ 这个值）")
    p("- **p90** = 只有顶部约 10% 超过这个值")
    p("- **p25 / p75** = 下四分位 / 上四分位")
    p("")
    p("### Bestsellers 已抓（已剔服装/成人）百分位")
    p("")
    p("| 字段 | p25 | p50 | p75 | p90 | 含义 |")
    p("|------|-----|-----|-----|-----|------|")
    r, rv, s = bs["rating"], bs["reviews"], bs["sold_count"]
    p(
        f"| rating | {r['p25']} | {r['p50']} | {r['p75']} | {r['p90']} | "
        f"一半 ≥{r['p50']}，整体很高 |"
    )
    p(
        f"| reviews | {rv['p25']} | {rv['p50']} | {rv['p75']} | {rv['p90']} | "
        f"一半评论不到 {rv['p50']}；只有约 10% >{rv['p90']} |"
    )
    p(
        f"| sold_count | {s['p25']} | {s['p50']} | {s['p75']} | {s['p90']} | "
        f"中位销量 {s['p50']}；约 10% >{s['p90']} |"
    )
    p("")
    p(
        f"样本 n：rating={r['n']}，reviews={rv['n']}，sold={s['n']}；"
        f"mean rating={r['mean']}，reviews={rv['mean']}，sold={s['mean']}。"
    )
    p("")

    p("## 3. 「≥ 某阈值」怎么读（单条件，已剔 blacklist）")
    p("")
    p("| 条件 | 数量 | 约占已抓 bestsellers（剔后） | 松紧 |")
    p("|------|------|------------------------------|------|")
    rows = [
        ("rating ≥ 4.4", r["gte_4_4"]),
        ("rating ≥ 4.6", r["gte_4_6"]),
        ("sold ≥ 500", s["gte_500"]),
        ("sold ≥ 1000", s["gte_1000"]),
        ("reviews ≥ 100", rv["gte_100"]),
        ("reviews ≥ 200", rv["gte_200"]),
        ("reviews ≥ 300", rv["gte_300"]),
        ("reviews ≥ 1000", rv["gte_1000"]),
    ]
    for label, cnt in rows:
        share = _pct(cnt, n_bs)
        p(f"| {label} | **{cnt}** | ~{share}% | {tightness(share)} |")
    p("")

    p("## 4. Blacklist 剔除明细")
    p("")
    p("| 指标 | 数量 |")
    p("|------|------|")
    p(f"| urls 命中 blacklist 的 product_id（去重） | **{bl['unique_pids']}** |")
    p(f"| bestsellers 命中 blacklist | **{bl['bestsellers_hit']}** |")
    p(f"| bestsellers 已抓且命中 | **{bs['skipped_blacklist']}** |")
    p("")
    p("### 按命中关键词（urls 行计数，非去重）")
    p("")
    p("| keyword | urls hits |")
    p("|---------|-----------|")
    for kw, c in bl["by_keyword"]:
        p(f"| {kw} | {c} |")
    p("")
    p("### Top blacklisted categories")
    p("")
    p("| category | count |")
    p("|----------|-------|")
    for cat, c in bl["top_categories"]:
        p(f"| {cat} | {c} |")
    p("")

    p("## 5. 结论")
    p("")
    p(
        f"- 导出门槛请看 **已剔 blacklist**："
        f"全库 ≥100 → **{ex['all_excluding_blacklist']['gte_100']}**，"
        f"≥200 → **{ex['all_excluding_blacklist']['gte_200']}**；"
        f"bestsellers ≥100 → **{rv['gte_100']}**，"
        f"≥200 → **{rv['gte_200']}**。"
    )
    p(
        f"- Bestsellers（剔后）评分很高（p50={r['p50']}），"
        f"但 reviews 仍偏紧（p50={rv['p50']}，p90={rv['p90']}）；"
        f"reviews 是最紧的单条件过滤。"
    )
    p(
        f"- sold 中位 {s['p50']}；sold≥500 约占 {_pct(s['gte_500'], n_bs)}%。"
    )
    p("")
    return "\n".join(a)


def main() -> int:
    settings = get_settings()
    es = make_es_client(settings)
    products_index = settings.es_products_index
    urls_index = settings.es_urls_index

    keywords = load_blacklist_keywords(
        blacklist_file=settings.enqueue_category_blacklist_file or "",
        env_keywords=os.environ.get("ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS", ""),
    )
    print(f"blacklist keywords={len(keywords)}", flush=True)

    meta = load_url_meta(es, urls_index, keywords)
    blacklisted_pids: set[str] = meta["blacklisted_pids"]
    bestseller_pids: set[str] = meta["bestseller_pids"]

    # Need all products for full-catalog export thresholds after blacklist
    metrics = load_product_metrics(es, products_index, want_pids=None)

    products_total = len(metrics)
    print("Analyzing cohorts …", flush=True)

    bs_raw = analyze_cohort(
        metrics,
        bestseller_pids,
        blacklisted_pids=blacklisted_pids,
        exclude_blacklist=False,
    )
    bs_clean = analyze_cohort(
        metrics,
        bestseller_pids,
        blacklisted_pids=blacklisted_pids,
        exclude_blacklist=True,
    )
    export = analyze_all_products_export(metrics, blacklisted_pids)

    bl_bs_hit = len(bestseller_pids & blacklisted_pids)
    report = {
        "generated_at": utc_now_iso(),
        "host": socket.gethostname(),
        "products_index": products_index,
        "urls_index": urls_index,
        "products_total": products_total,
        "bestsellers_urls": len(bestseller_pids),
        "blacklist": {
            "unique_pids": len(blacklisted_pids),
            "bestsellers_hit": bl_bs_hit,
            "by_keyword": list(meta["blacklist_hit_kw"].most_common()),
            "top_categories": list(meta["blacklist_cat"].most_common(20)),
            "keywords": list(keywords),
        },
        "bestsellers_raw": bs_raw,
        "bestsellers_excluding_blacklist": bs_clean,
        "export_thresholds": {
            "all_raw": export["raw_no_blacklist_filter"],
            "all_excluding_blacklist": export["excluding_clothing_adult"],
        },
    }

    md = render_md(report)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    reports_dir = _ROOT / "reports"
    docs_dir = _ROOT / "docs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    json_text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    (reports_dir / f"bestsellers_export_report_{day}.md").write_text(md, encoding="utf-8")
    (reports_dir / f"bestsellers_export_report_{day}.json").write_text(json_text, encoding="utf-8")
    (reports_dir / "bestsellers_export_report_latest.md").write_text(md, encoding="utf-8")
    (reports_dir / "bestsellers_export_report_latest.json").write_text(json_text, encoding="utf-8")
    (docs_dir / "bestsellers_export_report.md").write_text(md, encoding="utf-8")

    print(md)
    print(f"\nWrote docs/bestsellers_export_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
