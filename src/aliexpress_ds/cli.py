from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from aliexpress_ds.config import get_settings
from aliexpress_ds.es import (
    EsProductBuffer,
    iter_missing_urls,
    load_existing_product_ids,
    make_es_client,
    scroll_sources,
    upsert_url_docs,
)
from aliexpress_ds.iop_client import IopClient, IopError
from aliexpress_ds.product import ProductService
from aliexpress_ds.rate_limit import DailyQuotaExhausted
from aliexpress_ds.url_parser import extract_product_id

BEIJING = timezone(timedelta(hours=8))

app = typer.Typer(
    add_completion=False,
    help="AliExpress Dropshipping helper: product detail / price / stock from URL",
)
console = Console()
err_console = Console(stderr=True)


def _load_done_ids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("ok") is False:
                continue
            pid = str(obj.get("product_id") or "").strip()
            if not pid and isinstance(obj.get("product"), dict):
                pid = str(obj["product"].get("product_id") or "").strip()
            if pid:
                done.add(pid)
    return done


def _auth_fatal(message: str) -> bool:
    msg = message.lower()
    needles = (
        "incomplete signature",
        "invalid signature",
        "access token",
        "sessionkey",
        "session key",
        "missing credentials",
        "unauthorized",
        "invalidappkey",
    )
    return any(n in msg for n in needles)


# Everymarket quality gates (same defaults as em-tasks prepare_upload --quality-filter).
DEFAULT_MAX_PRICE = 100.0
DEFAULT_MIN_RATING = 4.4
DEFAULT_MIN_REVIEWS = 1000
DEFAULT_MIN_SOLD_COUNT = 1000


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def product_meets_quality(
    product: dict,
    *,
    max_price: float = DEFAULT_MAX_PRICE,
    min_rating: float = DEFAULT_MIN_RATING,
    min_reviews: int = DEFAULT_MIN_REVIEWS,
    min_sold_count: int = DEFAULT_MIN_SOLD_COUNT,
) -> bool:
    price = _as_float(product.get("price"))
    rating = _as_float(product.get("rating"))
    reviews = _as_int(product.get("reviews"))
    sold = _as_int(product.get("sold_count"))
    if price is None or price >= max_price:
        return False
    if rating is None or rating < min_rating:
        return False
    if reviews is None or reviews < min_reviews:
        return False
    if sold is None or sold < min_sold_count:
        return False
    return True


def _load_pending_from_urls_file(path: Path) -> list[dict]:
    """Load product_id/url rows from JSONL (e.g. quality_priority_urls.jsonl)."""
    rows: list[dict] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = str(obj.get("product_id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            url = str(obj.get("url") or "").strip() or f"https://www.aliexpress.us/item/{pid}.html"
            rows.append(
                {
                    "product_id": pid,
                    "url": url,
                    "source": obj.get("source") or "aliexpress.us",
                    "title": obj.get("title"),
                    "category": obj.get("category"),
                }
            )
    return rows


def _write_product_row(
    *,
    summary,
    item: dict,
    include_raw: bool,
    ship_to_country: str = "US",
    fetch_shipping: bool = False,
    fetch_categories: bool = False,
    client=None,
) -> dict:
    from aliexpress_ds.enrich import enrich_product
    from aliexpress_ds.standard_mapper import to_standard_product

    raw = summary.raw or {}
    product = to_standard_product(
        raw,
        url=item["url"],
        source=item.get("source") or item.get("es_source"),
    )
    if fetch_shipping or fetch_categories:
        product = enrich_product(
            product,
            raw_payload=raw,
            item=item,
            ship_to_country=ship_to_country,
            fetch_shipping=fetch_shipping,
            fetch_categories=fetch_categories,
            client=client,
        )
    row = {
        "ok": True,
        "product_id": product.get("product_id") or summary.product_id,
        "url": item["url"],
        "source": item.get("source"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "product": product,
    }
    if include_raw:
        row["raw"] = raw
    return row


async def _awrite_product_row(
    *,
    summary,
    item: dict,
    include_raw: bool,
    ship_to_country: str = "US",
    fetch_shipping: bool = False,
    fetch_categories: bool = False,
    client=None,
) -> dict:
    from aliexpress_ds.enrich import aenrich_product
    from aliexpress_ds.standard_mapper import to_standard_product

    raw = summary.raw or {}
    product = to_standard_product(
        raw,
        url=item["url"],
        source=item.get("source") or item.get("es_source"),
    )
    if fetch_shipping or fetch_categories:
        product = await aenrich_product(
            product,
            raw_payload=raw,
            item=item,
            ship_to_country=ship_to_country,
            fetch_shipping=fetch_shipping,
            fetch_categories=fetch_categories,
            client=client,
        )
    row = {
        "ok": True,
        "product_id": product.get("product_id") or summary.product_id,
        "url": item["url"],
        "source": item.get("source"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "product": product,
    }
    if include_raw:
        row["raw"] = raw
    return row


@app.command("parse-id")
def parse_id(url: str = typer.Argument(..., help="AliExpress product URL or numeric id")) -> None:
    """Only extract product_id from a URL."""
    console.print(extract_product_id(url))


@app.command("get")
def get_product(
    url: str = typer.Argument(..., help="Product URL or product_id"),
    country: str = typer.Option("US", "--country", "-c", help="ship_to_country / local_country"),
    currency: str = typer.Option("USD", "--currency", help="target_currency"),
    language: str = typer.Option("EN", "--language", "-l", help="target_language"),
    raw: bool = typer.Option(True, "--raw/--no-raw", help="Include full API JSON in output"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write JSON file"),
) -> None:
    """Fetch product detail; emit StandardProduct (+ raw API payload)."""
    from aliexpress_ds.standard_mapper import to_standard_product

    try:
        summary = ProductService().get_by_url(
            url,
            ship_to_country=country,
            target_currency=currency,
            target_language=language,
            local_country=country,
            local_language=language.lower(),
        )
        product = to_standard_product(
            summary.raw,
            url=url if "://" in url else f"https://www.aliexpress.us/item/{summary.product_id}.html",
        )
    except (ValueError, IopError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        if isinstance(exc, IopError) and exc.body is not None:
            err_console.print_json(data=exc.body)
        raise typer.Exit(code=1) from exc

    payload = {
        "ok": True,
        "product_id": product.get("product_id") or summary.product_id,
        "product": product,
    }
    if raw:
        payload["raw"] = summary.raw

    if output:
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"Wrote {output}")

    console.print(f"[bold]{product.get('product_id')}[/bold]  {product.get('title') or ''}")
    console.print(
        f"price={product.get('price')} {product.get('currency')}  "
        f"qty={product.get('available_qty')}  "
        f"variants={len(product.get('variants') or [])}  "
        f"default_only={product.get('has_only_default_variant')}"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("SKU")
    table.add_column("sku_price")
    table.add_column("offer_sale_price")
    table.add_column("available_stock")
    for sku in summary.skus:
        stock = sku.sku_available_stock if sku.sku_available_stock is not None else sku.ipm_sku_stock
        if stock is None:
            stock = sku.sku_stock
        table.add_row(
            sku.sku_id or "-",
            sku.sku_price or "-",
            sku.offer_sale_price or "-",
            str(stock) if stock is not None else "-",
        )
    console.print(table)


@app.command("config-check")
def config_check() -> None:
    """Verify .env credentials are present (optionally loads Redis token)."""
    settings = get_settings()
    try:
        settings.require_credentials()
        from aliexpress_ds.token_store import (
            access_token_expired,
            ensure_fresh_token,
            load_token_from_redis,
            resolve_access_token,
        )

        token = resolve_access_token(settings)
        data = load_token_from_redis(settings)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    from aliexpress_ds.app_registry import REDIS_APPS_KEY, load_app_from_redis

    app_meta = load_app_from_redis(settings.aliexpress_app_key, settings=settings)
    console.print("[green]Credentials OK[/green]")
    console.print(f"app_key={settings.aliexpress_app_key}")
    if app_meta and app_meta.get("label"):
        console.print(f"app_label={app_meta['label']}")
    console.print(
        f"app_secret_source={'redis:' + REDIS_APPS_KEY if app_meta else 'env'}"
    )
    console.print(f"api_url={settings.aliexpress_api_url}")
    console.print(f"oauth_redis={'yes' if settings.redis_url else 'no'}")
    if settings.redis_url:
        from aliexpress_ds.token_store import redis_token_key

        console.print(f"oauth_token_key={redis_token_key(settings)}")
    console.print(f"queue_redis={'yes' if settings.redis_queue_url else 'no'}")
    console.print(f"token=…{token[-6:]}")
    if data:
        if data.get("account"):
            console.print(f"account={data.get('account')}")
        console.print(f"expires_at={data.get('expires_at')}")
        console.print(f"refresh_expires_at={data.get('refresh_expires_at')}")
        console.print(f"access_expired_soon={access_token_expired(data)}")


@app.command("refresh-token")
def refresh_token_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        help="Call /auth/token/refresh even if access_token is still valid",
    ),
) -> None:
    """Ensure Upstash REDIS_URL token is fresh (auto-refresh when near expiry)."""
    from aliexpress_ds.token_store import (
        ensure_fresh_token,
        load_token_from_redis,
        refresh_access_token,
        refresh_token_expired,
    )

    settings = get_settings()
    try:
        settings.require_credentials()
        if not (settings.redis_url or "").strip():
            raise ValueError("REDIS_URL missing (Upstash rediss://…)")
        data = load_token_from_redis(settings)
        if not data:
            raise ValueError(
                "No token in Redis. Open "
                "https://aliexpress-oauth.onrender.com/oauth/authorize first."
            )
        if force:
            rt = str(data.get("refresh_token") or "").strip()
            if not rt:
                raise ValueError("No refresh_token in Redis")
            if refresh_token_expired(data):
                raise ValueError("refresh_token expired — re-authorize")
            data = refresh_access_token(rt, settings=settings, previous=data)
        else:
            data = ensure_fresh_token(settings)
    except (ValueError, RuntimeError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print("[green]Token OK[/green]")
    console.print(f"access_token=…{(data.get('access_token') or '')[-6:]}")
    console.print(f"expires_at={data.get('expires_at')}")
    console.print(f"refresh_expires_at={data.get('refresh_expires_at')}")


@app.command("feed-names")
def feed_names(
    bestsellers_only: bool = typer.Option(
        False,
        "--bestsellers-only",
        help="Only print feed names containing 'bestseller'",
    ),
    limit: int = typer.Option(40, "--limit", help="Max rows to print (0 = all)"),
) -> None:
    """List available DS recommend feed / promo names (aliexpress.ds.feedname.get)."""
    from aliexpress_ds.feed import FeedService, parse_feed_promos
    from aliexpress_ds.iop_client import IopError

    try:
        payload = FeedService().list_feed_names()
    except (ValueError, IopError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        if isinstance(exc, IopError) and exc.body is not None:
            err_console.print_json(data=exc.body)
        raise typer.Exit(code=1) from exc

    rows = parse_feed_promos(payload)
    if bestsellers_only:
        rows = [r for r in rows if "bestseller" in r["name"].lower()]
    console.print(f"feeds={len(rows)}")
    show = rows if limit <= 0 else rows[:limit]
    for row in show:
        n = row.get("product_num")
        console.print(f"  {(n if n is not None else '-'):>8}  {row['name']}")
    if limit > 0 and len(rows) > limit:
        console.print(f"  … {len(rows) - limit} more (use --limit 0)")


@app.command("categories")
def categories(
    sync_es: bool = typer.Option(
        False,
        "--sync-es",
        help="Also write flattened tree + crawl seeds into ES (same as sync-categories)",
    ),
) -> None:
    """Print DS category tree (optionally sync to Elasticsearch)."""
    if sync_es:
        sync_categories()
        return

    from aliexpress_ds.categories import CategoryService, flatten_category_tree
    from aliexpress_ds.iop_client import IopError

    try:
        payload = CategoryService().fetch_raw()
    except (ValueError, IopError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        if isinstance(exc, IopError) and exc.body is not None:
            err_console.print_json(data=exc.body)
        raise typer.Exit(code=1) from exc

    rows = flatten_category_tree(payload)
    console.print(f"Flattened categories: {len(rows)}")
    for row in rows[:30]:
        console.print(f"  L{row['level']} {row['category_id']:>12}  {row['path']}")
    if len(rows) > 30:
        console.print(f"  … {len(rows) - 30} more")


@app.command("sync-categories")
def sync_categories(
    yaml_out: Optional[Path] = typer.Option(
        None,
        "--yaml-out",
        help="Also write crawl seeds YAML (link-crawler compatible)",
    ),
    priority_only: bool = typer.Option(
        True,
        "--priority-only/--all-homepage",
        help="Crawl seeds: Everymarket priority list vs all PRIORITY+extras",
    ),
) -> None:
    """Fetch aliexpress.ds.category.get → ES tree + calp crawl seeds for link-crawler.

    Indices (defaults):
      - ES_CATEGORIES_INDEX       full DS category tree
      - ES_CRAWL_CATEGORIES_INDEX calp-plus seeds consumed by aliexpress-link-crawler
    """
    from aliexpress_ds.categories import (
        PRIORITY_CRAWL_NAMES,
        CategoryService,
        build_crawl_targets,
        flatten_category_tree,
    )
    from aliexpress_ds.es import ensure_index, upsert_crawl_category_seeds, upsert_docs
    from aliexpress_ds.iop_client import IopError

    settings = get_settings()
    try:
        settings.require_credentials()
        settings.require_es()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        payload = CategoryService().fetch_raw()
    except (ValueError, IopError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        if isinstance(exc, IopError) and exc.body is not None:
            err_console.print_json(data=exc.body)
        raise typer.Exit(code=1) from exc

    tree_rows = flatten_category_tree(payload)
    crawl_rows = build_crawl_targets(ds_rows=tree_rows, names=PRIORITY_CRAWL_NAMES)

    es = make_es_client(settings)
    ensure_index(es, settings.es_categories_index)
    ensure_index(es, settings.es_crawl_categories_index)

    n_tree = upsert_docs(
        es,
        settings.es_categories_index,
        tree_rows,
        id_field="category_id",
    )
    n_crawl = upsert_crawl_category_seeds(
        es,
        settings.es_crawl_categories_index,
        crawl_rows,
        id_field="name",
    )

    if yaml_out:
        lines = [
            "# Generated by aliexpress-ds sync-categories — calp-plus crawl seeds.",
            "# Consumed by aliexpress-link-crawler (or use ES_CRAWL_CATEGORIES_INDEX).",
            "categories:",
        ]
        for row in crawl_rows:
            lines.append(f"  - name: {row['name']}")
            lines.append(f"    url: {row['url']}")
            if row.get("ds_category_id"):
                lines.append(f"    ds_category_id: {row['ds_category_id']}")
            lines.append("")
        yaml_out.parent.mkdir(parents=True, exist_ok=True)
        yaml_out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        console.print(f"Wrote YAML → {yaml_out}")

    matched = sum(1 for r in crawl_rows if r.get("ds_category_id"))
    console.print(
        f"[green]Synced[/green] tree={n_tree}→{settings.es_categories_index}  "
        f"crawl_seeds={n_crawl}→{settings.es_crawl_categories_index}  "
        f"ds_id_matched={matched}/{len(crawl_rows)}"
    )
    for row in crawl_rows:
        console.print(
            f"  [{row['priority']:02d}] {row['display_name']}"
            f"  ds_id={row.get('ds_category_id') or '-'}"
        )


@app.command("discover-feed")
def discover_feed(
    feed_name: str = typer.Option(
        "AEB_Droplo_BestsellersItems_20241016",
        "--feed-name",
        "-f",
        help="Promo/feed name from `feed-names` (not the dead 'DS bestseller')",
    ),
    category_id: Optional[str] = typer.Option(
        None,
        "--category-id",
        "-C",
        help="DS category_id filter (works well with Droplo bestsellers)",
    ),
    pages: int = typer.Option(
        20,
        "--pages",
        "-p",
        help="Max pages (stops early when is_finished / empty)",
    ),
    page_size: int = typer.Option(50, "--page-size", help="1-50"),
    sort: str = typer.Option(
        "volumeDesc",
        "--sort",
        help="priceAsc|priceDesc|volumeAsc|volumeDesc|DSRratingAsc|DSRratingDesc|...",
    ),
    country: str = typer.Option("US", "--country", "-c"),
    write_es: bool = typer.Option(
        True,
        "--write-es/--no-write-es",
        help="Upsert discovered product URLs into ES_URLS_INDEX",
    ),
    output: Path = typer.Option(
        Path("data/discovered_urls.jsonl"),
        "--output",
        "-o",
    ),
) -> None:
    """Discover products via DS recommend feed (NOT free-text keyword search).

    Official Dropshipping discovery: aliexpress.ds.recommend.feed.get

    Example — bestsellers under Beauty (category 66), paginate up to 50 pages::

        uv run aliexpress-ds discover-feed -f AEB_Droplo_BestsellersItems_20241016 \\
          -C 66 -p 50 --sort volumeDesc --write-es
    """
    from aliexpress_ds.feed import (
        FeedService,
        extract_feed_meta,
        extract_products,
        product_to_url_doc,
    )
    from aliexpress_ds.rate_limit import RateLimiter

    settings = get_settings()
    try:
        settings.require_credentials()
        if write_es:
            settings.require_es()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(
        min_interval_sec=settings.aliexpress_min_interval_sec,
        daily_limit=settings.aliexpress_daily_limit,
        state_path=Path("data/rate_limit_state.json"),
    )
    service = FeedService(client=IopClient(limiter=limiter))
    docs: list[dict] = []
    seen: set[str] = set()

    with output.open("a", encoding="utf-8") as fh:
        for page in range(1, max(pages, 1) + 1):
            try:
                payload = service.recommend(
                    feed_name=feed_name,
                    category_id=category_id,
                    country=country,
                    page_no=page,
                    page_size=page_size,
                    sort=sort,
                )
            except (ValueError, IopError, DailyQuotaExhausted, RuntimeError) as exc:
                err_console.print(f"[red]page {page}:[/red] {exc}")
                if isinstance(exc, IopError) and exc.body is not None:
                    err_console.print_json(data=exc.body)
                break

            products = extract_products(payload)
            meta = extract_feed_meta(payload)
            console.print(
                f"page {page}: {len(products)} products "
                f"(total≈{meta.get('total_record_count')} finished={meta.get('is_finished')})"
            )
            if not products:
                break
            for product in products:
                doc = product_to_url_doc(
                    product, feed_name=feed_name, category_id=category_id
                )
                if not doc or doc["product_id"] in seen:
                    continue
                seen.add(doc["product_id"])
                docs.append(doc)
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            if meta.get("is_finished"):
                break

    indexed = 0
    if write_es and docs:
        es = make_es_client(settings)
        indexed = upsert_url_docs(es, settings.es_urls_index, docs)

    console.print(
        f"[green]Done[/green] discovered={len(docs)} es_indexed={indexed} → {output}"
    )


@app.command("discover-feed-loop")
def discover_feed_loop(
    feed_name: Optional[list[str]] = typer.Option(
        None,
        "--feed-name",
        "-f",
        help="Feed/promo name (repeatable). Default: Droplo bestsellers + DS_*_bestsellers",
    ),
    category_id: Optional[list[str]] = typer.Option(
        None,
        "--category-id",
        "-C",
        help="Category id to filter (repeatable). Default: priority L1 list",
    ),
    priority_categories: bool = typer.Option(
        True,
        "--priority-categories/--no-priority-categories",
        help="When -C omitted, use PRIORITY_FEED_CATEGORY_IDS (Automotive/Beauty/…)",
    ),
    bestsellers_only: bool = typer.Option(
        True,
        "--bestsellers-only/--all-default-feeds",
        help="If no -f: include only default bestseller feed names",
    ),
    pages_per_job: int = typer.Option(
        200,
        "--pages-per-job",
        "-p",
        help="Max pages per (feed, category) pair; stops early on is_finished/empty",
    ),
    page_size: int = typer.Option(50, "--page-size", help="1-50"),
    sort: str = typer.Option("volumeDesc", "--sort"),
    country: str = typer.Option("US", "--country", "-c"),
    min_interval: float = typer.Option(
        2.0,
        "--min-interval",
        help="Seconds between API calls",
    ),
    flush_every: int = typer.Option(100, "--flush-every"),
    checkpoint: Path = typer.Option(
        Path("data/discover_feed_checkpoint.json"),
        "--checkpoint",
    ),
    write_es: bool = typer.Option(True, "--write-es/--no-write-es"),
    output: Path = typer.Option(
        Path("data/discovered_feed_urls.jsonl"),
        "--output",
        "-o",
    ),
) -> None:
    """Paginate recommend.feed across feeds × categories → ES urls (补漏).

    Meaning: official curated pools (bestsellers etc.) filtered by category_id,
    page_no=1..N until finished. Complements text.search and link-crawler.

    Example::

        uv run aliexpress-ds discover-feed-loop --pages-per-job 100 --write-es
        uv run aliexpress-ds discover-feed-loop -f AEB_Droplo_BestsellersItems_20241016 -C 66 -C 34
    """
    from aliexpress_ds.feed import (
        DEFAULT_BESTSELLER_FEEDS,
        PRIORITY_FEED_CATEGORY_IDS,
        FeedService,
        extract_feed_meta,
        extract_products,
        product_to_url_doc,
    )
    from aliexpress_ds.rate_limit import RateLimiter

    settings = get_settings()
    try:
        settings.require_credentials()
        if write_es:
            settings.require_es()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    feeds = [f.strip() for f in (feed_name or []) if f and f.strip()]
    if not feeds:
        feeds = list(DEFAULT_BESTSELLER_FEEDS) if bestsellers_only else list(
            DEFAULT_BESTSELLER_FEEDS
        )

    cats: list[str | None]
    if category_id:
        cats = [c.strip() for c in category_id if c and str(c).strip()]
    elif priority_categories:
        cats = list(dict.fromkeys(PRIORITY_FEED_CATEGORY_IDS.values()))
    else:
        cats = [None]

    # Jobs: for named DS_*_bestsellers feeds, category filter is optional (feed
    # already scoped). Still run once with category=None. For Droplo (huge pool),
    # always pair with each category_id.
    jobs: list[tuple[str, str | None]] = []
    for fname in feeds:
        is_droplo = "droplo" in fname.lower() or fname.startswith("AEB_")
        if is_droplo:
            for cid in cats:
                jobs.append((fname, cid))
        else:
            # category-scoped DS_* feed — no extra category_id needed
            jobs.append((fname, None))
            # also try priority cats if user explicitly passed -C
            if category_id:
                for cid in cats:
                    jobs.append((fname, cid))

    # de-dupe jobs
    seen_jobs: set[tuple[str, str | None]] = set()
    uniq_jobs: list[tuple[str, str | None]] = []
    for job in jobs:
        if job in seen_jobs:
            continue
        seen_jobs.add(job)
        uniq_jobs.append(job)
    jobs = uniq_jobs

    state: dict = {"done_jobs": [], "unique": 0, "api_calls": 0}
    if checkpoint.exists():
        try:
            state.update(json.loads(checkpoint.read_text(encoding="utf-8")))
        except Exception:
            pass
    done_set = {tuple(x) if isinstance(x, list) else tuple(x) for x in state.get("done_jobs") or []}
    # normalize done entries to (feed, cat|None)
    normalized_done: set[tuple[str, str | None]] = set()
    for item in done_set:
        if not item:
            continue
        if len(item) == 1:
            normalized_done.add((str(item[0]), None))
        else:
            normalized_done.add((str(item[0]), item[1] if item[1] is not None else None))

    output.parent.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(
        min_interval_sec=float(min_interval),
        daily_limit=settings.aliexpress_daily_limit,
        state_path=Path("data/rate_limit_state.json"),
    )
    service = FeedService(client=IopClient(limiter=limiter))
    es = make_es_client(settings) if write_es else None
    buffer: list[dict] = []
    global_seen: set[str] = set()
    total_indexed = 0
    total_new = int(state.get("unique") or 0)

    def flush() -> None:
        nonlocal total_indexed, buffer
        if not buffer:
            return
        if es is not None:
            total_indexed += upsert_url_docs(es, settings.es_urls_index, buffer)
        buffer = []

    def save_ckpt() -> None:
        state["unique"] = total_new
        state["api_calls"] = int(state.get("api_calls") or 0)
        state["done_jobs"] = [list(x) for x in sorted(normalized_done, key=lambda t: (t[0], t[1] or ""))]
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    console.print(
        f"discover-feed-loop jobs={len(jobs)} feeds={len(feeds)} "
        f"cats={len(cats)} pages≤{pages_per_job} sort={sort}"
    )

    with output.open("a", encoding="utf-8") as fh:
        for fname, cid in jobs:
            key = (fname, cid)
            if key in normalized_done:
                console.print(f"[dim]skip done[/dim] {fname} cat={cid or '-'}")
                continue
            console.print(f"\n[bold]feed[/bold]={fname}  category={cid or '-'}")
            page_new = 0
            for page in range(1, max(int(pages_per_job), 1) + 1):
                try:
                    payload = service.recommend(
                        feed_name=fname,
                        category_id=cid,
                        country=country,
                        page_no=page,
                        page_size=page_size,
                        sort=sort,
                    )
                except (ValueError, IopError, DailyQuotaExhausted, RuntimeError) as exc:
                    err_console.print(f"[red]{fname} p{page}:[/red] {exc}")
                    if isinstance(exc, DailyQuotaExhausted):
                        flush()
                        save_ckpt()
                        raise typer.Exit(code=2) from exc
                    break

                state["api_calls"] = int(state.get("api_calls") or 0) + 1
                products = extract_products(payload)
                meta = extract_feed_meta(payload)
                kept = 0
                for product in products:
                    doc = product_to_url_doc(product, feed_name=fname, category_id=cid)
                    if not doc:
                        continue
                    pid = doc["product_id"]
                    if pid in global_seen:
                        continue
                    global_seen.add(pid)
                    # slim for ES (drop huge raw if desired — keep for now, upsert handles)
                    slim = {k: v for k, v in doc.items() if k != "raw_feed"}
                    slim["raw_feed"] = {
                        k: product.get(k)
                        for k in (
                            "evaluate_rate",
                            "lastest_volume",
                            "first_level_category_id",
                            "second_level_category_id",
                            "target_sale_price",
                        )
                        if k in product
                    }
                    buffer.append(slim)
                    fh.write(json.dumps(slim, ensure_ascii=False) + "\n")
                    kept += 1
                    total_new += 1
                    page_new += 1
                    if len(buffer) >= flush_every:
                        flush()
                        save_ckpt()

                console.print(
                    f"  p{page}: got={len(products)} new={kept} "
                    f"total≈{meta.get('total_record_count')} "
                    f"finished={meta.get('is_finished')} unique={total_new}"
                )
                if not products or meta.get("is_finished"):
                    break
                if kept == 0 and page > 3:
                    # pagination repeating — stop this job
                    console.print("  [dim]no new ids — next job[/dim]")
                    break

            normalized_done.add(key)
            flush()
            save_ckpt()
            console.print(f"  job done new={page_new}")

    flush()
    save_ckpt()
    console.print(
        f"[green]Done[/green] unique≈{total_new} es_indexed={total_indexed} "
        f"api_calls={state.get('api_calls')} → {output}"
    )


@app.command("bestsellers-daily")
def bestsellers_daily(
    pages_per_job: int = typer.Option(
        200,
        "--pages-per-job",
        "-p",
        help="Max feed pages per (feed, category) during sync",
    ),
    page_size: int = typer.Option(50, "--page-size"),
    sort: str = typer.Option("volumeDesc", "--sort"),
    country: str = typer.Option("US", "--country", "-c"),
    skip_sync: bool = typer.Option(
        False,
        "--skip-sync",
        help="Do not call recommend.feed; only check/enqueue/report from ES tags",
    ),
    enqueue_missing: bool = typer.Option(
        True,
        "--enqueue-missing/--no-enqueue-missing",
        help="RPUSH missing product_ids to Redis queue (priority)",
    ),
    force_enqueue: bool = typer.Option(
        True,
        "--force-enqueue/--no-force-enqueue",
        help="Re-queue even if id is already in Redis seen set",
    ),
    reports_dir: Path = typer.Option(
        Path("reports"),
        "--reports-dir",
        help="Where to write bestsellers_YYYYMMDD.md/json",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Sync/check/report only; do not push Redis",
    ),
) -> None:
    """Daily bestsellers job: sync feed → mark ES → enqueue unfetched → report.

    1) Pull recommend.feed bestsellers (Droplo + DS_*_bestsellers × priority cats)
    2) Upsert urls with is_bestseller / tags=bestseller / bestseller_synced_at
    3) Diff against ES_PRODUCTS_INDEX; missing ids → priority Redis queue (RPUSH)
    4) Write rating/reviews/sold_count distribution report under reports/
    """
    from aliexpress_ds.bestsellers import (
        build_bestseller_jobs,
        distribution_report,
        make_feed_service,
        partition_fetched,
        scroll_bestseller_urls,
        sync_bestsellers_from_feed,
        upsert_marked_batch,
        write_report,
    )
    from aliexpress_ds.queue import get_product_queue
    from aliexpress_ds.rate_limit import RateLimiter

    settings = get_settings()
    try:
        settings.require_es()
        if not skip_sync:
            settings.require_credentials()
        if enqueue_missing and not dry_run:
            settings.require_queue_redis()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    es = make_es_client(settings)
    synced_stats: dict = {"skipped": True} if skip_sync else {}

    if not skip_sync:
        limiter = RateLimiter(
            min_interval_sec=settings.aliexpress_min_interval_sec,
            daily_limit=settings.aliexpress_daily_limit,
            state_path=Path("data/rate_limit_state.json"),
        )
        iop = IopClient(limiter=limiter)
        service = make_feed_service(iop)
        jobs = build_bestseller_jobs()
        console.print(f"Syncing bestsellers: jobs={len(jobs)} pages≤{pages_per_job}")
        indexed = 0

        def _on_batch(docs: list[dict]) -> None:
            nonlocal indexed
            n = upsert_marked_batch(es, settings.es_urls_index, docs)
            indexed += n
            console.print(f"  upserted batch={n} total_indexed≈{indexed}")

        try:
            synced_stats = sync_bestsellers_from_feed(
                service,
                jobs=jobs,
                pages_per_job=pages_per_job,
                page_size=page_size,
                sort=sort,
                country=country,
                on_batch=_on_batch,
            )
        except DailyQuotaExhausted as exc:
            err_console.print(f"[red]daily quota:[/red] {exc}")
            synced_stats["error"] = str(exc)
        synced_stats["es_indexed"] = indexed
        console.print(
            f"[green]Sync done[/green] unique={synced_stats.get('unique_ids')} "
            f"api_calls={synced_stats.get('api_calls')} es_indexed={indexed}"
        )
        try:
            es.indices.refresh(index=settings.es_urls_index)
        except Exception:
            pass

    console.print("Loading bestseller urls from ES …")
    bestsellers = scroll_bestseller_urls(es, settings.es_urls_index)
    console.print(f"Bestsellers tagged in urls: {len(bestsellers)}")

    console.print("Loading existing product_ids from products index …")
    existing = load_existing_product_ids(es, settings.es_products_index)
    missing, fetched = partition_fetched(bestsellers, existing)
    console.print(
        f"Fetched={len(fetched)}  Missing={len(missing)}  "
        f"(products_index size≈{len(existing)})"
    )

    enqueued = 0
    skipped_q = 0
    if enqueue_missing and missing and not dry_run:
        q = get_product_queue(settings)
        # Priority: RPUSH so BRPOP takes these before older LPUSH backlog
        items = [
            {
                "product_id": d.get("product_id"),
                "url": d.get("url"),
                "source": d.get("source") or "aliexpress.us",
                "priority_reason": "bestseller_missing",
                "bestseller_feed": d.get("bestseller_feed"),
            }
            for d in missing
            if d.get("product_id")
        ]
        enqueued, skipped_q = q.enqueue_many(
            items, force=force_enqueue, priority=True
        )
        console.print(
            f"[green]Priority enqueue[/green] new={enqueued} skipped={skipped_q} "
            f"(force={force_enqueue})"
        )
    elif dry_run and missing:
        console.print(f"[dim]dry-run: would priority-enqueue {len(missing)}[/dim]")

    report = distribution_report(
        bestsellers,
        missing_count=len(missing),
        fetched_count=len(fetched),
        enqueued=enqueued,
        synced_stats=synced_stats,
    )
    json_path, md_path = write_report(report, reports_dir)
    console.print(f"Report → {md_path}")
    console.print(f"Report → {json_path}")
    console.print(
        f"[green]Done[/green] bestsellers={len(bestsellers)} "
        f"missing={len(missing)} enqueued={enqueued}"
    )


@app.command("discover-search")
def discover_search(
    category_id: Optional[str] = typer.Option(
        None,
        "--category-id",
        "-C",
        help="DS category_id to search within (from `categories` / sync-categories)",
    ),
    category_name: Optional[str] = typer.Option(
        None,
        "--category-name",
        "-N",
        help="Match category by name/path substring if id unknown (e.g. 'Beauty')",
    ),
    keyword: Optional[list[str]] = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Explicit keyword (repeatable). If omitted, auto-generate from category tree",
    ),
    keywords_file: Optional[Path] = typer.Option(
        None,
        "--keywords-file",
        help="One keyword per line (# comments ok)",
    ),
    selection_name: Optional[str] = typer.Option(
        None,
        "--selection-name",
        help="Optional DS selection pool name (search within that selection)",
    ),
    max_keywords: int = typer.Option(
        40,
        "--max-keywords",
        help="Cap auto-generated keywords (mass discovery)",
    ),
    max_leaves: int = typer.Option(
        60,
        "--max-leaves",
        help="How many child category leaves to expand into keywords",
    ),
    pages: int = typer.Option(
        5,
        "--pages",
        "-p",
        help="Max pages per keyword",
    ),
    page_size: int = typer.Option(20, "--page-size", help="1-50"),
    sort_by: str = typer.Option(
        "orders,desc",
        "--sort-by",
        help="min_price/orders/comments + ,asc|,desc (sort only; not a filter)",
    ),
    country: str = typer.Option("US", "--country", "-c"),
    local: str = typer.Option("en_US", "--local"),
    currency: str = typer.Option("USD", "--currency"),
    choice: bool = typer.Option(
        False,
        "--choice/--no-choice",
        help="API searchExtend: Choice products only (item_tag=choice)",
    ),
    ship_from: Optional[str] = typer.Option(
        None,
        "--ship-from",
        help="API searchExtend: ship_from country code (e.g. CN, US)",
    ),
    free_ship_to: Optional[str] = typer.Option(
        None,
        "--free-ship-to",
        help="API searchExtend: free_ship_to country code (e.g. US)",
    ),
    seller_level: Optional[str] = typer.Option(
        None,
        "--seller-level",
        help="API searchExtend: GOLD or SILVER",
    ),
    seller_online: Optional[str] = typer.Option(
        None,
        "--seller-online",
        help="API searchExtend: seller online within hours (48 or 72)",
    ),
    hot_area: Optional[str] = typer.Option(
        None,
        "--hot-area",
        help="API searchExtend: hot_area (BR/US/UK/GB/FR/AU)",
    ),
    search_extend_json: Optional[str] = typer.Option(
        None,
        "--search-extend-json",
        help='Raw searchExtend JSON list, e.g. \'[{"searchKey":"item_tag","searchValue":"choice"}]\'',
    ),
    min_rating: float = typer.Option(
        4.0,
        "--min-rating",
        help="Local filter: drop hits with rating < this (missing rating kept). 0 = keep all",
    ),
    write_es: bool = typer.Option(
        True,
        "--write-es/--no-write-es",
        help="Upsert kept hits into ES_URLS_INDEX (after --min-rating filter)",
    ),
    enqueue: bool = typer.Option(
        False,
        "--enqueue/--no-enqueue",
        help="Also LPUSH product_ids into Redis queue for queue-worker",
    ),
    dry_run_keywords: bool = typer.Option(
        False,
        "--dry-run-keywords",
        help="Only print generated keywords and exit",
    ),
    output: Path = typer.Option(
        Path("data/discovered_search_urls.jsonl"),
        "--output",
        "-o",
    ),
) -> None:
    """Discover product_ids via aliexpress.ds.text.search (keyword + category).

    Like link-crawler URL scrape, but through the official DS Search API:
    pick a category directory, auto-generate keywords (or pass your own),
    paginate results → ES urls index (and optional Redis queue).

    Note: API has NO price/rating/sold filters. Use --sort-by for ordering and
    searchExtend flags (--choice / --ship-from / …) for platform-supported filters.
    Locally drops rating < --min-rating (default 4.0); missing rating is kept.
    """
    from aliexpress_ds.categories import CategoryService, flatten_category_tree
    from aliexpress_ds.keywords import (
        keywords_from_category_rows,
        keywords_from_names,
        load_keywords_file,
    )
    from aliexpress_ds.rate_limit import RateLimiter
    from aliexpress_ds.search import (
        TextService,
        build_search_extend,
        extract_search_products,
        search_product_to_url_doc,
    )

    settings = get_settings()
    try:
        settings.require_credentials()
        if write_es or category_name:
            settings.require_es()
        if enqueue:
            settings.require_queue_redis()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    limiter = RateLimiter(
        min_interval_sec=settings.aliexpress_min_interval_sec,
        daily_limit=settings.aliexpress_daily_limit,
        state_path=Path("data/rate_limit_state.json"),
    )
    iop = IopClient(limiter=limiter)
    cats = CategoryService(client=iop)
    text_svc = TextService(client=iop)

    extra_extend: list[dict] | None = None
    if search_extend_json:
        try:
            parsed = json.loads(search_extend_json)
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]Invalid --search-extend-json:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if not isinstance(parsed, list):
            err_console.print("[red]--search-extend-json must be a JSON list[/red]")
            raise typer.Exit(code=1)
        extra_extend = [x for x in parsed if isinstance(x, dict)]

    search_extend = build_search_extend(
        choice=choice,
        ship_from=ship_from,
        free_ship_to=free_ship_to,
        seller_level=seller_level,
        seller_online=seller_online,
        hot_area=hot_area,
        extra=extra_extend,
    )

    # Resolve category tree for keyword expansion / name → id
    console.print("Loading DS category tree …")
    try:
        tree_payload = cats.fetch_raw()
    except (ValueError, IopError, DailyQuotaExhausted, RuntimeError) as exc:
        err_console.print(f"[red]category.get failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    rows = flatten_category_tree(tree_payload)

    resolved_id = str(category_id or "").strip() or None
    category_path: str | None = None
    if not resolved_id and category_name:
        needle = category_name.strip().lower()
        matches = [
            r
            for r in rows
            if needle in str(r.get("path") or "").lower()
            or needle in str(r.get("category_name") or "").lower()
        ]
        matches.sort(key=lambda r: (int(r.get("level") or 99), str(r.get("path") or "")))
        if not matches:
            err_console.print(f"[red]No category matches[/red] name={category_name!r}")
            raise typer.Exit(code=1)
        resolved_id = str(matches[0]["category_id"])
        category_path = str(matches[0].get("path") or "")
        console.print(
            f"Matched category [cyan]{category_path}[/cyan] id={resolved_id} "
            f"(from {len(matches)} hits)"
        )
    elif resolved_id:
        for r in rows:
            if str(r.get("category_id")) == resolved_id:
                category_path = str(r.get("path") or "")
                break

    # Build keyword list
    keywords: list[str] = []
    if keyword:
        keywords.extend(k.strip() for k in keyword if k and k.strip())
    if keywords_file is not None:
        keywords.extend(load_keywords_file(str(keywords_file)))
    if not keywords:
        if not resolved_id and not category_name:
            err_console.print(
                "[red]Need --keyword / --keywords-file and/or --category-id / --category-name[/red]"
            )
            raise typer.Exit(code=1)
        if resolved_id:
            keywords = keywords_from_category_rows(
                rows,
                parent_category_id=resolved_id,
                max_leaves=max_leaves,
            )
        else:
            keywords = keywords_from_names([category_name or ""])
        keywords = keywords[: max(1, int(max_keywords))]

    # de-dupe preserve order
    seen_kw: set[str] = set()
    uniq_kw: list[str] = []
    for kw in keywords:
        k = kw.strip().lower()
        if not k or k in seen_kw:
            continue
        seen_kw.add(k)
        uniq_kw.append(k)
    keywords = uniq_kw

    console.print(
        f"Keywords={len(keywords)} category_id={resolved_id or '-'} "
        f"path={category_path or '-'} selection={selection_name or '-'} "
        f"pages≤{pages} page_size={page_size} sort={sort_by} "
        f"min_rating={min_rating} "
        f"searchExtend={json.dumps(search_extend, ensure_ascii=False) if search_extend else '-'}"
    )
    for i, kw in enumerate(keywords[:30], 1):
        console.print(f"  {i:3}. {kw}")
    if len(keywords) > 30:
        console.print(f"  … {len(keywords) - 30} more")

    if dry_run_keywords:
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    docs: list[dict] = []
    seen_pids: set[str] = set()
    empty_kw = 0
    dropped_low_rating = 0
    rating_floor = float(min_rating)

    with output.open("a", encoding="utf-8") as fh:
        for ki, kw in enumerate(keywords, 1):
            got_any = False
            for page in range(1, max(pages, 1) + 1):
                try:
                    payload = text_svc.text_search(
                        key_word=kw,
                        category_id=resolved_id,
                        country_code=country,
                        local=local,
                        currency=currency,
                        page_index=page,
                        page_size=page_size,
                        sort_by=sort_by,
                        selection_name=selection_name,
                        search_extend=search_extend,
                    )
                except DailyQuotaExhausted as exc:
                    err_console.print(f"[yellow]{exc}[/yellow]")
                    raise typer.Exit(code=2) from exc
                except (ValueError, IopError, RuntimeError) as exc:
                    err_console.print(f"[red]kw={kw!r} page={page}:[/red] {exc}")
                    if isinstance(exc, IopError) and exc.body is not None:
                        err_console.print_json(data=exc.body)
                    break

                products, meta = extract_search_products(payload)
                console.print(
                    f"[{ki}/{len(keywords)}] {kw!r} p{page}: "
                    f"{len(products)} hits total≈{meta.get('total_count')}"
                )
                if not products:
                    break
                got_any = True
                for product in products:
                    doc = search_product_to_url_doc(
                        product,
                        keyword=kw,
                        category_id=resolved_id,
                        category_path=category_path,
                    )
                    if not doc or doc["product_id"] in seen_pids:
                        continue
                    seen_pids.add(doc["product_id"])
                    rating = doc.get("rating")
                    if (
                        rating_floor > 0
                        and rating is not None
                        and float(rating) < rating_floor
                    ):
                        dropped_low_rating += 1
                        continue
                    if search_extend:
                        doc["search_extend"] = search_extend
                    docs.append(doc)
                    fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            if not got_any:
                empty_kw += 1

    indexed = 0
    if write_es and docs:
        es = make_es_client(settings)
        # strip heavy raw before ES
        slim = []
        for d in docs:
            row = dict(d)
            row.pop("raw_search", None)
            slim.append(row)
        indexed = upsert_url_docs(es, settings.es_urls_index, slim)

    queued = 0
    if enqueue and docs:
        from aliexpress_ds.queue import get_product_queue

        q = get_product_queue(settings)
        items = [
            {
                "product_id": d["product_id"],
                "url": d.get("url"),
                "source": d.get("source"),
                "category": d.get("category"),
            }
            for d in docs
        ]
        queued, _skipped = q.enqueue_many(items, force=False)

    console.print(
        f"[green]Done[/green] discovered={len(docs)} es_indexed={indexed} "
        f"enqueued={queued} empty_keywords={empty_kw} "
        f"dropped_rating_lt_{rating_floor}={dropped_low_rating} → {output}"
    )


@app.command("discover-search-loop")
def discover_search_loop(
    target_unique: int = typer.Option(
        1_000_000,
        "--target-unique",
        help="Stop after this many unique product_ids kept (rating filter applied)",
    ),
    max_pages_per_keyword: int = typer.Option(
        200,
        "--max-pages-per-keyword",
        help="Soft ceiling per keyword (0 = unlimited until empty/no-new)",
    ),
    page_size: int = typer.Option(50, "--page-size", help="1-50"),
    sort_by: str = typer.Option("orders,desc", "--sort-by"),
    country: str = typer.Option("US", "--country", "-c"),
    local: str = typer.Option("en_US", "--local"),
    currency: str = typer.Option("USD", "--currency"),
    min_interval: float = typer.Option(
        2.0,
        "--min-interval",
        help="Seconds between API calls (plus response-driven ban sleep)",
    ),
    min_rating: float = typer.Option(
        4.0,
        "--min-rating",
        help="Drop hits with rating < this (missing rating kept). 0 = keep all",
    ),
    max_keywords: int = typer.Option(200, "--max-keywords"),
    max_leaves: int = typer.Option(150, "--max-leaves"),
    flush_every: int = typer.Option(
        100,
        "--flush-every",
        help="Upsert to ES every N kept docs",
    ),
    checkpoint: Path = typer.Option(
        Path("data/discover_search_checkpoint.json"),
        "--checkpoint",
    ),
    categories_file: Optional[Path] = typer.Option(
        None,
        "--categories-file",
        help="Optional file: one category_id per line (overrides built-in priority L1 list)",
    ),
    choice: bool = typer.Option(False, "--choice/--no-choice"),
    ship_from: Optional[str] = typer.Option(None, "--ship-from"),
    free_ship_to: Optional[str] = typer.Option(None, "--free-ship-to"),
    seller_level: Optional[str] = typer.Option(None, "--seller-level"),
    seller_online: Optional[str] = typer.Option(None, "--seller-online"),
    hot_area: Optional[str] = typer.Option(None, "--hot-area"),
    write_es: bool = typer.Option(True, "--write-es/--no-write-es"),
    output: Path = typer.Option(
        Path("data/discovered_search_urls_mass.jsonl"),
        "--output",
        "-o",
    ),
) -> None:
    """Continuous multi-L1 text.search until --target-unique product_ids.

    Paginates each keyword until empty / no-new pages (soft max-pages ceiling).
    Checkpoint/resume; ES update+doc_as_upsert; local rating filter.
    """
    from aliexpress_ds.categories import CategoryService, flatten_category_tree
    from aliexpress_ds.keywords import (
        PRIORITY_L1_CATEGORY_IDS,
        SKIP_L1_CATEGORY_IDS,
        keywords_from_category_rows,
    )
    from aliexpress_ds.rate_limit import RateLimiter
    from aliexpress_ds.search import (
        TextService,
        build_search_extend,
        extract_search_products,
        search_product_to_url_doc,
    )

    settings = get_settings()
    try:
        settings.require_credentials()
        if write_es:
            settings.require_es()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    interval = max(0.5, float(min_interval))
    rating_floor = float(min_rating)
    soft_pages = int(max_pages_per_keyword)
    flush_n = max(1, int(flush_every))
    target = max(1, int(target_unique))

    limiter = RateLimiter(
        min_interval_sec=interval,
        daily_limit=settings.aliexpress_daily_limit,
        state_path=Path("data/rate_limit_discover.json"),
    )
    iop = IopClient(limiter=limiter)
    cats = CategoryService(client=iop)
    text_svc = TextService(client=iop)
    search_extend = build_search_extend(
        choice=choice,
        ship_from=ship_from,
        free_ship_to=free_ship_to,
        seller_level=seller_level,
        seller_online=seller_online,
        hot_area=hot_area,
    )

    console.print("Loading DS category tree …")
    try:
        tree_payload = cats.fetch_raw()
    except (ValueError, IopError, DailyQuotaExhausted, RuntimeError) as exc:
        err_console.print(f"[red]category.get failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    rows = flatten_category_tree(tree_payload)
    path_by_id = {
        str(r.get("category_id")): str(r.get("path") or "")
        for r in rows
        if r.get("category_id")
    }

    if categories_file is not None:
        cat_ids = [
            ln.strip()
            for ln in categories_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    else:
        cat_ids = [
            cid
            for cid in PRIORITY_L1_CATEGORY_IDS
            if cid not in SKIP_L1_CATEGORY_IDS
        ]

    # Resume state
    state: dict = {
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_unique": target,
        "unique_count": 0,
        "es_indexed": 0,
        "api_calls": 0,
        "dropped_low_rating": 0,
        "cat_index": 0,
        "kw_index": 0,
        "page": 1,
        "category_id": None,
        "keyword": None,
        "seen_pids": [],  # truncated list for resume; full set loaded from output/ES optional
    }
    if checkpoint.exists():
        try:
            loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
                console.print(
                    f"Resumed checkpoint unique={state.get('unique_count')} "
                    f"cat={state.get('category_id')} kw={state.get('keyword')!r} "
                    f"page={state.get('page')}"
                )
        except (OSError, json.JSONDecodeError) as exc:
            err_console.print(f"[yellow]checkpoint load failed, starting fresh:[/yellow] {exc}")

    seen_pids: set[str] = set(str(x) for x in (state.get("seen_pids") or []) if x)
    kept_count = int(state.get("unique_count") or 0)
    # Seed seen + kept from existing output jsonl (unique across restarts)
    if output.exists():
        file_kept = 0
        with output.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = str(row.get("product_id") or "").strip()
                if pid:
                    seen_pids.add(pid)
                    file_kept += 1
        kept_count = max(kept_count, file_kept)

    es = make_es_client(settings) if write_es else None
    pending: list[dict] = []
    total_indexed = int(state.get("es_indexed") or 0)
    dropped = int(state.get("dropped_low_rating") or 0)
    api_calls = int(state.get("api_calls") or 0)

    def _save_checkpoint(**extra: object) -> None:
        state.update(extra)
        state["unique_count"] = kept_count
        state["seen_total"] = len(seen_pids)
        state["es_indexed"] = total_indexed
        state["dropped_low_rating"] = dropped
        state["api_calls"] = api_calls
        state["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Keep last 5k ids for resume hints without huge files
        state["seen_pids"] = list(seen_pids)[-5000:]
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _flush() -> None:
        nonlocal pending, total_indexed
        if not pending or es is None:
            pending = []
            return
        slim = []
        for d in pending:
            row = dict(d)
            row.pop("raw_search", None)
            slim.append(row)
        n = upsert_url_docs(es, settings.es_urls_index, slim)
        total_indexed += n
        pending = []
        _save_checkpoint()

    output.parent.mkdir(parents=True, exist_ok=True)
    console.print(
        f"Loop target={target} cats={len(cat_ids)} page_size={page_size} "
        f"interval≥{interval}s min_rating={rating_floor} "
        f"max_pages/kw={soft_pages or '∞'} flush_every={flush_n} "
        f"already_kept={kept_count}"
    )

    start_cat = int(state.get("cat_index") or 0)
    resume_kw = int(state.get("kw_index") or 0)
    resume_page = int(state.get("page") or 1)

    try:
        with output.open("a", encoding="utf-8") as fh:
            for ci in range(start_cat, len(cat_ids)):
                if kept_count >= target:
                    break
                cid = str(cat_ids[ci])
                cpath = path_by_id.get(cid) or cid
                keywords = keywords_from_category_rows(
                    rows,
                    parent_category_id=cid,
                    max_leaves=max_leaves,
                )[: max(1, int(max_keywords))]
                # de-dupe
                seen_kw: set[str] = set()
                uniq_kw: list[str] = []
                for kw in keywords:
                    k = kw.strip().lower()
                    if not k or k in seen_kw:
                        continue
                    seen_kw.add(k)
                    uniq_kw.append(k)
                keywords = uniq_kw
                console.print(
                    f"[bold]Category[/bold] [{ci + 1}/{len(cat_ids)}] "
                    f"id={cid} path={cpath} keywords={len(keywords)}"
                )

                kw_start = resume_kw if ci == start_cat else 0
                for ki in range(kw_start, len(keywords)):
                    if kept_count >= target:
                        break
                    kw = keywords[ki]
                    page = resume_page if (ci == start_cat and ki == kw_start) else 1
                    # clear one-shot resume
                    resume_kw = 0
                    resume_page = 1
                    empty_new_streak = 0
                    while True:
                        if soft_pages > 0 and page > soft_pages:
                            break
                        if kept_count >= target:
                            break
                        try:
                            payload = text_svc.text_search(
                                key_word=kw,
                                category_id=cid,
                                country_code=country,
                                local=local,
                                currency=currency,
                                page_index=page,
                                page_size=page_size,
                                sort_by=sort_by,
                                search_extend=search_extend,
                            )
                            api_calls += 1
                        except DailyQuotaExhausted as exc:
                            _flush()
                            _save_checkpoint(
                                cat_index=ci,
                                kw_index=ki,
                                page=page,
                                category_id=cid,
                                keyword=kw,
                            )
                            err_console.print(f"[yellow]{exc}[/yellow]")
                            raise typer.Exit(code=2) from exc
                        except (ValueError, IopError, RuntimeError) as exc:
                            err_console.print(f"[red]kw={kw!r} page={page}:[/red] {exc}")
                            if isinstance(exc, IopError) and exc.body is not None:
                                err_console.print_json(data=exc.body)
                            _flush()
                            _save_checkpoint(
                                cat_index=ci,
                                kw_index=ki,
                                page=page,
                                category_id=cid,
                                keyword=kw,
                            )
                            break

                        products, meta = extract_search_products(payload)
                        new_this_page = 0
                        for product in products:
                            doc = search_product_to_url_doc(
                                product,
                                keyword=kw,
                                category_id=cid,
                                category_path=cpath,
                            )
                            if not doc or doc["product_id"] in seen_pids:
                                continue
                            seen_pids.add(doc["product_id"])
                            rating = doc.get("rating")
                            if (
                                rating_floor > 0
                                and rating is not None
                                and float(rating) < rating_floor
                            ):
                                dropped += 1
                                continue
                            if search_extend:
                                doc["search_extend"] = search_extend
                            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
                            pending.append(doc)
                            new_this_page += 1
                            kept_count += 1
                            if len(pending) >= flush_n:
                                _flush()

                        console.print(
                            f"  [{ci + 1}/{len(cat_ids)}][{ki + 1}/{len(keywords)}] "
                            f"{kw!r} p{page}: hits={len(products)} new={new_this_page} "
                            f"kept={kept_count}/{target} "
                            f"total≈{meta.get('total_count')}"
                        )
                        _save_checkpoint(
                            cat_index=ci,
                            kw_index=ki,
                            page=page + 1,
                            category_id=cid,
                            keyword=kw,
                        )

                        if not products:
                            break
                        if new_this_page == 0:
                            empty_new_streak += 1
                            if empty_new_streak >= 2:
                                break
                        else:
                            empty_new_streak = 0
                        page += 1
                # finished category — advance checkpoint
                _flush()
                _save_checkpoint(
                    cat_index=ci + 1,
                    kw_index=0,
                    page=1,
                    category_id=cid,
                    keyword=None,
                )
    finally:
        _flush()
        _save_checkpoint()

    console.print(
        f"[green]Done[/green] kept={kept_count} es_indexed={total_indexed} "
        f"api_calls={api_calls} dropped_rating_lt_{rating_floor}={dropped} "
        f"checkpoint={checkpoint} → {output}"
    )


def _is_rate_limited(exc: Exception) -> tuple[bool, float]:
    """Detect Open Platform flow-control errors; return (matched, cooldown_seconds)."""
    from aliexpress_ds.rate_limit import parse_flow_control

    fc = parse_flow_control(exc)
    if fc is None:
        return False, 0.0
    return True, fc.cooldown_sec


def _count_rows_today(path: Path) -> int:
    """Approximate today's Beijing-day API usage from existing JSONL rows."""
    if not path.exists():
        return 0
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fa = row.get("fetched_at") or ""
            if not fa:
                continue
            try:
                dt = datetime.fromisoformat(fa.replace("Z", "+00:00"))
                if dt.astimezone(BEIJING).strftime("%Y-%m-%d") == today:
                    n += 1
            except ValueError:
                continue
    return n


@app.command("fetch-es")
def fetch_es(
    output: Path = typer.Option(
        Path("data/ds_products.jsonl"),
        "--output",
        "-o",
        help="Append JSONL results here",
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Max products to fetch (0 = all)"),
    country: str = typer.Option("US", "--country", "-c"),
    currency: str = typer.Option("USD", "--currency"),
    language: str = typer.Option("EN", "--language", "-l"),
    delay: float = typer.Option(
        -1.0,
        "--delay",
        help="Seconds between API calls; <0 uses ALIEXPRESS_MIN_INTERVAL_SEC (default 1.0 for Test apps)",
    ),
    daily_limit: int = typer.Option(
        -1,
        "--daily-limit",
        help="Max API calls/day (Beijing). <0 uses ALIEXPRESS_DAILY_LIMIT (5000 for Test apps)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Only write pending URL rows (no AliExpress API call)",
    ),
    include_raw: bool = typer.Option(
        True,
        "--raw/--no-raw",
        help="Include full AliExpress API payload as raw (default on)",
    ),
    index_es: bool = typer.Option(
        True,
        "--index-es/--no-index-es",
        help="Upsert StandardProduct into ES_PRODUCTS_INDEX after each successful fetch",
    ),
    urls_file: Optional[Path] = typer.Option(
        None,
        "--urls-file",
        help="Fetch only these URLs (JSONL with product_id/url); skip ES pending scan",
    ),
    quality_filter: bool = typer.Option(
        False,
        "--quality-filter/--no-quality-filter",
        help=(
            "Also write rows that pass Everymarket quality gates "
            f"(price<{DEFAULT_MAX_PRICE}, rating>={DEFAULT_MIN_RATING}, "
            f"reviews>={DEFAULT_MIN_REVIEWS}, sold>={DEFAULT_MIN_SOLD_COUNT}) "
            "to --quality-output"
        ),
    ),
    quality_output: Path = typer.Option(
        Path("data/ds_products_quality.jsonl"),
        "--quality-output",
        help="Append quality-passing products here when --quality-filter is set",
    ),
) -> None:
    """Fetch products missing from products index → JSONL (raw + StandardProduct)."""
    from aliexpress_ds.rate_limit import RateLimiter

    settings = get_settings()
    try:
        settings.require_es()
        if not dry_run:
            settings.require_credentials()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done_ids(output)

    interval = settings.aliexpress_min_interval_sec if delay < 0 else delay
    quota = settings.aliexpress_daily_limit if daily_limit < 0 else daily_limit
    limiter = None
    if not dry_run:
        limiter = RateLimiter(
            min_interval_sec=interval,
            daily_limit=quota,
            state_path=Path("data/rate_limit_state.json"),
        )
        # Align local counter with rows already written today (prior process may not
        # have shared state file).
        already_today = _count_rows_today(output)
        limiter.ensure_min_count(already_today)
        console.print(
            f"Rate limit: ≥{interval:.2f}s/call, daily≤{quota or '∞'} "
            f"(Beijing day; used≈{already_today} remaining≈{limiter.remaining_today})"
        )

    console.print("Connecting ES and loading existing product_ids …")
    es = make_es_client(settings)
    existing = load_existing_product_ids(es, settings.es_products_index)
    console.print(f"Already in products index: {len(existing)}")
    console.print(f"Already in output JSONL:   {len(done)}")
    if urls_file is not None:
        if not urls_file.exists():
            err_console.print(f"[red]urls file not found:[/red] {urls_file}")
            raise typer.Exit(code=1)
        pending = [
            item
            for item in _load_pending_from_urls_file(urls_file)
            if item["product_id"] not in existing and item["product_id"] not in done
        ]
        console.print(f"From {urls_file}: pending={len(pending)} (excl. ES+JSONL done)")
    else:
        console.print("Scanning URLs index for pending (materialize before API) …")
        pending = list(
            iter_missing_urls(
                es,
                urls_index=settings.es_urls_index,
                products_index=settings.es_products_index,
                existing_ids=existing,
            )
        )
        console.print(f"Pending vs products index: {len(pending)}")

    if quality_filter:
        quality_output.parent.mkdir(parents=True, exist_ok=True)
        console.print(
            f"Quality filter ON → {quality_output} "
            f"(price<{DEFAULT_MAX_PRICE} rating≥{DEFAULT_MIN_RATING} "
            f"reviews≥{DEFAULT_MIN_REVIEWS} sold≥{DEFAULT_MIN_SOLD_COUNT})"
        )

    iop = (
        None
        if dry_run
        else IopClient(limiter=limiter, max_retries=settings.aliexpress_max_retries)
    )
    service = None if dry_run else ProductService(client=iop)
    es_buf = (
        EsProductBuffer(es, settings.es_products_index)
        if index_es and not dry_run
        else None
    )
    fetched = 0
    skipped = 0
    errors = 0
    es_indexed = 0
    quality_kept = 0

    quality_fh = (
        quality_output.open("a", encoding="utf-8") if quality_filter and not dry_run else None
    )
    try:
        with output.open("a", encoding="utf-8") as fh:
            for item in pending:
                pid = item["product_id"]
                if pid in done:
                    skipped += 1
                    continue

                if dry_run:
                    row = {
                        "ok": True,
                        "dry_run": True,
                        "product_id": pid,
                        "url": item["url"],
                        "source": item.get("source"),
                        "title": item.get("title"),
                        "category": item.get("category"),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fetched += 1
                else:
                    assert service is not None and limiter is not None and iop is not None
                    try:
                        summary = service.get_by_url(
                            item["url"],
                            ship_to_country=country,
                            target_currency=currency,
                            target_language=language,
                            local_country=country,
                            local_language=language.lower(),
                        )
                        row = _write_product_row(
                            summary=summary,
                            item=item,
                            include_raw=include_raw,
                            ship_to_country=country,
                            fetch_shipping=settings.fetch_shipping_fee,
                            fetch_categories=settings.fetch_categories,
                            client=iop,
                        )
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        fh.flush()
                        fetched += 1
                        done.add(pid)
                        product = row.get("product")
                        if (
                            quality_fh is not None
                            and isinstance(product, dict)
                            and product_meets_quality(product)
                        ):
                            quality_fh.write(
                                json.dumps(row, ensure_ascii=False) + "\n"
                            )
                            quality_fh.flush()
                            quality_kept += 1
                        if es_buf is not None and isinstance(product, dict):
                            es_buf.add(product)
                            es_indexed = es_buf.indexed
                        if fetched % 20 == 0:
                            console.print(
                                f"… fetched {fetched} es={es_indexed} "
                                f"pending_es={es_buf.pending if es_buf else 0} "
                                f"quality={quality_kept} "
                                f"interval≈{limiter.effective_interval:.2f}s "
                                f"remaining_today≈{limiter.remaining_today}"
                            )
                    except DailyQuotaExhausted as exc:
                        err_console.print(f"[yellow]{exc}[/yellow]")
                        break
                    except (ValueError, IopError, Exception) as exc:
                        errors += 1
                        row = {
                            "ok": False,
                            "product_id": pid,
                            "url": item["url"],
                            "error": str(exc),
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }
                        if isinstance(exc, IopError) and exc.body is not None:
                            row["error_body"] = exc.body
                            row["raw"] = exc.body
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        fh.flush()
                        err_console.print(f"[red]{pid}[/red] {exc}")
                        if _auth_fatal(str(exc)):
                            err_console.print(
                                "[yellow]Stopping early due to auth/sign error.[/yellow]"
                            )
                            break

                if limit and fetched >= limit:
                    break
    finally:
        if quality_fh is not None:
            quality_fh.close()
        if es_buf is not None:
            es_buf.close()
            es_indexed = es_buf.indexed
        if iop is not None:
            iop.close()

    msg = (
        f"[green]Done[/green] wrote={fetched} es_indexed={es_indexed} "
        f"skipped_in_jsonl={skipped} errors={errors} → {output}"
    )
    if quality_filter:
        msg += f"  quality_kept={quality_kept} → {quality_output}"
    console.print(msg)


@app.command("queue-status")
def queue_status() -> None:
    """Show Redis product task queue length / seen count."""
    from aliexpress_ds.queue import get_product_queue

    settings = get_settings()
    try:
        settings.require_queue_redis()
        q = get_product_queue(settings)
        q.ping()
    except (ValueError, Exception) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"queue={settings.redis_queue_key} len={q.length()}")
    console.print(f"seen={settings.redis_queue_seen_key} count={q.seen_count()}")


@app.command("queue-clear")
def queue_clear(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm clearing the shared Redis product queue (+ seen set)",
    ),
    keep_seen: bool = typer.Option(
        False,
        "--keep-seen",
        help="Only delete the pending list; keep the seen set",
    ),
) -> None:
    """Empty Redis product task queue (and seen set by default)."""
    from aliexpress_ds.queue import get_product_queue

    settings = get_settings()
    try:
        settings.require_queue_redis()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    q = get_product_queue(settings)
    console.print(
        f"queue={settings.redis_queue_key} len={q.length()}  "
        f"seen={settings.redis_queue_seen_key} count={q.seen_count()}"
    )
    if not yes:
        err_console.print("[red]Refusing without --yes[/red]")
        raise typer.Exit(code=2)
    stats = q.clear(clear_seen=not keep_seen)
    console.print(
        f"[green]Cleared[/green] queue_removed={stats['queue_removed']} "
        f"seen_removed={stats['seen_removed']}"
    )
    console.print(f"queue len now={q.length()} seen now={q.seen_count()}")


@app.command("rate-status")
def rate_status() -> None:
    """Show local daily counter vs last platform flow-control error (if any).

    Successful AE responses do not include remaining daily quota. The only
    platform-reported limits we see are error bodies (e.g. AppApiCallLimit ban
    seconds). ALIEXPRESS_DAILY_LIMIT is a local client-side cap only.
    """
    from pathlib import Path

    settings = get_settings()
    state_path = Path("data/rate_limit_state.json")
    platform_path = Path("data/platform_rate_limit.json")

    console.print("[bold]Local client cap[/bold] (ALIEXPRESS_DAILY_LIMIT — not from API)")
    console.print(f"  daily_limit={settings.aliexpress_daily_limit or '∞ (disabled)'}")
    console.print(f"  min_interval_sec={settings.aliexpress_min_interval_sec}")
    console.print(f"  queue_concurrency={settings.queue_concurrency}")
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            used = int(data.get("count") or 0)
            day = data.get("day")
            lim = settings.aliexpress_daily_limit
            rem = None if lim <= 0 else max(0, lim - used)
            console.print(f"  beijing_day={day} used≈{used} remaining≈{rem}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            console.print(f"  [yellow]could not read {state_path}: {exc}[/yellow]")
    else:
        console.print(f"  {state_path}: (none yet)")

    console.print(
        "[bold]Last platform signal[/bold] "
        "(from API error body only — success responses have no quota fields)"
    )
    if platform_path.exists():
        try:
            plat = json.loads(platform_path.read_text(encoding="utf-8"))
            for k in (
                "recorded_at",
                "kind",
                "sub_code",
                "ban_seconds",
                "stop_for_day",
                "message",
            ):
                if k in plat:
                    console.print(f"  {k}={plat[k]}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            console.print(f"  [yellow]could not read {platform_path}: {exc}[/yellow]")
    else:
        console.print(
            "  (none yet — will appear after AppApiCallLimit / App Call Limited)"
        )


@app.command("enqueue-es")
def enqueue_es(
    limit: int = typer.Option(0, "--limit", "-n", help="Max quality-pass product_ids (0 = all)"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-enqueue even if product_id is already in Redis seen set",
    ),
    skip_existing_products: bool = typer.Option(
        True,
        "--skip-existing/--include-existing",
        help="Only enqueue ids missing from ES_PRODUCTS_INDEX (default)",
    ),
    quality_filter: bool | None = typer.Option(
        None,
        "--quality-filter/--no-quality-filter",
        help=(
            "Filter urls by price/rating/reviews/sold_count "
            "(default: ENQUEUE_QUALITY_FILTER from .env)"
        ),
    ),
    include_missing_fields: bool | None = typer.Option(
        None,
        "--include-missing-fields/--no-include-missing-fields",
        help=(
            "After quality-pass batch, append urls missing rating/reviews/sold "
            "(default: ENQUEUE_INCLUDE_MISSING_FIELDS)"
        ),
    ),
    missing_limit: int = typer.Option(
        0,
        "--missing-limit",
        help="Max missing-field ids to append (0 = all)",
    ),
    max_price: float | None = typer.Option(
        None,
        "--max-price",
        help="price < this (default: ENQUEUE_MAX_PRICE)",
    ),
    min_rating: float | None = typer.Option(
        None,
        "--min-rating",
        help="rating >= this (default: ENQUEUE_MIN_RATING)",
    ),
    min_reviews: int | None = typer.Option(
        None,
        "--min-reviews",
        help="reviews >= this (default: ENQUEUE_MIN_REVIEWS)",
    ),
    min_sold_count: int | None = typer.Option(
        None,
        "--min-sold",
        help="sold_count >= this (default: ENQUEUE_MIN_SOLD_COUNT)",
    ),
    sort_by: str = typer.Option(
        "reviews,desc",
        "--sort-by",
        help="Sort quality candidates before LPUSH: reviews,desc|rating,desc|none",
    ),
    category_blacklist: bool | None = typer.Option(
        None,
        "--category-blacklist/--no-category-blacklist",
        help="Skip clothing/adult categories (default: ENQUEUE_CATEGORY_BLACKLIST)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Scan ES and report counts without pushing to Redis",
    ),
) -> None:
    """Load eligible product_ids from ES_URLS_INDEX and push into Redis queue.

    Default eligibility:
      - in ``user1_aliexpress_us_product_urls``
      - not yet in ``user1_aliexpress_us_products``
      - passes quality gates (price/rating/reviews/sold) when enabled
      - not in category blacklist (clothing / adult) when enabled

    When quality filter is on and ``--include-missing-fields`` (default), urls
    missing rating/reviews/sold_count are appended after the quality-pass batch
    so workers finish higher-quality jobs first (LPUSH+BRPOP order).
    """
    from aliexpress_ds.category_blacklist import filter_items_by_category_blacklist
    from aliexpress_ds.queue import get_product_queue

    settings = get_settings()
    try:
        settings.require_es()
        if not dry_run:
            settings.require_queue_redis()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    use_quality = (
        settings.enqueue_quality_filter if quality_filter is None else quality_filter
    )
    use_missing_fields = (
        settings.enqueue_include_missing_fields
        if include_missing_fields is None
        else include_missing_fields
    )
    use_category_blacklist = (
        settings.enqueue_category_blacklist
        if category_blacklist is None
        else category_blacklist
    )
    q_max_price = settings.enqueue_max_price if max_price is None else max_price
    q_min_rating = settings.enqueue_min_rating if min_rating is None else min_rating
    q_min_reviews = settings.enqueue_min_reviews if min_reviews is None else min_reviews
    q_min_sold = (
        settings.enqueue_min_sold_count if min_sold_count is None else min_sold_count
    )

    def _apply_blacklist(items: list[dict], label: str) -> list[dict]:
        if not use_category_blacklist:
            return items
        before = len(items)
        filtered, blocked = filter_items_by_category_blacklist(
            items,
            blacklist_file=settings.enqueue_category_blacklist_file,
            env_keywords=os.environ.get("ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS", ""),
        )
        console.print(
            f"Category blacklist ({label}) removed {blocked} "
            f"(before={before} after={len(filtered)})"
        )
        return filtered

    def _apply_sort(items: list[dict]) -> list[dict]:
        sort_key = (sort_by or "none").strip().lower()
        if sort_key in {"", "none", "-"} or not items:
            return items
        field, _, direction = sort_key.partition(",")
        field = field.strip() or "rating"
        reverse = (direction.strip() or "desc").lower() != "asc"

        def _sort_val(item: dict) -> float:
            raw = item.get(field)
            try:
                if raw is None or raw == "":
                    return float("-inf") if reverse else float("inf")
                return float(raw)
            except (TypeError, ValueError):
                return float("-inf") if reverse else float("inf")

        items.sort(key=_sort_val, reverse=reverse)
        top = items[0]
        console.print(
            f"Sorted by {field} {'desc' if reverse else 'asc'} "
            f"(top sample {field}={top.get(field, '-')} "
            f"rating={top.get('rating', '-')} reviews={top.get('reviews', '-')})"
        )
        return items

    console.print("Connecting ES …")
    es = make_es_client(settings)
    existing: set[str] = set()
    if skip_existing_products:
        console.print(f"Loading product_ids from {settings.es_products_index} …")
        existing = load_existing_product_ids(es, settings.es_products_index)
        console.print(f"Already in products index: {len(existing)}")

    if use_quality:
        console.print(
            f"Quality filter ON: price<{q_max_price} rating≥{q_min_rating} "
            f"reviews≥{q_min_reviews} sold≥{q_min_sold}"
        )
    else:
        console.print("Quality filter OFF")

    if use_quality and use_missing_fields:
        console.print(
            "Missing-fields batch ON (append after quality: "
            "no rating / reviews / sold_count)"
        )
    elif use_quality:
        console.print("Missing-fields batch OFF")

    if use_category_blacklist:
        console.print(
            "Category blacklist ON (skip 服装 / 成人用品 — "
            "config/category_blacklist.yaml)"
        )
    else:
        console.print("Category blacklist OFF")

    console.print(f"Scanning {settings.es_urls_index} for quality candidates …")
    if skip_existing_products:
        pending = list(
            iter_missing_urls(
                es,
                urls_index=settings.es_urls_index,
                products_index=settings.es_products_index,
                existing_ids=existing,
                quality_filter=use_quality,
                max_price=q_max_price,
                min_rating=q_min_rating,
                min_reviews=q_min_reviews,
                min_sold_count=q_min_sold,
            )
        )
    else:
        from aliexpress_ds.es import urls_quality_query

        pending = []
        query = (
            urls_quality_query(
                max_price=q_max_price,
                min_rating=q_min_rating,
                min_reviews=q_min_reviews,
                min_sold_count=q_min_sold,
            )
            if use_quality
            else None
        )
        for doc in scroll_sources(
            es,
            settings.es_urls_index,
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
            query=query,
        ):
            pid = str(doc.get("product_id") or "").strip()
            if not pid:
                continue
            pending.append(
                {
                    "product_id": pid,
                    "url": doc.get("url") or f"https://www.aliexpress.us/item/{pid}.html",
                    "source": doc.get("source") or "aliexpress.us",
                    "title": doc.get("title"),
                    "category": doc.get("category"),
                    "price": doc.get("price"),
                    "rating": doc.get("rating"),
                    "reviews": doc.get("reviews"),
                    "sold_count": doc.get("sold_count"),
                }
            )

    pending = _apply_blacklist(pending, "quality")
    pending = _apply_sort(pending)
    if limit and limit > 0:
        pending = pending[:limit]
    quality_count = len(pending)
    console.print(f"Quality candidates: {quality_count}")

    missing_pending: list[dict] = []
    if use_quality and use_missing_fields and skip_existing_products:
        console.print(
            f"Scanning {settings.es_urls_index} for missing-field candidates …"
        )
        quality_ids = {str(x["product_id"]) for x in pending}
        missing_pending = list(
            iter_missing_urls(
                es,
                urls_index=settings.es_urls_index,
                products_index=settings.es_products_index,
                existing_ids=existing,
                missing_fields_only=True,
                max_price=q_max_price,
                exclude_ids=quality_ids,
            )
        )
        missing_pending = _apply_blacklist(missing_pending, "missing-fields")
        if missing_limit and missing_limit > 0:
            missing_pending = missing_pending[:missing_limit]
        console.print(f"Missing-field candidates (queue back): {len(missing_pending)}")

    # Quality first, missing last → LPUSH order → BRPOP processes quality first.
    pending = pending + missing_pending
    console.print(
        f"Candidates to enqueue: {len(pending)} "
        f"(quality={quality_count} missing_fields={len(missing_pending)})"
    )
    if dry_run:
        console.print("[yellow]dry-run[/yellow] — not writing to Redis")
        for item in pending[:10]:
            console.print(
                f"  {item['product_id']}  cat={item.get('category')} "
                f"price={item.get('price')} rating={item.get('rating')} "
                f"reviews={item.get('reviews')} sold={item.get('sold_count')}"
            )
        if len(pending) > 10:
            console.print(f"  … {len(pending) - 10} more")
        return

    q = get_product_queue(settings)
    new, skipped = q.enqueue_many(pending, force=force)
    console.print(
        f"[green]Enqueued[/green] new={new} skipped={skipped} "
        f"quality={quality_count} missing_fields={len(missing_pending)} "
        f"queue_len={q.length()} seen={q.seen_count()} "
        f"→ {settings.redis_queue_key}"
    )


@app.command("enqueue-cny")
def enqueue_cny(
    force: bool = typer.Option(
        True,
        "--force/--no-force",
        help="Re-enqueue even if product_id is in Redis seen set (default: force)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Only report CNY docs without pushing to Redis",
    ),
) -> None:
    """Re-queue products in ES_PRODUCTS_INDEX with currency=CNY for re-fetch."""
    from aliexpress_ds.queue import get_product_queue

    settings = get_settings()
    try:
        settings.require_es()
        if not dry_run:
            settings.require_queue_redis()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    es = make_es_client(settings)
    console.print(f"Scanning {settings.es_products_index} for currency=CNY …")

    pending: list[dict] = []
    resp = es.search(
        index=settings.es_products_index,
        size=5000,
        scroll="10m",
        query={"match": {"currency": "CNY"}},
        _source=["product_id", "url", "source", "category", "categories"],
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                src = hit.get("_source") or {}
                pid = str(src.get("product_id") or "").strip()
                if not pid:
                    continue
                url = str(src.get("url") or "").strip()
                if not url:
                    url = f"https://www.aliexpress.us/item/{pid}.html"
                pending.append(
                    {
                        "product_id": pid,
                        "url": url,
                        "source": src.get("source") or "aliexpress.us",
                        "category": src.get("category") or src.get("categories"),
                    }
                )
            resp = es.scroll(scroll_id=scroll_id, scroll="10m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

    console.print(f"CNY products found: {len(pending)}")

    # Prefer urls-index category breadcrumb when product doc lacks it.
    for item in pending:
        if item.get("category"):
            continue
        pid = item["product_id"]
        for src in ("aliexpress.us", "aliexpress.com"):
            try:
                doc = es.get(index=settings.es_urls_index, id=f"{src}_{pid}")
                cat = (doc.get("_source") or {}).get("category")
                if cat:
                    item["category"] = cat
                    break
            except Exception:
                continue

    if dry_run:
        for item in pending[:10]:
            console.print(f"  {item['product_id']}  {item.get('url')}")
        if len(pending) > 10:
            console.print(f"  … {len(pending) - 10} more")
        return

    q = get_product_queue(settings)
    new, skipped = q.enqueue_many(pending, force=force)
    console.print(
        f"[green]Enqueued CNY refresh[/green] new={new} skipped={skipped} "
        f"queue_len={q.length()} → {settings.redis_queue_key}"
    )


@app.command("queue-worker")
def queue_worker(
    country: str = typer.Option("US", "--country", "-c"),
    currency: str = typer.Option("USD", "--currency"),
    language: str = typer.Option("EN", "--language", "-l"),
    delay: float = typer.Option(
        -1.0,
        "--delay",
        help="Seconds between API calls; <0 uses ALIEXPRESS_MIN_INTERVAL_SEC",
    ),
    daily_limit: int = typer.Option(
        -1,
        "--daily-limit",
        help="Max API calls/day (Beijing). <0 uses ALIEXPRESS_DAILY_LIMIT",
    ),
    concurrency: int = typer.Option(
        -1,
        "--concurrency",
        "-j",
        help="Parallel products (asyncio). <0 uses QUEUE_CONCURRENCY. "
        "Default: product.get only; enable FETCH_SHIPPING_FEE for freight after.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Process at most one job then exit (also exits on empty queue)",
    ),
    max_jobs: int = typer.Option(
        0,
        "--max-jobs",
        "-n",
        help="Stop after N successful product fetches (0 = unlimited)",
    ),
    index_es: bool = typer.Option(
        True,
        "--index-es/--no-index-es",
        help="Upsert StandardProduct into ES_PRODUCTS_INDEX",
    ),
    include_raw: bool = typer.Option(
        False,
        "--raw/--no-raw",
        help="Also append full API payload to --output JSONL",
    ),
    output: Optional[Path] = typer.Option(
        Path("data/ds_queue_products.jsonl"),
        "--output",
        "-o",
        help="Append fetch results JSONL (set empty string to disable)",
    ),
    requeue_on_rate_limit: bool = typer.Option(
        True,
        "--requeue-on-rate-limit/--drop-on-rate-limit",
        help="Put job back on queue when daily quota / hard rate limit stops work",
    ),
) -> None:
    """Listen on Redis queue → DS product.get → upsert ES.

    Uses asyncio: N products in parallel, shared RateLimiter.
    Optional FETCH_SHIPPING_FEE / FETCH_CATEGORIES enrich after product.get.
    """
    import asyncio

    from aliexpress_ds.queue import get_async_product_queue
    from aliexpress_ds.rate_limit import RateLimiter

    settings = get_settings()
    try:
        settings.require_credentials()
        settings.require_queue_redis()
        if index_es:
            settings.require_es()
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    out_path: Path | None = output
    if output is not None and str(output).strip() in ("", "-", "/dev/null"):
        out_path = None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    interval = settings.aliexpress_min_interval_sec if delay < 0 else delay
    quota = settings.aliexpress_daily_limit if daily_limit < 0 else daily_limit
    workers = 1 if once else (
        settings.queue_concurrency if concurrency < 0 else concurrency
    )
    workers = max(1, int(workers))

    limiter = RateLimiter(
        min_interval_sec=interval,
        daily_limit=quota,
        state_path=Path("data/rate_limit_state.json"),
    )
    if out_path is not None:
        limiter.ensure_min_count(_count_rows_today(out_path))

    async def _run() -> tuple[int, int, int]:
        q = get_async_product_queue(settings)
        es = make_es_client(settings) if index_es else None
        es_buf = (
            EsProductBuffer(es, settings.es_products_index) if es is not None else None
        )
        iop = IopClient(limiter=limiter, max_retries=settings.aliexpress_max_retries)
        service = ProductService(client=iop)
        stop = asyncio.Event()
        stats_lock = asyncio.Lock()
        out_lock = asyncio.Lock()
        fetched = 0
        errors = 0
        idle_rounds = 0

        qlen = await q.length()
        console.print(
            f"Worker listening {settings.redis_queue_key} "
            f"(len={qlen}) → ES={settings.es_products_index if index_es else 'off'} "
            f"concurrency={workers} pace≥{interval:.2f}s daily≤{quota or '∞'} "
            f"retries≤{settings.aliexpress_max_retries}"
        )

        async def process_job(job: dict) -> None:
            nonlocal fetched, errors
            pid = str(job.get("product_id") or "").strip()
            if not pid:
                err_console.print(f"[yellow]skip bad job:[/yellow] {job}")
                return

            url = str(job.get("url") or "").strip() or f"https://www.aliexpress.us/item/{pid}.html"
            source = str(job.get("source") or "aliexpress.us").strip()
            item = {
                "product_id": pid,
                "url": url,
                "source": source,
                "category": job.get("category") or job.get("categories"),
            }

            try:
                # product.get only by default; optional freight/categories via settings.
                summary = await service.aget_by_url(
                    url,
                    ship_to_country=country,
                    target_currency=currency,
                    target_language=language,
                    local_country=country,
                    local_language=language.lower(),
                )
                row = await _awrite_product_row(
                    summary=summary,
                    item=item,
                    include_raw=include_raw,
                    ship_to_country=country,
                    fetch_shipping=settings.fetch_shipping_fee,
                    fetch_categories=settings.fetch_categories,
                    client=iop,
                )
                product = row.get("product")
                if out_path is not None:
                    line = json.dumps(row, ensure_ascii=False) + "\n"
                    async with out_lock:
                        with out_path.open("a", encoding="utf-8") as fh:
                            fh.write(line)

                async with stats_lock:
                    fetched += 1
                    done_n = fetched

                if es_buf is not None and isinstance(product, dict):
                    es_buf.add(product)

                price = product.get("price") if isinstance(product, dict) else None
                console.print(
                    f"[green]ok[/green] {pid} price={price} "
                    f"es={es_buf.indexed if es_buf else 0} "
                    f"pending_es={es_buf.pending if es_buf else 0} "
                    f"fetched={done_n} "
                    f"interval≈{limiter.effective_interval:.2f}s "
                    f"remaining≈{limiter.remaining_today}"
                )

                if once or (max_jobs and done_n >= max_jobs):
                    if max_jobs and done_n >= max_jobs:
                        console.print(f"Reached --max-jobs={max_jobs}")
                    stop.set()

            except DailyQuotaExhausted as exc:
                err_console.print(f"[yellow]{exc}[/yellow]")
                if requeue_on_rate_limit:
                    await q.requeue(job)
                    console.print(f"Requeued {pid} (daily quota)")
                stop.set()
            except (ValueError, IopError, Exception) as exc:
                async with stats_lock:
                    errors += 1
                err_row = {
                    "ok": False,
                    "product_id": pid,
                    "url": url,
                    "error": str(exc),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                if isinstance(exc, IopError) and exc.body is not None:
                    err_row["error_body"] = exc.body
                if out_path is not None:
                    line = json.dumps(err_row, ensure_ascii=False) + "\n"
                    async with out_lock:
                        with out_path.open("a", encoding="utf-8") as fh:
                            fh.write(line)
                err_console.print(f"[red]{pid}[/red] {exc}")
                if _auth_fatal(str(exc)):
                    if requeue_on_rate_limit:
                        await q.requeue(job)
                    err_console.print("[yellow]Stopping due to auth/sign error.[/yellow]")
                    stop.set()

        async def slot_worker(slot: int) -> None:
            nonlocal idle_rounds
            while not stop.is_set():
                async with stats_lock:
                    if max_jobs and fetched >= max_jobs:
                        stop.set()
                        return
                # blocking_pop retries Redis timeouts internally; do not let
                # transient queue errors kill the systemd unit.
                try:
                    job = await q.blocking_pop()
                except Exception as exc:
                    err_console.print(
                        f"[yellow]queue pop error (will retry):[/yellow] {exc}"
                    )
                    await asyncio.sleep(5)
                    continue
                if stop.is_set():
                    if job is not None:
                        await q.requeue(job)
                    return
                if job is None:
                    idle_rounds += 1
                    if once:
                        console.print("Queue empty — exiting (--once)")
                        stop.set()
                        return
                    if idle_rounds == 1 or idle_rounds % 12 == 0:
                        try:
                            qlen_now = await q.length()
                        except Exception as exc:
                            err_console.print(
                                f"[yellow]queue len error:[/yellow] {exc}"
                            )
                            qlen_now = "?"
                        console.print(f"… waiting for jobs (queue_len={qlen_now})")
                    continue
                idle_rounds = 0
                await process_job(job)

        try:
            await asyncio.gather(*(slot_worker(i) for i in range(workers)))
        finally:
            es_indexed = 0
            if es_buf is not None:
                es_buf.close()
                es_indexed = es_buf.indexed
            await iop.aclose()
            qlen_end = 0
            try:
                qlen_end = await q.length()
            except Exception:
                pass
            await q.aclose()
            console.print(
                f"[green]Worker done[/green] fetched={fetched} es_indexed={es_indexed} "
                f"errors={errors} queue_len={qlen_end}"
            )

    asyncio.run(_run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
