"""모듈 1: 통화 기록 수신.

아이폰 에이닷에서 통화 녹음/텍스트를 공유하면, iOS 단축어가 이 엔드포인트로 업로드한다.
받은 파일은 data/calls/<call_id>/ 아래에 원본 그대로 저장하고, 백그라운드에서 파이프라인을 돌린다.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from starlette.datastructures import UploadFile  # request.form()이 돌려주는 타입

from . import pipeline
from .config import data_dir, default_caller, max_upload_bytes, upload_token

router = APIRouter()


def require_token(authorization: str = Header(default="")) -> None:
    token = upload_token()
    if not token:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "OBA_UPLOAD_TOKEN is not set")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
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


@router.post("/v1/calls", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_token)])
async def upload_call(request: Request, background: BackgroundTasks) -> dict:
    """통화 기록 업로드.

    - multipart/form-data: 파일 필드(이름 무관, 여러 개 가능) + 선택 텍스트 필드 `text`, 선택 `caller`
    - 그 외: 요청 본문 전체를 파일 하나로 저장 (파일명은 X-Filename 헤더)
    발신자는 `caller` 필드 또는 X-Caller 헤더(URL 인코딩 가능), 없으면 OBA_DEFAULT_CALLER.
    """
    limit = max_upload_bytes()
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

    items: list[tuple[str, str, bytes]] = []
    caller = request.headers.get("x-caller") or ""
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form(max_part_size=limit)
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                items.append((_safe_name(value.filename, f"{key}.bin"), value.content_type or "", data))
            elif key == "caller":
                caller = str(value).strip()
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

    from urllib.parse import unquote

    caller = unquote(caller) or default_caller()
    call_id, files = save_files(items)
    meta = {"call_id": call_id, "caller": caller, "files": files, "status": "received",
            "source": request.headers.get("x-source", "aidot-shortcut")}
    (data_dir() / "calls" / call_id / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    pipeline.register_call(call_id, files, caller=caller, source=meta["source"])
    background.add_task(pipeline.process_call, call_id)
    return meta
