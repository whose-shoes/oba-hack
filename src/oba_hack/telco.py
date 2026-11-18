"""통신사(SKT 에이닷) 연동 웹훅 — 0탭 경로.

SKT 망에서 통화가 끝나면 에이닷이 이 엔드포인트를 호출한다고 가정한다.
등록된 부모님 번호와의 통화만 받아서 처리하고, 나머지는 아무것도 저장하지 않는다.
에이닷이 통화 요약에서 찾은 '할일'(todos)을 함께 보내면 부탁 추출의 힌트로 쓴다.
(데모에서는 대시보드·/skt 페이지의 통화 시뮬레이터가 같은 함수를 호출한다.)

인증: Authorization: Bearer <OBA_TELCO_TOKEN>. OBA_TELCO_SIGNING_SECRET을 설정하면
X-Telco-Timestamp + X-Telco-Signature: sha256=HMAC(secret, "<timestamp>.<본문>")도 요구한다 (5분 안).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from . import family, ingest, notify, pipeline, store
from .config import demo_mode, telco_signing_secret, telco_token

router = APIRouter()
MAX_SKEW_SEC = 300


class CallEnded(BaseModel):
    call_id: str | None = Field(default=None, max_length=128)  # 통신사 쪽 통화 ID (중복 전달 방지)
    from_number: str = Field(max_length=32)                     # 건 사람
    to_number: str = Field(max_length=32)                       # 받은 사람
    ended_at: str | None = None
    duration_sec: int | None = None
    summary: str | None = Field(default=None, max_length=4000)  # 에이닷 통화 요약 (있으면 함께 보관)
    todos: list[str] | None = Field(default=None, max_length=20)  # 에이닷 '할일' (통화 요약에서 찾은 후속 조치)
    transcript: str = Field(max_length=50000)                   # 화자 표기된 통화 텍스트


def require_telco(authorization: str = Header(default="")) -> None:
    token = telco_token()
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OBA_TELCO_TOKEN is not set")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(headers, body: bytes) -> None:
    """서명 비밀이 설정돼 있을 때만 검사한다."""
    secret = telco_signing_secret()
    if not secret:
        return
    ts = headers.get("x-telco-timestamp", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > MAX_SKEW_SEC:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "stale or missing X-Telco-Timestamp")
    if not hmac.compare_digest(headers.get("x-telco-signature", "").encode(), sign(secret, ts, body).encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid X-Telco-Signature")


def receive(ev: CallEnded, *, source: str = "SKT 망 연동") -> tuple[dict, str | None]:
    """(응답, 처리할 call_id 또는 None)"""
    if ev.call_id:
        seen = store.get("telco_call", ev.call_id)
        if seen:
            return {"accepted": True, "duplicate": True, "call_id": seen["call_id"]}, None

    match = family.match_call(ev.from_number, ev.to_number)
    if match is None:
        # 번호도 내용도 남기지 않는다. 자녀 번호가 우리 계정이면 그 계정 알림에만 '폐기 1건'을 남긴다.
        nums = {family.normalize_phone(ev.from_number), family.normalize_phone(ev.to_number)}
        owner = next((f for f in family.families() if f["child_phone"] in nums), None)
        store.log("filter", "telco", "🔒 등록되지 않은 번호와의 통화 1건 → 저장하지 않고 폐기",
                  family_id=owner["id"] if owner else None)
        return {"accepted": False, "reason": "not a registered family call"}, None

    fam, parent = match
    direction = "수신" if family.normalize_phone(ev.from_number) == parent["phone"] else "발신"
    items = [("transcript.txt", "text/plain", ev.transcript.encode())]
    if ev.summary:
        items.append(("aidot-summary.txt", "text/plain", ev.summary.encode()))
    todos = [t.strip()[:200] for t in ev.todos or [] if t and t.strip()]
    if todos:
        items.append(("aidot-todos.txt", "text/plain", "\n".join(todos).encode()))
    call_id, files = ingest.save_files(items)
    pipeline.register_call(call_id, files, caller=parent["label"], source=f"{source} · {direction}",
                           transcript=ev.transcript, family_id=fam["id"],
                           aidot={"summary": (ev.summary or "").strip(), "todos": todos,
                                  "telco_call_id": ev.call_id, "ended_at": ev.ended_at})
    if ev.call_id:
        store.put("telco_call", ev.call_id, {"call_id": call_id})
    return {"accepted": True, "call_id": call_id}, call_id


@router.post("/v1/telco/call-ended", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_telco)])
async def call_ended(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    verify_signature(request.headers, body)
    try:
        ev = CallEnded.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, e.errors(include_url=False, include_context=False))
    result, call_id = await run_in_threadpool(receive, ev)
    if call_id:
        background.add_task(pipeline.process_call, call_id)
    return result


def outcome(call_id: str) -> dict:
    """통화 한 건의 처리 결과 (통신사 연동 확인용·데모 화면용): 추출 → 선택 → 위임 판단 → 결제 → 알림."""
    call = store.get("call", call_id) or {}
    orders = [o for o in (store.get("order", oid) for oid in call.get("order_ids", [])) if o]
    digest = store.get("digest", call_id) or {}
    ex = call.get("extraction") or {}
    refs = {call_id, *(o["id"] for o in orders)}
    sms = [e["text"] for e in reversed(store.events(300, family_id=call.get("family_id")))
           if e["kind"] == "sms_to_mom" and e.get("ref") in refs]
    return {
        "call_id": call_id,
        "status": call.get("status"),
        "caller": call.get("caller"),
        "source": call.get("source"),
        "engine": call.get("engine"),
        "summary": ex.get("summary", ""),
        "aidot": call.get("aidot"),
        "requests": [{k: r.get(k) for k in ("item", "detail", "quantity", "evidence")} for r in ex.get("requests", [])],
        "orders": [notify._item(o) | {
            "decision": o.get("decision"),
            "reasons": (o.get("product") or {}).get("reasons", []),
            "receipt": ({k: o["receipt"].get(k) for k in ("method", "tx_id", "order_number")} if o.get("receipt") else None),
            "attempts": o.get("attempts", []),
        } for o in orders],
        "other_tasks": ex.get("other_tasks", []),
        "scam_signals": ex.get("scam_signals", []),
        "sms_to_mom": sms,
        "digest_text": digest.get("text", ""),
        "digest_delivered": digest.get("delivered_via") if digest.get("delivered") else None,
    }


@router.get("/v1/telco/calls/{telco_call_id}", dependencies=[Depends(require_telco)])
def call_result(telco_call_id: str) -> dict:
    """웹훅으로 보낸 통화가 어떻게 처리됐는지 (데모 모드에서만). 통신사가 보낸 call_id로 조회한다."""
    if not demo_mode():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    seen = store.get("telco_call", telco_call_id)
    if seen is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call_id (등록되지 않은 번호의 통화는 저장하지 않아요)")
    return outcome(seen["call_id"])
