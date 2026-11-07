"""웹 앱: 통화 수신 API + 자식용 대시보드(승인·취소·정책) + 데모용 샘플 통화."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import catalog, family, ingest, llm, mcp_server, notify, pipeline, policy, store, telco
from .config import data_dir

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent / "samples"

async def _retry_loop() -> None:
    """웹훅 실패분·웹훅을 나중에 켰을 때 쌓인 정리본을 주기적으로 다시 보낸다."""
    interval = float(os.environ.get("OBA_VOOY_RETRY_SEC", "10"))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(notify.retry_pending)
        except Exception as e:  # 루프는 죽지 않게
            print(f"[vooy] retry error: {e!r}", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp_server.server.session_manager.run():  # vooy용 MCP(Streamable HTTP)
        print(f"[mcp] vooy 연결 주소: {mcp_server.public_url()}", flush=True)
        task = asyncio.create_task(_retry_loop())
        try:
            yield
        finally:
            task.cancel()


app = FastAPI(title="오바란 — 엄마 전화 한 통이면 끝", lifespan=lifespan)
app.include_router(ingest.router)
app.include_router(telco.router)
app.router.routes.extend(mcp_server.http_app.routes)


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
        "family": family.public(family.current()),
        "mcp_url": mcp_server.public_url(),
        "vooy_pending": len(notify.pending()),
        "vooy": notify.public_settings(),
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
        db.execute("delete from docs where kind in ('call', 'order', 'telco_call', 'digest')")
        db.execute("delete from events")
        db.commit()
    return {"ok": True}


# ---------- 가입·연동 ----------

class ParentIn(BaseModel):
    label: str = "엄마"
    phone: str
    address: str = ""


class SignupIn(BaseModel):
    child_name: str
    child_phone: str
    parents: list[ParentIn]


@app.post("/api/families", status_code=201)
def signup(body: SignupIn) -> dict:
    try:
        fam = family.register(body.child_name, body.child_phone, [p.model_dump() for p in body.parents])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return family.public(fam)


class ConsentIn(BaseModel):
    granted: bool = True


@app.post("/api/families/{fid}/parents/{pid}/consent")
def parent_consent(fid: str, pid: str, body: ConsentIn) -> dict:
    """엄마의 '예' 답장 (데모에서는 버튼으로 시뮬레이션)."""
    try:
        return family.public(family.consent(fid, pid, body.granted))
    except KeyError:
        raise HTTPException(404)


@app.delete("/api/families/{fid}")
def leave(fid: str) -> dict:
    family.delete(fid)
    return {"ok": True}


# ---------- 통화 시뮬레이터 (SKT 망 연동 모의) ----------

class SimCall(BaseModel):
    transcript: str
    who: str = "parent"          # parent | unknown
    direction: str = "incoming"  # incoming(엄마→나) | outgoing(나→엄마)
    unknown_number: str = "010-9876-5432"


@app.post("/api/simulate/call-ended", status_code=202)
def simulate_call(body: SimCall, background: BackgroundTasks) -> dict:
    """에이닷이 통화 종료 웹훅을 보낸 것처럼 처리한다. 실제 웹훅과 같은 함수(telco.receive)를 쓴다."""
    fam = family.current()
    if fam is None:
        raise HTTPException(409, "먼저 가입하고 부모님 번호를 등록해 주세요.")
    if not body.transcript.strip():
        raise HTTPException(400, "empty transcript")
    other = fam["parents"][0]["phone"] if body.who == "parent" else body.unknown_number
    a, b = (other, fam["child_phone"]) if body.direction == "incoming" else (fam["child_phone"], other)
    ev = telco.CallEnded(call_id=f"sim-{uuid.uuid4().hex[:10]}", from_number=a, to_number=b,
                         transcript=body.transcript)
    result, call_id = telco.receive(ev, source="SKT 망 연동 모의")
    if call_id:
        background.add_task(pipeline.process_call, call_id)
    return result


# ---------- vooy 웹훅 설정 · 테스트 · 모의 수신함 ----------

class VooySettingsIn(BaseModel):
    push_url: str | None = None
    push_token: str | None = None
    user_id: str | None = None
    signing_secret: str | None = None


@app.get("/api/vooy")
def vooy_settings() -> dict:
    return notify.public_settings() | {"mcp_url": mcp_server.public_url()}


@app.put("/api/vooy")
def vooy_update(body: VooySettingsIn) -> dict:
    try:
        out = notify.update_settings(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    notify.retry_pending()  # 웹훅을 막 켰다면 쌓여 있던 정리본을 바로 보낸다
    return out


@app.post("/api/vooy/test")
def vooy_test() -> dict:
    return notify.send_test()


@app.post("/mock/vooy/webhook")
async def mock_vooy_webhook(request: Request) -> dict:
    """데모용 '모의 vooy': 우리가 보낸 웹훅을 vooy처럼 받아 인증·서명을 확인하고 도착 메시지를 남긴다."""
    body = await request.body()
    s = notify.settings()
    if s["push_token"] and request.headers.get("authorization") != f"Bearer {s['push_token']}":
        raise HTTPException(401, "bad token")
    verified = None
    if s["signing_secret"]:
        expect = notify.sign(s["signing_secret"], request.headers.get("x-oba-timestamp", ""), body)
        verified = hmac.compare_digest(request.headers.get("x-oba-signature", ""), expect)
        if not verified:
            raise HTTPException(401, "bad signature")
    data = json.loads(body)
    store.log("vooy_mock", request.headers.get("x-oba-delivery", ""),
              "📱 vooy(모의)에 도착" + (" · 서명 확인" if verified else "") + ": " + data.get("text", ""))
    return {"ok": True, "signature_verified": verified}


# ---------- vooy 크론용 REST (MCP 대신 HTTP로 가져갈 때) ----------

@app.get("/v1/vooy/digests")
def poll_digests(mark: bool = True, _: dict = Depends(ingest.upload_auth)) -> dict:
    """새 정리본. mark=true(기본)면 가져가며 전달됨 표시, false면 보기만 하고 /ack로 확인."""
    if mark:
        items = notify.take_pending("rest")
    else:
        items = notify.pending()
        notify.record_poll(len(items), "rest-peek")
    return {"has_new": bool(items), "digests": [notify.public(d) for d in items]}


class AckIn(BaseModel):
    ids: list[str]


@app.post("/v1/vooy/digests/ack")
def ack_digests(body: AckIn, _: dict = Depends(ingest.upload_auth)) -> dict:
    return {"acked": notify.mark_delivered(body.ids, "rest")}
