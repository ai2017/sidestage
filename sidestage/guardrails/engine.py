"""
Guardrail engine — depth area #1 (agentic-write safety).

The core design decision: guardrails never trust the reply generator's own
citations. They re-derive ground truth themselves, straight from the
store, using the exact same GroundingTools the generator used (or should
have used), and then parse the *drafted text* for factual claims to check
against that ground truth. This means a hallucinated price, a stale stock
count, or a made-up policy is caught even if it came from a well-behaved
tool-use transcript that merely misread its own tool result, or from an
LLM backend that skipped the tool call entirely and guessed.

Three checks, each independent and each producing a structured violation:

  1. Price accuracy   — every "$X.XX" in the draft must equal the current
     effective (promo-aware) price for the resolved SKU.
  2. Availability      — "N left" must not exceed real stock; "sold out"
     must not be claimed while stock > 0; "in stock" must not be claimed
     while stock == 0.
  3. Policy grounding  — for policy-intent replies, the draft must
     substantially overlap with the actual top-matched policy text, not a
     paraphrase invented on the spot.

Plus a tone check (independent of grounding): profanity or shouting is
blocked regardless of factual accuracy.

Concrete failure path: a *correctable* violation (price only) is
auto-corrected and forced back down to suggest-only (never sent
automatically, even if the ladder would otherwise allow it, because the
draft was wrong once). An *uncorrectable* violation (availability, policy,
tone, or low-confidence resolution) blocks the send outright and is
escalated to the seller with the specific reason. See TDD.md, "Guardrails:
what fails open vs. closed."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sidestage.grounding.retrieval import GroundingTools
from sidestage.ingestion.stream import ChatMessage, contains_profanity
from sidestage.reply.generator import ReplyDraft

_MONEY_RE = re.compile(r"\$(\d+(?:\.\d{2})?)")
_LEFT_RE = re.compile(r"\b(\d+)\s+left\b", re.IGNORECASE)
_SOLD_OUT_RE = re.compile(r"\bsold out\b|\bout of stock\b|\bno more\b", re.IGNORECASE)
_IN_STOCK_RE = re.compile(r"\byes[!,]?\s+we have\b|\bin stock\b|\bwe'?ve got\b", re.IGNORECASE)
_DISMISSIVE_RE = re.compile(r"figure it out|not my problem|read the description", re.IGNORECASE)


@dataclass
class Violation:
    kind: str          # "price" | "availability" | "policy" | "tone" | "confidence"
    detail: str
    correctable: bool


@dataclass
class GuardrailResult:
    passed: bool
    final_text: str
    violations: list[Violation] = field(default_factory=list)
    corrected: bool = False

    @property
    def blocking_violations(self) -> list[Violation]:
        return [v for v in self.violations if not v.correctable]


class GuardrailEngine:
    def __init__(self, tools: GroundingTools) -> None:
        self.tools = tools

    def review(self, message: ChatMessage, draft: ReplyDraft) -> GuardrailResult:
        violations: list[Violation] = []
        text = draft.text

        if draft.confidence == "low":
            violations.append(Violation(
                "confidence", "Product reference could not be resolved with confidence.", False))

        if draft.resolved_sku and message.intent == "price_question":
            text, price_violation = self._check_price(text, draft.resolved_sku)
            if price_violation:
                violations.append(price_violation)

        if draft.resolved_sku and message.intent in ("availability_question", "purchase_intent"):
            avail_violation = self._check_availability(text, draft.resolved_sku, message.size_hint)
            if avail_violation:
                violations.append(avail_violation)

        if message.intent == "policy_question":
            policy_violation = self._check_policy(text, message.text)
            if policy_violation:
                violations.append(policy_violation)

        tone_violation = self._check_tone(text)
        if tone_violation:
            violations.append(tone_violation)

        blocking = [v for v in violations if not v.correctable]
        corrected = text != draft.text
        passed = len(blocking) == 0
        return GuardrailResult(passed=passed, final_text=text, violations=violations,
                                corrected=corrected)

    # ---- individual checks ----

    def _check_price(self, text: str, sku: str) -> tuple[str, Optional[Violation]]:
        truth = self.tools.lookup_price(sku)
        if not truth.get("found"):
            return text, None
        effective = truth["effective_price_cents"] / 100
        # A promo'd item legitimately mentions both the effective price and
        # the crossed-out list price ("$54.00, marked down from $64.00") --
        # both are true statements and neither should trip the guardrail.
        acceptable = {round(effective, 2)}
        if truth.get("promo_price_cents"):
            acceptable.add(round(truth["list_price_cents"] / 100, 2))

        bad_values: list[float] = []

        def repl(m: re.Match) -> str:
            val = float(m.group(1))
            if any(abs(val - a) < 0.001 for a in acceptable):
                return m.group(0)
            bad_values.append(val)
            return f"${effective:.2f}"

        corrected_text = _MONEY_RE.sub(repl, text)
        if not bad_values:
            return text, None
        return corrected_text, Violation(
            "price",
            f"Draft stated ${bad_values[0]:.2f} but authoritative effective price is ${effective:.2f}; auto-corrected.",
            correctable=True,
        )

    def _check_availability(self, text: str, sku: str, size_hint: Optional[str]) -> Optional[Violation]:
        stock = self.tools.lookup_stock(sku, size_hint) if size_hint else self.tools.lookup_stock(sku)
        if not stock.get("found"):
            return None

        if size_hint:
            if not stock.get("known_size"):
                return None
            qty = stock["stock_qty"]
        else:
            by_size = stock.get("by_size", {})
            qty = sum(by_size.values())

        left_match = _LEFT_RE.search(text)
        if left_match:
            claimed = int(left_match.group(1))
            if claimed > qty:
                return Violation(
                    "availability",
                    f"Draft claims {claimed} left but actual stock is {qty}.",
                    correctable=False,
                )
        if _SOLD_OUT_RE.search(text) and qty > 0:
            return Violation(
                "availability",
                f"Draft claims sold out but {qty} units are actually in stock.",
                correctable=False,
            )
        if _IN_STOCK_RE.search(text) and qty == 0:
            return Violation(
                "availability",
                "Draft implies availability but stock is 0.",
                correctable=False,
            )
        return None

    def _check_policy(self, text: str, viewer_question: str) -> Optional[Violation]:
        hits = self.tools.search_policy(viewer_question)
        if not hits:
            return Violation("policy", "No matching policy found to ground this answer.", False)
        best = hits[0]
        # Require the reply to substantially reuse the authoritative policy
        # text rather than free-associate a paraphrase (crude but auditable:
        # word-overlap ratio against the source policy).
        src_words = set(re.findall(r"[a-z']+", best["text"].lower()))
        reply_words = set(re.findall(r"[a-z']+", text.lower()))
        overlap = len(src_words & reply_words) / max(1, len(src_words))
        if overlap < 0.3:
            return Violation(
                "policy",
                f"Reply overlaps only {overlap:.0%} with authoritative '{best['topic']}' policy text; "
                "looks unsupported rather than grounded.",
                correctable=False,
            )
        return None

    def _check_tone(self, text: str) -> Optional[Violation]:
        if contains_profanity(text):
            return Violation("tone", "Draft contains profanity.", correctable=False)
        if _DISMISSIVE_RE.search(text):
            return Violation("tone", "Draft is dismissive toward the customer.", correctable=False)
        letters = [c for c in text if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6 and len(letters) > 8:
            return Violation("tone", "Draft is in excessive caps (reads as shouting).", correctable=False)
        return None
