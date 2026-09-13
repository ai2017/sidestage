"""Depth area #1 (retrieval): correctness of exact lookups, fuzzy product
resolution, and the char n-gram policy search -- including the documented
lexical-overlap limitation."""


def test_exact_price_lookup(tools):
    result = tools.lookup_price("DRS-CORAL-01")
    assert result["found"]
    assert result["effective_price_cents"] == 5800


def test_promo_price_uses_promo_as_effective(tools):
    result = tools.lookup_price("DRS-SAGE-02")
    assert result["effective_price_cents"] == 5400
    assert result["list_price_cents"] == 6400


def test_stock_lookup_known_and_unknown_size(tools):
    known = tools.lookup_stock("DRS-CORAL-01", "M")
    assert known["known_size"] and known["stock_qty"] == 5

    unknown = tools.lookup_stock("DRS-CORAL-01", "XXL")
    assert unknown["known_size"] is False


def test_fuzzy_product_search_matches_color(tools):
    hits = tools.search_products("do you have the coral one")
    assert hits
    assert hits[0]["sku"] == "DRS-CORAL-01"


def test_fuzzy_product_search_distinguishes_sage_from_coral(tools):
    hits = tools.search_products("how much is the sage dress")
    assert hits
    assert hits[0]["sku"] == "DRS-SAGE-02"


def test_policy_search_handles_morphological_variants(tools):
    # "return" (singular, in the question) vs "returns"/"returnable" (in the
    # policy text) -- word-level TF-IDF scores this 0.0 (verified while
    # building this module); char n-grams recover it. See TDD.md.
    hits = tools.search_policy("what's your return policy if it doesn't fit")
    assert hits
    assert hits[0]["topic"] == "returns"


def test_policy_search_known_limitation_no_semantic_match(tools):
    """Documented limitation: lexical (char n-gram) retrieval only. A
    question using none of the policy's actual vocabulary does not match,
    even though a human would connect "real or fake" to the authenticity
    policy. This is intentional: search_policy returns [] below its
    threshold rather than guessing, and the guardrail engine then forces
    the reply to suggest-only instead of stating an ungrounded answer.
    See PRD.md / TDD.md known limitations."""
    hits = tools.search_policy("is this real or some fake crap")
    assert hits == []
