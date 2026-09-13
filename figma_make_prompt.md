# Figma Make prompt — SideStage Seller Console

Paste everything below the line into the Figma Make prompt box ("Describe your idea").
It's written to match the working prototype exactly (same SKUs, prices, stock counts,
guardrail copy, ladder levels), so the Figma prototype and the code tell the same story
when reviewers compare them.

---

Build **SideStage**, a real-time seller console for a solo live-commerce seller (a boutique apparel seller running a live shopping stream). This is an operator tool — dense, fast, legible at a glance while she's on camera. Not a marketing page.

**Layout:** dark slate top bar (`#111827`) with "SideStage — Live Selling Copilot" on the left and "Maya's Boutique · live" on the right with a small red pulsing dot. Below it, a two-column layout on a warm off-white background (`#f6f5f3`): a wide left column (~60%) for the chat feed, a narrower right column for three stacked panels. All panels are white cards, 10px radius, 1px `#e5e7eb` border. System font stack, 13px body text, uppercase 11px gray panel headers with letter-spacing.

**Left column — "Live chat & copilot replies":** a toolbar with a dark "▶ Run simulated chat stream" button, a small viewer-name input, a wide message input ("type a chat message…"), and a Send button. Below it a scrolling feed, newest at top. Each feed card shows: viewer handle in bold, then status and intent badges, then the viewer's message in quotes in gray, then the copilot's reply in a light gray box with a 3px colored left border, then (when present) violation lines and an action row, then a small gray metadata line.

Status badges: `sent` = green pill (`#dcfce7` bg, `#166534` text), `suggested` = amber pill (`#fef3c7` / `#92400e`). Intent badge = gray pill showing the classified intent (`price_question`, `availability_question`, `policy_question`, `purchase_intent`, `general`). Reply border color: gray when clean, amber when auto-corrected, red when a guardrail blocked auto-send. Violation lines are 12px red text beginning with ⚠.

Seed the feed with these exact cards, newest first:

1. `random_v` · sent · policy_question — "does shipping take forever" → "Standard shipping is 3-5 business days, $5.99 flat rate, free over $75." — meta: "template backend · seller-configured automation level AUTO_SEND · 6.9ms"
2. `shopper2` · suggested · purchase_intent — "sold! i'll take the coral in L" → "Yay, so glad you want the Coral Wrap Midi Dress! Tap the pinned link/cart to grab it before it's gone." — meta: "seller-configured automation level SUGGEST · 2.1ms" — with a Send button and an Edit button on the card
3. `skeptic99` · suggested · policy_question — "is this real or some fake crap" → "Great question — let me check on that policy and get back to you!" — red border, two violations: "⚠ policy: No matching policy found to ground this answer." and "⚠ confidence: Product reference could not be resolved with confidence."
4. `q_and_a` · sent · policy_question — "what's your return policy if it doesn't fit" → "Returns accepted within 14 days of delivery for unworn items with tags. Final-sale items marked as such at checkout are not returnable."
5. `buyer_lyn` · sent · price_question — "how much is the sage dress rn" → "The Sage Linen Midi Dress is $54.00 right now (marked down from $64.00)!"
6. `t.marie` · sent · availability_question — "wait what about small, is that in stock" → "Just sold out of size S in the Coral Wrap Midi Dress — sorry! I'll flag it if we restock."
7. `t.marie` · sent · availability_question — "do you have the coral one in a M??" → "Yes! We have 5 left of the Coral Wrap Midi Dress in size M."
8. `jess_84` · suggested · general — "omg is the coral dress true to size?" → "Thanks for the message! Let me know if you have questions about the Coral Wrap Midi Dress 💕" — one violation: "⚠ confidence: Product reference could not be resolved with confidence."

Include one more card demonstrating an auto-corrected reply (amber border): `deal_hunter` · sent · price_question — "how much for the coral" → "The Coral Wrap Midi Dress is $58.00!" with violation line "⚠ price: Draft stated $12.00 but authoritative effective price is $58.00; auto-corrected."

And one demonstrating an executed action: `fastbuyer` · sent · purchase_intent — "sold ill take the coral in L" → "Yay, so glad you want the Coral Wrap Midi Dress!" with an action row reading "action executed: `stock_adjust`" as an indigo pill, followed by a small "Undo" button.

**Right column, panel 1 — "Automation ladder":** a table with intent types and a 0/1/2 dropdown per row: price_question = 1, availability_question = 1, policy_question = 1, purchase_intent = 0, general = 0. Footnote in small gray text: "0 = suggest only · 1 = auto-send reply · 2 = auto-send + auto-act (bound action)". Changing a dropdown should visibly update that row.

**Right column, panel 2 — "Listings & stock":** rows for Coral Wrap Midi Dress ($58.00, "🔴 live" badge) and Sage Linen Midi Dress ($54.00 with "was $64.00" struck through, plus a "-15%" indigo pill, not live). Under each, a compact per-size stock strip as small chips: Coral — XS 2, S 0, M 5, L 3, XL 1; Sage — XS 0, S 4, M 4, L 0. Chips with 0 stock are red and bold.

**Right column, panel 3 — "Audit log":** table of recent actions with columns Action / Actor / control. Rows: `stock_adjust` / copilot / [Undo button]; `markdown` / seller / [Undo button]; `stock_adjust` / copilot / "reversed" gray pill; `push` / copilot / "reversed" gray pill. Clicking Undo should swap that row's button for a "reversed" pill.

**Interactions to make work:** typing in the message box and pressing Send or Enter prepends a new feed card (classify by keyword — "how much"/"price" → price_question, "in stock"/"do you have"/"left" → availability_question, "return"/"shipping"/"refund"/"real"/"fake" → policy_question, "sold"/"i'll take" → purchase_intent, else general — and generate the reply from the seeded catalog data above, marking it `sent` when the intent's ladder level is 1 or 2 and `suggested` when it's 0). "Run simulated chat stream" replays the eight seeded messages one at a time with a ~600ms gap. Undo buttons flip to "reversed" pills. The Send button on a suggested card turns its badge green.

Keep it clean and functional — no gradients, no hero imagery, no marketing copy.
