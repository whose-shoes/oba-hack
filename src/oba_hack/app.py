"""웹 앱: 통화 수신 API + 자식용 대시보드(승인·취소·정책) + 데모용 샘플 통화."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (account, catalog, family, ingest, llm, mcp_server, notify, pipeline, policy, ratelimit, samples, store,
               telco, usage)
from .config import data_dir, demo_mode, telco_signing_secret

STATIC = Path(__file__).parent / "static"

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


app = FastAPI(title="don't call me, Mama", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """페이지·정적 파일은 매번 다시 확인하게 한다 (재배포 뒤 옛 화면이 캐시에서 뜨지 않도록, 바뀐 게 없으면 304)."""
    response = await call_next(request)
    if request.url.path in ("/", "/skt", "/vooy", "/admin") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(ingest.router)
app.include_router(telco.router)
app.router.routes.extend(mcp_server.http_app.routes)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "llm": llm.provider_label()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/skt")
def skt_page() -> FileResponse:
    """SKT용 시작 페이지: 에이닷 통화 종료 → 결제까지 직접 체험하고, 연동 명세를 본다."""
    return FileResponse(STATIC / "skt.html")


@app.get("/vooy")
def vooy_page() -> FileResponse:
    """vooy 팀용 시작 페이지: MCP 연결·레시피·도구 목록과 체험 계정."""
    return FileResponse(STATIC / "vooy.html")


# ---------- 운영자: LLM 사용량 (OBA_ADMIN_TOKEN) ----------

def require_admin(authorization: str = Header(default="")) -> None:
    token = os.environ.get("OBA_ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(404, "not found")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid token")


@app.get("/admin")
def admin_page() -> FileResponse:
    """운영자용 LLM 사용량 화면 (토큰은 화면에서 입력, 주소에 넣지 않음)."""
    return FileResponse(STATIC / "admin.html")


@app.get("/admin/usage", dependencies=[Depends(require_admin)])
def admin_usage(days: int = 14, recent: int = 50) -> dict:
    """LLM 사용량: 오늘·일별·팀별·계정별·용도별·모델별 합계와 최근 호출 (프롬프트·응답 내용은 없음)."""
    return usage.summary(max(1, min(days, 90)), max(1, min(recent, 500)))


def _limit(request: Request, key: str, limit: int, window_sec: float) -> None:
    ip = ratelimit.client_ip(request.headers, request.client.host if request.client else "")
    if not ratelimit.allow(f"{key}:{ip}", limit, window_sec):
        raise HTTPException(429, "요청이 너무 많아요. 잠시 후 다시 시도해 주세요.")


@app.get("/api/public")
async def public_info() -> dict:
    """시작 페이지용 공개 정보 (토큰·개인정보 없음)."""
    return {
        "demo": demo_mode(),
        "llm": llm.provider_label(),
        "signup_mcp_url": mcp_server.public_url(),
        "public_url": os.environ.get("OBA_PUBLIC_URL", "").rstrip("/"),
        "telco_signing": bool(telco_signing_secret()),
        "tools": await mcp_server.tool_catalog(),
    }


class DemoIn(BaseModel):
    audience: str = "skt"   # skt: 우리 서버가 테스트 결제 | vooy: vooy가 1회용 토큰으로 테스트몰 결제


@app.post("/api/demo", status_code=201)
def start_demo(request: Request, body: DemoIn | None = None) -> dict:
    """체험용 샌드박스 계정을 바로 만든다 (데모 번호·배송지·엄마 메모가 채워져 있음)."""
    if not demo_mode():
        raise HTTPException(404, "not found")
    _limit(request, "demo", 20, 3600)
    vooy = (body.audience if body else "") == "vooy"
    fam = family.register_demo("vooy 체험" if vooy else "체험", payment_executor="vooy" if vooy else "server",
                               origin="vooy 체험" if vooy else "SKT 체험")
    return family.public(fam)


# ---------- 계정: 개인 대시보드 토큰으로 판별 ----------

def current_account(request: Request) -> dict:
    tok = request.headers.get("x-account") or request.query_params.get("a") or ""
    fam = family.by_dashboard_token(tok) if tok else None
    if fam is None:
        raise HTTPException(401, "개인 대시보드 링크로 접속해 주세요.")
    return fam


def _mine(kind: str, fid: str) -> list[dict]:
    return [d for d in store.all_docs(kind) if d.get("family_id") == fid]


def _own(kind: str, doc_id: str, fam: dict) -> dict:
    d = store.get(kind, doc_id)
    if d is None or d.get("family_id") != fam["id"]:
        raise HTTPException(404, "not found")
    account.log_family.set(fam["id"])
    return d


@app.get("/api/state")
def state(fam: dict = Depends(current_account)) -> dict:
    fid = fam["id"]
    return {
        "calls": _mine("call", fid),
        "orders": _mine("order", fid),
        "events": store.events(150, family_id=fid),
        "policy": policy.get(fid),
        "spent_this_month": policy.spent_this_month(fid),
        "llm": llm.provider_label(),
        "family": family.public(fam),
        "mcp_url": family.links(fam)["mcp_url"],
        "signup_mcp_url": mcp_server.public_url(),
        "vooy_pending": len(notify.pending(fid)),
        "vooy": notify.public_settings(fid),
        "history": _history(fid, fam["parents"][0]["label"]),
    }


def _history(fid: str, label: str) -> list[dict]:
    """화면용 구매 이력: 상품별로 묶어 가장 최근 날짜와 횟수."""
    out: dict[str, dict] = {}
    for h in pipeline.purchase_history(fid, label):
        cur = out.setdefault(h["product_id"], {"product_id": h["product_id"], "date": "", "times": 0,
                                                "name": (catalog.product(h["product_id"]) or {}).get("name")})
        cur["date"], cur["times"] = max(cur["date"], h["date"]), cur["times"] + 1
    return sorted(out.values(), key=lambda h: h["date"], reverse=True)


class DemoCall(BaseModel):
    transcript: str
    caller: str = "엄마"


@app.post("/api/calls", status_code=201)
def demo_call(body: DemoCall, background: BackgroundTasks, fam: dict = Depends(current_account)) -> dict:
    """데모·붙여넣기용: 통화 텍스트를 직접 넣어 처리 (내 계정으로)."""
    if not body.transcript.strip():
        raise HTTPException(400, "empty transcript")
    call_id, files = ingest.save_files([("transcript.txt", "text/plain", body.transcript.encode())])
    call = pipeline.register_call(call_id, files, caller=body.caller.strip() or "엄마", source="demo",
                                  transcript=body.transcript, family_id=fam["id"])
    background.add_task(pipeline.process_call, call_id)
    return call


class TranscriptIn(BaseModel):
    text: str


@app.post("/api/calls/{call_id}/transcript")
def set_transcript(call_id: str, body: TranscriptIn, background: BackgroundTasks,
                   fam: dict = Depends(current_account)) -> dict:
    call = _own("call", call_id, fam)
    call["transcript"] = body.text
    store.put("call", call_id, call)
    (data_dir() / "calls" / call_id / "transcript.txt").write_text(body.text)
    background.add_task(pipeline.process_call, call_id)
    return call


@app.get("/api/samples")
def sample_calls() -> list[dict]:
    return list(samples.all_samples())


@app.get("/api/calls/{call_id}/outcome")
def call_outcome(call_id: str, fam: dict = Depends(current_account)) -> dict:
    """통화 한 건이 어떻게 처리됐는지 (진행 단계 표시용)."""
    _own("call", call_id, fam)
    return telco.outcome(call_id)


def _act(fn, oid: str, *args) -> dict:
    try:
        return fn(oid, *args)
    except KeyError:
        raise HTTPException(404, "order not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/orders/{oid}/approve")
def approve(oid: str, fam: dict = Depends(current_account)) -> dict:
    """서버 결제 모드면 테스트 결제, vooy 결제 모드면 구매 승인만 내고 결제는 vooy가 한다."""
    _own("order", oid, fam)
    if policy.get(fam["id"]).get("payment_executor") == "vooy":
        return _act(pipeline.authorize, oid)
    return _act(pipeline.approve, oid)


@app.post("/api/orders/{oid}/reject")
def reject(oid: str, fam: dict = Depends(current_account)) -> dict:
    _own("order", oid, fam)
    return _act(pipeline.reject, oid)


@app.post("/api/orders/{oid}/cancel")
def cancel(oid: str, fam: dict = Depends(current_account)) -> dict:
    _own("order", oid, fam)
    return _act(pipeline.cancel, oid)


class SwapIn(BaseModel):
    product_id: str


@app.post("/api/orders/{oid}/swap")
def swap(oid: str, body: SwapIn, fam: dict = Depends(current_account)) -> dict:
    _own("order", oid, fam)
    return _act(pipeline.swap, oid, body.product_id)


@app.put("/api/policy")
def put_policy(changes: dict, fam: dict = Depends(current_account)) -> dict:
    return policy.update(changes, fam["id"])


def _wipe(fid: str) -> None:
    """한 계정의 통화·주문·정리본·알림을 지운다."""
    with store._lock:
        db = store._db()
        for kind in ("call", "order", "digest"):
            ids = [d["id"] for d in store.all_docs(kind) if d.get("family_id") == fid]
            db.executemany("delete from docs where kind=? and id=?", [(kind, i) for i in ids])
        rows = db.execute("select seq, body from events").fetchall()
        db.executemany("delete from events where seq=?",
                       [(seq,) for seq, b in rows if json.loads(b).get("family_id") == fid])
        db.execute("delete from docs where kind='settings' and id=?", (f"poll:{fid}",))
        db.commit()


@app.post("/api/reset")
def reset(fam: dict = Depends(current_account)) -> dict:
    """내 계정 데모 초기화: 통화·주문·정리본·알림 삭제 (정책·가입 정보는 유지)."""
    _wipe(fam["id"])
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
def signup(body: SignupIn, request: Request) -> dict:
    _limit(request, "signup", 20, 3600)
    if any(f["child_phone"] == family.normalize_phone(body.child_phone) for f in family.families()):
        raise HTTPException(409, "이미 가입된 번호예요. 가입할 때 받은 개인 대시보드 링크로 들어와 주세요.")
    try:
        fam = family.register(body.child_name, body.child_phone, [p.model_dump() for p in body.parents],
                              origin="대시보드 가입")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return family.public(fam)


@app.delete("/api/families/{fid}")
def leave(fid: str, fam: dict = Depends(current_account)) -> dict:
    if fid != fam["id"]:
        raise HTTPException(404, "not found")
    _wipe(fid)
    family.delete(fid)
    return {"ok": True}


# ---------- 통화 시뮬레이터 (SKT 망 연동 모의) ----------

class SimCall(BaseModel):
    transcript: str
    who: str = "parent"          # parent | unknown
    direction: str = "incoming"  # incoming(엄마→나) | outgoing(나→엄마)
    unknown_number: str = "010-9876-5432"
    summary: str = ""            # 에이닷 통화 요약 (선택)
    todos: list[str] = []        # 에이닷 할일 (선택)


@app.post("/api/simulate/call-ended", status_code=202)
def simulate_call(body: SimCall, request: Request, background: BackgroundTasks,
                  fam: dict = Depends(current_account)) -> dict:
    """에이닷이 내 계정 번호의 통화 종료 웹훅을 보낸 것처럼 처리한다 (실제 웹훅과 같은 telco.receive)."""
    if not body.transcript.strip():
        raise HTTPException(400, "empty transcript")
    _limit(request, f"sim:{fam['id']}", 60, 3600)
    other = fam["parents"][0]["phone"] if body.who == "parent" else body.unknown_number
    a, b = (other, fam["child_phone"]) if body.direction == "incoming" else (fam["child_phone"], other)
    try:
        ev = telco.CallEnded(call_id=f"sim-{uuid.uuid4().hex[:10]}", from_number=family.pretty_phone(a),
                             to_number=family.pretty_phone(b),
                             ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                             summary=body.summary.strip() or None, todos=body.todos or None,
                             transcript=body.transcript)
    except ValueError as e:
        raise HTTPException(422, str(e))
    result, call_id = telco.receive(ev, source="SKT 망 연동 모의")
    if call_id:
        background.add_task(pipeline.process_call, call_id)
    return result | {"sent": ev.model_dump(exclude_none=True)}


# ---------- vooy 웹훅 설정 · 테스트 · 모의 수신함 ----------

class VooySettingsIn(BaseModel):
    push_url: str | None = None
    push_token: str | None = None
    user_id: str | None = None
    signing_secret: str | None = None


@app.get("/api/vooy")
def vooy_settings(fam: dict = Depends(current_account)) -> dict:
    return notify.public_settings(fam["id"]) | {"mcp_url": family.links(fam)["mcp_url"]}


@app.put("/api/vooy")
def vooy_update(body: VooySettingsIn, fam: dict = Depends(current_account)) -> dict:
    try:
        notify.update_settings(body.model_dump(exclude_none=True), fam["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    notify.retry_pending(fam["id"])  # 웹훅을 막 켰다면 쌓여 있던 정리본을 바로 보낸다
    return notify.public_settings(fam["id"])


@app.post("/api/vooy/test")
def vooy_test(fam: dict = Depends(current_account)) -> dict:
    account.log_family.set(fam["id"])
    return notify.send_test(fam["id"])


MOCK_PATH = "/mock/vooy/webhook"


@app.post(MOCK_PATH)
async def mock_vooy_webhook(request: Request) -> dict:
    """데모용 '모의 vooy': 우리가 보낸 웹훅을 vooy처럼 받아 인증·서명을 확인하고 도착 메시지를 남긴다.

    모의 수신함을 웹훅 주소로 고른 계정만 받는다. 그 계정의 토큰·서명 비밀로 검증한다.
    """
    body = await request.body()
    try:
        data = json.loads(body)
        fid = (data.get("data") or {}).get("family_id")
    except (ValueError, AttributeError):
        raise HTTPException(400, "bad payload")
    s = notify.settings(fid) if fid and family.get(fid) else None
    if s is None or not s["push_url"].split("?")[0].endswith(MOCK_PATH):
        raise HTTPException(404, "not found")
    if s["push_token"] and not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                                   f"Bearer {s['push_token']}".encode()):
        raise HTTPException(401, "bad token")
    verified = None
    if s["signing_secret"]:
        expect = notify.sign(s["signing_secret"], request.headers.get("x-oba-timestamp", ""), body)
        verified = hmac.compare_digest(request.headers.get("x-oba-signature", "").encode(), expect.encode())
        if not verified:
            raise HTTPException(401, "bad signature")
    store.log("vooy_mock", request.headers.get("x-oba-delivery", ""),
              "📱 vooy(모의)에 도착" + (" · 서명 확인" if verified else "") + ": " + data.get("text", ""),
              family_id=fid)
    return {"ok": True, "signature_verified": verified}


# ---------- vooy 크론용 REST (MCP 대신 HTTP로 가져갈 때) ----------

@app.get("/v1/vooy/digests")
def poll_digests(mark: bool = True, auth: dict = Depends(ingest.upload_auth)) -> dict:
    """내 계정의 새 정리본 (가족 업로드 토큰). mark=true(기본)면 가져가며 전달됨 표시, false면 보기만 하고 /ack로 확인."""
    if not auth["family"]:
        raise HTTPException(400, "가족 업로드 토큰으로 호출해 주세요.")
    fid = auth["family"]["id"]
    if mark:
        items = notify.take_pending("rest", fid)
    else:
        items = notify.pending(fid)
        notify.record_poll(len(items), "rest-peek", fid)
    return {"has_new": bool(items), "digests": [notify.public(d) for d in items]}


class AckIn(BaseModel):
    ids: list[str]


@app.post("/v1/vooy/digests/ack")
def ack_digests(body: AckIn, auth: dict = Depends(ingest.upload_auth)) -> dict:
    if not auth["family"]:
        raise HTTPException(400, "가족 업로드 토큰으로 호출해 주세요.")
    mine = {d["id"] for d in store.all_docs("digest") if d.get("family_id") == auth["family"]["id"]}
    return {"acked": notify.mark_delivered([i for i in body.ids if i in mine], "rest")}


class LinkIn(BaseModel):
    url: str
    name: str = ""
    price: int = 0


@app.put("/api/orders/{oid}/link")
def set_order_link(oid: str, body: LinkIn, fam: dict = Depends(current_account)) -> dict:
    """찾아 둔 쇼핑몰(11번가·네이버) 상품 링크를 주문에 지정한다 (vooy가 그 링크에서 구매)."""
    if not body.url.startswith(("https://smartstore.naver.com/", "https://brand.naver.com/",
                                "https://search.shopping.naver.com/", "https://shopping.naver.com/",
                                "https://m.smartstore.naver.com/", "https://www.11st.co.kr/products/",
                                "https://m.11st.co.kr/products/")):
        raise HTTPException(400, "11번가·네이버 쇼핑 상품 링크만 지정할 수 있어요.")
    o = _own("order", oid, fam)
    o["product"] = {**o["product"], "buy_url": body.url}
    if body.name:
        o["product"]["link_name"] = body.name
    if body.price:
        o["product"]["link_price"] = body.price
    o.setdefault("timeline", []).append(
        {"at": pipeline.now(), "text": f"구매 링크 지정({catalog.merchant_of(body.url)}): {body.name or body.url}"})
    return store.put("order", oid, o)
