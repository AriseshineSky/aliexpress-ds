from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


PRODUCT_ID_RE = re.compile(r"(?<!\d)(\d{10,20})(?!\d)")


def extract_product_id(url_or_id: str) -> str:
    """Extract AliExpress product_id from a URL or raw numeric id."""
    text = (url_or_id or "").strip()
    if not text:
        raise ValueError("Empty URL / product id")

    if text.isdigit() and len(text) >= 10:
        return text

    parsed = urlparse(text if "://" in text else f"https://{text}")
    query = parse_qs(parsed.query)

    for key in ("productId", "product_id", "itemId", "item_id"):
        if key in query and query[key]:
            candidate = query[key][0]
            if candidate.isdigit():
                return candidate

    # /item/100500....html  or  /item/slug/100500....html
    path_match = re.search(r"/item/(?:[^/]+/)?(\d{10,20})\.html", parsed.path)
    if path_match:
        return path_match.group(1)

    # Fallback: longest digit run in the whole string
    matches = PRODUCT_ID_RE.findall(text)
    if matches:
        return max(matches, key=len)

    raise ValueError(f"Cannot extract product_id from: {url_or_id!r}")
