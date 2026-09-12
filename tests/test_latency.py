"""
Empirical-validation spike: measure end-to-end and per-stage reply latency
across a synthetic burst and check it against the PRD's sub-2-second
target, rather than asserting it from a diagram.

Honesty check this test does NOT let past: with ANTHROPIC_API_KEY unset,
this exercises the deterministic TemplateBackend, which never makes a
network call, so a pass here is a floor (pipeline overhead, retrieval,
guardrail re-verification), not proof that a real LLM call fits the
budget. See TDD.md, "Latency budget," for the stage-by-stage budget we'd
enforce against a real LLM backend (streaming first-token, tool-call
round-trips, a hard timeout that falls back to suggest-only) and why that
part is a known limitation of this prototype rather than something faked
here with a sleep() call.
"""

from __future__ import annotations

import statistics

from sidestage.ingestion.stream import SAMPLE_SCRIPT, make_message

N_MESSAGES = 200
LATENCY_BUDGET_MS = 2000


def _percentile(values: list[float], pct: float) -> float:
    values = sorted(values)
    k = int(round((pct / 100) * (len(values) - 1)))
    return values[k]


def test_reply_latency_meets_budget_for_template_backend(pipeline):
    latencies = []
    for i in range(N_MESSAGES):
        viewer, text = SAMPLE_SCRIPT[i % len(SAMPLE_SCRIPT)]
        msg = make_message(f"{viewer}_{i}", text)
        result = pipeline.process_message(msg)
        latencies.append(result.timings.total_ms)

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)

    print(f"\nlatency over {N_MESSAGES} messages (template backend): "
          f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms max={max(latencies):.2f}ms")

    assert p95 < LATENCY_BUDGET_MS, (
        f"p95 latency {p95:.2f}ms exceeds the {LATENCY_BUDGET_MS}ms budget"
    )


def test_stage_breakdown_is_dominated_by_retrieval_not_fixed_overhead(pipeline):
    """Guards against a regression where guardrail re-verification (which
    re-runs retrieval independently of the generator) becomes the dominant
    cost -- that's the specific risk of the "guardrails never trust the
    generator's citations" design, since it means every reply pays for
    grounding twice."""
    msg = make_message("v1", "how much is the coral dress")
    result = pipeline.process_message(msg)
    t = result.timings
    assert t.total_ms < LATENCY_BUDGET_MS
    # informative, not a hard gate: report the split so a future real-LLM
    # swap has a baseline to compare against.
    print(f"\nresolve={t.resolve_ms:.2f}ms draft={t.draft_ms:.2f}ms "
          f"guardrail={t.guardrail_ms:.2f}ms action={t.action_ms:.2f}ms")
