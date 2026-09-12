"""Depth area #2 (agentic-write safety): the guardrail engine must catch a
factual claim that doesn't match ground truth, independent of which
backend produced the claim -- these tests hand it hand-built ReplyDrafts,
including *wrong* ones, exactly as a misbehaving LLM backend would."""

from sidestage.guardrails.engine import GuardrailEngine
from sidestage.ingestion.stream import make_message
from sidestage.reply.generator import ReplyDraft


def test_correct_price_passes_untouched(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "how much is the coral dress")
    draft = ReplyDraft("The Coral Wrap Midi Dress is $58.00!", "high", "template",
                        resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert result.passed
    assert not result.corrected
    assert result.final_text == draft.text


def test_wrong_price_is_auto_corrected_and_still_passes(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "how much is the coral dress")
    draft = ReplyDraft("The Coral Wrap Midi Dress is $12.00!", "high", "template",
                        resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert result.passed  # correctable violations don't block
    assert result.corrected
    assert "$58.00" in result.final_text
    assert any(v.kind == "price" for v in result.violations)


def test_promo_price_mentioning_original_price_is_not_flagged(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "how much is the sage dress")
    draft = ReplyDraft("The Sage Linen Midi Dress is $54.00 (marked down from $64.00)!",
                        "high", "template", resolved_sku="DRS-SAGE-02")
    result = engine.review(msg, draft)
    assert result.passed
    assert not result.corrected


def test_overclaiming_stock_count_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "how many in medium do you have")
    msg.size_hint = "M"
    draft = ReplyDraft("Yes! We have 99 left of the Coral Wrap Midi Dress in size M.",
                        "high", "template", resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed
    assert any(v.kind == "availability" and not v.correctable for v in result.violations)


def test_false_sold_out_claim_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "is the M in stock")
    msg.size_hint = "M"
    draft = ReplyDraft("Sorry, that's sold out!", "high", "template", resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed
    assert result.blocking_violations[0].kind == "availability"


def test_false_in_stock_claim_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "is small in stock")
    msg.size_hint = "S"
    draft = ReplyDraft("Yes we have it in stock!", "high", "template", resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)  # size S is 0 qty in fixture data
    assert not result.passed


def test_ungrounded_policy_answer_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "what's your return policy")
    draft = ReplyDraft("Sure, you can return it anytime for any reason no questions asked!",
                        "high", "template", resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed
    assert result.blocking_violations[0].kind == "policy"


def test_profanity_in_reply_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "is this in stock")
    draft = ReplyDraft("hell yeah we have it", "high", "template", resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed
    assert result.blocking_violations[0].kind == "tone"


def test_shouting_reply_blocks(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "is this in stock")
    draft = ReplyDraft("YES WE HAVE IT HURRY UP AND BUY IT NOW", "high", "template",
                        resolved_sku="DRS-CORAL-01")
    result = engine.review(msg, draft)
    assert not result.passed


def test_low_confidence_draft_blocks_regardless_of_text(tools):
    engine = GuardrailEngine(tools)
    msg = make_message("v1", "what about that other thing")
    draft = ReplyDraft("Sure!", "low", "template", resolved_sku=None)
    result = engine.review(msg, draft)
    assert not result.passed
    assert result.blocking_violations[0].kind == "confidence"
