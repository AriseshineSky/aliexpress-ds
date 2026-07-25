# -*- coding: utf-8 -*-
"""Category blacklist: block 成衣, allow Apparel Accessories."""

from aliexpress_ds.category_blacklist import (
    load_blacklist_keywords,
    text_hits_blacklist,
)


def _keywords() -> tuple[str, ...]:
    load_blacklist_keywords.cache_clear()
    return load_blacklist_keywords()


def test_apparel_accessories_not_blocked():
    kws = _keywords()
    assert text_hits_blacklist("Apparel Accessories", kws) is None
    assert (
        text_hits_blacklist(
            "Apparel Accessories > Women's Hair Accessories", kws
        )
        is None
    )


def test_apparel_and_accessories_clothing_tree_blocked():
    kws = _keywords()
    assert text_hits_blacklist("US / Apparel & Accessories", kws) == (
        "apparel & accessories"
    )
    assert text_hits_blacklist(
        "COM / Apparel & Accessories > Women > Dresses", kws
    ) in {"apparel & accessories", "dress", "dresses"}


def test_mens_womens_clothing_still_blocked():
    kws = _keywords()
    assert text_hits_blacklist("Men's Clothing", kws) == "men's clothing"
    assert text_hits_blacklist("Women's Clothing", kws) == "women's clothing"
    assert (
        text_hits_blacklist(
            "US / Mother & Kids > Children's Clothing", kws
        )
        == "clothing"
    )


def test_bare_apparel_not_in_default_keywords():
    kws = _keywords()
    assert "apparel" not in kws
    assert "apparel & accessories" in kws
