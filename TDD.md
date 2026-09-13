# SideStage — Technical Design Document

## System overview

```
 ChatStream (async gen)                                                                 
       |                                                                                
       v                                                                                
 classify_intent + extract_size  --------------------------------------------+          
       |                                                                     |          
       v                                                                     |          
 resolve_product (fuzzy match or live-listing fallback)  -- GroundingTools --+          
       |                                                                     |          
       v                                                                     |          
 ReplyGenerator.draft  (TemplateBackend | LLMBackend, tool-calling) -- GroundingTools    
       |                                                                                
       v                                                                                
 GuardrailEngine.review  -- independently re-calls GroundingTools against resolved_sku   
       |                                                                                
       v                                                                                
 ladder.decide(configured_level, confidence, guardrail_passed) -> SUGGEST|AUTO_SEND|AUTO_ACT
       |                                                                                
       v                                                                                
 [optional] actions/tools.py (push/swap/markdown/stock_adjust) -> audit_log (sqlite)     
       |                                                                                
       v                                                                                
 store.record_message  -->  FastAPI /api/chat, /ws  -->  console/index.html
```

One sqlite file (`sidestage/store.py`) is the single source of truth for catalog, inventory, listings, policies, ladder settings, the message log, and the audit log. Every stage above is timed independently (`pipeline.StageTimings`) so the latency budget can be attributed to a specific stage rather than measured only end to end.

Two depth areas were chosen, per the brief's instruction to go deep in at least one: **agentic-write safety** (the guardrail engine, and action auditability/rollback) as the primary spike, and **retrieval & function calling** (grounding tools + resolution) as the second. A third, smaller spike is empirical: `tests/test_latency.py` measures p50/p95/p99 latency over a synthetic burst rather than asserting a latency budget was met from a diagram.

---

## Streaming ingestion

**Decision:** `sidestage/ingestion/stream.py` exposes an `async def simulated_stream()` that yields `ChatMessage` objects from a fixed script, and a synchronous `classify_intent`/`extract_size`/`contains_profanity` set of pure functions that any transport can call.

**Alternatives considered:**
- A real websocket client against an actual live-commerce platform. Rejected for this build because no such platform's chat API is available to integrate against in the challenge window, and because coupling the core loop to one vendor's event schema would make the mandatory-workflow demo dependent on that vendor being reachable at review time. The async-generator boundary is deliberately the exact shape a real adapter would have to satisfy (`AsyncIterator[ChatMessage]`), so integrating platform N is "write one function," not a redesign — see "Marketplace integrations" below.
- An LLM-based intent classifier instead of keyword/regex patterns. Rejected for the classification step specifically (not for reply drafting) because classification gates which ladder rung even applies before any model cost is spent, so it has to be cheap and — just as important — *auditable*: a reviewer or a seller can read `_PRICE_PATTERNS`/`_AVAILABILITY_PATTERNS`/etc. and know exactly why a message was classified a given way, which is not true of a model's internal decision. The cost of this choice is real: natural-language variation the pattern list doesn't anticipate (a size given as "medium" was one such gap, fixed during development — see `_SIZE_WORDS` in `stream.py`) falls through to `"general"`, which is a safe failure mode (forces suggest-only) but not a good one long-term. A production version would run the same message through both the classifier and, on ambiguous/low-confidence classifications, an LLM prompted specifically for confidence-scored intent classification, keeping the cheap path as the fast common case.
- Backpressure/ordering: the generator is intentionally single-consumer and processes one message fully before the next (no concurrent pipeline instances against the same sqlite connection), which is fine for the write volume of a single seller's stream but would need per-seller sharding or a proper task queue at multi-seller scale (see "Known limitations").

---

## Catalog grounding & retrieval (depth area)

**Decision:** `sidestage/grounding/retrieval.py` exposes both exact-key lookups (`lookup_price`, `lookup_stock` — SKU/size are unambiguous keys, so these are direct sqlite reads) and fuzzy retrieval (`search_products`, `search_policy`) for the "which product/policy does this message mean" step, using a **character n-gram (3-5) TF-IDF vectorizer with cosine similarity**, not word-level TF-IDF and not an embedding/vector-DB stack.

**Why not word-level TF-IDF:** this was the first implementation, and it failed empirically during development. A viewer asking "what's your *return* policy" against a policy whose text says "*Returns* accepted... not *returnable*" scored a cosine similarity of **exactly 0.0** — word tokenization treats "return," "returns," and "returnable" as three unrelated vocabulary entries, so a real question about the store's actual, correctly-worded return policy would never retrieve it. This was caught by building a scratch repro (kept conceptually as `tests/test_retrieval.py::test_policy_search_handles_morphological_variants`) before it ever reached the guardrail layer. Switching to `analyzer="char_wb", ngram_range=(3,5)` fixed this specific case (0.0 -> ~0.40 cosine similarity) because "return," "returns," and "returnable" share most of their 3-5 character substrings.

**Why not an embedding/vector-DB stack:** at four SKUs and four policy documents, an embedding model call (local or hosted) per query adds latency and an external dependency for a lookup that character n-grams already solve, and it would need a model download or an API call this prototype specifically avoids requiring (see "Latency budget" and the LLM-backend design below). This is a decision that inverts at scale: past a few hundred SKUs with real product-description overlap (many "midi dress" entries, say), character n-grams stop being discriminating enough and a real embedding index (even a local, small one) becomes the right call. That crossover point, not "TF-IDF forever," is the actual claim being made here.

**What this still can't do, and why that's the correct failure mode, not a bug to paper over:** `search_policy("is this real or some fake crap")` returns `[]`. The authenticity policy's actual text ("purchased new from the listed brand," "not... replicas") shares essentially no character substrings with "real"/"fake"/"crap." A semantic embedding model would very likely connect these; lexical retrieval, by construction, cannot. `POLICY_MATCH_THRESHOLD` (0.12) exists specifically so that a low-quality match is treated as *no* match rather than confidently returned — and the guardrail engine's policy check (below) then forces that reply to suggest-only with a legible reason, rather than the copilot inventing a plausible-sounding authenticity claim from nothing. This is exercised directly in `tests/test_retrieval.py::test_policy_search_known_limitation_no_semantic_match` and is called out again in PRD.md's known-limitations section, per the instruction to note divergences directly rather than let a reviewer find them first.

**Function calling:** `GroundingTools.tool_schemas()` returns Anthropic tool-use-compatible JSON schemas for all five tools. `LLMBackend` (see below) passes these to a real tool-use loop; `TemplateBackend` calls the exact same methods directly and records an identical-shaped trace (`{tool, input, output}`), so the guardrail layer and the console UI's "tool trace" display don't need to know which backend produced a draft.

---

## Reply generation and the LLM/template backend split

**Decision:** `ReplyGenerator` picks `LLMBackend` (real Claude, tool-use loop, `sidestage/reply/generator.py`) when `ANTHROPIC_API_KEY` is set in the environment, and `TemplateBackend` (deterministic, same tool calls made explicitly in code) otherwise. This is not a cost-cutting shortcut for the challenge submission — it's the reason the mandatory workflow can be graded end to end with zero external dependencies or secrets, and, more importantly, it is what makes the guardrail engine's contract well-defined: **guardrails do not trust either backend's self-report.** They re-derive ground truth themselves (below), so a hallucinating LLM and a buggy template produce the exact same downstream behavior — a caught, corrected-or-blocked reply — rather than the guardrail engine having a "trust the LLM path more" special case.

**Constraint that shaped this:** no `ANTHROPIC_API_KEY` was available in the build environment, so the `LLMBackend` path is implemented and unit-testable in isolation (the tool-use loop, the bounded 4-turn cap, the schema wiring) but has not been exercised against a live model call as part of this submission — see PRD/TDD known limitations and the latency section below for exactly what that means for the reviewed latency numbers.

---

## Reply guardrails (primary depth area — agentic-write safety)

**Decision:** `GuardrailEngine.review` (`sidestage/guardrails/engine.py`) runs three independent factual checks (price, availability, policy) plus a tone check, on the *drafted text*, using ground truth it fetches itself from `GroundingTools` — not from whatever the generator claims it looked up.

**Why re-verify text instead of trusting the tool-call trace:** the tempting simpler design is "if the model called `lookup_price` and the tool returned $58, trust that the reply says $58." That design has a gap: nothing forces the model (or a template bug) to actually use the number the tool returned in the text it emits — a tool call can succeed while the final sentence still says the wrong thing, whether from a copy-paste-style templating bug or a model that "read" the tool result, generated a plausible-sounding number anyway, and never surfaced the discrepancy. Re-parsing the emitted text for `$X.XX` and re-fetching the authoritative price closes that specific gap, and it's the reason `guardrails/engine.py` imports `GroundingTools` directly rather than accepting the generator's `tool_trace` as input.

**Price check — concrete failure path and recovery:** `_check_price` extracts every `$X.XX` substring from the draft, computes the *set* of acceptable values (the effective price, plus the original list price when a promo is active — both are true statements on a marked-down item), and replaces any value outside that set with the correct effective price via a regex substitution callback, flagging the violation as `correctable=True`. This was caught and fixed mid-build: an early version replaced *every* dollar amount in a reply blindly, which broke a shipping-policy answer that legitimately mentions "$5.99 flat rate, free over $75" — those got incorrectly "corrected" to the product's price. The fix was two-fold and both parts matter: (1) gate the price check to `intent == "price_question"` only, so a policy answer's incidental dollar amounts are never touched, and (2) use a substitution callback that only rewrites values outside the acceptable set, not a blanket replace. Both bugs were caught by an ad hoc smoke script run against the full sample chat script before any guardrail unit test was written — see "Testing strategy."

**Availability check — blocking, not correctable:** `_check_availability` compares "N left" claims and sold-out/in-stock claims against the real stock count for the resolved SKU/size. Unlike price, this is never auto-corrected — a wrong stock claim (either direction) blocks the send outright and forces suggest-only, because "we auto-corrected the count for you" is a much worse failure mode for inventory than it is for price: a seller silently having her available-count claims rewritten by the system is exactly the kind of thing that should surface to her, not be smoothed over.

**Policy check — grounding-by-overlap:** rather than attempting full NLI-style claim verification (out of scope for this build), `_check_policy` requires the drafted text to have at least 30% word overlap with the actual top-matched policy text. This is a crude but auditable and fast proxy: a reply that substantially reuses the authoritative policy's own wording passes; a free-associated paraphrase or an invented policy ("no questions asked, return it anytime!") does not. `tests/test_guardrails.py::test_ungrounded_policy_answer_blocks` exercises exactly this.

**Tone check:** independent of grounding — profanity, a small set of dismissive phrases, and an all-caps/"shouting" heuristic (>60% uppercase letters in a reply longer than 8 letters) block regardless of factual accuracy, because a factually correct but rude reply is still not one that should go out under the seller's name.

**Confidence gate:** any draft with `confidence == "low"` (product reference did not resolve unambiguously) is an automatic blocking violation, independent of what the drafted text says — a plausible-sounding answer to the wrong product is arguably worse than an honest "which one did you mean?"

**A boundary of this design, found by testing the prototype rather than by reading the code:** re-verification catches false *statements*, not misleading *omissions*. The original purchase-intent reply ("Yay, so glad you want the Coral Wrap Midi Dress! Tap the pinned link/cart to grab it before it's gone") contains no checkable stock claim, so `_check_availability` had nothing to compare against — and it went out unchanged even when the requested size had zero units. The action layer still refused to act (`stock_adjust` raises rather than driving stock negative, and `pipeline._execute_bound_action` swallows it), so inventory was never corrupted, but the *buyer* was still told to go grab something that didn't exist. The fix was to check stock in the reply branch itself before encouraging the sale (`reply/generator.py`, purchase-intent branch, with `tests/test_ladder.py::test_purchase_intent_on_sold_out_size_says_so_and_takes_no_action` locking it in). The generalizable lesson, and the reason this is written up rather than quietly patched: a claim-verification guardrail is not a substitute for the generator being correct about what it chooses to say — it is a net under it. Anywhere the copilot can mislead without asserting a checkable fact, the guardrail is structurally blind, and that class of gap has to be closed in generation or in a separate intent-level policy check.

**What fails open vs. closed, explicitly:** correctable (price) -> ship the corrected text, still eligible for auto-send/auto-act if nothing else is wrong. Everything else (availability, policy, tone, confidence) -> forced to suggest-only regardless of the seller's configured ladder rung. There is no "block entirely, show nothing" state in this build — even a guardrail-flagged draft is still shown to the seller as a suggestion, with the violation reason attached, because hiding it entirely would mean Maya has no reply at all to work from and no visibility into why the system didn't trust itself.

---

## Action auditability and rollback

**Decision:** `sidestage/actions/tools.py` implements `push`, `swap`, `markdown`, and `stock_adjust` with an identical three-step shape — read the current state, apply the mutation, write one `audit_log` row containing actor, action type, input payload, before-state, and after-state, in the same sqlite transaction as the mutation (`store.write_audit`, called inside the same `cursor()` context manager as the update). `rollback(store, action_id)` reads that row and replays the before-state, then marks the row `reversed`.

**Why full-state snapshots instead of compensating transactions:** a compensating-transaction design (a `markdown` action's rollback is "set discount back to what a diff says it was") is more storage-efficient at scale but requires action-specific undo logic for every action type, and — critically for an audit trail a seller might actually have to explain to a marketplace or a customer dispute — it's harder to prove the rollback is *exactly* correct rather than approximately correct. Storing the full before-state means `rollback` is one function for all four action types (`_rollback_swap` is the only action-specific branch, because a swap's "before" is a pair of listings, not a single one) and a rollback's correctness is a direct equality check against a stored snapshot, not a replayed computation. The cost is real: this does not scale to high-frequency, high-cardinality actions (e.g., per-unit stock ticks at checkout-firehose volume) the way an append-only ledger with periodic compaction would — see known limitations.

**Guardrails on the actions themselves, not just on replies:** `markdown` takes a `max_discount_pct` ceiling (default 30%) and raises `ActionError` above it — this is the concrete enforcement of "a seller sets a floor the automation can never cross," independent of anything the reply guardrail checks. `stock_adjust` raises rather than allowing a negative resulting count. Both are exercised directly in `tests/test_actions_rollback.py`.

**Binding actions to the ladder, not to the model:** `pipeline.Pipeline._execute_bound_action` maps `purchase_intent` at ladder rung `AUTO_ACT` to exactly one action (`stock_adjust(-1)` on the resolved SKU/size) via a small explicit table, rather than letting the reply-generation model choose which action tool to invoke at that rung. An LLM choosing "swap the listing" in response to a chat message would be a much larger blast radius for a misclassification than a bounded, pre-declared action per intent type. This is a deliberate scope limit on how far "agentic write" goes in this build — see known limitations for what a broader action-selection design would need (almost certainly a second, action-specific guardrail layer, not just the reply guardrails reused).

---

## Latency budget

**Target:** sub-2-second reply latency (PRD success metric and the brief's stated target).

**What was actually measured (`tests/test_latency.py`):** over 200 synthetic messages through the full pipeline (resolution -> draft -> guardrail re-verification -> ladder decision), p50 ≈ 2ms, p95 ≈ 6-7ms, using the deterministic `TemplateBackend`. That is the floor: sqlite reads, TF-IDF scoring over a four-item catalog, and regex-based guardrail checks. It is not, and should not be read as, proof that a real-LLM-backed reply fits the 2-second budget — with `ANTHROPIC_API_KEY` unset (the state of this build environment), the `LLMBackend` path was never exercised under this benchmark, and a real tool-use round trip (potentially 2-4 model calls in the bounded loop before a final answer) is the actual latency risk, not the deterministic-path overhead measured here. Faking a passing benchmark with a `time.sleep()` stand-in for a model call would have been dishonest about what was validated; the TDD's job is to say plainly what the number is a floor for.

**What a real-LLM latency budget would need, not yet built:** a hard per-call timeout that falls back to suggest-only (a slow, uncertain answer is worse than a visibly-delayed one), streaming the first tokens of a reply rather than waiting for the full tool-use loop to resolve, and a cache of recent (SKU, intent) -> drafted-reply pairs for the highest-frequency FAQ patterns within a single stream, since the same "is the M in stock" question tends to repeat dozens of times in one broadcast.

---

## Marketplace integrations

Not built against a real platform in this submission (see "Streaming ingestion"). The integration surface is deliberately narrow: one function that yields `ChatMessage` from a platform's event format, and, symmetrically, one function per action type that a real platform's listing/inventory API would need to accept a `push`/`swap`/`markdown`/`stock_adjust` call and return enough state to populate the same before/after audit shape used here. The store schema (`sidestage/store.py`) is platform-agnostic on purpose — SKU, size, price, stock, listing, policy are concepts that exist on every live-commerce platform's own data model, even though the field names and APIs differ.

---

## Testing strategy

36 tests, `pytest -q`, all passing (`tests/`). `test_retrieval.py` covers exact lookups, fuzzy product/policy matching, and the documented lexical-retrieval limitation as a *passing* test (asserting `[]` is correct behavior, not a gap to hide). `test_guardrails.py` hand-constructs both correct and deliberately wrong `ReplyDraft`s — including the exact price-substitution bug and the price/policy cross-contamination bug found via the ad hoc smoke script described above — to lock in the fix as a regression test. `test_actions_rollback.py` covers all four action types, their ceilings/guards, rollback, and double-rollback rejection. `test_ladder.py` covers the rung-decision table in isolation and the pipeline-level ladder enforcement, including that a guardrail failure or low confidence downgrades rung even at the seller's configured maximum. `test_latency.py` is the empirical-validation spike described above.

The two technical spikes claimed for this submission: **(1) mechanism depth** — the guardrail engine's independent re-verification design (agentic-write safety) and the action audit/rollback mechanism, both with concrete failure paths exercised in tests, not just described; **(2) empirical validation** — the retrieval-technique comparison (word-level vs. char n-gram TF-IDF, with the actual 0.0-similarity failure that motivated the switch) and the latency benchmark with an explicit statement of what it does and doesn't prove.

## Known limitations (engineering)

No concurrency control beyond sqlite's own transaction locking — fine for one seller's single-threaded message stream, not for multiple simultaneous streams against one store. No seller authentication or multi-tenancy; `DB_PATH` is a single fixed file. The FastAPI layer has no auth on any endpoint — acceptable for a local prototype, not for anything deployed. The intent classifier's keyword patterns are a known incomplete list (the "small"/"medium"/"large" word-form gap, found during development, is fixed; others like this likely remain and would surface as `general`-classified, suggest-only-forced messages — a safe but not ideal failure mode). The console UI polls `/api/state` every 4 seconds in addition to the websocket push, which is redundant and was a pragmatic choice to keep the UI simple rather than fully event-driven. Relatedly, `/api/chat` broadcasts to every connected websocket including the client that submitted the message, so a sent message reaches its own sender twice (once as the POST response, once as the echo); the console dedupes on `message.id` client-side, but the correct fix is to exclude the originating socket from the broadcast server-side, which needs a client/session id the current API doesn't carry.
