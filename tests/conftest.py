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
    for k in ("OBA_AX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OBA_STT_API_KEY"):
        monkeypatch.delenv(k, raising=False)  # 규칙 기반 경로로 결정적으로 테스트
    from oba_hack.app import app

    return TestClient(app), tmp_path
