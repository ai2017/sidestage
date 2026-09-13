"""
End-to-end orchestration: ingestion -> grounding/resolution -> reply
generation -> guardrails -> automation ladder -> (optional) action
execution -> persistence. One function, `process_message`, is the "core
loop traced end to end" the AI-interview logistics ask for: what comes in
(ChatMessage), what transforms it (resolution, drafting, guardrails,
ladder), where state lives (sidestage/store.py, one sqlite file), and what
comes out (a PipelineResult with the sent/blocked reply and any action
taken).

Every stage is wrapped in perf_counter() timing so the latency budget
(sub-2s reply latency, PRD success metric) is measured per-stage, not just
end to end -- see tests/test_latency.py, the empirical-validation spike.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from sidestage.actions.tools import ActionError, ActionResult, stock_adjust
from sidestage.grounding.retrieval import GroundingTools
from sidestage.guardrails.engine import GuardrailEngine, GuardrailResult
from sidestage.ingestion.stream import ChatMessage
from sidestage.ladder import Decision, Rung, decide
from sidestage.reply.generator import ReplyDraft, ReplyGenerator
from sidestage.store import Store

# The one bound action per intent type that AUTO_ACT (ladder rung 2) is
# allowed to trigger. Deliberately a tiny, explicit table rather than a
# model-chosen action -- see TDD.md, "Why the ladder's action binding is a
# lookup table, not a tool the LLM picks at rung 2."
RESOLUTION_MATCH_THRESHOLD = 0.2


@dataclass
class StageTimings:
    resolve_ms: float = 0.0
    draft_ms: float = 0.0
    guardrail_ms: float = 0.0
    action_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.resolve_ms + self.draft_ms + self.guardrail_ms + self.action_ms


@dataclass
class PipelineResult:
    message: ChatMessage
    resolved_sku: Optional[str]
    draft: ReplyDraft
    guardrail: GuardrailResult
    decision: Decision
    action: Optional[ActionResult]
    timings: StageTimings
    final_status: str  # "sent" | "suggested"


def resolve_product(tools: GroundingTools, message: ChatMessage) -> Optional[str]:
    """Best-effort mapping from natural-language chat text to a catalog SKU.

    Exact reference (none here -- viewers don't type SKUs) would always win
    if present; absent that, fuzzy match on the message text, and fall back
    to whatever's currently live on-air, since most unqualified questions
    ("is this in stock?", "what about small") are about the item the seller
    is actively showing.
    """
    matches = tools.search_products(message.text, top_k=2)
    if matches and matches[0]["match_score"] >= RESOLUTION_MATCH_THRESHOLD:
        if len(matches) == 1 or (matches[0]["match_score"] - matches[1]["match_score"] > 0.05):
            return matches[0]["sku"]

    live = tools.get_live_listing()
    if live.get("found"):
        return live["product"]["sku"]
    return None


class Pipeline:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.tools = GroundingTools(store)
        self.generator = ReplyGenerator(self.tools)
        self.guardrails = GuardrailEngine(self.tools)

    def process_message(self, message: ChatMessage) -> PipelineResult:
        timings = StageTimings()

        t0 = time.perf_counter()
        resolved_sku = resolve_product(self.tools, message)
        timings.resolve_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        draft = self.generator.draft(message, resolved_sku)
        timings.draft_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        guardrail = self.guardrails.review(message, draft)
        timings.guardrail_ms = (time.perf_counter() - t0) * 1000

        configured_level = self.store.ladder_level(message.intent)
        decision = decide(configured_level, confidence=draft.confidence,
                           guardrail_passed=guardrail.passed)

        action: Optional[ActionResult] = None
        t0 = time.perf_counter()
        if decision.rung == Rung.AUTO_ACT:
            action = self._execute_bound_action(message, resolved_sku)
        timings.action_ms = (time.perf_counter() - t0) * 1000

        # Status is driven entirely by the ladder decision, which has already
        # folded guardrail pass/fail and draft confidence into the rung
        # (see ladder.decide). A guardrail violation never "hides" a reply --
        # it downgrades the rung to SUGGEST so the seller still sees the
        # (possibly auto-corrected) draft plus the reason it wasn't auto-sent.
        status = "sent" if decision.rung in (Rung.AUTO_SEND, Rung.AUTO_ACT) else "suggested"

        self.store.record_message({
            "id": message.id, "ts": message.ts, "viewer": message.viewer, "text": message.text,
            "intent": message.intent, "reply_text": guardrail.final_text, "reply_status": status,
            "guardrail_notes": [f"{v.kind}: {v.detail}" for v in guardrail.violations],
            "latency_ms": timings.total_ms,
        })

        return PipelineResult(message, resolved_sku, draft, guardrail, decision, action,
                               timings, status)

    def _execute_bound_action(self, message: ChatMessage, resolved_sku: Optional[str]) -> Optional[ActionResult]:
        if message.intent != "purchase_intent" or not resolved_sku or not message.size_hint:
            return None
        try:
            return stock_adjust(self.store, resolved_sku, message.size_hint, -1, actor="copilot")
        except ActionError:
            return None
