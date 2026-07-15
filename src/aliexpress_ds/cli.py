from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from aliexpress_ds.config import get_settings
from aliexpress_ds.es import (
    iter_missing_urls,
    load_existing_product_ids,
    make_es_client,
    upsert_standard_products,
    upsert_url_docs,
)
from aliexpress_ds.iop_client import IopError
from aliexpress_ds.product import ProductService
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
) -> dict:
    from aliexpress_ds.standard_mapper import to_standard_product

    raw = summary.raw or {}
    product = to_standard_product(
        raw,
        url=item["url"],
        source=item.get("source") or item.get("es_source"),
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
        from aliexpress_ds.token_store import resolve_access_token

        token = resolve_access_token(settings)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green]Credentials OK[/green]")
    console.print(f"app_key={settings.aliexpress_app_key}")
    console.print(f"api_url={settings.aliexpress_api_url}")
    console.print(f"redis={'yes' if settings.redis_url else 'no'}")
    console.print(f"token=…{token[-6:]}")


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
    from aliexpress_ds.es import ensure_index, upsert_docs
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
    n_crawl = upsert_docs(
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
    from aliexpress_ds.iop_client import IopError
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
    service = FeedService()
    docs: list[dict] = []
    seen: set[str] = set()

    with output.open("a", encoding="utf-8") as fh:
        for page in range(1, max(pages, 1) + 1):
            try:
                limiter.wait_turn()
                payload = service.recommend(
                    feed_name=feed_name,
                    category_id=category_id,
                    country=country,
                    page_no=page,
                    page_size=page_size,
                    sort=sort,
                )
            except (ValueError, IopError, RuntimeError) as exc:
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
    msg = str(exc)
    low = msg.lower()
    body = ""
    if isinstance(exc, IopError) and exc.body is not None:
        body = json.dumps(exc.body, ensure_ascii=False).lower()
    blob = f"{low}\n{body}"
    markers = (
        "apicalllimits",
        "app call limited",
        "call limited",
        "accesscontrol.limited",
        "limited-by-app",
        "limited-by-api",
        "ban will last",
        "flowlimit",
        "frequency",
    )
    if not any(m in blob for m in markers):
        return False, 0.0

    m = re.search(r"last(?:s)?\s+for\s+(\d+)\s+more\s+seconds", blob)
    if m:
        return True, float(m.group(1)) + 1.0
    return True, 60.0


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

    service = None if dry_run else ProductService()
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
                    assert service is not None and limiter is not None
                    success = False
                    stop_all = False
                    while not success:
                        try:
                            limiter.wait_turn()
                        except RuntimeError as exc:
                            err_console.print(f"[yellow]{exc}[/yellow]")
                            stop_all = True
                            break

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
                            )
                            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                            fh.flush()
                            fetched += 1
                            done.add(pid)
                            success = True
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
                            if index_es and isinstance(product, dict):
                                try:
                                    es_indexed += upsert_standard_products(
                                        es,
                                        settings.es_products_index,
                                        [product],
                                    )
                                except Exception as es_exc:
                                    err_console.print(
                                        f"[yellow]ES upsert failed {pid}:[/yellow] {es_exc}"
                                    )
                            if fetched % 20 == 0:
                                console.print(
                                    f"… fetched {fetched} es={es_indexed} "
                                    f"quality={quality_kept} "
                                    f"remaining_today≈{limiter.remaining_today}"
                                )
                        except (ValueError, IopError, Exception) as exc:
                            limited, cool = _is_rate_limited(exc)
                            if limited:
                                err_console.print(
                                    f"[yellow]Rate limited[/yellow] {exc}; cooling {cool:.0f}s"
                                )
                                limiter.penalize(cool)
                                continue

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
                                stop_all = True
                            break

                    if stop_all:
                        break

                if limit and fetched >= limit:
                    break
    finally:
        if quality_fh is not None:
            quality_fh.close()

    msg = (
        f"[green]Done[/green] wrote={fetched} es_indexed={es_indexed} "
        f"skipped_in_jsonl={skipped} errors={errors} → {output}"
    )
    if quality_filter:
        msg += f"  quality_kept={quality_kept} → {quality_output}"
    console.print(msg)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
