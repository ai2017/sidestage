"""
FastAPI wrapper around the pipeline, for the seller console UI (seller UX
is a *supporting* concern here, not the chosen depth area, so this layer
is intentionally thin: REST + one broadcast websocket, no auth, no
multi-tenant seller accounts. See PRD.md "Known limitations" for what a
real pilot would need on top of this.)

Run with:  uvicorn sidestage.api.server:app --reload --port 8000
Then open  http://localhost:8000/
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sidestage.actions.tools import ActionError, rollback as rollback_action
from sidestage.ingestion.stream import SAMPLE_SCRIPT, make_message
from sidestage.ladder import Rung
from sidestage.pipeline import Pipeline
from sidestage.store import seed

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "data" / "live.db")
CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console"

app = FastAPI(title="SideStage Copilot")

store = seed(DB_PATH, reset=True)
pipeline = Pipeline(store)

_sockets: list[WebSocket] = []


def _dollars(cents: Optional[int]) -> Optional[float]:
    return None if cents is None else round(cents / 100, 2)


def serialize_result(res) -> dict:
    return {
        "message": {
            "id": res.message.id, "viewer": res.message.viewer, "text": res.message.text,
            "intent": res.message.intent, "size_hint": res.message.size_hint,
        },
        "resolved_sku": res.resolved_sku,
        "draft": {
            "text": res.draft.text, "confidence": res.draft.confidence,
            "backend": res.draft.backend, "tool_trace": res.draft.tool_trace,
        },
        "guardrail": {
            "passed": res.guardrail.passed,
            "final_text": res.guardrail.final_text,
            "corrected": res.guardrail.corrected,
            "violations": [
                {"kind": v.kind, "detail": v.detail, "correctable": v.correctable}
                for v in res.guardrail.violations
            ],
        },
        "decision": {"rung": res.decision.rung.name, "reason": res.decision.reason},
        "action": None if res.action is None else {
            "action_id": res.action.action_id, "action_type": res.action.action_type,
            "before": res.action.before, "after": res.action.after,
        },
        "timings_ms": {
            "resolve": round(res.timings.resolve_ms, 2), "draft": round(res.timings.draft_ms, 2),
            "guardrail": round(res.timings.guardrail_ms, 2), "action": round(res.timings.action_ms, 2),
            "total": round(res.timings.total_ms, 2),
        },
        "status": res.final_status,
    }


async def _broadcast(event: dict) -> None:
    dead = []
    for ws in _sockets:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _sockets.remove(ws)


class ChatIn(BaseModel):
    viewer: str
    text: str


class LadderIn(BaseModel):
    intent_type: str
    level: int


@app.get("/api/state")
def get_state() -> dict:
    return {
        "catalog": store.all_products(),
        "listings": [store.get_listing(lid) for lid in ("L-001", "L-002")],
        "ladder": {
            it: store.ladder_level(it)
            for it in ("price_question", "availability_question", "policy_question",
                       "purchase_intent", "general")
        },
        "recent_messages": store.recent_messages(30),
        "recent_audit": store.recent_audit(30),
    }


@app.post("/api/chat")
async def post_chat(body: ChatIn) -> dict:
    msg = make_message(body.viewer, body.text)
    result = pipeline.process_message(msg)
    payload = serialize_result(result)
    await _broadcast({"type": "message", **payload})
    return payload


@app.post("/api/simulate")
async def post_simulate() -> dict:
    async def run():
        for viewer, text in SAMPLE_SCRIPT:
            msg = make_message(viewer, text)
            result = pipeline.process_message(msg)
            await _broadcast({"type": "message", **serialize_result(result)})
            await asyncio.sleep(0.6)

    asyncio.create_task(run())
    return {"started": True, "count": len(SAMPLE_SCRIPT)}


@app.post("/api/rollback/{action_id}")
async def post_rollback(action_id: str) -> dict:
    try:
        result = rollback_action(store, action_id, actor="seller")
    except ActionError as e:
        return {"ok": False, "error": str(e)}
    payload = {"ok": True, "action_type": result.action_type, "before": result.before,
               "after": result.after}
    await _broadcast({"type": "rollback", "action_id": action_id, **payload})
    return payload


@app.post("/api/ladder")
async def post_ladder(body: LadderIn) -> dict:
    if not (0 <= body.level <= 2):
        return {"ok": False, "error": "level must be 0, 1, or 2"}
    store.set_ladder_level(body.intent_type, body.level)
    await _broadcast({"type": "ladder", "intent_type": body.intent_type, "level": body.level})
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _sockets.append(ws)
    try:
        while True:
            await ws.receive_text()  # unused; keeps the connection open
    except WebSocketDisconnect:
        if ws in _sockets:
            _sockets.remove(ws)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(CONSOLE_DIR / "index.html"))


if (CONSOLE_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(CONSOLE_DIR / "assets")), name="assets")
