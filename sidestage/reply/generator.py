"""
Reply generation: turns a classified chat message + a resolved product
into a draft reply, via function-calling grounding tools.

Two backends behind one interface (`ReplyGenerator.draft`):

  * `LLMBackend`  — real Claude, tool-use loop over GroundingTools. Used
    whenever ANTHROPIC_API_KEY is set.
  * `TemplateBackend` — deterministic, no network call, same tool calls
    made explicitly in code instead of chosen by a model.

This is not a cost-cutting shortcut: it is what lets the whole pipeline
run and be graded with zero external dependencies, and it is what makes
the guardrail engine's job well-defined. Guardrails do not trust *either*
backend's self-report of what it looked up — they independently re-call
lookup_price/lookup_stock/search_policy themselves (guardrails/engine.py)
against the resolved SKU and compare against the drafted text. So a
hallucinating LLM backend and a buggy template backend fail the same way:
the guardrail catches a mismatch between claim and ground truth, not
between "which backend produced this."
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from sidestage.grounding.retrieval import GroundingTools
from sidestage.ingestion.stream import ChatMessage


# Fit facts live in the catalog description ("...true to size, hand wash
# cold"), so a fit answer can be grounded in the same store as price and
# stock rather than improvised. Key = the phrase as it appears in a
# description; value = the same claim in sentence form for a reply. The
# guardrail maps the sentence form back to the key and checks it really is
# in that product's description (guardrails/engine.py::_check_fit).
FIT_PHRASES = {
    "true to size": "is true to size",
    "runs one size small": "runs one size small",
    "runs one size big": "runs one size big",
    "runs small": "runs small",
    "runs big": "runs big",
    "oversized fit": "has an oversized fit",
}


def fit_phrase_for(product: Optional[dict]) -> Optional[str]:
    """The fit claim this product's description actually supports, if any."""
    if not product:
        return None
    desc = (product.get("description") or "").lower()
    for raw in sorted(FIT_PHRASES, key=len, reverse=True):
        if raw in desc:
            return raw
    return None


@dataclass
class ReplyDraft:
    text: str
    confidence: str  # "high" | "low"
    backend: str     # "llm" | "template"
    tool_trace: list[dict] = field(default_factory=list)
    resolved_sku: Optional[str] = None


class TemplateBackend:
    """Deterministic function-calling stand-in. Every branch explicitly
    calls a GroundingTools method and records the call in tool_trace, so
    the trace looks the same shape as a real tool-use transcript would."""

    def draft(self, message: ChatMessage, tools: GroundingTools,
              resolved_sku: Optional[str]) -> ReplyDraft:
        trace: list[dict] = []

        def call(name: str, **kwargs) -> dict:
            result = tools.call(name, kwargs)
            trace.append({"tool": name, "input": kwargs, "output": result})
            return result

        if resolved_sku is None:
            return ReplyDraft(
                text="Hey! Which item did you mean — can you tell me the color or drop a "
                     "screenshot? Want to make sure I get you the right one \U0001F495",
                confidence="low", backend="template", tool_trace=trace, resolved_sku=None,
            )

        product = tools.store.get_product(resolved_sku)
        name = product["name"] if product else resolved_sku

        if message.intent == "price_question":
            price = call("lookup_price", sku=resolved_sku)
            cents = price["effective_price_cents"]
            dollars = cents / 100
            if price.get("promo_price_cents"):
                orig = price["list_price_cents"] / 100
                text = f"The {name} is ${dollars:.2f} right now (marked down from ${orig:.2f})!"
            else:
                text = f"The {name} is ${dollars:.2f}!"
            return ReplyDraft(text, "high", "template", trace, resolved_sku)

        if message.intent == "availability_question":
            size = message.size_hint
            stock = call("lookup_stock", sku=resolved_sku, size=size) if size else \
                call("lookup_stock", sku=resolved_sku)
            if size:
                if not stock.get("known_size"):
                    text = f"We don't carry the {name} in that size, sorry!"
                elif stock["stock_qty"] > 0:
                    text = f"Yes! We have {stock['stock_qty']} left of the {name} in size {size.upper()}."
                else:
                    text = f"Just sold out of size {size.upper()} in the {name} — sorry! I'll flag it if we restock."
            else:
                by_size = stock.get("by_size", {})
                have = [s for s, q in by_size.items() if q > 0]
                text = (f"We've got the {name} left in: {', '.join(have)}!" if have
                        else f"The {name} is sold out in every size right now, sorry!")
            return ReplyDraft(text, "high", "template", trace, resolved_sku)

        if message.intent == "policy_question":
            hits = call("search_policy", query=message.text)
            if hits:
                text = hits[0]["text"]
            else:
                text = "Great question — let me check on that policy and get back to you!"
            return ReplyDraft(text, "high" if hits else "low", "template", trace, resolved_sku)

        if message.intent == "fit_question":
            sizes = json.loads(product["sizes"]) if product else []
            raw = fit_phrase_for(product)
            wants_petite = bool(re.search(r"\bpetite\b|\btall\b", message.text.lower()))
            stocks_petite = any(("petite" in s.lower() or "tall" in s.lower()) for s in sizes)

            if wants_petite and not stocks_petite:
                # Grounded in the catalog's own size list, not an assumption.
                tail = f" It {FIT_PHRASES[raw]}." if raw else ""
                text = (f"We don't carry petite sizing, sorry! The {name} comes in "
                        f"{', '.join(sizes)}.{tail}")
                return ReplyDraft(text, "high", "template", trace, resolved_sku)

            if raw:
                text = f"The {name} {FIT_PHRASES[raw]} — hope that helps!"
                return ReplyDraft(text, "high", "template", trace, resolved_sku)

            # No fit information in the catalog for this item: say nothing
            # rather than guess, and let the ladder force suggest-only.
            text = f"Let me double-check the fit on the {name} and come right back to you!"
            return ReplyDraft(text, "low", "template", trace, resolved_sku)

        if message.intent == "purchase_intent":
            # Check stock before encouraging the sale. Without this, a buyer
            # claiming a sold-out size gets "grab it before it's gone" -- and
            # while the action layer correctly refuses to drive stock negative
            # (pipeline._execute_bound_action swallows the ActionError), the
            # *reply* would still have promised something we can't deliver.
            # The guardrail can't catch this one for us: the cheerful text
            # makes no stock claim for _check_availability to compare against.
            size = message.size_hint
            if size:
                stock = call("lookup_stock", sku=resolved_sku, size=size)
                if stock.get("known_size") and stock["stock_qty"] <= 0:
                    text = (f"Ah — the {name} just sold out in size {size.upper()}, sorry! "
                            "I'll flag it if we restock.")
                    return ReplyDraft(text, "high", "template", trace, resolved_sku)
            text = f"Yay, so glad you want the {name}! Tap the pinned link/cart to grab it before it's gone."
            return ReplyDraft(text, "high", "template", trace, resolved_sku)

        text = f"Thanks for the message! Let me know if you have questions about the {name} \U0001F495"
        return ReplyDraft(text, "low", "template", trace, resolved_sku)


class LLMBackend:
    """Real Claude tool-use loop. Only exercised when ANTHROPIC_API_KEY is set;
    kept behind the same interface as TemplateBackend so swapping backends is
    a one-line change (see ReplyGenerator.__init__)."""

    def __init__(self, model: str = "claude-3-5-haiku-latest") -> None:
        import anthropic  # imported lazily; only needed on this path

        self._client = anthropic.Anthropic()
        self._model = model

    def draft(self, message: ChatMessage, tools: GroundingTools,
              resolved_sku: Optional[str]) -> ReplyDraft:
        trace: list[dict] = []
        system = (
            "You are a live-shopping seller's chat copilot. Use tools to look up any "
            "price, stock, or policy fact before stating it. Keep replies under 30 words, "
            "warm, on-brand, no emoji spam. If you are not sure which product the viewer "
            "means, ask a short clarifying question instead of guessing."
        )
        context = f"Resolved product SKU (may be None if ambiguous): {resolved_sku}"
        messages = [{"role": "user", "content": f"{context}\nViewer message: {message.text!r}"}]
        tool_schemas = tools.tool_schemas()

        for _ in range(4):  # bounded tool-use loop
            resp = self._client.messages.create(
                model=self._model, max_tokens=300, system=system,
                tools=tool_schemas, messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                final_text = "".join(b.text for b in resp.content if b.type == "text")
                return ReplyDraft(final_text.strip(), "high", "llm", trace, resolved_sku)

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                result = tools.call(block.name, block.input)
                trace.append({"tool": block.name, "input": block.input, "output": result})
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": str(result),
                })
            messages.append({"role": "user", "content": tool_results})

        return ReplyDraft("Let me get back to you on that in just a sec!", "low", "llm",
                           trace, resolved_sku)


@dataclass
class ReplyGenerator:
    tools: GroundingTools
    backend: object = None

    def __post_init__(self) -> None:
        if self.backend is None:
            self.backend = LLMBackend() if os.environ.get("ANTHROPIC_API_KEY") else TemplateBackend()

    def draft(self, message: ChatMessage, resolved_sku: Optional[str]) -> ReplyDraft:
        return self.backend.draft(message, self.tools, resolved_sku)
