"""Category blacklist for enqueue-es (clothing / adult products)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BLACKLIST_FILE = _PKG_ROOT / "config" / "category_blacklist.yaml"

# Fallback when YAML is missing.
_DEFAULT_KEYWORDS: tuple[str, ...] = (
    "women's clothing",
    "men's clothing",
    "apparel",
    "clothing",
    "clothes",
    "dress",
    "dresses",
    "underwear",
    "lingerie",
    "swimwear",
    "服装",
    "novelty & special use",
    "adult toy",
    "adult toys",
    "sex toy",
    "sex toys",
    "erotic",
    "成人",
    "情趣",
    "成人用品",
)


def _parse_csv_lower(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _load_yaml_keywords(path: Path) -> list[str]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML missing; run: uv add pyyaml") from exc
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(k).strip().lower() for k in (raw.get("keywords") or []) if str(k).strip()]


@lru_cache(maxsize=1)
def load_blacklist_keywords(
    *,
    blacklist_file: str = "",
    env_keywords: str = "",
) -> tuple[str, ...]:
    """Load blacklist keywords from YAML + optional env override."""
    keywords: list[str] = []
    path = Path(blacklist_file.strip()) if blacklist_file.strip() else _DEFAULT_BLACKLIST_FILE
    if not path.is_absolute():
        path = _PKG_ROOT / path
    if path.exists():
        keywords = _load_yaml_keywords(path)
    else:
        keywords = list(_DEFAULT_KEYWORDS)
    if env_keywords.strip():
        keywords = _parse_csv_lower(env_keywords)
    # dedupe preserve order
    return tuple(dict.fromkeys(keywords))


def text_hits_blacklist(text: str, keywords: tuple[str, ...]) -> str | None:
    """Return first matching keyword, else None."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    normalized = lowered.replace("_", " ").replace("%27", "'")
    for keyword in keywords:
        if not keyword:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        if re.search(pattern, normalized):
            return keyword
        # CJK keywords: simple substring (no word boundaries).
        if any(ord(ch) > 127 for ch in keyword) and keyword in normalized:
            return keyword
    return None


def is_blacklisted_category(
    category: str,
    *,
    blacklist_file: str = "",
    env_keywords: str = "",
) -> bool:
    keywords = load_blacklist_keywords(
        blacklist_file=blacklist_file,
        env_keywords=env_keywords,
    )
    return text_hits_blacklist(category, keywords) is not None


def filter_items_by_category_blacklist(
    items: list[dict],
    *,
    blacklist_file: str = "",
    env_keywords: str = "",
) -> tuple[list[dict], int]:
    """Drop items whose ``category`` hits the blacklist. Returns (kept, blocked_count)."""
    keywords = load_blacklist_keywords(
        blacklist_file=blacklist_file,
        env_keywords=env_keywords,
    )
    kept: list[dict] = []
    blocked = 0
    for item in items:
        cat = str(item.get("category") or "")
        if text_hits_blacklist(cat, keywords):
            blocked += 1
            continue
        kept.append(item)
    return kept, blocked
