"""
Live chat ingestion.

`ChatStream` is an async generator standing in for the real transport (a
platform webhook or websocket firehose). It exists so the rest of the
pipeline is written against a stable `ChatMessage` shape and can be
back-pressure-tested with a synthetic burst independent of any one
marketplace's API. Swapping in a real adapter means writing one function
that yields `ChatMessage` from that platform's event format — see
TDD.md, "Marketplace integrations."

`classify_intent` is a fast, explainable, keyword/pattern classifier
rather than an LLM call. Classification gates which automation-ladder
rung applies (ladder.py) *before* we spend any money or latency on a
model call, so it has to be cheap, deterministic, and auditable on its
own. A message that reads as ambiguous is intentionally graded down to
"general", which forces suggest-only (ladder level 0) rather than letting
a misclassification slip into auto-send. See TDD.md for the false-negative
vs. false-positive trade-off.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

SIZE_TOKENS = {"XS", "S", "M", "L", "XL", "XXL"}
_SIZE_RE = re.compile(r"\b(XXL|XS|S|M|L|XL)\b", re.IGNORECASE)
_SIZE_WORDS = {
    "extra small": "XS", "small": "S", "medium": "M", "large": "L",
    "extra large": "XL", "x-large": "XL", "xlarge": "XL",
}

_PRICE_PATTERNS = [r"\bhow much\b", r"\bprice\b", r"\bcost\b", r"\$"]
_AVAILABILITY_PATTERNS = [r"\bin stock\b", r"\bavailable\b", r"\bdo you have\b", r"\bleft\b",
                          r"\bsold out\b", r"\bany more\b", r"\bhow many\b", r"\bstill got\b",
                          r"\bany\s+\w+\s+left\b"]
_POLICY_PATTERNS = [r"\bshipping\b", r"\breturn\b", r"\brefund\b", r"\bdamaged?\b", r"\bauthentic\b",
                     r"\bfake\b", r"\breal\b"]
_PURCHASE_PATTERNS = [r"\bi'?ll take\b", r"\bsold\b!", r"\bcan i buy\b", r"\badd to cart\b",
                      r"\bi want (it|one|this)\b"]
_FIT_PATTERNS = [r"\btrue to size\b", r"\bruns? (one size )?(small|big|large)\b", r"\bpetite\b",
                 r"\bsize (up|down)\b", r"\bsizing\b", r"\bhow does it fit\b", r"\bfits? (me|true)\b",
                 r"\boversized\b", r"\bstretchy?\b"]
# Fit language that borrows size words ("runs small") must not be read as a
# size request; see extract_size.
_FIT_SIZE_CONTEXT = r"\b(?:runs?|fits?)\s+(?:one\s+size\s+)?{word}\b"
_PROFANITY = {"damn", "hell", "crap", "shit", "fuck", "ass", "bitch"}  # tone guardrail wordlist


@dataclass
class ChatMessage:
    id: str
    ts: float
    viewer: str
    text: str
    intent: str = "general"
    sku_hint: Optional[str] = None
    size_hint: Optional[str] = None
    contains_profanity: bool = False


def extract_size(text: str) -> Optional[str]:
    m = _SIZE_RE.search(text)
    if m:
        return m.group(1).upper()
    lowered = text.lower()
    for word, token in sorted(_SIZE_WORDS.items(), key=lambda kv: -len(kv[0])):
        if not re.search(rf"\b{re.escape(word)}\b", lowered):
            continue
        # "runs small" / "fits large" describe cut, not a requested size.
        # Without this check, "do you have anything that runs small" would be
        # answered with size-S stock counts -- a true statement, and not an
        # answer to the question asked. The guardrail cannot catch that class
        # of error, since nothing stated is false (see TDD.md).
        if re.search(_FIT_SIZE_CONTEXT.format(word=re.escape(word)), lowered):
            continue
        return token
    return None


def classify_intent(text: str) -> str:
    t = text.lower()
    if any(re.search(p, t) for p in _PURCHASE_PATTERNS):
        return "purchase_intent"
    if any(re.search(p, t) for p in _POLICY_PATTERNS):
        return "policy_question"
    # Fit is checked before availability on purpose. "do you have this in
    # petite" matches both, and the useful answer is about sizing (we carry
    # no petite cut) rather than a stock count for a size that doesn't exist
    # in the catalog. The cost is that a message mixing both ("does it run
    # small, do you have an M?") answers the fit half only.
    if any(re.search(p, t) for p in _FIT_PATTERNS):
        return "fit_question"
    if any(re.search(p, t) for p in _AVAILABILITY_PATTERNS):
        return "availability_question"
    if any(re.search(p, t) for p in _PRICE_PATTERNS):
        return "price_question"
    return "general"


def contains_profanity(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())
    return any(w in _PROFANITY for w in words)


def make_message(viewer: str, text: str) -> ChatMessage:
    return ChatMessage(
        id=str(uuid.uuid4()),
        ts=time.time(),
        viewer=viewer,
        text=text,
        intent=classify_intent(text),
        size_hint=extract_size(text),
        contains_profanity=contains_profanity(text),
    )


# A representative burst standing in for a real live-stream chat firehose.
# Deliberately includes: exact-SKU-free natural language, a size question on
# a sold-out size, a promo-price question, a policy question, an out-of-scope
# purchase intent, and one hostile/off-tone message to exercise the tone
# guardrail.
SAMPLE_SCRIPT = [
    ("jess_84", "omg is the coral dress true to size?"),
    ("t.marie", "do you have the coral one in a M??"),
    ("t.marie", "wait what about small, is that in stock"),
    ("buyer_lyn", "how much is the sage dress rn"),
    ("q_and_a", "what's your return policy if it doesn't fit"),
    ("skeptic99", "is this real or some fake crap"),
    ("shopper2", "sold! i'll take the coral in L"),
    ("random_v", "does shipping take forever"),
]


async def simulated_stream(delay_s: float = 0.0) -> AsyncIterator[ChatMessage]:
    for viewer, text in SAMPLE_SCRIPT:
        yield make_message(viewer, text)
        if delay_s:
            await asyncio.sleep(delay_s)
