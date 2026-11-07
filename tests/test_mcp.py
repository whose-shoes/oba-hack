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
                          "approve_order", "checkout_order", "report_purchase", "reject_order", "cancel_order", "update_delegation",
                          "setup_auto_check"}
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


def test_vooy_agent_executes_payment(client):
    """결제 실행 주체가 vooy: 서버는 승인만, vooy가 결제 후 report_purchase로 알린다."""
    c, _ = client
    c.put("/api/policy", json={"payment_executor": "vooy"})
    fam = family.register("레오", CHILD, [{"label": "엄마", "phone": MOM, "address": "서울 마포구 월드컵로 1"}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    c.post("/api/simulate/call-ended", json={"transcript": DAILY})
    st = {o["request"]["item"]: o for o in c.get("/api/state").json()["orders"]}
    assert st["휴지"]["status"] == "ready_for_vooy" and st["전기요"]["status"] == "awaiting_approval"
    assert not [e for e in c.get("/api/state").json()["events"] if e["kind"] == "sms_to_mom" and "주문했어요" in e["text"]]

    early, ok, done, again = mcp(
        ("report_purchase", {"order_id": "전기요", "merchant": "네이버쇼핑"}),
        ("approve_order", {"order_id": "전기요"}),
        ("report_purchase", {"order_id": "전기요", "success": True, "merchant": "네이버쇼핑", "paid_amount": 58000, "order_number": "N-1"}),
        ("approve_order", {"order_id": st["휴지"]["id"]}),
    )
    assert early["ok"] is False
    assert ok["ok"] and ok["purchase"]["max_amount"] == 59000 and ok["purchase"]["ship_to"] == "서울 마포구 월드컵로 1"
    assert done["ok"] and "주문·결제 완료" in done["message"]
    assert again["ok"] and again["purchase"]["product"].startswith("깨끗한나라")
    order = next(o for o in c.get("/api/state").json()["orders"] if o["request"]["item"] == "전기요")
    assert order["status"] == "paid" and order["receipt"]["executor"] == "vooy" and order["receipt"]["amount"] == 58000
    assert any("전기요" in e["text"] and "주문했어요" in e["text"] for e in c.get("/api/state").json()["events"] if e["kind"] == "sms_to_mom")


def test_auto_check_is_requested_until_vooy_polls(client):
    """폴링이 없으면 응답이 vooy에게 5분 반복 작업을 만들라고 안내하고, 폴링이 오면 안내가 사라진다."""
    status_before, created, setup = mcp(
        ("get_family_status", {}),
        ("setup_parent", {"parent_phone": MOM, "child_phone": CHILD}),
        ("setup_auto_check", {}),
    )
    assert created["auto_check_needed"] and created["auto_check"]["interval_minutes"] == 5
    assert setup["already_on"] is False and "get_new_parent_requests" in setup["auto_check"]["instruction"]
    poll, after = mcp(("get_new_parent_requests", {}), ("get_family_status", {}))
    assert after["auto_check_on"] is True and "auto_check_needed" not in after


def test_vooy_checks_out_with_single_use_token(client):
    """vooy는 카드번호 없이, 승인 때 받은 1회용 토큰으로 테스트몰에서 결제한다."""
    c, _ = client
    c.put("/api/policy", json={"payment_executor": "vooy"})
    fam = family.register("레오", CHILD, [{"label": "엄마", "phone": MOM, "address": "서울 강남구 강남대로 374"}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    c.post("/api/simulate/call-ended", json={"transcript": DAILY})
    (ok,) = mcp(("approve_order", {"order_id": "전기요"}))
    co = ok["checkout"]
    assert co["tool"] == "checkout_order" and co["payment_token"].startswith("ptok_") and "4242" in co["payment_method"]
    assert "4242" in json.dumps(ok) and not any(len(x) >= 13 and x.isdigit() for x in json.dumps(ok).split('"'))
    bad, good, again = mcp(
        ("checkout_order", {"order_id": "전기요", "payment_token": "ptok_wrong"}),
        ("checkout_order", {"order_id": "전기요", "payment_token": co["payment_token"]}),
        ("checkout_order", {"order_id": "전기요", "payment_token": co["payment_token"]}),
    )
    assert bad["ok"] is False and "토큰" in bad["message"]
    assert good["ok"] and good["order_number"].startswith("TM-") and good["paid_amount"] == 59000
    assert again["ok"] is False
    order = next(o for o in c.get("/api/state").json()["orders"] if o["request"]["item"] == "전기요")
    assert order["status"] == "paid" and order["receipt"]["executor"] == "vooy" and order["mandate"]["used"] is True


def test_vooy_buys_on_naver_and_reports_real_product(client):
    """vooy가 네이버 스마트스토어에서 고른 상품으로 결제·보고하면 그 상품명으로 기록하고, vooy가 쓴 문자를 엄마에게 보낸다."""
    c, _ = client
    c.put("/api/policy", json={"payment_executor": "vooy"})
    fam = family.register("레오", CHILD, [{"label": "엄마", "phone": MOM, "address": "서울 강남구 강남대로 374"}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    c.post("/api/simulate/call-ended", json={"transcript": DAILY})
    ok, done = mcp(
        ("approve_order", {"order_id": "전기요"}),
        ("report_purchase", {"order_id": "전기요", "success": True, "merchant": "네이버 스마트스토어",
                             "paid_amount": 54900, "order_number": "2026110712345",
                             "note": "보국 카본매트 전기요 싱글", "message_to_mom": "엄마, 전기요 주문했어! 모레 와~"}),
    )
    assert ok["handoff"]["where"].startswith("네이버") and ok["handoff"]["max_amount"] == 59000
    assert done["ok"] and "보국 카본매트 전기요 싱글" in done["message"]
    order = next(o for o in c.get("/api/state").json()["orders"] if o["request"]["item"] == "전기요")
    assert order["product"]["name"] == "보국 카본매트 전기요 싱글" and order["total"] == 54900
    assert order["catalog_product"]["name"].startswith("한일") and order["receipt"]["method"].startswith("vooy")
    assert any(e["text"] == "엄마, 전기요 주문했어! 모레 와~" for e in c.get("/api/state").json()["events"] if e["kind"] == "sms_to_mom")
