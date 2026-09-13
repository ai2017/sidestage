"""
Grounding tools: the only way the reply generator is allowed to learn a
fact about price, stock, listing state, or policy. Everything here is a
plain Python function with a JSON-schema wrapper so it can be exposed to an
LLM as a function-calling "tool" (Anthropic tool_use format) *and* called
directly by the guardrail engine to re-verify a draft reply — same code
path, so grounding and verification can never silently drift apart.

Depth area #1 (retrieval & function calling): exact-match lookups
(SKU/size) are used whenever the message gives us an exact key, because
guessing at a price or stock count is exactly the failure mode this system
exists to prevent. Fuzzy retrieval (TF-IDF cosine similarity over
name+description+color+category) is used only for the "which product are
they talking about" step, when the viewer says "the coral one" or
"the linen dress" instead of a SKU. See TDD.md for why TF-IDF over a local
catalog was chosen over an embedding/vector-DB stack at this scale, and
what would change if the catalog were 10,000+ SKUs instead of ~4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from sidestage.store import Store


def _doc_text(product: dict) -> str:
    return f"{product['name']} {product['color']} {product['category']} {product['description']}"


def _vectorizer() -> TfidfVectorizer:
    # Character n-grams, not word tokens: a viewer says "return" and the
    # policy says "returns"/"returnable"; a word-level TF-IDF treats those
    # as unrelated tokens (zero cosine similarity -- verified empirically,
    # see TDD.md "Why char n-grams, not word tokens"), while a 3-5 character
    # window shares most of its grams across the three forms. This is a
    # lexical-overlap technique, not semantic search: a query that shares no
    # substrings with the source text (e.g. "is this real or fake" against
    # an authenticity policy that never says "real" or "fake") still won't
    # match. See PRD.md/TDD.md known limitations for the concrete case.
    return TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))


POLICY_MATCH_THRESHOLD = 0.12
PRODUCT_MATCH_THRESHOLD = 0.15


@dataclass
class GroundingTools:
    store: Store

    # ---- exact lookups ----

    def lookup_price(self, sku: str) -> dict:
        p = self.store.get_product(sku)
        if not p:
            return {"found": False, "sku": sku}
        effective = p["promo_price_cents"] or p["price_cents"]
        return {
            "found": True,
            "sku": sku,
            "list_price_cents": p["price_cents"],
            "promo_price_cents": p["promo_price_cents"],
            "effective_price_cents": effective,
        }

    def lookup_stock(self, sku: str, size: Optional[str] = None) -> dict:
        p = self.store.get_product(sku)
        if not p:
            return {"found": False, "sku": sku}
        if size:
            qty = self.store.get_stock(sku, size.upper())
            if qty is None:
                return {"found": True, "sku": sku, "size": size, "known_size": False}
            return {"found": True, "sku": sku, "size": size.upper(), "known_size": True,
                     "stock_qty": qty, "in_stock": qty > 0}
        return {"found": True, "sku": sku, "by_size": self.store.stock_for_sku(sku)}

    def get_live_listing(self) -> dict:
        listing = self.store.live_listing()
        if not listing:
            return {"found": False}
        product = self.store.get_product(listing["sku"])
        return {"found": True, "listing": listing, "product": product}

    # ---- fuzzy retrieval ----

    def search_products(self, query: str, top_k: int = 3) -> list[dict]:
        products = self.store.all_products()
        if not products:
            return []
        corpus = [_doc_text(p) for p in products]
        vec = _vectorizer()
        try:
            matrix = vec.fit_transform(corpus)
            qvec = vec.transform([query])
        except ValueError:
            return []
        sims = cosine_similarity(qvec, matrix).flatten()
        ranked = sorted(zip(products, sims), key=lambda t: t[1], reverse=True)
        return [
            {**prod, "match_score": round(float(score), 4)}
            for prod, score in ranked[:top_k] if score >= PRODUCT_MATCH_THRESHOLD
        ]

    def search_policy(self, query: str, top_k: int = 1) -> list[dict]:
        policies = self.store.all_policies()
        if not policies:
            return []
        corpus = [f"{p['topic']} {p['text']}" for p in policies]
        vec = _vectorizer()
        try:
            matrix = vec.fit_transform(corpus)
            qvec = vec.transform([query])
        except ValueError:
            return []
        sims = cosine_similarity(qvec, matrix).flatten()
        ranked = sorted(zip(policies, sims), key=lambda t: t[1], reverse=True)
        return [
            {**pol, "match_score": round(float(score), 4)}
            for pol, score in ranked[:top_k] if score >= POLICY_MATCH_THRESHOLD
        ]

    # ---- tool schemas (Anthropic tool_use / OpenAI function-calling compatible) ----

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        return [
            {
                "name": "lookup_price",
                "description": "Get the authoritative list and effective (promo-applied) price for a SKU.",
                "input_schema": {
                    "type": "object",
                    "properties": {"sku": {"type": "string"}},
                    "required": ["sku"],
                },
            },
            {
                "name": "lookup_stock",
                "description": "Get authoritative stock quantity for a SKU, optionally a specific size.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "size": {"type": "string"},
                    },
                    "required": ["sku"],
                },
            },
            {
                "name": "search_products",
                "description": "Fuzzy-match a natural-language product description ('the coral one') to catalog SKUs.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "search_policy",
                "description": "Find the store policy text most relevant to a question (shipping/returns/damage/authenticity).",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "get_live_listing",
                "description": "Get the product currently on-air in the live stream.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]

    def call(self, tool_name: str, tool_input: dict) -> dict | list[dict]:
        fn = getattr(self, tool_name, None)
        if fn is None:
            return {"error": f"unknown tool {tool_name}"}
        return fn(**tool_input)
