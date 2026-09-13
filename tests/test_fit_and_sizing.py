"""
Fit/sizing questions, size ordering, and the size-word false positive.

All three came out of running the console against real typed messages
rather than from reading the code, which is why they're locked in here.
The fit intent is the interesting one: the catalog descriptions already
carry fit facts ("true to size", "runs one size small"), so a fit answer
can be grounded in the same store as price and stock -- and re-verified by
the guardrail the same way.
"""

from sidestage.guardrails.engine import GuardrailEngine
from sidestage.ingestion.stream import classify_intent, extract_size, make_message
from sidestage.reply.generator import ReplyDraft, fit_phrase_for
from sidestage.store import SIZE_ORDER


# ---- size ordering ----

def test_stock_for_sku_returns_canonical_garment_order(store):
    sizes = list(store.stock_for_sku("DRS-CORAL-01").keys())
    assert sizes == ["XS", "S", "M", "L", "XL"]
    # ...and specifically not the alphabetical order sqlite returns from the
    # composite-PK index, which is what the bug looked like in the console.
    assert sizes != sorted(sizes)


def test_available_sizes_reply_is_in_garment_order(pipeline):
    result = pipeline.process_message(make_message("v1", "what size is available?"))
    text = result.guardrail.final_text
    listed = [s for s in ["XS", "M", "L", "XL"] if s in text]
    assert listed == ["XS", "M", "L", "XL"]  # S is sold out and omitted


def test_unknown_size_sorts_last_without_crashing(store):
    with store.cursor() as cur:
        cur.execute("INSERT INTO inventory (sku, size, stock_qty) VALUES (?,?,?)",
                    ("DRS-CORAL-01", "ONESIZE", 4))
    sizes = list(store.stock_for_sku("DRS-CORAL-01").keys())
    assert sizes[-1] == "ONESIZE"
    assert sizes[:len(SIZE_ORDER)][0] == "XS"


# ---- size-word false positive ----

def test_fit_language_is_not_read_as_a_size_request(store):
    # "runs small" describes cut, not a requested size. Before the fix this
    # returned "S", so "do you have anything that runs small" would have been
    # answered with size-S stock -- true, and not the question asked.
    assert extract_size("does this run small") is None
    assert extract_size("it runs one size big right?") is None
    assert extract_size("how does it fit small people") is None


def test_genuine_size_requests_still_parse(store):
    assert extract_size("do you have this in small") == "S"
    assert extract_size("do you have the coral one in a M??") == "M"
    assert extract_size("anything in extra large") == "XL"


# ---- fit intent ----

def test_fit_questions_classify_as_fit(store):
    for text in ["how about petite?", "is it true to size", "does this run small",
                 "should i size up", "what's the sizing like"]:
        assert classify_intent(text) == "fit_question", text


def test_fit_precedence_over_availability_for_petite(store):
    # matches both "do you have" and "petite"; the useful answer is sizing
    assert classify_intent("do you have this in petite") == "fit_question"


def test_fit_answer_is_grounded_in_the_description(pipeline, store):
    result = pipeline.process_message(make_message("v1", "is the coral dress true to size?"))
    assert "true to size" in result.guardrail.final_text.lower()
    assert result.final_status == "sent"
    assert result.guardrail.violations == []


def test_petite_answer_states_what_the_catalog_actually_carries(pipeline):
    result = pipeline.process_message(make_message("v1", "how about petite?"))
    text = result.guardrail.final_text.lower()
    assert "don't carry petite" in text
    assert "xs, s, m, l, xl" in text  # the real size list, in order
    assert result.final_status == "sent"


def test_fit_phrase_only_returned_when_description_supports_it(store):
    assert fit_phrase_for(store.get_product("DRS-CORAL-01")) == "true to size"
    assert fit_phrase_for(store.get_product("DRS-SAGE-02")) == "runs one size small"
    # the tank's description carries a fit phrase; the jacket's says "runs one
    # size big" -- both supported. An item with no fit wording returns None:
    assert fit_phrase_for({"description": "Cotton tee in four colors."}) is None


def test_unsupported_fit_claim_is_blocked(tools):
    """The catalog says the coral dress is true to size. A draft claiming it
    runs small must not go out, however plausible it sounds."""
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "does the coral dress run small?")
    draft = ReplyDraft("The Coral Wrap Midi Dress runs small — size up!", "high", "template",
                        resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed
    assert result.blocking_violations[0].kind == "fit"


def test_supported_fit_claim_passes(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "does the sage dress run small?")
    draft = ReplyDraft("The Sage Linen Midi Dress runs one size small — hope that helps!",
                        "high", "template", resolved_sku="DRS-SAGE-02")
    result = engine.review(msg, draft)
    assert result.passed
