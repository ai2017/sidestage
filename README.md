# SideStage — Live Selling Copilot (prototype)

A real-time seller copilot for live-commerce chat: grounded, guardrailed replies to price/availability/policy questions, plus auditable, reversible listing/inventory actions (push, swap, markdown, stock adjust), gated by a per-intent copilot-to-automation ladder.

Read **SYSTEM_DESIGN.md** for the message-in/reply-out trace and a plain answer to "which part is the AI". Read **PRD.md** for the product story (who this is for, the first workflow, the ladder, the pilot plan, success metrics) and **TDD.md** for the architecture and the reasoning behind each technical decision, including the failure modes found and fixed while building and demoing this — a retrieval bug, a guardrail false-positive, and two cases where the copilot said something true that wasn't an answer to the question asked — each documented and locked in as a regression test rather than quietly patched.

## Quickstart

```bash
python3 -m venv .my_new_env &&  source ./my_new_env/bin/activate      # optional but recommended
pip install -r requirements.txt

# run the test suite (62 tests: guardrails, retrieval, actions/rollback, ladder, fit/sizing, LLM fallback, latency)
python3 -m pytest -q

# Set `ANTHROPIC_API_KEY` in the environment to switch to the real Claude tool-use backend (`sidestage/reply/generator.py: LLMBackend`) and the model. The key expires in 7 days but to raise the ceiling > 2s, set sidestage llm budget to 10
 export ANTHROPIC_API_KEY="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"

# Use sonnet model or claude-haiku-4-5-20251001
 export SIDESTAGE_MODEL="claude-sonnet-4-5-20250929"

# To see the reply as LLM backed stamped with what a tool-use loop genuinely costs you.
export SIDESTAGE_LLM_BUDGET_S=10

# run the seller console
uvicorn sidestage.api.server:app --reload --port 8000
# open http://localhost:8000  ->  click "Run simulated chat stream", or type your own message
```

No API key is required to run the prototype end to end — the reply generator uses a deterministic, fully-grounded template backend by default. Set `ANTHROPIC_API_KEY` in the environment to switch to the real Claude tool-use backend (`sidestage/reply/generator.py: LLMBackend`); the guardrail engine, ladder, and action layer behave identically either way, since guardrails independently re-verify the drafted text rather than trusting either backend's self-report (see TDD.md).

## Layout

```
sidestage/
  store.py           single sqlite source of truth: catalog, inventory, listings, policies,
                      ladder settings, message log, audit log (+ seed data for "Maya's Boutique")
  ingestion/          simulated live chat stream + intent classification
  grounding/          exact + fuzzy (char n-gram TF-IDF) retrieval, exposed as function-calling tools
  reply/              reply drafting: TemplateBackend (deterministic) / LLMBackend (real Claude)
  guardrails/         independent price/availability/policy/tone verification against ground truth
  actions/            push / swap / markdown / stock_adjust, each audited + reversible
  ladder.py           copilot-to-automation ladder decision logic
  pipeline.py         end-to-end orchestration + per-stage latency instrumentation
  api/server.py       FastAPI backend for the console
  console/index.html  minimal seller console UI (chat feed, ladder controls, audit log)
tests/                36 pytest tests across all of the above
```

## Known limitations

See the "Known limitations" sections at the end of PRD.md (product) and TDD.md (engineering). Short version: fit/sizing questions aren't grounded to real data yet, policy retrieval is lexical rather than semantic (documented with a concrete failing example), the real-LLM backend hasn't been latency-tested against a live model call, and there's no auth, multi-tenancy, or real marketplace integration in this prototype.

## User Inputs to try 
Start by clicking ▶ Run simulated chat stream — that replays the eight seeded messages and fills the feed. Then type these to drive it yourself.

The core loop (grounded answers):

how much is the coral dress → $58.00, auto-sent
how much is the sage dress → $54.00 marked down from $64.00 — proves it uses the promo price, not the list price
do you have the coral one in a M? → "5 left in size M"
is the coral in small still available → sold out in S. This is the one worth pausing on: it refuses to claim stock that isn't there, and it understood "small" as size S
what's your return policy if it doesn't fit → returns the actual policy text verbatim, not a paraphrase

The write path, end to end — this is the sequence I'd actually demo to reviewers, because it proves the action layer is real and reversible:

do you have the coral in L → "3 left"
In the Automation ladder panel, set purchase_intent to 2
sold ill take the coral in L → now auto-sent, with a stock_adjust pill and an Undo button
do you have the coral in L → "2 left" — the action actually mutated inventory
Click Undo in the Audit log, then ask again → back to "3 left"

The guardrails refusing to guess:

is this real or fake → comes back amber/suggested with "⚠ policy: No matching policy found to ground this answer." That's not a bug, it's the documented lexical-retrieval limitation doing the right thing — the authenticity policy never uses the words "real" or "fake," so rather than inventing an answer it hands the draft to you
omg is this true to size? → suggested with "⚠ confidence: Product reference could not be resolved with confidence"
Set price_question back to 0, then ask a price question → the same correct answer now arrives as a draft instead of being sent. Shows the ladder is a live ceiling, not a setting that only applies at startup
