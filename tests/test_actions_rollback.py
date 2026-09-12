"""Action auditability and rollback: every action writes a before/after
audit row in the same transaction as its mutation, and rollback replays
the before-state exactly, for all four action types."""

import pytest

from sidestage.actions.tools import (
    ActionError, markdown, push, rollback, stock_adjust, swap,
)


def test_push_writes_audit_and_rolls_back(store):
    before = store.get_listing("L-002")["featured"]
    result = push(store, "L-002", featured=True)
    assert store.get_listing("L-002")["featured"] == 1
    audit = store.get_audit(result.action_id)
    assert audit["action_type"] == "push" and audit["reversed"] == 0

    rollback(store, result.action_id)
    assert store.get_listing("L-002")["featured"] == before
    assert store.get_audit(result.action_id)["reversed"] == 1


def test_swap_and_rollback_restores_live_listing(store):
    assert store.get_listing("L-001")["is_live"] == 1
    assert store.get_listing("L-002")["is_live"] == 0

    result = swap(store, "L-001", "L-002")
    assert store.get_listing("L-001")["is_live"] == 0
    assert store.get_listing("L-002")["is_live"] == 1

    rollback(store, result.action_id)
    assert store.get_listing("L-001")["is_live"] == 1
    assert store.get_listing("L-002")["is_live"] == 0


def test_markdown_ceiling_is_enforced(store):
    with pytest.raises(ActionError):
        markdown(store, "L-001", 50, max_discount_pct=30)


def test_markdown_and_rollback(store):
    before_pct = store.get_listing("L-001")["discount_pct"]
    result = markdown(store, "L-001", 20)
    assert store.get_listing("L-001")["discount_pct"] == 20
    rollback(store, result.action_id)
    assert store.get_listing("L-001")["discount_pct"] == before_pct


def test_stock_adjust_cannot_go_negative(store):
    # DRS-SAGE-02 / XS has 0 units in the seed fixture.
    with pytest.raises(ActionError):
        stock_adjust(store, "DRS-SAGE-02", "XS", -1)


def test_stock_adjust_and_rollback(store):
    before_qty = store.get_stock("DRS-CORAL-01", "L")
    result = stock_adjust(store, "DRS-CORAL-01", "L", -2)
    assert store.get_stock("DRS-CORAL-01", "L") == before_qty - 2
    rollback(store, result.action_id)
    assert store.get_stock("DRS-CORAL-01", "L") == before_qty


def test_double_rollback_is_rejected(store):
    result = stock_adjust(store, "DRS-CORAL-01", "L", -1)
    rollback(store, result.action_id)
    with pytest.raises(ActionError):
        rollback(store, result.action_id)


def test_audit_log_before_after_state_is_exact(store):
    result = stock_adjust(store, "TOP-CREAM-03", "S", -3)
    audit = store.get_audit(result.action_id)
    import json
    before = json.loads(audit["before_json"])
    after = json.loads(audit["after_json"])
    assert before["stock_qty"] - after["stock_qty"] == 3
