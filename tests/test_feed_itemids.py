"""Unit tests for aliexpress.ds.feed.itemids.get parsing / mapping."""

from __future__ import annotations

from aliexpress_ds.feed import extract_itemids, itemid_to_url_doc


def test_extract_itemids_basic():
    payload = {
        "result": {
            "total": 200000,
            "products": {"number": [1005001, "1005002", 1005003]},
            "search_id": "abc123",
        },
        "ret": True,
    }
    ids, meta = extract_itemids(payload)
    assert ids == ["1005001", "1005002", "1005003"]
    assert meta["total"] == 200000
    assert meta["search_id"] == "abc123"


def test_extract_itemids_empty():
    ids, meta = extract_itemids({"result": {"products": {"number": []}, "total": 0}})
    assert ids == []
    assert meta["total"] == 0


def test_itemid_to_url_doc_has_no_metrics():
    doc = itemid_to_url_doc(
        "1005006659879732",
        feed_name="AEB_Droplo_BestsellersItems_20241016",
        category_id="66",
    )
    assert doc is not None
    assert doc["product_id"] == "1005006659879732"
    assert "1005006659879732" in doc["url"]
    assert doc["discovery"] == "feed_itemids"
    assert "price" not in doc
    assert "rating" not in doc
    assert "sold_count" not in doc
    assert doc.get("is_bestseller") is True


def test_itemid_to_url_doc_rejects_junk():
    assert itemid_to_url_doc("") is None
    assert itemid_to_url_doc("abc") is None
