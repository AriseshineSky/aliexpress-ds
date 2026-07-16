"""Resolve product category_id → StandardProduct categories path."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


def normalize_category_path(text: str) -> str:
    """Convert urls-index style 'US / A > B' to StandardProduct 'A>B'."""
    s = (text or "").strip()
    if not s:
        return s
    s = re.sub(r"^(?:US|COM)\s*/\s*", "", s, flags=re.I)
    s = s.replace(" / ", ">").replace(" > ", ">")
    return s.strip()


@lru_cache(maxsize=1)
def _load_paths_from_es(index: str, es_host: str, es_port: int) -> dict[str, str]:
    """Build category_id → path map from synced DS category tree in ES."""
    try:
        from aliexpress_ds.config import get_settings
        from aliexpress_ds.es import make_es_client, scroll_sources

        settings = get_settings()
        es = make_es_client(settings)
        mapping: dict[str, str] = {}
        for doc in scroll_sources(
            es,
            index,
            source_fields=["category_id", "path", "category_name"],
            page_size=2000,
        ):
            cid = str(doc.get("category_id") or "").strip()
            path = str(doc.get("path") or doc.get("category_name") or "").strip()
            if cid and path:
                mapping[cid] = normalize_category_path(path)
        return mapping
    except Exception as exc:
        logger.warning("category ES lookup failed: %s", exc)
        return {}


@lru_cache(maxsize=1)
def _load_paths_from_api() -> dict[str, str]:
    try:
        from aliexpress_ds.categories import CategoryService, flatten_category_tree

        payload = CategoryService().fetch_raw()
        rows = flatten_category_tree(payload)
        return {
            str(r["category_id"]): normalize_category_path(str(r.get("path") or ""))
            for r in rows
            if r.get("category_id") and r.get("path")
        }
    except Exception as exc:
        logger.warning("category API lookup failed: %s", exc)
        return {}


def get_category_paths() -> dict[str, str]:
    from aliexpress_ds.config import get_settings

    settings = get_settings()
    paths = _load_paths_from_es(
        settings.es_categories_index,
        settings.es_host,
        settings.es_port,
    )
    if not paths:
        paths = _load_paths_from_api()
    return paths


def resolve_categories(
    *,
    category_id: str | None = None,
    fallback_category: str | None = None,
) -> str | None:
    """Resolve categories string for StandardProduct.

    Priority: DS category_id path (API/ES tree) → urls-index fallback text.
    """
    cid = str(category_id or "").strip()
    if cid:
        path = get_category_paths().get(cid)
        if path:
            return path

    fb = str(fallback_category or "").strip()
    if fb:
        return normalize_category_path(fb)

    return None
