# SideStage — Product Requirements Document

## The user

SideStage is built for one person: **Maya**, a solo boutique apparel seller who runs 2-3 live shopping streams a week on a TikTok-Shop-style live-commerce platform. She sources and photographs her own inventory, manages a spreadsheet of stock counts, and does $3,000-$8,000 in GMV a month, split across maybe 15-25 SKUs at a time. She streams alone, holding her phone or working a single camera, with no moderator and no second screen. During a good stream she has 150-400 concurrent viewers and a chat scrolling faster than she can read it.

This is deliberately not "a copilot for live-commerce sellers" in general. A marketer running a brand's live-shopping program at a large retailer has a team, a real catalog system, and compliance review; their pain is coordination and reporting, not keyboard bandwidth. Maya's pain is that she is the entire operation, live, in real time, and every unanswered question in chat is a sale that quietly walks away. Designing for both of these people at once produces a product that serves neither well, so this PRD and this build are about Maya.

### Maya's pain, concretely

While she's live, three things compete for the same attention: talking to the camera, watching the chat for questions she can turn into sales, and keeping track of what's actually left in stock so she doesn't sell something twice. In practice she loses to the second and third. Questions like "is this in a medium," "how much is the sage one," and "what's your return policy" pile up faster than she can answer them, and by the time she gets to a size question the buyer has often moved on. Separately, she oversells: two people both hear "yes I have a medium" because she said it from memory rather than checking, and now one of them gets a cancellation email later, which is the fastest way to lose a repeat customer in this category.

## Why this problem fits an AI-copilot wedge, not a full-automation product

Maya will not hand a bot her chat and walk away — one wrong price or one confident "yes it's in stock" when it isn't costs her a refund, a chargeback, or a public reply in her own comments section that she has to walk back live. But she also cannot read and answer 400 concurrent messages by herself. The right shape of product is a copilot that removes the *mechanical* load (looking up a price, checking a stock count, finding the return policy) while keeping her fully in control of anything that touches money or inventory until she has a track record of the system being right — which is exactly what a graduated automation ladder is for, rather than a binary "AI replies for you" toggle.

## The first workflow

The workflow this build is built and evaluated against: **a viewer asks a grounded question in live chat (price, availability/size, or policy), and the copilot drafts or sends a reply that is checked against real catalog/inventory/policy data before it goes out — and, where the seller has opted in, the same event can trigger a bounded inventory action (a stock decrement on a confirmed sale) that is logged and reversible.**

This is the workflow, not "the copilot can also do X, Y, Z," because it is the one that recurs dozens of times a stream, it is fully groundable in data Maya already has (a catalog, a stock count, a policy paragraph), and it is the one where a wrong answer is expensive enough that guardrails matter and cheap enough to validate in a two-week pilot.

## The copilot-to-automation ladder

Sellers do not trust a new system with their storefront on day one, and they shouldn't have to. The ladder is configured **per intent type**, not as one global switch, because the risk profile of "what's the price" is not the risk profile of "process this sale":

- **Rung 0 — Suggest.** The copilot drafts a reply; Maya sees it and taps Send. This is the default for every intent type for a new seller, and it is where anything low-confidence or guardrail-flagged always lands regardless of configuration.
- **Rung 1 — Auto-send.** Once guardrails pass and the product reference resolves with high confidence, the copilot sends the reply itself. No inventory or listing state changes. This is where price/availability/policy FAQ answers live once Maya trusts the copilot's factual accuracy — which in this build is enforced by the guardrail engine, not by hoping the model is right (see TDD.md).
- **Rung 2 — Auto-act.** The copilot sends the reply *and* executes one bounded action tied to that intent (in this build: a confirmed purchase-intent message decrements stock by one unit for the resolved SKU/size). Every auto-act is audited with a full before/after state and is one click from being rolled back.

A seller moves an intent type up the ladder on her own schedule; the system never overrides her configured ceiling, but it does override *downward* automatically — any guardrail violation or low-confidence resolution forces that single reply back to Rung 0 no matter what the configured level is. The ladder is a ceiling on automation, not a promise of it.

## Pilot plan

A two-week pilot with **3-5 sellers** who match Maya's profile: solo or two-person apparel/boutique live-sellers on a comparable live-commerce platform, streaming at least twice a week, with an existing (even if informal) catalog and stock list we can seed the grounding store from. Each seller starts every intent type at Rung 0 for the first 2-3 streams (baseline + trust-building), then is invited to raise price/availability/policy to Rung 1 once they've seen the guardrail catch or correct a handful of drafts themselves. Purchase-intent stays at Rung 0/1 for the full pilot; Rung 2 for it is a stretch goal we'd only enable with a seller who explicitly asks for it after seeing the audit log.

During the pilot we would sit in on at least one live stream per seller (or review the recording) and specifically note: which suggested replies she edited before sending and why, which she ignored entirely, and any moment where she visibly lost track of chat while the copilot could have caught it. That observation list is the input to the next iteration of the guardrail rules and the ladder defaults, not a one-time survey.

## Success metrics

- **GMV per stream**, before/after, for participating sellers, and specifically GMV attributable to a chat interaction the copilot touched (a reply that led to an "I'll take it").
- **Unanswered-question rate**: the share of grounded questions (price/availability/policy) that got zero reply within 60 seconds, before vs. during the pilot.
- **Operator load**, self-reported after each stream on a short scale ("How in-control did you feel of chat during this stream?") plus an objective proxy: the size of the chat backlog (messages received but not yet acted on) at its peak during the stream.
- **Guardrail intervention rate**: what fraction of drafted replies the guardrail corrected or blocked, broken down by violation type. A rate that stays flat or drops as a seller's catalog/policy data stabilizes is a sign the grounding is working; a rate that stays high is a sign the retrieval or classifier needs work before that seller should be invited up the ladder.
- **Trust signal**: whether a seller voluntarily raises an intent type's ladder rung during the pilot, and whether she lowers one back down — the latter is a real product signal, not just noise to explain away.

## User evidence: what's real vs. what's assumed

Maya, as described here, is a composite built from live-commerce seller behavior documented by platforms and creators in this space (return/authenticity questions and size/stock questions dominating live chat, sellers running solo without a moderator, oversell-driven refunds being a recurring complaint), plus the shape of the problem stated in this challenge brief — not a specific person we interviewed during the build window. That is a real gap, and it is the first thing a week-one discovery plan has to close: **recruit 3-5 sellers matching this profile in week one of the engagement, show them this prototype specifically (not a deck), and change the default ladder rungs, the classifier's intent list, and the guardrail's tone rules based on what they say and, more importantly, what they don't say — which questions they answer themselves anyway rather than trusting a draft.** Thin user evidence going into a build is not disqualifying; treating the resulting prototype as validated when it isn't would be.

## What "the right answer" means here

The alternative most obviously on the table is a general-purpose customer-service chatbot pointed at a live-commerce chat feed. That product is easier to build and would demo well, but it fails Maya's actual constraint: she isn't worried about *whether* an AI can write a plausible-sounding reply, she's worried about it confidently saying something false while she's on camera and can't immediately correct it. The bet this PRD is making is that the defensible product here is not "AI that talks to your customers," it's "AI that only ever tells your customers things that are true, and that earns the right to talk to them unsupervised one intent type at a time." That is a narrower, less flashy product, and it is the one worth funding.

## Known limitations of this build (see TDD.md for the technical detail)

Fit and sizing questions ("does this run small," "how about petite") are grounded, but only as far as the catalog's free-text description happens to carry a fit phrase — the copilot answers "runs one size small" because the sage dress's description says exactly that, and it says so only when the description supports it. An item described without any fit wording gets an honest "let me double-check and come back to you" at suggest-only rather than a guess. A real version needs a structured fit attribute per SKU (and per size) instead of phrase-matching prose, plus fabric and measurement fields, which is the difference between answering the fit questions a seller's own copy happens to anticipate and answering the ones buyers actually ask. Policy retrieval is lexical (character-level text matching), not semantic, so a question that shares no vocabulary with the actual policy text (a viewer asking "is this legit" when the policy says "authenticity") won't be found and is correctly, safely treated as unresolved rather than answered by guesswork — this is visible directly in the test suite (`tests/test_retrieval.py::test_policy_search_known_limitation_no_semantic_match`). The reply generator's real-LLM backend is implemented and used automatically when `ANTHROPIC_API_KEY` is set, but the pilot's actual latency and cost profile under a real model call has not been measured — only the deterministic backend's latency floor has (see TDD.md, "Latency budget"). There is no seller authentication, multi-tenant account model, or connection to a real marketplace API in this build; `sidestage/ingestion/stream.py` is a synthetic script standing in for that integration, by design (see TDD.md, "Marketplace integrations").
