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
