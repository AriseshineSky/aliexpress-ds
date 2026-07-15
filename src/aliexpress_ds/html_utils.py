from __future__ import annotations

import re

_RE_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_RE_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_RE_LINK = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_RE_IFRAME = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)
_RE_A_OPEN = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_RE_A_CLOSE = re.compile(r"</a>", re.IGNORECASE)
_RE_EMPTY_TAG = re.compile(
    r"<(p|div|span|strong|em|b|i|u|h[1-6])(?:\s[^>]*)?>(?:\s|&nbsp;|<br\s*/?>)*</\1>",
    re.IGNORECASE,
)
_RE_BR_RUN = re.compile(r"(?:<br\s*/?>\s*){3,}", re.IGNORECASE)


def clean_product_description(html: str) -> str:
    """Normalize AliExpress description HTML for StandardProduct validation."""
    if not html:
        return ""

    cleaned = html.strip()
    cleaned = _RE_SCRIPT.sub("", cleaned)
    cleaned = _RE_STYLE.sub("", cleaned)
    cleaned = _RE_LINK.sub("", cleaned)
    cleaned = _RE_IFRAME.sub("", cleaned)
    cleaned = _RE_A_OPEN.sub("", cleaned)
    cleaned = _RE_A_CLOSE.sub("", cleaned)

    for _ in range(10):
        updated = _RE_EMPTY_TAG.sub("", cleaned)
        if updated == cleaned:
            break
        cleaned = updated

    cleaned = _RE_BR_RUN.sub("<br><br>", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()
