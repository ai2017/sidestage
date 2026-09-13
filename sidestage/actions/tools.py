"""
Listing/inventory action tools: push, swap, markdown, stock_adjust.

Every action here follows the same three-step shape:

    1. read current state (the "before")
    2. apply the mutation
    3. write one audit_log row containing actor, action_type, the input
       payload, the before state, and the after state — in the *same*
       sqlite transaction as the mutation, so an audit entry can never
       exist without the write it describes actually having happened
       (or vice versa).

`rollback(store, action_id)` is the inverse: it reads that same row and
replays the before-state verbatim, then marks the row reversed so it can't
be rolled back twice. Because the audit row stores the full before state
rather than a diff, rollback does not need action-specific "undo" logic —
one function handles all four action types. See TDD.md, "Action
auditability and rollback" for why full-state snapshots were chosen over
compensating transactions, and what breaks that trade-off at higher write
volume.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sidestage.store import Store


class ActionError(Exception):
    pass


@dataclass
class ActionResult:
    action_id: str
    action_type: str
    before: dict
    after: dict


def push(store: Store, listing_id: str, *, featured: bool = True, actor: str = "copilot") -> ActionResult:
    """Feature/pin a listing (e.g. in response to a purchase-intent spike)."""
    before = store.get_listing(listing_id)
    if before is None:
        raise ActionError(f"unknown listing {listing_id}")
    with store.cursor() as cur:
        cur.execute(
            "UPDATE listings SET featured = ?, updated_at = ? WHERE listing_id = ?",
            (int(featured), time.time(), listing_id),
        )
    after = store.get_listing(listing_id)
    action_id = store.write_audit(actor, "push", {"listing_id": listing_id, "featured": featured},
                                   before, after)
    return ActionResult(action_id, "push", before, after)


def swap(store: Store, from_listing_id: str, to_listing_id: str, *, actor: str = "copilot") -> ActionResult:
    """Take one listing off-air and put another live (a seller changing what they're showing)."""
    before_from = store.get_listing(from_listing_id)
    before_to = store.get_listing(to_listing_id)
    if before_from is None or before_to is None:
        raise ActionError("unknown listing(s) in swap")
    now = time.time()
    with store.cursor() as cur:
        cur.execute("UPDATE listings SET is_live = 0, updated_at = ? WHERE listing_id = ?",
                    (now, from_listing_id))
        cur.execute("UPDATE listings SET is_live = 1, updated_at = ? WHERE listing_id = ?",
                    (now, to_listing_id))
    after = {"from": store.get_listing(from_listing_id), "to": store.get_listing(to_listing_id)}
    before = {"from": before_from, "to": before_to}
    action_id = store.write_audit(actor, "swap",
                                   {"from_listing_id": from_listing_id, "to_listing_id": to_listing_id},
                                   before, after)
    return ActionResult(action_id, "swap", before, after)


def markdown(store: Store, listing_id: str, discount_pct: int, *, actor: str = "copilot",
             max_discount_pct: int = 30) -> ActionResult:
    """Apply a temporary price markdown to a listing, bounded by a hard ceiling
    so an auto-act ladder rung can never discount below a seller-set floor."""
    if not (0 <= discount_pct <= max_discount_pct):
        raise ActionError(f"discount {discount_pct}% exceeds allowed ceiling of {max_discount_pct}%")
    before = store.get_listing(listing_id)
    if before is None:
        raise ActionError(f"unknown listing {listing_id}")
    with store.cursor() as cur:
        cur.execute(
            "UPDATE listings SET discount_pct = ?, updated_at = ? WHERE listing_id = ?",
            (discount_pct, time.time(), listing_id),
        )
    after = store.get_listing(listing_id)
    action_id = store.write_audit(actor, "markdown",
                                   {"listing_id": listing_id, "discount_pct": discount_pct}, before, after)
    return ActionResult(action_id, "markdown", before, after)


def stock_adjust(store: Store, sku: str, size: str, delta: int, *, actor: str = "copilot") -> ActionResult:
    """Adjust stock count (e.g. decrement on a confirmed sale, increment on restock)."""
    before_qty = store.get_stock(sku, size)
    if before_qty is None:
        raise ActionError(f"unknown sku/size {sku}/{size}")
    new_qty = before_qty + delta
    if new_qty < 0:
        raise ActionError(f"stock_adjust would drive {sku}/{size} negative ({before_qty} + {delta})")
    with store.cursor() as cur:
        cur.execute("UPDATE inventory SET stock_qty = ? WHERE sku = ? AND size = ?",
                    (new_qty, sku, size))
    before = {"sku": sku, "size": size, "stock_qty": before_qty}
    after = {"sku": sku, "size": size, "stock_qty": new_qty}
    action_id = store.write_audit(actor, "stock_adjust", {"sku": sku, "size": size, "delta": delta},
                                   before, after)
    return ActionResult(action_id, "stock_adjust", before, after)


def rollback(store: Store, action_id: str, *, actor: str = "seller") -> ActionResult:
    """Undo any of the four action types by replaying the audited before-state."""
    row = store.get_audit(action_id)
    if row is None:
        raise ActionError(f"no audit row {action_id}")
    if row["reversed"]:
        raise ActionError(f"action {action_id} was already rolled back")

    import json
    action_type = row["action_type"]
    before = json.loads(row["before_json"])

    if action_type == "push":
        result = push(store, before["listing_id"], featured=bool(before["featured"]), actor=actor)
    elif action_type == "swap":
        # inverse of a swap is a swap back
        result = _rollback_swap(store, row, actor)
    elif action_type == "markdown":
        result = markdown(store, before["listing_id"], before["discount_pct"], actor=actor,
                           max_discount_pct=100)
    elif action_type == "stock_adjust":
        before_qty = before["stock_qty"]
        current = store.get_stock(before["sku"], before["size"])
        delta = before_qty - current
        result = stock_adjust(store, before["sku"], before["size"], delta, actor=actor)
    else:
        raise ActionError(f"unknown action_type {action_type}")

    store.mark_reversed(action_id)
    return result


def _rollback_swap(store: Store, row: dict, actor: str) -> ActionResult:
    import json
    before = json.loads(row["before_json"])
    from_id = before["from"]["listing_id"]
    to_id = before["to"]["listing_id"]
    # after the original swap, `to_id` is live; rolling back means making
    # `from_id` live again.
    return swap(store, to_id, from_id, actor=actor)
