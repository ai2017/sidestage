"""Copilot-to-automation ladder: the configured level is a ceiling, never a
guarantee -- a guardrail failure or low-confidence resolution always forces
a downgrade to suggest-only regardless of what the seller configured."""

from sidestage.ingestion.stream import make_message
from sidestage.ladder import Rung, decide


def test_level_0_always_suggests():
    d = decide(0, confidence="high", guardrail_passed=True)
    assert d.rung == Rung.SUGGEST


def test_level_1_auto_sends_when_clean():
    d = decide(1, confidence="high", guardrail_passed=True)
    assert d.rung == Rung.AUTO_SEND


def test_level_2_auto_acts_when_clean():
    d = decide(2, confidence="high", guardrail_passed=True)
    assert d.rung == Rung.AUTO_ACT


def test_guardrail_failure_forces_suggest_even_at_level_2():
    d = decide(2, confidence="high", guardrail_passed=False)
    assert d.rung == Rung.SUGGEST


def test_low_confidence_forces_suggest_even_at_level_2():
    d = decide(2, confidence="low", guardrail_passed=True)
    assert d.rung == Rung.SUGGEST


def test_pipeline_auto_sends_price_question_by_default(pipeline):
    msg = make_message("v1", "how much is the coral dress")
    result = pipeline.process_message(msg)
    assert result.final_status == "sent"


def test_pipeline_suggests_purchase_intent_by_default(pipeline, store):
    # seed default: purchase_intent starts at ladder level 0 (suggest-only)
    assert store.ladder_level("purchase_intent") == 0
    msg = make_message("v1", "sold! i'll take the coral in L")
    result = pipeline.process_message(msg)
    assert result.final_status == "suggested"
    assert result.action is None


def test_pipeline_auto_acts_purchase_intent_when_seller_opts_in(pipeline, store):
    store.set_ladder_level("purchase_intent", 2)
    before = store.get_stock("DRS-CORAL-01", "L")
    msg = make_message("v1", "sold! i'll take the coral in L")
    result = pipeline.process_message(msg)
    assert result.final_status == "sent"
    assert result.action is not None
    assert result.action.action_type == "stock_adjust"
    assert store.get_stock("DRS-CORAL-01", "L") == before - 1


def test_pipeline_never_auto_acts_without_size_even_at_level_2(pipeline, store):
    store.set_ladder_level("purchase_intent", 2)
    msg = make_message("v1", "sold i'll take it")  # no size mentioned
    result = pipeline.process_message(msg)
    assert result.action is None
