"""Generate mass-discovery keywords for aliexpress.ds.text.search.

Uses category tree leaf names + shopping modifiers so one category_id can
drive many search queries (link-crawler style coverage via API).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Appended to category leaf names for broader recall (English DS catalog).
DEFAULT_MODIFIERS: tuple[str, ...] = (
    "",
    "wholesale",
    "bulk",
    "kit",
    "set",
    "pack",
    "mini",
    "portable",
    "wireless",
    "led",
    "usb",
    "2024",
    "2025",
)

# Extra seeds per L1-ish name (matched case-insensitively against path).
CATEGORY_SEED_KEYWORDS: dict[str, tuple[str, ...]] = {
    "automotive": (
        "car accessories",
        "dash cam",
        "car charger",
        "obd2",
        "car led light",
        "windshield",
    ),
    "toys": (
        "fidget",
        "building blocks",
        "remote control car",
        "puzzle",
        "educational toys",
    ),
    "beauty": (
        "nail art",
        "eyelash",
        "makeup brush",
        "face mask",
        "hair clip",
        "skincare",
    ),
    "jewelry": (
        "necklace",
        "bracelet",
        "earrings",
        "ring",
        "bead",
    ),
    "hair": (
        "wig",
        "hair extension",
        "hair clip",
        "headband",
    ),
    "pet": (
        "dog toy",
        "cat toy",
        "pet leash",
        "pet bowl",
        "aquarium",
    ),
    "phone": (
        "phone case",
        "tempered glass",
        "phone holder",
        "cable organizer",
        "power bank",
    ),
    "electronics": (
        "bluetooth earbuds",
        "usb hub",
        "smart watch band",
        "webcam",
        "hdmi cable",
    ),
    "garden": (
        "plant pot",
        "garden tool",
        "watering",
        "solar light",
    ),
    "tools": (
        "screwdriver set",
        "multimeter",
        "wrench",
        "drill bit",
        "tape measure",
    ),
    "sports": (
        "yoga mat",
        "resistance band",
        "bicycle light",
        "camping",
        "fishing",
    ),
    "crafts": (
        "sewing",
        "embroidery",
        "resin mold",
        "beading",
    ),
    "office": (
        "notebook",
        "pen set",
        "desk organizer",
        "sticky notes",
        "stapler",
    ),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _leaf_name(path: str) -> str:
    parts = [p.strip() for p in (path or "").split(">") if p.strip()]
    return parts[-1] if parts else (path or "").strip()


def category_seed_bucket(path_or_name: str) -> str | None:
    blob = _norm(path_or_name)
    for key in CATEGORY_SEED_KEYWORDS:
        if key in blob:
            return key
    return None


def keywords_from_category_rows(
    rows: Iterable[dict[str, Any]],
    *,
    parent_category_id: str | None = None,
    max_leaves: int = 80,
    modifiers: Iterable[str] | None = None,
    include_seeds: bool = True,
) -> list[str]:
    """Build keyword list from flattened DS category rows under a parent.

    Prefer deeper leaves (level >= 2). Always include parent leaf name + seeds.
    """
    mods = tuple(DEFAULT_MODIFIERS if modifiers is None else modifiers)
    parent = str(parent_category_id or "").strip()
    candidates: list[dict[str, Any]] = []
    parent_row: dict[str, Any] | None = None

    for row in rows:
        cid = str(row.get("category_id") or "").strip()
        path = str(row.get("path") or row.get("category_name") or "").strip()
        if not cid or not path:
            continue
        if parent and cid == parent:
            parent_row = row
        level = int(row.get("level") or 0)
        # Under parent: path contains parent name or parent_id matches ancestry
        if parent:
            if cid == parent:
                continue
            # Prefer children whose path is longer and shares prefix with parent path
            if parent_row:
                ppath = str(parent_row.get("path") or "")
                if ppath and not path.startswith(ppath):
                    continue
            elif str(row.get("parent_category_id") or "").strip() != parent and level < 2:
                # without parent_row loaded yet, keep likely descendants for 2nd pass
                pass
        if level >= 2 or not parent:
            candidates.append(row)

    # Second pass if parent_row known: keep only paths under parent
    if parent and parent_row:
        ppath = str(parent_row.get("path") or "")
        candidates = [
            r
            for r in candidates
            if str(r.get("path") or "").startswith(ppath)
            and str(r.get("category_id")) != parent
        ]

    # Sort deeper / longer paths first
    candidates.sort(
        key=lambda r: (
            -int(r.get("level") or 0),
            -len(str(r.get("path") or "")),
            str(r.get("path") or ""),
        )
    )
    leaves = candidates[: max(1, int(max_leaves))]

    out: list[str] = []
    seen: set[str] = set()

    def add(kw: str) -> None:
        text = _norm(kw)
        if len(text) < 2 or text in seen:
            return
        seen.add(text)
        out.append(text)

    if parent_row:
        add(_leaf_name(str(parent_row.get("path") or parent_row.get("category_name") or "")))
        bucket = category_seed_bucket(str(parent_row.get("path") or ""))
        if include_seeds and bucket:
            for seed in CATEGORY_SEED_KEYWORDS[bucket]:
                add(seed)

    for row in leaves:
        leaf = _leaf_name(str(row.get("path") or row.get("category_name") or ""))
        if not leaf:
            continue
        for mod in mods:
            if not mod:
                add(leaf)
            else:
                add(f"{leaf} {mod}")

    return out


def keywords_from_names(
    names: Iterable[str],
    *,
    modifiers: Iterable[str] | None = None,
) -> list[str]:
    mods = tuple(DEFAULT_MODIFIERS if modifiers is None else modifiers)
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        leaf = _leaf_name(str(name))
        if not leaf:
            continue
        for mod in mods:
            kw = leaf if not mod else f"{leaf} {mod}"
            text = _norm(kw)
            if text in seen or len(text) < 2:
                continue
            seen.add(text)
            out.append(text)
    return out


def load_keywords_file(path: str) -> list[str]:
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kw = _norm(line)
        if kw in seen:
            continue
        seen.add(kw)
        out.append(kw)
    return out
