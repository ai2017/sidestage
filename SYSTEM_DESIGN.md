# SideStage — System Design: message in, reply out

This document traces the exact path one viewer message takes through the system, states what
decides each step, and answers the question that comes up first in every conversation about
this build: **which part of it is actually an AI model?**

PRD.md covers who this is for and why. TDD.md covers the alternatives weighed at each
architectural decision. This document is the mechanism, in order.

---

## The short answer on the model

Only one step in this system is a model, and it is the step that writes the sentence.
Everything that decides what is *true*, what is *allowed*, and what changes *inventory* is
ordinary code reading a database.

When `ANTHROPIC_API_KEY` is set, the sentence is drafted by Claude (`claude-3-5-haiku-latest`)
inside a bounded tool-use loop where the model can ask the catalog for a price or a stock
count. When no key is set — which is how the prototype runs by default — the same sentence is
assembled by deterministic templates making the same lookups explicitly in code. Every stage
after drafting behaves identically either way, which is the point: the safety properties do not
depend on the model being well behaved.

For a non-technical listener: think of a pharmacy. The AI is the person who writes the label in
friendly language. The prescription itself — the drug, the dose — is looked up in a system, and
a second person checks the written label against that system before it leaves the counter. If
the label claims something the records do not support, it does not go out.

---

## The flow

```
        Viewer message
        "do you have the coral one in a M??"
               |
               v
     1 · Classify intent  ................  keyword rules, no model
               |
               v
     2 · Resolve product  ----------------> [ SQLite store ]   match name / colour
               |                            |               |
               v                            |  catalog      |
     3 · Draft the reply  ---------------->  |  inventory    |   reads price · stock · policy
         (the one AI step)                  |  listings     |
               |                            |  policies     |
               v                            |  ladder cfg   |
     4 · Guardrail review ===============>  |  audit log    |   RE-READS the same facts,
               |                            [_______________]   independently
               v
     5 · Ladder decides how far it may go
         (seller's level for this intent, forced down to
          Suggest if the guardrail objected)
               |
      +--------+--------+------------------+
      v                 v                  v
   Suggest          Auto-send       Auto-send + act
   seller sends     reply only      one bound action ---> writes + audit row
```

The mechanism the whole design rests on is the pair of arrows into the store. The drafter reads
the catalog to write the reply; the guardrail then reads it again, independently, to check what
was written. Remove the second read and every safety property disappears — you are left
trusting whatever the drafter said it looked up.

---

## Stage by stage

### 1 · Classify the intent — `ingestion/stream.py :: classify_intent()`

The message is matched against ordered lists of keyword patterns; the first list that matches
wins, and a message matching nothing becomes `general`.

**What decides it:** plain regular expressions in a fixed order — purchase ("i'll take",
"sold!"), then policy ("return", "shipping", "refund", "fake"), then fit ("true to size", "runs
small", "petite"), then availability ("do you have", "in stock", "left"), then price ("how
much", "cost"). Order is the tie-breaker and it is a real decision: "do you have this in petite"
matches both fit and availability, and fit wins because the useful answer is about sizing, not a
stock count for a size the catalog does not carry.

Deliberately not a model: this step gates which automation rung applies before any model cost is
spent, and a seller or reviewer can read the pattern list and know exactly why a message was
routed as it was. The price is that unanticipated phrasing falls through to `general`, forcing
suggest-only — a safe failure, not a good one.

### 2 · Resolve the product — `pipeline.py :: resolve_product()`

Viewers do not type SKUs, they say "the coral one". The message is scored against every catalog
entry's name, colour, category and description; the best match above a threshold wins, and if
nothing clears it the system assumes the item currently on air.

**What decides it:** character n-gram TF-IDF with cosine similarity (scikit-learn) — statistical
text overlap, not a neural embedding. Character n-grams rather than whole words because a
shopper writes "return" while the policy says "returns"/"returnable"; word-level scoring rated
that pair exactly 0.0 similar in testing, which would have made the return policy permanently
unreachable. Being lexical, it cannot bridge pure synonyms ("is this real or fake" against an
authenticity policy that never uses those words), and that case is left unmatched rather than
answered from the nearest guess.

### 3 · Draft the reply — `reply/generator.py` — the only AI step

Two interchangeable backends behind one interface. `LLMBackend` drafts with Claude in a bounded
tool-use loop (max four turns) with five tools available: `lookup_price`, `lookup_stock`,
`search_products`, `search_policy`, `get_live_listing`. `TemplateBackend` assembles the sentence
from the same tool calls, made explicitly in code.

**What decides it:** the model chooses which facts to fetch and how to phrase the answer. It does
not choose what is true, what the seller authorised, or whether anything is written to
inventory — those are stages 4, 5 and 6, which run identically whichever backend produced the
words.

### 4 · Check the words against the data — `guardrails/engine.py :: review()`

The guardrail reads the drafted sentence, extracts every factual claim, and re-queries the
database itself to test each one. It never consults what the drafter said it looked up.

**What decides it:** four independent checks.

*Price* — every `$X.XX` must equal the current effective price, or the crossed-out list price on
a promo item. *Availability* — "N left" must not exceed real stock, and "sold out" must not be
claimed while stock remains. *Policy and fit* — the wording must substantially reuse the
catalog's own policy text or fit phrase, so a plausible invention fails. *Tone* — profanity,
dismissiveness or shouting blocks regardless of accuracy.

A wrong price is corrected in place and flagged. Every other violation is blocking: the reply is
never sent automatically and drops to a draft with the reason attached. Nothing is hidden from
the seller — a flagged draft is still shown, because hiding it would leave her with no reply and
no explanation.

### 5 · Decide how far the reply may go — `ladder.py :: decide()`

Each intent type carries a seller-chosen level: 0 suggest, 1 auto-send, 2 auto-send plus one
bound action.

**What decides it:** the configured level is a *ceiling*, never a guarantee. If the guardrail
objected or the product reference was uncertain, the rung is forced to Suggest regardless of the
setting. The ladder can only ever move automation downward on its own.

### 6 · Act, and record it reversibly — `actions/tools.py`

Only rung 2 writes. Each action (`push`, `swap`, `markdown`, `stock_adjust`) reads current
state, applies the change, and records one audit row holding actor, inputs, and the complete
before and after state — in the same transaction as the change, so an audit entry can never
exist without the write it describes, or the reverse.

**What decides it:** a fixed lookup table binds one action to one intent (purchase intent
decrements stock by one). The model never chooses the action: a misclassification that swaps the
live listing is a far larger blast radius than one that sends an odd sentence. `rollback()`
replays the stored before-state, which is why one function undoes all four action types.

---

## Worked example

| Step | Result |
| --- | --- |
| input | "do you have the coral one in a M??" |
| 1 · intent | `availability_question` (matched "do you have"); size `M` extracted separately |
| 2 · product | `DRS-CORAL-01` — "coral" scored above threshold; no live-listing fallback needed |
| 3 · draft | called `lookup_stock(DRS-CORAL-01, M)` → 5 units → "Yes! We have 5 left of the Coral Wrap Midi Dress in size M." |
| 4 · guardrail | independently re-read stock for that SKU/size → 5. Claim "5 left" ≤ 5. No violation |
| 5 · ladder | `availability_question` at level 1, guardrail passed, confidence high → **Auto-send** |
| 6 · action | none — rung 1 never writes |
| output | sent to chat, logged with stage timings; measured 1.7 ms end to end on the deterministic backend |

Change one thing: had the drafter said "we have 9 left", stage 4 would have compared 9 against 5,
raised a blocking availability violation, and stage 5 would have forced the rung down to Suggest.
The seller sees the draft and the reason; the customer sees nothing until a human agrees.

---

## Every decision in the system, and what makes it

| Decision | Technique | Model? |
| --- | --- | --- |
| Which kind of question is this | Ordered regex keyword patterns | no — rules |
| Which product they mean | Character n-gram TF-IDF, cosine similarity | no — statistics |
| Which policy or fit fact applies | Same similarity scoring with a floor threshold | no — statistics |
| What the price / stock actually is | Direct database lookup by key | no — database |
| **How to word the reply** | **Claude `claude-3-5-haiku-latest` via tool-use, or templates with no key** | **yes** |
| Whether the reply is factually true | Claim extraction plus independent re-query | no — rules |
| Whether it may send without a human | Integer comparison against seller settings | no — rules |
| Which inventory action may fire | Fixed intent-to-action lookup table | no — rules |
| How to undo an action | Replay of the stored before-state | no — database |

One row of nine. That ratio is the architecture's thesis: use the model for the part models are
good at — turning facts into a sentence a shopper wants to read — and refuse to let it be the
authority on anything a wrong answer would cost money.

---

## Where state lives

One SQLite file holds catalog, inventory, listings, policies, ladder settings, the message log
and the audit log. Nothing of consequence lives in memory, and nothing lives in the model.

Each message is handled independently — there is no per-viewer conversation state. "Wait, what
about small?" works only because resolution falls back to the item currently on air, not because
the system remembers the previous question. A real conversation thread needs per-viewer state
that does not exist here.

---

## Where this design is structurally blind

**It verifies truth, not relevance.** Every check compares a stated claim against data, so a
reply that states nothing false but answers the wrong question passes cleanly. Two real
instances, both found by using the console rather than reading the code: telling a buyer to
"grab it before it's gone" on a sold-out size, and reading "runs small" as a request for size S
and answering with size-S stock counts. Both were true sentences and wrong answers, and both had
to be fixed in the drafting stage, because no amount of claim-checking would have caught them. A
production version wants a separate relevance gate — and that is one of the few places worth
reaching for a model call rather than a rule, since relevance is exactly the judgement rules are
worst at.

**The latency figure is a floor, not a proof.** Measured p50 ≈ 2 ms, p95 ≈ 7 ms over 200
messages — on the template backend, which makes no network call. A real model round trip is the
actual risk to the sub-2-second target and has not been measured. See TDD.md, "Latency budget".

**Grounding is only as good as the catalog.** Fit answers work because a seller happened to
write "true to size" in a description. There is no structured fit, fabric or measurement data,
so the copilot answers the fit questions the seller's own copy anticipated rather than the ones
buyers actually ask.
