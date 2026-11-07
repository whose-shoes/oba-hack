"""웹 앱: 통화 수신 API + 자식용 대시보드(승인·취소·정책) + 데모용 샘플 통화."""

from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import catalog, ingest, llm, pipeline, policy, store
from .config import data_dir

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples"

app = FastAPI(title="오바란 — 엄마 전화 한 통이면 끝")
app.include_router(ingest.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm": llm.provider_label()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state() -> dict:
    return {
        "calls": store.all_docs("call"),
        "orders": store.all_docs("order"),
        "events": store.events(150),
        "policy": policy.get(),
        "spent_this_month": policy.spent_this_month(),
        "llm": llm.provider_label(),
        "history": [{**h, "name": (catalog.product(h["product_id"]) or {}).get("name")}
                    for h in catalog.history("엄마")],
    }


class DemoCall(BaseModel):
    transcript: str
    caller: str = "엄마"


@app.post("/api/calls", status_code=201)
def demo_call(body: DemoCall, background: BackgroundTasks) -> dict:
    """데모·붙여넣기용: 통화 텍스트를 직접 넣어 처리."""
    if not body.transcript.strip():
        raise HTTPException(400, "empty transcript")
    call_id, files = ingest.save_files([("transcript.txt", "text/plain", body.transcript.encode())])
    call = pipeline.register_call(call_id, files, caller=body.caller.strip() or "엄마", source="demo",
                                  transcript=body.transcript)
    background.add_task(pipeline.process_call, call_id)
    return call


class TranscriptIn(BaseModel):
    text: str


@app.post("/api/calls/{call_id}/transcript")
def set_transcript(call_id: str, body: TranscriptIn, background: BackgroundTasks) -> dict:
    call = store.get("call", call_id)
    if call is None:
        raise HTTPException(404)
    call["transcript"] = body.text
    store.put("call", call_id, call)
    (data_dir() / "calls" / call_id / "transcript.txt").write_text(body.text)
    background.add_task(pipeline.process_call, call_id)
    return call


@app.get("/api/samples")
def samples() -> list[dict]:
    out = []
    for p in sorted(SAMPLES.glob("*.txt")):
        lines = p.read_text().splitlines()
        title, caller = lines[0].lstrip("# ").strip(), lines[1].split(":", 1)[1].strip()
        out.append({"id": p.stem, "title": title, "caller": caller, "transcript": "\n".join(lines[2:]).strip()})
    return out


def _act(fn, oid: str, *args) -> dict:
    try:
        return fn(oid, *args)
    except KeyError:
        raise HTTPException(404, "order not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/orders/{oid}/approve")
def approve(oid: str) -> dict:
    return _act(pipeline.approve, oid)


@app.post("/api/orders/{oid}/reject")
def reject(oid: str) -> dict:
    return _act(pipeline.reject, oid)


@app.post("/api/orders/{oid}/cancel")
def cancel(oid: str) -> dict:
    return _act(pipeline.cancel, oid)


class SwapIn(BaseModel):
    product_id: str


@app.post("/api/orders/{oid}/swap")
def swap(oid: str, body: SwapIn) -> dict:
    return _act(pipeline.swap, oid, body.product_id)


@app.put("/api/policy")
def put_policy(changes: dict) -> dict:
    return policy.update(changes)


@app.post("/api/reset")
def reset() -> dict:
    """데모 초기화: 통화·주문·이벤트 삭제 (정책은 유지)."""
    db = store._db()
    with store._lock:
        db.execute("delete from docs where kind in ('call', 'order')")
        db.execute("delete from events")
        db.commit()
    return {"ok": True}
