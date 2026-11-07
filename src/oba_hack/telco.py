"""통신사(SKT 에이닷) 연동 웹훅 — 0탭 경로.

SKT 망에서 통화가 끝나면 에이닷이 이 엔드포인트를 호출한다고 가정한다.
등록된 부모님 번호와의 통화만 받아서 처리하고, 나머지는 아무것도 저장하지 않는다.
(해커톤에서는 대시보드의 통화 시뮬레이터가 같은 함수를 호출한다.)
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from pydantic import BaseModel

from . import family, ingest, pipeline, store
from .config import telco_token

router = APIRouter()


class CallEnded(BaseModel):
    call_id: str | None = None      # 통신사 쪽 통화 ID (중복 전달 방지)
    from_number: str                # 건 사람
    to_number: str                  # 받은 사람
    ended_at: str | None = None
    duration_sec: int | None = None
    summary: str | None = None      # 에이닷 요약 (있으면 함께 보관)
    transcript: str                 # 화자 표기된 통화 텍스트


def require_telco(authorization: str = Header(default="")) -> None:
    token = telco_token()
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OBA_TELCO_TOKEN is not set")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


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
    call_id, files = ingest.save_files(items)
    pipeline.register_call(call_id, files, caller=parent["label"], source=f"{source} · {direction}",
                           transcript=ev.transcript, family_id=fam["id"])
    if ev.call_id:
        store.put("telco_call", ev.call_id, {"call_id": call_id})
    return {"accepted": True, "call_id": call_id}, call_id


@router.post("/v1/telco/call-ended", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_telco)])
def call_ended(ev: CallEnded, background: BackgroundTasks) -> dict:
    result, call_id = receive(ev)
    if call_id:
        background.add_task(pipeline.process_call, call_id)
    return result
