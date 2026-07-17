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
    console.print("[green]Credentials OK[/green]")
    console.print(f"app_key={settings.aliexpress_app_key}")
    console.print(f"api_url={settings.aliexpress_api_url}")
    console.print(f"oauth_redis={'yes' if settings.redis_url else 'no'}")
    console.print(f"queue_redis={'yes' if settings.redis_queue_url else 'no'}")
    console.print(f"token=…{token[-6:]}")
    if data:
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
def feed_names() -> None:
    """List available DS recommend feed names."""
    from aliexpress_ds.feed import FeedService
    from aliexpress_ds.iop_client import IopError

    try:
        payload = FeedService().list_feed_names()
    except (ValueError, IopError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        if isinstance(exc, IopError) and exc.body is not None:
            err_console.print_json(data=exc.body)
        raise typer.Exit(code=1) from exc
    console.print_json(data=payload)


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
    feed_name: str = typer.Option("DS bestseller", "--feed-name", "-f"),
    category_id: Optional[str] = typer.Option(
        None,
        "--category-id",
        "-C",
        help="AE category id from `categories` command (optional filter)",
    ),
    pages: int = typer.Option(2, "--pages", "-p", help="How many pages to pull"),
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
    Affiliate keyword search needs separate Affiliate API permission (your app currently lacks it).
    """
    from aliexpress_ds.feed import FeedService, extract_products, product_to_url_doc
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
            console.print(f"page {page}: {len(products)} products")
            if not products:
                break
            for product in products:
                doc = product_to_url_doc(product)
                if not doc or doc["product_id"] in seen:
                    continue
                seen.add(doc["product_id"])
                docs.append(doc)
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")

    indexed = 0
    if write_es and docs:
        es = make_es_client(settings)
        indexed = upsert_url_docs(es, settings.es_urls_index, docs)

    console.print(
        f"[green]Done[/green] discovered={len(docs)} es_indexed={indexed} → {output}"
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


@app.command("enqueue-es")
def enqueue_es(
    limit: int = typer.Option(0, "--limit", "-n", help="Max product_ids to enqueue (0 = all)"),
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

    if use_category_blacklist:
        console.print(
            "Category blacklist ON (skip 服装 / 成人用品 — "
            "config/category_blacklist.yaml)"
        )
    else:
        console.print("Category blacklist OFF")

    console.print(f"Scanning {settings.es_urls_index} for candidates …")
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

    before_blacklist = len(pending)
    blocked_category = 0
    if use_category_blacklist:
        pending, blocked_category = filter_items_by_category_blacklist(
            pending,
            blacklist_file=settings.enqueue_category_blacklist_file,
            env_keywords=os.environ.get("ENQUEUE_CATEGORY_BLACKLIST_KEYWORDS", ""),
        )
        console.print(
            f"Category blacklist removed {blocked_category} "
            f"(before={before_blacklist} after={len(pending)})"
        )

    if limit and limit > 0:
        pending = pending[:limit]

    console.print(f"Candidates to enqueue: {len(pending)}")
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
                job = await q.blocking_pop()
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
                        qlen_now = await q.length()
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
