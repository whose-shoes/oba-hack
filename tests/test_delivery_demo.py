"""SKT·vooy 팀이 직접 체험하는 데모 서버 기능: 체험 계정, 에이닷 연동(할일·서명·결과 조회), 학습하는 구매 이력,
체험용 MCP 도구, 계정별 웹훅, 웹훅 주소 검사, 분석 엔진 폴백."""

import asyncio
import json
import time

import pytest
from mcp import Client

from conftest import TELCO
from oba_hack import family, llm, notify, telco

MOM, CHILD = "010-2345-6789", "010-1234-5678"


def mcp(fid, *calls):
    from oba_hack import account
    from oba_hack.mcp_server import server

    async def run():
        async with Client(server) as c:
            out = []
            for name, args in calls:
                r = await c.call_tool(name, args)
                sc = getattr(r, "structured_content", None)
                out.append(sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc)
            return out

    tok = account.override.set(fid)
    try:
        return asyncio.run(run())
    finally:
        account.override.reset(tok)


def test_sandbox_accounts_have_their_own_demo_numbers(client):
    c, _ = client
    a = c.post("/api/demo").json()
    b = c.post("/api/demo", json={"audience": "vooy"}).json()
    assert a["demo"] and b["demo"] and a["id"] != b["id"]
    assert a["demo_numbers"]["child"].startswith("010-0") and a["demo_numbers"] != b["demo_numbers"]
    assert a["parents"][0]["notes"] and a["parents"][0]["address"]
    # SKT 체험은 서버가 테스트 결제, vooy 체험은 vooy가 1회용 토큰으로 테스트몰 결제
    assert c.get("/api/state", headers={"X-Account": a["dashboard_token"]}).json()["policy"]["payment_executor"] == "server"
    pol = c.get("/api/state", headers={"X-Account": b["dashboard_token"]}).json()["policy"]
    assert pol["payment_executor"] == "vooy" and pol["vooy_checkout"] == "test_mall"


def test_sandbox_creation_is_rate_limited(client):
    c, _ = client
    codes = [c.post("/api/demo").status_code for _ in range(21)]
    assert codes[:20] == [201] * 20 and codes[20] == 429


def test_aidot_todos_reach_extraction_and_result_endpoint(client, monkeypatch):
    c, _ = client
    seen = {}

    def fake(system, user, timeout=40.0, **kw):
        seen.setdefault("prompts", []).append(user)
        return {"summary": "쌀 부탁", "requests": [{"item": "쌀", "quantity": 1, "evidence": "쌀 다 떨어졌어"}]}

    monkeypatch.setattr(llm, "chat_json", fake)
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    r = c.post("/v1/telco/call-ended", headers=TELCO, json={
        "call_id": "skt-1", "from_number": MOM, "to_number": CHILD, "summary": "쌀 부탁",
        "todos": ["장보기: 쌀 10kg"], "transcript": "엄마: 쌀 다 떨어졌어. 늘 먹던 거 하나 시켜줘."})
    assert r.status_code == 202 and r.json()["accepted"]
    assert "장보기: 쌀 10kg" in seen["prompts"][0]                       # 에이닷 할일이 추출 힌트로 간다
    out = c.get("/v1/telco/calls/skt-1", headers=TELCO).json()
    assert out["status"] == "processed" and out["aidot"]["todos"] == ["장보기: 쌀 10kg"]
    assert out["orders"][0]["product"] == "이천 햅쌀 10kg" and out["orders"][0]["decision"] == "ask"
    assert "결제할까요" in out["digest_text"]
    assert c.get("/v1/telco/calls/skt-1").status_code == 401
    assert c.get("/v1/telco/calls/nope", headers=TELCO).status_code == 404
    # 계정 화면에서 보는 같은 결과
    call_id = c.get("/api/state", headers={"X-Account": fam["dashboard_token"]}).json()["calls"][0]["id"]
    mine = c.get(f"/api/calls/{call_id}/outcome", headers={"X-Account": fam["dashboard_token"]}).json()
    assert mine["call_id"] == call_id and mine["orders"][0]["reasons"]
    other = family.register("레오", "010-5555-6666", [{"label": "엄마", "phone": "010-7777-8888"}])
    assert c.get(f"/api/calls/{call_id}/outcome", headers={"X-Account": other["dashboard_token"]}).status_code == 404


def test_telco_signature_when_secret_is_set(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("OBA_TELCO_SIGNING_SECRET", "sig-secret")
    family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    body = json.dumps({"call_id": "skt-sig", "from_number": MOM, "to_number": CHILD,
                       "transcript": "엄마: 휴지 다 떨어졌어."}, ensure_ascii=False).encode()
    assert c.post("/v1/telco/call-ended", headers=TELCO, content=body).status_code == 401
    ts = str(int(time.time()))
    bad = {**TELCO, "X-Telco-Timestamp": ts, "X-Telco-Signature": "sha256=00"}
    assert c.post("/v1/telco/call-ended", headers=bad, content=body).status_code == 401
    old = str(int(time.time()) - 3600)
    stale = {**TELCO, "X-Telco-Timestamp": old, "X-Telco-Signature": telco.sign("sig-secret", old, body)}
    assert c.post("/v1/telco/call-ended", headers=stale, content=body).status_code == 401
    good = {**TELCO, "X-Telco-Timestamp": ts, "X-Telco-Signature": telco.sign("sig-secret", ts, body),
            "Content-Type": "application/json"}
    r = c.post("/v1/telco/call-ended", headers=good, content=body)
    assert r.status_code == 202 and r.json()["accepted"]
    assert c.post("/v1/telco/call-ended", headers=good, content=b'{"from_number": 1}').status_code == 401  # 서명 불일치
    odd = {**TELCO, "X-Telco-Timestamp": ts, "X-Telco-Signature": "sha256=é".encode()}  # 원시 바이트 헤더
    assert c.post("/v1/telco/call-ended", headers=odd, content=body).status_code == 401      # 이상한 헤더도 500이 아니라 401


def test_bought_once_becomes_a_usual_item(client):
    """처음 사는 상품은 묻고, 한 번 승인해 산 뒤에는 '늘 사던 상품'이라 한도 안이면 바로 결제한다."""
    c, _ = client
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    c.headers["X-Account"] = fam["dashboard_token"]
    talk = "엄마: 샴푸 다 썼어. 하나 사줘."
    c.post("/api/simulate/call-ended", json={"transcript": talk})
    first = c.get("/api/state").json()["orders"][0]
    assert first["status"] == "awaiting_approval" and "처음 사는" in first["decision_reason"]
    c.post(f"/api/orders/{first['id']}/approve")
    c.post("/api/simulate/call-ended", json={"transcript": talk})
    second = c.get("/api/state").json()["orders"][0]
    assert second["id"] != first["id"] and second["status"] == "paid" and "늘 사던 상품" in second["decision_reason"]
    hist = {h["name"]: h for h in c.get("/api/state").json()["history"]}
    assert hist["려 자양윤모 샴푸 820ml"]["times"] == 2


def test_demo_call_tool_from_vooy(client):
    c, _ = client
    (made,) = mcp(None, ("setup_parent", {"parent_phone": MOM, "child_phone": CHILD}))
    fid = family.by_mcp_token(made["mcp_url"].rsplit("/", 1)[-1])["id"]
    called, got = mcp(fid, ("demo_parent_call", {"scenario": "장보기"}), ("get_new_parent_requests", {}))
    assert called["ok"] and called["next_action"] == "get_new_parent_requests"
    assert got["has_new"] and "전기요" in got["message"]
    scam, got2 = mcp(fid, ("demo_parent_call", {"scenario": "엄마 사칭 전화"}), ("get_new_parent_requests", {}))
    blocked = [i for d in got2["new"] for i in d["items"] if i["status"] == "blocked"]
    assert blocked and "상품권" in blocked[0]["product"]
    (refused,) = mcp(fid, ("approve_order", {"order_id": blocked[0]["order_id"]}))
    assert refused["ok"] is False and refused["message"].startswith("🚫 결제를 막은 주문이에요")
    (custom,) = mcp(fid, ("demo_parent_call", {"transcript": "엄마: 우유 다 먹었다. 우유 좀 사 줘."}))
    assert custom["scenario"] == "직접 입력한 통화"
    (shared,) = mcp(None, ("demo_parent_call", {}))
    assert shared["ok"] is False and shared["need_personal_url"]


def test_shared_url_suggests_demo_numbers(client):
    (need,) = mcp(None, ("setup_parent", {"parent_phone": MOM}))
    assert need["ok"] is False and need["demo_numbers"]["child_phone"].startswith("010-0")
    assert "아무 번호나 괜찮아요" in need["message"]


def test_webhook_settings_are_per_account(client, monkeypatch):
    c, _ = client
    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: (sent.append(url), type("R", (), {"status_code": 200})())[1])
    a = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    b = family.register("레오", "010-5555-6666", [{"label": "엄마", "phone": "010-7777-8888"}])
    assert c.put("/api/vooy", headers={"X-Account": a["dashboard_token"]},
                 json={"push_url": "http://127.0.0.1:9/hook-a"}).status_code == 200
    assert c.get("/api/vooy", headers={"X-Account": b["dashboard_token"]}).json()["webhook_on"] is False
    c.post("/api/simulate/call-ended", headers={"X-Account": b["dashboard_token"]}, json={"transcript": "엄마: 휴지 다 떨어졌어."})
    assert sent == [] and notify.stats(b["id"])["pending"] == 1      # 다른 계정 웹훅으로 새지 않는다
    c.post("/api/simulate/call-ended", headers={"X-Account": a["dashboard_token"]}, json={"transcript": "엄마: 휴지 다 떨어졌어."})
    assert sent == ["http://127.0.0.1:9/hook-a"] and notify.stats(a["id"])["webhook"] == 1


def test_mock_inbox_only_accepts_opted_in_accounts(client):
    c, _ = client
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    forged = {"event": "digest.created", "text": "가짜", "data": {"family_id": fam["id"]}}
    assert c.post("/mock/vooy/webhook", json=forged).status_code == 404


@pytest.mark.parametrize("url,ok", [
    ("http://localhost:8000/hook", False),          # 공개 서버에서는 내부 주소 금지
    ("https://10.0.0.5/hook", False),               # 사설망
    ("https://169.254.169.254/latest", False),      # 클라우드 메타데이터
    ("https://[fdaa::3]/hook", False),              # Fly 사설망(IPv6 ULA)
    ("https://demo.example/mock/vooy/webhook", True),  # 이 서버 자신(모의 vooy)
    ("http://demo.example/hook", False),            # https만
])
def test_webhook_url_guard_on_public_server(client, monkeypatch, url, ok):
    monkeypatch.setenv("OBA_PUBLIC_URL", "https://demo.example")
    if ok:
        notify._check_url(url)
    else:
        with pytest.raises(ValueError):
            notify._check_url(url)


def test_llm_falls_back_to_the_next_engine(monkeypatch):
    from oba_hack import extract

    monkeypatch.setenv("OBA_AX_API_KEY", "ax")
    monkeypatch.setenv("OPENAI_API_KEY", "oa")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tried = []

    def fake_request(p, system, user, timeout):
        tried.append(p)
        if p == "ax":
            raise RuntimeError("A.X down")
        return json.dumps({"summary": "휴지 부탁", "requests": [{"item": "휴지", "quantity": 1, "evidence": "휴지 다 떨어졌어"}]}), \
            {"model": "gpt-4o-mini", "prompt_tokens": 1000, "cached_tokens": 0, "completion_tokens": 100}

    monkeypatch.setattr(llm, "_request", fake_request)
    assert llm.provider_label() == "SKT A.X → OpenAI"
    result, engine = extract.extract("엄마: 휴지 다 떨어졌어.")
    assert tried == ["ax", "openai"] and engine == "OpenAI" and "_engine" not in result


def test_start_pages_and_public_info(client):
    c, _ = client
    skt, vooy = c.get("/skt"), c.get("/vooy")
    assert skt.status_code == 200 and "에이닷" in skt.text and "/v1/telco/call-ended" in skt.text
    assert vooy.status_code == 200 and "MCP" in vooy.text
    info = c.get("/api/public").json()
    names = {t["name"] for t in info["tools"]}
    assert info["demo"] is True and "demo_parent_call" in names and "approve_order" in names
    assert next(t for t in info["tools"] if t["name"] == "approve_order")["destructive"] is True
    shown = json.dumps(info)
    assert "telco-token" not in shown and "test-token" not in shown        # 토큰은 공개 정보에 없다
    assert "get_new_parent_requests" in c.get("/static/vooy-recipe.txt").text
