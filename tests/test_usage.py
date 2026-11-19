"""LLM 사용량 기록: 호출마다 토큰·비용·용도·계정을 남기고, 하루 한도를 넘으면 LLM을 쉬며, 운영자만 본다."""

import json

import httpx
import pytest

from oba_hack import family, llm, usage

MOM, CHILD = "010-2345-6789", "010-1234-5678"
EXTRACT = {"summary": "휴지 부탁", "requests": [{"item": "휴지", "quantity": 1, "evidence": "휴지 다 떨어졌어"}]}


class FakeResponse:
    def __init__(self, content: dict, usage_: dict, model="gpt-4o-mini-2024-07-18", status=200):
        self.status_code = status
        self.headers = {"x-request-id": "req_123"}
        self._body = {"model": model, "usage": usage_, "choices": [{"message": {"content": json.dumps(content)}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)

    def json(self):
        return self._body


def fake_openai(monkeypatch, responses):
    """llm 모듈이 부르는 httpx.post를 순서대로 가짜 응답으로 바꾼다."""
    calls = []

    def post(url, **kw):
        calls.append(kw["json"]["model"])
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(llm.httpx, "post", post)
    return calls


def test_cost_uses_cached_rate_and_prefix_match():
    # gpt-4o-mini: 입력 $0.15, 캐시 $0.075, 출력 $0.60 (100만 토큰당)
    assert usage.cost("gpt-4o-mini-2024-07-18", 1_000_000, 0, 0) == pytest.approx(0.15)
    assert usage.cost("gpt-4o-mini", 1_000_000, 400_000, 100_000) == pytest.approx(0.6 * 0.15 + 0.4 * 0.075 + 0.1 * 0.60)
    assert usage.cost("gpt-4o", 1_000_000, 0, 0) == pytest.approx(2.50)   # gpt-4o-mini 단가로 잘못 잡지 않는다
    assert usage.cost("unknown-model", 1000, 0, 1000) is None


def test_prices_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OBA_LLM_PRICES", json.dumps({"skt/a.ll/A.X-K2-ADOT": [1.0, 0.5, 2.0]}))
    assert usage.cost("skt/a.ll/A.X-K2-ADOT", 1_000_000, 0, 1_000_000) == pytest.approx(3.0)


def test_each_llm_call_is_recorded_with_tokens_cost_and_context(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}], origin="SKT 체험")
    c.headers["X-Account"] = fam["dashboard_token"]
    fake_openai(monkeypatch, [
        FakeResponse(EXTRACT, {"prompt_tokens": 1200, "completion_tokens": 150, "prompt_tokens_details": {"cached_tokens": 200}}),
        FakeResponse({"product_id": "p-tissue-30", "reason": "늘 쓰시던 거예요", "search_keyword": "휴지",
                      "buy_criteria": []}, {"prompt_tokens": 600, "completion_tokens": 60}),
    ])
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 휴지 다 떨어졌어."})
    rows = usage.summary(1)["recent"]
    assert [r["purpose"] for r in rows] == ["choose", "extract"]          # 최근 것부터
    ex = rows[1]
    assert ex["provider"] == "openai" and ex["model"] == "gpt-4o-mini-2024-07-18" and ex["ok"] == 1
    assert (ex["prompt_tokens"], ex["cached_tokens"], ex["completion_tokens"]) == (1200, 200, 150)
    assert ex["cost_usd"] == pytest.approx((1000 * 0.15 + 200 * 0.075 + 150 * 0.60) / 1e6)
    assert ex["family_id"] == fam["id"] and ex["ref"] and ex["request_id"] == "req_123"
    assert "휴지" not in json.dumps(rows)                                   # 프롬프트·응답 내용은 남기지 않는다
    s = usage.summary(1)
    assert s["today"]["calls"] == 2 and s["by_team"][0]["key"] == "SKT 체험"
    assert {p["key"] for p in s["by_purpose"]} == {"extract", "choose"}


def test_failures_are_recorded_and_fall_back(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    c.headers["X-Account"] = fam["dashboard_token"]
    fake_openai(monkeypatch, [FakeResponse({}, {}, status=429), httpx.ConnectError("down")])
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 휴지 다 떨어졌어."})
    assert c.get("/api/state").json()["calls"][0]["engine"] == "규칙 기반"
    rows = usage.summary(1)["recent"]
    assert all(r["ok"] == 0 for r in rows) and "429" in rows[-1]["error"] and rows[-1]["cost_usd"] == 0


def test_daily_limit_pauses_the_llm(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OBA_LLM_DAILY_USD_LIMIT", "0.0001")
    usage.record("openai", "gpt-4o-mini", "extract", ok=True, latency_ms=10, prompt_tokens=1_000_000)  # $0.15
    calls = fake_openai(monkeypatch, [])
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    c.headers["X-Account"] = fam["dashboard_token"]
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 휴지 다 떨어졌어."})
    assert calls == []                                                     # 한도를 넘어 OpenAI를 부르지 않았다
    assert c.get("/api/state").json()["calls"][0]["engine"] == "규칙 기반"


def test_admin_usage_needs_the_admin_token(client, monkeypatch):
    c, _ = client
    assert c.get("/admin/usage").status_code == 404                         # 토큰을 안 정하면 아예 없음
    monkeypatch.setenv("OBA_ADMIN_TOKEN", "admin-secret")
    assert c.get("/admin/usage").status_code == 401
    assert c.get("/admin/usage", headers={"Authorization": "Bearer nope"}).status_code == 401
    usage.record("openai", "gpt-4o-mini", "extract", ok=True, latency_ms=10, prompt_tokens=100, completion_tokens=10)
    r = c.get("/admin/usage", headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200 and r.json()["today"]["calls"] == 1
    assert c.get("/admin").status_code == 200 and "OBA_ADMIN_TOKEN" in c.get("/admin").text


def test_parallel_product_choice_keeps_account_context(client, monkeypatch):
    """품목별 LLM 상품 선택을 동시에 돌려도 사용량은 그 계정·그 통화로 기록되고, 주문마다 선택이 붙는다."""
    c, _ = client
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    meta = {"model": "gpt-4o-mini", "prompt_tokens": 100, "completion_tokens": 10}

    def fake_request(p, system, user, timeout):
        if "쇼핑 비서" in system:
            first = json.loads(user)["candidates"][0]["product_id"]
            return json.dumps({"product_id": first, "reason": "골랐어요", "search_keyword": "", "buy_criteria": []}), meta
        items = [{"item": i, "quantity": 1, "evidence": f"{i} 사줘"} for i in ("휴지", "계란", "쌀")]
        return json.dumps({"summary": "", "requests": items}), meta

    monkeypatch.setattr(llm, "_request", fake_request)
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}], origin="vooy 체험")
    c.headers["X-Account"] = fam["dashboard_token"]
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 휴지랑 계란이랑 쌀 사줘."})
    chooses = [r for r in usage.summary(1)["recent"] if r["purpose"] == "choose"]
    assert len(chooses) == 3 and all(r["family_id"] == fam["id"] and r["ref"] for r in chooses)
    orders = c.get("/api/state").json()["orders"]
    assert len(orders) == 3 and all(o.get("ai_pick") for o in orders)
    assert usage.summary(1)["by_team"][0]["key"] == "vooy 체험"
