"""
Ground-truth store for SideStage.

One SQLite file is the single source of truth for catalog, live inventory,
listings (what's currently "on air"), policies, seller automation-ladder
settings, and the audit log. Every reply the copilot drafts and every
action it executes is checked against (or written to) this store — never
against whatever an LLM merely *said*. That separation is the backbone of
the guardrail design (see guardrails/engine.py and TDD.md, "Why a re-verify
step and not just a better prompt").

We use raw sqlite3 rather than an ORM on purpose: the schema is small,
every row that matters (a price, a stock count, an audit entry) has to be
auditable in a one-line `sqlite3 shop.db "select ..."`, and we did not want
an abstraction layer between "what the guardrail checked" and "what is
actually in the database." See TDD.md for the trade-off discussion.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

# Canonical garment size order. Anything not listed sorts to the end,
# alphabetically, so an unexpected size never crashes the sort.
SIZE_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    color TEXT,
    price_cents INTEGER NOT NULL,
    promo_price_cents INTEGER,
    description TEXT NOT NULL,
    sizes TEXT NOT NULL -- json list
);

CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT NOT NULL,
    size TEXT NOT NULL,
    stock_qty INTEGER NOT NULL,
    PRIMARY KEY (sku, size),
    FOREIGN KEY (sku) REFERENCES catalog(sku)
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    is_live INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    discount_pct INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY (sku) REFERENCES catalog(sku)
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ladder_settings (
    intent_type TEXT PRIMARY KEY,
    automation_level INTEGER NOT NULL DEFAULT 0 -- 0 suggest, 1 auto-send, 2 auto-act
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,           -- 'copilot' | 'seller' | 'system'
    action_type TEXT NOT NULL,     -- 'push' | 'swap' | 'markdown' | 'stock_adjust' | 'reply_sent'
    payload_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reversed INTEGER NOT NULL DEFAULT 0,
    reverses_id TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    viewer TEXT NOT NULL,
    text TEXT NOT NULL,
    intent TEXT,
    reply_text TEXT,
    reply_status TEXT,   -- 'sent' | 'suggested' | 'blocked'
    guardrail_notes TEXT,
    latency_ms REAL
);
"""


@dataclass
class Store:
    path: str

    def __post_init__(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ---------- reads ----------

    def get_product(self, sku: str) -> Optional[dict]:
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM catalog WHERE sku = ?", (sku,)).fetchone()
            return dict(row) if row else None

    def find_products(self, name_query: str) -> list[dict]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM catalog WHERE lower(name) LIKE ? OR lower(color) LIKE ?",
                (f"%{name_query.lower()}%", f"%{name_query.lower()}%"),
            ).fetchall()
            return [dict(r) for r in rows]

    def all_products(self) -> list[dict]:
        with self.cursor() as cur:
            return [dict(r) for r in cur.execute("SELECT * FROM catalog").fetchall()]

    def get_stock(self, sku: str, size: str) -> Optional[int]:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT stock_qty FROM inventory WHERE sku = ? AND size = ?", (sku, size)
            ).fetchone()
            return row["stock_qty"] if row else None

    def stock_for_sku(self, sku: str) -> dict:
        """Stock by size, in canonical garment order.

        Without the explicit sort this returns sizes alphabetically -- SQLite
        satisfies the `WHERE sku = ?` lookup from the composite primary-key
        index, so rows arrive ordered by size *as text*: L, M, S, XL, XS.
        That's a defensible database answer and a nonsense answer to a
        shopper, which is exactly the sort of thing that makes a tool feel
        broken to the one user we built it for.
        """
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT size, stock_qty FROM inventory WHERE sku = ?", (sku,)
            ).fetchall()
            ordered = sorted(
                rows,
                key=lambda r: (SIZE_ORDER.index(r["size"]) if r["size"] in SIZE_ORDER
                               else len(SIZE_ORDER), r["size"]),
            )
            return {r["size"]: r["stock_qty"] for r in ordered}

    def all_policies(self) -> list[dict]:
        with self.cursor() as cur:
            return [dict(r) for r in cur.execute("SELECT * FROM policies").fetchall()]

    def get_listing(self, listing_id: str) -> Optional[dict]:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            return dict(row) if row else None

    def live_listing(self) -> Optional[dict]:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM listings WHERE is_live = 1 ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def ladder_level(self, intent_type: str) -> int:
        with self.cursor() as cur:
            row = cur.execute(
                "SELECT automation_level FROM ladder_settings WHERE intent_type = ?",
                (intent_type,),
            ).fetchone()
            return row["automation_level"] if row else 0

    def set_ladder_level(self, intent_type: str, level: int) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO ladder_settings (intent_type, automation_level) VALUES (?, ?) "
                "ON CONFLICT(intent_type) DO UPDATE SET automation_level = excluded.automation_level",
                (intent_type, level),
            )

    def recent_audit(self, limit: int = 50) -> list[dict]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_audit(self, action_id: str) -> Optional[dict]:
        with self.cursor() as cur:
            row = cur.execute("SELECT * FROM audit_log WHERE id = ?", (action_id,)).fetchone()
            return dict(row) if row else None

    def recent_messages(self, limit: int = 50) -> list[dict]:
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- writes used by the pipeline / actions ----------

    def record_message(self, msg: dict) -> None:
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (id, ts, viewer, text, intent, reply_text, reply_status, "
                "guardrail_notes, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    msg["id"], msg["ts"], msg["viewer"], msg["text"], msg.get("intent"),
                    msg.get("reply_text"), msg.get("reply_status"),
                    json.dumps(msg.get("guardrail_notes", [])), msg.get("latency_ms"),
                ),
            )

    def write_audit(self, actor: str, action_type: str, payload: dict, before: dict, after: dict,
                     reverses_id: Optional[str] = None) -> str:
        action_id = str(uuid.uuid4())
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (id, ts, actor, action_type, payload_json, before_json, "
                "after_json, reversed, reverses_id) VALUES (?,?,?,?,?,?,?,0,?)",
                (action_id, time.time(), actor, action_type, json.dumps(payload),
                 json.dumps(before), json.dumps(after), reverses_id),
            )
        return action_id

    def mark_reversed(self, action_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE audit_log SET reversed = 1 WHERE id = ?", (action_id,))

    def raw(self) -> sqlite3.Connection:
        """Escape hatch for the action layer to run writes inside the same
        connection/transaction as their audit-log entry."""
        return self._conn


def seed(db_path: str, reset: bool = True) -> Store:
    """Seed a fresh store with Maya's Boutique fixture data (see PRD.md persona)."""
    p = Path(db_path)
    if reset and p.exists():
        p.unlink()
    store = Store(db_path)

    products = [
        dict(sku="DRS-CORAL-01", name="Coral Wrap Midi Dress", category="dress", color="coral",
             price_cents=5800, promo_price_cents=None,
             description="Stretch-jersey wrap midi, true to size, hand wash cold.",
             sizes=json.dumps(["XS", "S", "M", "L", "XL"])),
        dict(sku="DRS-SAGE-02", name="Sage Linen Midi Dress", category="dress", color="sage",
             price_cents=6400, promo_price_cents=5400,
             description="Linen-blend midi with side pockets, runs one size small.",
             sizes=json.dumps(["XS", "S", "M", "L"])),
        dict(sku="TOP-CREAM-03", name="Cream Ribbed Tank", category="top", color="cream",
             price_cents=2200, promo_price_cents=None,
             description="Ribbed knit tank, true to size.",
             sizes=json.dumps(["XS", "S", "M", "L", "XL"])),
        dict(sku="DEN-BLU-04", name="Classic Blue Denim Jacket", category="jacket", color="blue",
             price_cents=7200, promo_price_cents=None,
             description="Mid-wash denim jacket, oversized fit, runs one size big.",
             sizes=json.dumps(["S", "M", "L"])),
    ]
    with store.cursor() as cur:
        for prod in products:
            cur.execute(
                "INSERT INTO catalog (sku, name, category, color, price_cents, promo_price_cents, "
                "description, sizes) VALUES (:sku,:name,:category,:color,:price_cents,"
                ":promo_price_cents,:description,:sizes)",
                prod,
            )

        stock = {
            "DRS-CORAL-01": {"XS": 2, "S": 0, "M": 5, "L": 3, "XL": 1},
            "DRS-SAGE-02": {"XS": 0, "S": 4, "M": 4, "L": 0},
            "TOP-CREAM-03": {"XS": 6, "S": 6, "M": 6, "L": 6, "XL": 2},
            "DEN-BLU-04": {"S": 3, "M": 0, "L": 2},
        }
        for sku, sizes in stock.items():
            for size, qty in sizes.items():
                cur.execute(
                    "INSERT INTO inventory (sku, size, stock_qty) VALUES (?,?,?)",
                    (sku, size, qty),
                )

        policies = [
            ("shipping", "Standard shipping is 3-5 business days, $5.99 flat rate, free over $75."),
            ("returns", "Returns accepted within 14 days of delivery for unworn items with tags. "
                        "Final-sale items marked as such at checkout are not returnable."),
            ("damage", "If an item arrives damaged, contact us within 48 hours of delivery with a "
                       "photo and we will replace or refund it at no cost."),
            ("authenticity", "All items are purchased new from the listed brand or its authorized "
                             "distributor; we do not sell replicas."),
        ]
        for topic, text in policies:
            cur.execute(
                "INSERT INTO policies (policy_id, topic, text) VALUES (?,?,?)",
                (str(uuid.uuid4()), topic, text),
            )

        cur.execute(
            "INSERT INTO listings (listing_id, sku, is_live, featured, discount_pct, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("L-001", "DRS-CORAL-01", 1, 1, 0, time.time()),
        )
        cur.execute(
            "INSERT INTO listings (listing_id, sku, is_live, featured, discount_pct, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("L-002", "DRS-SAGE-02", 0, 0, 15, time.time()),
        )

        # Default ladder: everything starts at suggest-only (level 0) except
        # low-risk FAQ intents, which Maya has already opted into auto-send.
        for intent_type, level in [
            ("price_question", 1),
            ("availability_question", 1),
            ("policy_question", 1),
            ("fit_question", 1),
            ("purchase_intent", 0),
            ("general", 0),
        ]:
            cur.execute(
                "INSERT INTO ladder_settings (intent_type, automation_level) VALUES (?,?)",
                (intent_type, level),
            )

    return store
