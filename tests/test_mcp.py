"""vooy가 부를 MCP 도구를 실제 MCP 클라이언트(in-process)로 검증한다."""

import asyncio
import json

from mcp import Client

from oba_hack import family, notify

MOM, CHILD = "010-2345-6789", "010-1234-5678"
DAILY = (
    "엄마: 휴지가 다 떨어졌어. 저번에 시켜준 30롤짜리 그거 좋더라.\n"
    "엄마: 그리고 계란도 없네. 계란 한 판만 사줘.\n"
    "엄마: 요즘 밤에 너무 추워서 전기요 하나 있으면 좋겠어.\n"
    "엄마: 다음 주 화요일에 병원 예약도 좀 알아봐 줘."
)


def payload(res):
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def mcp(*calls):
    """[(도구, 인자), ...]를 한 세션에서 차례로 부르고 결과 목록을 돌려준다."""
    from oba_hack.mcp_server import server

    async def run():
        out = []
        async with Client(server) as c:
            for name, args in calls:
                out.append(payload(await c.call_tool(name, args)))
        return out

    return asyncio.run(run())


def test_tools_are_listed_with_annotations(client):
    from oba_hack.mcp_server import server

    async def run():
        async with Client(server) as c:
            return (await c.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {"get_family_status", "setup_parent", "list_parent_requests", "get_new_parent_requests",
                          "approve_order", "reject_order", "cancel_order", "update_delegation"}
    assert tools["approve_order"].annotations.destructive_hint is True
    assert tools["list_parent_requests"].annotations.read_only_hint is True
    assert "결제해" in tools["approve_order"].description


def test_voice_onboarding_then_digest_then_pay(client):
    c, _ = client
    status, need, created = mcp(
        ("get_family_status", {}),
        ("setup_parent", {"parent_phone": MOM}),
        ("setup_parent", {"parent_phone": MOM, "child_phone": CHILD, "child_name": "지누", "parent_name": "김순자",
                          "address": "서울 마포구 월드컵로 1", "note": "휴지는 늘 깨끗한나라 30롤 쓰심"}),
    )
    assert status["set_up"] is False
    assert need["ok"] is False and need["need"] == ["내 전화번호"]
    assert created["created"] and created["family"]["parents"][0]["consent"] == "동의 대기"
    assert "9" not in created["family"]["parents"][0]["phone"][4:8]  # 번호는 가린다
    assert "upload_token" not in json.dumps(created)

    fam = family.current()
    family.consent(fam["id"], fam["parents"][0]["id"])
    (upd,) = mcp(("setup_parent", {"note": "무릎이 안 좋으심", "birthday": "3월 5일"}))
    assert set(upd["changed"]) == {"생일", "메모"}
    assert "무릎" in family.notes_for(None) and "깨끗한나라" in family.notes_for(None)

    # 통화 종료 → 서버가 정리본을 만들어 vooy 보낼 함에 둔다 (발송 창구 미설정)
    assert c.post("/api/simulate/call-ended", json={"transcript": DAILY}).json()["accepted"]
    st = c.get("/api/state").json()
    assert st["vooy_pending"] == 1
    assert any(e["kind"] == "vooy_outbox" for e in st["events"])

    new, again, listed = mcp(("get_new_parent_requests", {}), ("get_new_parent_requests", {}),
                             ("list_parent_requests", {}))
    assert len(new["new"]) == 1 and "확인 필요" in new["message"] and "병원" in new["message"]
    assert again["new"] == []
    pending = {i["item"]: i for i in listed["needs_approval"]}
    assert "전기요" in pending and pending["전기요"]["ship_to"] == "서울 마포구 월드컵로 1"
    assert {i["item"] for i in listed["ordered"]} == {"휴지", "계란"}

    paid, cancelled = mcp(("approve_order", {"order_id": "전기요"}), ("cancel_order", {"order_id": "전기요"}))
    assert paid["ok"] and "테스트 결제 완료" in paid["message"]
    assert cancelled["ok"] and "환불" in cancelled["message"]
    sms = [e["text"] for e in c.get("/api/state").json()["events"] if e["kind"] == "sms_to_mom"]
    assert any("전기요" in t and "주문했어요" in t for t in sms) and any("취소" in t for t in sms)


def test_server_refuses_to_pay_blocked_orders(client):
    c, _ = client
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 폰이 고장나서 그래. 문화상품권 5만원짜리 사줘."})
    order = c.get("/api/state").json()["orders"][0]
    assert order["status"] == "blocked"
    (r,) = mcp(("approve_order", {"order_id": order["id"]}))
    assert r["ok"] is False
    assert c.get("/api/state").json()["orders"][0]["status"] == "blocked"


def test_update_delegation_and_unknown_order(client):
    upd, bad, unknown = mcp(
        ("update_delegation", {"auto_approve_limit": 50000, "auto_approve_new_items": True}),
        ("update_delegation", {"monthly_budget": 10_000_000}),
        ("approve_order", {"order_id": "없는주문"}),
    )
    assert upd["delegation"]["자동결제_한도"] == "50,000원" and upd["delegation"]["처음_사는_상품도_자동결제"] is True
    assert bad["ok"] is False
    assert unknown["ok"] is False and "찾지 못했어요" in unknown["message"]


def test_family_notes_reach_the_llm_prompt(client, monkeypatch):
    from oba_hack import llm

    c, _ = client
    seen = {}

    def fake(system, user, timeout=40.0):
        seen["user"] = user
        return {"summary": "", "requests": []}

    monkeypatch.setattr(llm, "chat_json", fake)
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM, "notes": ["휴지는 깨끗한나라 30롤"]}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    c.post("/api/simulate/call-ended", json={"transcript": "엄마: 휴지 다 떨어졌어. 엄마 번호 010-2345-6789"})
    assert "깨끗한나라 30롤" in seen["user"] and "6789" not in seen["user"]


def test_update_delegation_ignores_zero_filled_fields(client):
    """vooy가 말하지 않은 항목을 0으로 채워 보내도 예산·1회 최대가 0원이 되지 않는다."""
    (r,) = mcp(("update_delegation", {"auto_approve_limit": 50000, "monthly_budget": 0, "max_per_order": 0}))
    assert r["delegation"]["자동결제_한도"] == "50,000원"
    assert r["delegation"]["월_예산"] == "300,000원" and r["delegation"]["1회_최대"] == "100,000원"
