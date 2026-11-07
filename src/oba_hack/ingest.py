"""통화(또는 문자) 기록 수신 — 아이폰 단축어 경로.

- 에이닷 공유(1탭): 자식이 엄마 통화를 골라 공유 → 단축어가 업로드
- 문자 자동화(0탭): 엄마에게 문자가 오면 단축어 자동화가 업로드 (보낸 사람 포함)

발신자 번호(caller_number)가 오면 등록·동의된 부모님인지 확인하고, 아니면 저장하지 않고 버린다.
받은 파일은 data/calls/<call_id>/ 아래에 원본 그대로 저장하고, 백그라운드에서 파이프라인을 돌린다.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile  # request.form()이 돌려주는 타입

from . import family, pipeline, store
from .config import data_dir, default_caller, max_upload_bytes, upload_token

router = APIRouter()

SOURCE_LABELS = {"aidot-shortcut": "에이닷 공유 (1탭)", "sms-shortcut": "문자 자동화"}


def upload_auth(authorization: str = Header(default="")) -> dict:
    """전역 토큰(OBA_UPLOAD_TOKEN) 또는 가족별 업로드 토큰. {'family': 가족 또는 None}"""
    token = authorization.removeprefix("Bearer ").strip()
    if token:
        g = upload_token()
        if g and secrets.compare_digest(token.encode(), g.encode()):
            return {"family": None}
        fam = family.by_token(token)
        if fam:
            return {"family": fam}
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


def _safe_name(name: str | None, fallback: str) -> str:
    name = Path(name or "").name  # 경로 성분 제거
    name = re.sub(r"[^\w.\-가-힣 ]", "_", name).strip(" .")
    return name[:120] or fallback


def _write_unique(call_dir: Path, name: str, data: bytes) -> Path:
    path = call_dir / name
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = call_dir / f"{stem}-{n}{suffix}"
        n += 1
    path.write_bytes(data)
    return path


def save_files(items: list[tuple[str, str, bytes]]) -> tuple[str, list[dict]]:
    call_id = pipeline.new_call_id()
    call_dir = data_dir() / "calls" / call_id
    call_dir.mkdir(parents=True, exist_ok=False)
    files = []
    for name, ctype, data in items:
        path = _write_unique(call_dir, name, data)
        files.append({"name": path.name, "content_type": ctype, "bytes": len(data)})
    return call_id, files


@router.post("/v1/calls", status_code=status.HTTP_201_CREATED)
async def upload_call(request: Request, background: BackgroundTasks, auth: dict = Depends(upload_auth)):
    """통화 기록 업로드.

    - multipart/form-data: 파일 필드(여러 개 가능) + 선택 텍스트 `text`, `caller`(호칭), `caller_number`(번호 또는 연락처 이름)
    - 그 외: 요청 본문 전체를 파일 하나로 저장 (파일명 X-Filename, 발신자 X-Caller / X-Caller-Number, URL 인코딩 가능)
    """
    limit = max_upload_bytes()
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

    items: list[tuple[str, str, bytes]] = []
    caller = unquote(request.headers.get("x-caller") or "")
    caller_number = unquote(request.headers.get("x-caller-number") or "")
    content_type = request.headers.get("content-type", "")

    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form(max_part_size=limit)
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                items.append((_safe_name(value.filename, f"{key}.bin"), value.content_type or "", data))
            elif key == "caller":
                caller = str(value).strip()
            elif key == "caller_number":
                caller_number = str(value).strip()
            elif str(value).strip():
                items.append((_safe_name(f"{key}.txt", "text.txt"), "text/plain", str(value).encode()))
    else:
        data = await request.body()
        default = "transcript.txt" if content_type.startswith("text/") else "upload.bin"
        items.append((_safe_name(request.headers.get("x-filename"), default), content_type, data))

    items = [it for it in items if it[2]]
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    if sum(len(d) for _, _, d in items) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

    src = request.headers.get("x-source", "aidot-shortcut")
    fam = auth["family"]
    if caller_number:
        match = family.match_sender(caller_number, fam)
        if match is None:
            # 번호도 내용도 남기지 않는다
            store.log("filter", "upload", "🔒 등록·동의되지 않은 발신자의 기록 1건 → 저장하지 않고 폐기")
            return JSONResponse({"accepted": False, "reason": "not a registered parent"},
                                status_code=status.HTTP_202_ACCEPTED)
        fam, parent = match
        caller = parent["label"]

    caller = caller or default_caller()
    call_id, files = save_files(items)
    meta = {"call_id": call_id, "caller": caller, "files": files, "status": "received", "source": src,
            "accepted": True}
    (data_dir() / "calls" / call_id / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    pipeline.register_call(call_id, files, caller=caller, source=SOURCE_LABELS.get(src, src),
                           family_id=fam["id"] if fam else None)
    background.add_task(pipeline.process_call, call_id)
    return meta
