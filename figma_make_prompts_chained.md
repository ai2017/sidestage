# Figma Make — 4 chained prompts (each under 2,000 characters)

Paste these into Figma Make **in order**, waiting for each build to finish before sending
the next. Prompt 1 establishes the whole structure; 2-4 layer on real data, guardrail
states, and interactions. Everything matches the working prototype exactly.

---

## PROMPT 1 — shell, layout, chat feed

Build SideStage, a seller console for a live-commerce seller running a live shopping stream. An operator tool: dense, fast, legible at a glance. Not a marketing page.

Top bar: dark slate #111827, "SideStage — Live Selling Copilot" left, "Maya's Boutique · live" right with a small red dot.

Body: warm off-white #f6f5f3, two columns. Left ~60% = chat feed panel. Right = three stacked panels titled "Automation ladder", "Listings & stock", "Audit log" (leave empty for now). All panels: white cards, 10px radius, 1px #e5e7eb border, 14px padding, uppercase 11px gray headers with letter-spacing.

Left panel "Live chat & copilot replies": a toolbar with a dark "▶ Run simulated chat stream" button, a narrow viewer-name input, a wide "type a chat message…" input, and a Send button. Below it a scrolling feed, newest at top.

Each feed card: viewer handle in bold + status badge + intent badge; the viewer's message in gray quotes; the copilot's reply in a light gray box with a 3px gray left border; a small gray meta line. Status badge: sent = green (#dcfce7/#166534), suggested = amber (#fef3c7/#92400e). Intent badge = gray pill.

Seed the feed, newest first:
1. random_v · sent · policy_question — "does shipping take forever" → "Standard shipping is 3-5 business days, $5.99 flat rate, free over $75." meta: "AUTO_SEND · 6.9ms"
2. buyer_lyn · sent · price_question — "how much is the sage dress rn" → "The Sage Linen Midi Dress is $54.00 right now (marked down from $64.00)!"
3. t.marie · sent · availability_question — "do you have the coral one in a M??" → "Yes! We have 5 left of the Coral Wrap Midi Dress in size M."
4. jess_84 · suggested · general — "omg is the coral dress true to size?" → "Thanks for the message! Let me know if you have questions about the Coral Wrap Midi Dress"

System font, 13px body. No gradients, no hero imagery, no marketing copy.

---

## PROMPT 2 — right column panels with real data

Now fill in the three right-column panels.

"Automation ladder": a table of intent types with a 0/1/2 dropdown per row — price_question = 1, availability_question = 1, policy_question = 1, purchase_intent = 0, general = 0. Below it, small gray footnote text: "0 = suggest only · 1 = auto-send reply · 2 = auto-send + auto-act (bound action)". Changing a dropdown visibly updates that row.

"Listings & stock": two rows.
Row 1 — Coral Wrap Midi Dress, $58.00, with a "🔴 live" badge.
Row 2 — Sage Linen Midi Dress, $54.00 with "was $64.00" struck through next to it, plus a "-15%" indigo pill (#eef2ff bg, #3730a3 text), no live badge.
Under each row, a compact per-size stock strip of small chips: Coral — XS 2, S 0, M 5, L 3, XL 1. Sage — XS 0, S 4, M 4, L 0. Any chip showing 0 is red and bold.

"Audit log": a table with columns Action / Actor / control, four rows:
- stock_adjust / copilot / [Undo button]
- markdown / seller / [Undo button]
- stock_adjust / copilot / gray "reversed" pill
- push / copilot / gray "reversed" pill
Clicking an Undo button replaces it with a gray "reversed" pill.

Keep the existing chat feed and layout unchanged.

---

## PROMPT 3 — guardrail states in the feed

Add three more cards to the top of the chat feed, showing how the copilot's guardrails behave. Violation lines are 12px red text starting with ⚠, placed under the reply box.

1. fastbuyer · sent · purchase_intent — "sold ill take the coral in L" → "Yay, so glad you want the Coral Wrap Midi Dress!" Below the reply, an action row: the text "action executed:" followed by an indigo pill reading "stock_adjust" and a small "Undo" button.

2. deal_hunter · sent · price_question — "how much for the coral" → "The Coral Wrap Midi Dress is $58.00!" — this reply box gets an AMBER left border, and one violation line: "⚠ price: Draft stated $12.00 but authoritative effective price is $58.00; auto-corrected."

3. skeptic99 · suggested · policy_question — "is this real or some fake crap" → "Great question — let me check on that policy and get back to you!" — this reply box gets a RED left border and two violation lines: "⚠ policy: No matching policy found to ground this answer." and "⚠ confidence: Product reference could not be resolved with confidence."

Also add a "Send" and an "Edit" button to any card whose status badge reads "suggested" — those are drafts the seller has to approve.

---

## PROMPT 4 — make it interactive

Make the console actually work.

Typing in the message input and pressing Enter or Send prepends a new feed card. Classify the intent by keyword: "how much"/"price"/"cost" → price_question; "in stock"/"do you have"/"left"/"sold out" → availability_question; "return"/"shipping"/"refund"/"real"/"fake" → policy_question; "sold"/"i'll take"/"i want" → purchase_intent; anything else → general.

Generate the reply from this catalog: Coral Wrap Midi Dress $58.00 (XS 2, S 0, M 5, L 3, XL 1); Sage Linen Midi Dress $54.00 marked down from $64.00 (XS 0, S 4, M 4, L 0); Cream Ribbed Tank $22.00; Classic Blue Denim Jacket $72.00 (S 3, M 0, L 2). Policies: shipping is 3-5 business days, $5.99 flat, free over $75; returns within 14 days, unworn with tags. If the message names no product, assume the live listing (Coral Wrap Midi Dress).

Mark the new card "sent" (green) when that intent's ladder level is 1 or 2, and "suggested" (amber) when it's 0 — read the level live from the Automation ladder dropdowns, so changing a dropdown to 0 makes the next matching reply come back as a draft instead of being sent.

If the message asks about a size with 0 stock, the reply must say it's sold out — never claim stock that isn't there.

"▶ Run simulated chat stream" replays the seeded messages one at a time with a ~600ms gap between them. Clicking "Send" on a suggested card turns its badge green and removes the Send/Edit buttons.
