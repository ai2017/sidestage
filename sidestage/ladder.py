"""
Copilot-to-automation ladder.

Three rungs, configured per intent type (store.ladder_settings), not
globally — a seller can trust the copilot to auto-send price/availability
FAQ answers on day one while keeping purchase-intent replies (which can
trigger an inventory-mutating action) on suggest-only until they've built
trust in the guardrails. See PRD.md, "The copilot-to-automation ladder."

  0  SUGGEST   — draft shown to the seller; seller must click Send.
  1  AUTO_SEND — copilot sends the reply itself once guardrails pass.
                 Never mutates inventory/listing state.
  2  AUTO_ACT  — copilot sends the reply *and* executes the one bound
                 action for that intent (see ladder.ACTION_FOR_INTENT),
                 inside the same guardrail-and-confidence gate.

Regardless of the configured level, the ladder never allows AUTO_SEND or
AUTO_ACT when the draft's confidence is "low" or guardrails found a
blocking violation — the ladder is a ceiling on automation, not an
override of safety. That downgrade path is enforced in `decide()` itself,
not left to callers to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Rung(IntEnum):
    SUGGEST = 0
    AUTO_SEND = 1
    AUTO_ACT = 2


@dataclass
class Decision:
    rung: Rung
    reason: str


def decide(configured_level: int, *, confidence: str, guardrail_passed: bool) -> Decision:
    if not guardrail_passed:
        return Decision(Rung.SUGGEST, "guardrail blocked a violation; forced to suggest-only")
    if confidence == "low":
        return Decision(Rung.SUGGEST, "low-confidence product resolution; forced to suggest-only")
    rung = Rung(min(configured_level, Rung.AUTO_ACT))
    return Decision(rung, f"seller-configured automation level {rung.name}")
