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


def telco_token() -> str:
    """통신사(SKT) 연동 웹훅 인증 토큰."""
    return os.environ.get("OBA_TELCO_TOKEN", "")


def demo_mode() -> bool:
    """데모 서버 기능(샌드박스 계정, vooy용 데모 통화 도구, 통화 결과 조회)을 켤지. 기본 켬."""
    return os.environ.get("OBA_DEMO_MODE", "1").strip().lower() not in ("0", "false", "no", "off", "")


def telco_signing_secret() -> str:
    """설정하면 통신사 웹훅에 HMAC 서명(X-Telco-Signature)도 요구한다."""
    return os.environ.get("OBA_TELCO_SIGNING_SECRET", "")


def audience() -> str:
    """이 배포가 누구를 위한 것인지: "skt" | "vooy" | ""(로컬 개발 — 대시보드와 두 시작 페이지를 모두 띄움).

    팀마다 배포를 따로 하고, 첫 화면(/)이 그 팀의 시작 페이지가 된다. 다른 팀 페이지는 열리지 않는다.
    """
    v = os.environ.get("OBA_AUDIENCE", "").strip().lower()
    return v if v in ("skt", "vooy") else ""
