from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from aliexpress_ds.iop_client import IopClient

# Priority seeds for Everymarket quality URL crawl (homepage calp-plus tabs).
PRIORITY_CRAWL_NAMES = [
    "Automotive",
    "Toys & Games",
    "Beauty & Health",
    "Jewelry & Accessories",
    "Hair Extensions & Wigs",
    "Pet Supplies",
    "Cell Phones & Accessories",
    "Electronics",
    "Patio, Lawn & Garden",
    "Tools & Home Improvement",
    "Sports & Outdoors",
    "Arts, Crafts & Sewing",
    "Office & School Supplies",
]


def name_to_category_tab(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def calp_url(category_tab: str) -> str:
    return (
        "https://www.aliexpress.us/p/calp-plus/index.html"
        f"?categoryTab={quote(category_tab, safe='')}"
    )


def _unwrap_categories(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("resp_result") or payload.get("result") or payload
    if isinstance(root, dict) and isinstance(root.get("result"), dict):
        root = root["result"]
    if not isinstance(root, dict):
        return []
    node = root.get("categories") or root.get("category") or root
    if isinstance(node, dict):
        for key in ("category", "categories", "ae_category"):
            inner = node.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
            if isinstance(inner, dict):
                return [inner]
        if "category_id" in node or "category_name" in node:
            return [node]
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    return []


def flatten_category_tree(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten DS category.get payload into rows with path / level / parent."""
    raw = _unwrap_categories(payload)
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw:
        cid = str(item.get("category_id") or "").strip()
        if not cid:
            continue
        by_id[cid] = {
            "category_id": cid,
            "category_name": str(item.get("category_name") or "").strip(),
            "parent_category_id": (
                str(item["parent_category_id"]).strip()
                if item.get("parent_category_id") is not None
                else None
            ),
        }

    def path_for(cid: str, seen: set[str] | None = None) -> list[str]:
        seen = seen or set()
        if cid in seen or cid not in by_id:
            return []
        seen.add(cid)
        row = by_id[cid]
        parent = row.get("parent_category_id")
        names = path_for(parent, seen) if parent else []
        if row["category_name"]:
            names.append(row["category_name"])
        return names

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for cid, row in by_id.items():
        path = path_for(cid)
        rows.append(
            {
                "category_id": cid,
                "category_name": row["category_name"],
                "parent_category_id": row.get("parent_category_id"),
                "path": " > ".join(path),
                "level": len(path),
                "source": "aliexpress.ds.category.get",
                "updated_at": now,
            }
        )
    rows.sort(key=lambda r: (r["level"], r["path"]))
    return rows


# Homepage calp L1 name → possible DS category.get names (case-insensitive).
NAME_ALIASES: dict[str, list[str]] = {
    "Automotive": ["Automobiles", "Automobile", "Auto", "Car"],
    "Toys & Games": ["Toys", "Toys & Hobbies", "Mother & Kids"],
    "Beauty & Health": ["Beauty", "Beauty & Health", "Health"],
    "Jewelry & Accessories": ["Jewelry", "Jewelry & Accessories", "Accessories"],
    "Hair Extensions & Wigs": ["Hair Extensions & Wigs", "Hair", "Wigs"],
    "Pet Supplies": ["Pet Products", "Pet Supplies", "Pet"],
    "Cell Phones & Accessories": [
        "Phones & Telecommunications",
        "Mobile Phone Accessories",
        "Cellphones & Telecommunications",
    ],
    "Electronics": ["Consumer Electronics", "Electronics"],
    "Patio, Lawn & Garden": ["Home & Garden", "Garden Supplies", "Lawn"],
    "Tools & Home Improvement": ["Home Improvement", "Tools", "Hardware"],
    "Sports & Outdoors": ["Sports & Entertainment", "Sports", "Outdoor"],
    "Arts, Crafts & Sewing": ["Arts,Crafts & Sewing", "Arts & Crafts", "Crafts"],
    "Office & School Supplies": ["Office & School Supplies", "Office", "Computer & Office"],
}


def match_ds_category_id(rows: list[dict[str, Any]], name: str) -> str | None:
    """Best-effort map homepage calp name → DS category_id (prefer L1)."""
    target = name.strip()
    if not target:
        return None

    def norm(s: str) -> str:
        return (
            s.lower()
            .replace("&", "and")
            .replace(",", " ")
            .replace("'", "")
            .replace("-", " ")
            .replace("/", " ")
        )

    candidates = [target] + NAME_ALIASES.get(target, [])
    norms = [norm(c) for c in candidates]

    # Exact L1
    for row in rows:
        if row.get("level") != 1:
            continue
        n = norm(row.get("category_name") or "")
        if n in norms:
            return row["category_id"]
    # L1 fuzzy contains
    for row in rows:
        if row.get("level") != 1:
            continue
        n = norm(row.get("category_name") or "")
        if any(t in n or n in t for t in norms if len(t) >= 4):
            return row["category_id"]
    # Any level exact alias
    for row in rows:
        n = norm(row.get("category_name") or "")
        if n in norms:
            return row["category_id"]
    return None


def build_crawl_targets(
    *,
    ds_rows: list[dict[str, Any]] | None = None,
    names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build calp-plus crawl seeds for link-crawler (+ optional DS id)."""
    ds_rows = ds_rows or []
    names = names or PRIORITY_CRAWL_NAMES
    now = datetime.now(timezone.utc).isoformat()
    out: list[dict[str, Any]] = []
    for i, name in enumerate(names):
        tab = name_to_category_tab(name)
        out.append(
            {
                "name": f"US / {name}",
                "display_name": name,
                "category_tab": tab,
                "url": calp_url(tab),
                "ds_category_id": match_ds_category_id(ds_rows, name),
                "enabled": True,
                "priority": i + 1,
                "site": "aliexpress.us",
                "updated_at": now,
            }
        )
    return out


class CategoryService:
    API = "aliexpress.ds.category.get"

    def __init__(self, client: IopClient | None = None):
        self.client = client or IopClient()

    def fetch_raw(self) -> dict[str, Any]:
        return self.client.execute(self.API)
