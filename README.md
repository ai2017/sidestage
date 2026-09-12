# SideStage — Live Selling Copilot (prototype)

A real-time seller copilot for live-commerce chat: grounded, guardrailed replies to price/availability/policy questions, plus auditable, reversible listing/inventory actions (push, swap, markdown, stock adjust), gated by a per-intent copilot-to-automation ladder.

Read **PRD.md** for the product story (who this is for, the first workflow, the ladder, the pilot plan, success metrics) and **TDD.md** for the architecture and the reasoning behind each technical decision, including the two failure modes found and fixed while building this (a retrieval bug and a guardrail false-positive), documented as regression tests rather than just fixed silently.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt

# run the test suite (36 tests: guardrails, retrieval, actions/rollback, ladder, latency)
python3 -m pytest -q

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
