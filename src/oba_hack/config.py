"""환경변수 설정. 테스트에서 바꿀 수 있도록 매번 읽는다."""

import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("OBA_DATA_DIR", "data")).resolve()


def upload_token() -> str:
    return os.environ.get("OBA_UPLOAD_TOKEN", "")


def max_upload_bytes() -> int:
    return int(os.environ.get("OBA_MAX_UPLOAD_MB", "100")) * 1024 * 1024


def default_caller() -> str:
    """에이닷 공유본에는 발신자 정보가 없을 수 있다. 그때 쓸 기본 발신자."""
    return os.environ.get("OBA_DEFAULT_CALLER", "엄마")


def josa(word: str, with_batchim: str, without: str) -> str:
    """받침 여부에 맞는 조사를 붙인다. josa('쌀', '은', '는') → '쌀은'"""
    last = word[-1:] if word else ""
    if "가" <= last <= "힣":
        return word + (with_batchim if (ord(last) - 0xAC00) % 28 else without)
    return f"{word}{with_batchim}({without})"
