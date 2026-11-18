import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TELCO = {"Authorization": "Bearer telco-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OBA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OBA_UPLOAD_TOKEN", TOKEN)
    monkeypatch.setenv("OBA_TELCO_TOKEN", "telco-token")
    monkeypatch.delenv("OBA_TELCO_SIGNING_SECRET", raising=False)
    for k in ("OBA_AX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OBA_STT_API_KEY", "OBA_PUBLIC_URL",
              "OBA_VOOY_PUSH_URL", "OBA_VOOY_PUSH_TOKEN", "OBA_VOOY_USER_ID", "OBA_VOOY_SIGNING_SECRET"):
        monkeypatch.delenv(k, raising=False)  # 규칙 기반 경로로 결정적으로 테스트
    from oba_hack import ratelimit
    from oba_hack.app import app

    ratelimit.reset()  # 요청 제한 기록은 프로세스 메모리라 테스트마다 비운다
    return TestClient(app), tmp_path
