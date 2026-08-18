"""Classify AliExpress DS product.get failures for queue handling."""

from __future__ import annotations

# Permanent classes written to Redis + ES urls (`ds_error_class`).
CLASS_UNSALEABLE = "unsaleable"
CLASS_NOT_FOUND = "not_found"
CLASS_SHIP_PROHIBITED = "ship_prohibited"
CLASS_OFFLINE = "offline"
CLASS_BAD_ID = "bad_id"

DISCARD_CLASSES = frozenset(
    {
        CLASS_UNSALEABLE,
        CLASS_NOT_FOUND,
        CLASS_SHIP_PROHIBITED,
        CLASS_OFFLINE,
        CLASS_BAD_ID,
    }
)

# ES urls-index fields. enqueue-es skips `ds_discard=true`.
DS_DISCARD_FIELD = "ds_discard"
DS_ERROR_CLASS_FIELD = "ds_error_class"
DS_ERROR_FIELD = "ds_error"
DS_FAILED_AT_FIELD = "ds_failed_at"

_CLASS_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        CLASS_UNSALEABLE,
        ("all sku unsaleable",),
    ),
    (
        CLASS_NOT_FOUND,
        ("item_id_not_found", "product not found"),
    ),
    (
        CLASS_SHIP_PROHIBITED,
        ("ship_to_country_prohibited",),
    ),
    (
        CLASS_OFFLINE,
        ("the product is off shelf", "off the shelf", "item is offline"),
    ),
    (
        CLASS_BAD_ID,
        ("cannot extract product_id",),
    ),
)

_TRANSIENT_NEEDLES = (
    "system error",
    "timeout",
    "temporarily",
    "internal error",
    "try again",
    "service unavailable",
)


def classify_ds_error(message: str, code: str | None = None) -> str | None:
    """Return a discard class, or None if the error is not a permanent DS miss."""
    text = f"{code or ''} {message or ''}".lower()
    for cls, needles in _CLASS_NEEDLES:
        if any(n in text for n in needles):
            return cls
    return None


def is_permanent_ds_error(message: str, code: str | None = None) -> bool:
    return classify_ds_error(message, code) is not None


def is_transient_ds_error(message: str, code: str | None = None) -> bool:
    text = f"{code or ''} {message or ''}".lower()
    return any(n in text for n in _TRANSIENT_NEEDLES)


def discard_url_doc_ids(
    product_id: str,
    *,
    source: str | None = None,
    es_id: str | None = None,
) -> list[str]:
    """Candidate urls-index `_id` values for a product_id."""
    pid = str(product_id or "").strip()
    if not pid:
        return []
    ids: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            ids.append(text)

    if es_id:
        _add(str(es_id))
    src = str(source or "aliexpress.us").strip() or "aliexpress.us"
    _add(f"{src}_{pid}")
    if src != "aliexpress.us":
        _add(f"aliexpress.us_{pid}")
    if src != "aliexpress.com":
        _add(f"aliexpress.com_{pid}")
    return ids
