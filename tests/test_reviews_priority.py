from aliexpress_ds.es import (
    category_roots_clause,
    iter_urls_by_reviews_priority,
    restrict_to_categories,
    urls_reviews_priority_phases,
)


def test_category_roots_clause_matches_substrings_case_insensitive() -> None:
    clause = category_roots_clause(["Automotive", " Pet Supplies ", ""])

    assert clause is not None
    wildcards = [
        entry["wildcard"]["category"] for entry in clause["bool"]["should"]
    ]
    assert wildcards == [
        {"value": "*Automotive*", "case_insensitive": True},
        {"value": "*Pet Supplies*", "case_insensitive": True},
    ]
    assert clause["bool"]["minimum_should_match"] == 1
    assert category_roots_clause([]) is None
    assert category_roots_clause(None) is None


def test_category_wildcard_escapes_special_characters() -> None:
    clause = category_roots_clause(["A*B?C", "back\\slash"])

    assert clause is not None
    values = [entry["wildcard"]["category"]["value"] for entry in clause["bool"]["should"]]
    assert values == ["*A\\*B\\?C*", "*back\\\\slash*"]


def test_restrict_to_categories_ands_into_bool_filter() -> None:
    query = {"bool": {"filter": [{"exists": {"field": "product_id"}}]}}

    restricted = restrict_to_categories(query, ["Automotive"])

    filters = restricted["bool"]["filter"]
    assert filters[0] == {"exists": {"field": "product_id"}}
    assert filters[1]["bool"]["should"][0]["wildcard"]["category"]["value"] == (
        "*Automotive*"
    )
    assert restrict_to_categories(query, []) == query
    assert restrict_to_categories(query, None) == query


def test_priority_phases_carry_category_filter() -> None:
    phases = urls_reviews_priority_phases(category_roots=["Automotive"])

    for _name, query, _sort in phases:
        clauses = [
            clause
            for clause in query["bool"]["filter"]
            if "should" in clause.get("bool", {})
        ]
        assert any(
            wildcard["wildcard"]["category"]["value"] == "*Automotive*"
            for clause in clauses
            for wildcard in clause["bool"]["should"]
        )


def test_reviews_priority_phases_are_disjoint_and_ordered() -> None:
    phases = urls_reviews_priority_phases(
        threshold=90,
        max_valid_reviews=5_000_000,
    )

    assert [phase for phase, _query, _sort in phases] == [
        "reviews_high",
        "reviews_unknown",
        "reviews_low",
    ]
    high_range = phases[0][1]["bool"]["filter"][1]["range"]["reviews"]
    low_range = phases[2][1]["bool"]["filter"][1]["range"]["reviews"]
    assert high_range == {"gt": 90, "lte": 5_000_000}
    assert low_range == {"gte": 0, "lte": 90}
    assert phases[0][2][0]["reviews"]["order"] == "desc"
    assert phases[1][2][0]["created_at"]["order"] == "desc"
    assert phases[2][2][0]["created_at"]["order"] == "desc"
    for _name, query, _sort in phases:
        assert {"term": {"ds_discard": True}} in query["bool"]["must_not"]
    for _name, query, _sort in phases:
        assert {"term": {"ds_discard": True}} in query["bool"]["must_not"]


class _FakeEs:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self._scroll_calls = 0

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        phase = len(self.search_calls)
        docs = {
            1: [
                {"_id": "us_1", "_source": {"product_id": "1", "reviews": 500}},
                {"_id": "us_2", "_source": {"product_id": "2", "reviews": 100}},
            ],
            2: [
                {"_id": "us_3", "_source": {"product_id": "3"}},
            ],
            3: [
                {"_id": "us_4", "_source": {"product_id": "4", "reviews": 90}},
                {"_id": "us_5", "_source": {"product_id": "5", "reviews": 10}},
            ],
        }[phase]
        return {"_scroll_id": f"scroll-{phase}", "hits": {"hits": docs}}

    def scroll(self, **_kwargs):
        self._scroll_calls += 1
        return {"_scroll_id": "done", "hits": {"hits": []}}

    def clear_scroll(self, **_kwargs):
        return None


def test_iter_urls_streams_high_unknown_low_and_skips_existing() -> None:
    es = _FakeEs()

    rows = list(
        iter_urls_by_reviews_priority(
            es,
            urls_index="urls",
            existing_ids={"2"},
            threshold=90,
        )
    )

    assert [(phase, row["product_id"]) for phase, row in rows] == [
        ("reviews_high", "1"),
        ("reviews_unknown", "3"),
        ("reviews_low", "4"),
        ("reviews_low", "5"),
    ]
    assert es.search_calls[0]["sort"][0]["reviews"]["order"] == "desc"
    assert es.search_calls[1]["sort"][0]["created_at"]["order"] == "desc"
    assert es.search_calls[2]["sort"][0]["created_at"]["order"] == "desc"
