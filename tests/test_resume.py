"""서버가 처리 도중 꺼졌다 켜질 때의 복구, 끝난 주문에 대한 안내, '알아서 골라 줘' 전달."""

import asyncio
import json

import pytest
from mcp import Client

from oba_hack import extract, family, pipeline, store

MOM, CHILD = "010-2345-6789", "010-1234-5678"
DAILY = (
    "엄마: 휴지가 다 떨어졌어. 저번에 시켜준 30롤짜리 그거 좋더라.\n"
    "엄마: 그리고 계란도 없네. 계란 한 판만 사줘.\n"
    "엄마: 요즘 밤에 너무 추워서 전기요 하나 있으면 좋겠어."
)


def login(c, **parent):
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM, **parent}])
    c.headers["X-Account"] = fam["dashboard_token"]
    return fam


def orders_of(call_id):
    return [o for o in store.all_docs("order") if o.get("call_id") == call_id]


def mcp(fid, *calls):
    from oba_hack import account
    from oba_hack.mcp_server import server

    def payload(res):
        sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if isinstance(sc, dict):
            return sc.get("result", sc) if set(sc) == {"result"} else sc
        return json.loads(res.content[0].text)

    async def run():
        async with Client(server) as c:
            return [payload(await c.call_tool(name, args)) for name, args in calls]

    tok = account.override.set(fid)
    try:
        return asyncio.run(run())
    finally:
        account.override.reset(tok)


def test_resume_processes_call_stuck_after_receive(client):
    c, _ = client
    fam = login(c)
    pipeline.register_call("c-stuck", [], caller="엄마", source="테스트", transcript=DAILY, family_id=fam["id"])
    assert pipeline.resume_interrupted() == ["c-stuck"]
    call = store.get("call", "c-stuck")
    assert call["status"] == "processed" and len(call["order_ids"]) == 3
    assert any("이어서" in e["text"] for e in c.get("/api/state").json()["events"])


def test_resume_after_crash_mid_orders_does_not_order_twice(client, monkeypatch):
    c, _ = client
    fam = login(c)
    pipeline.register_call("c-crash", [], caller="엄마", source="테스트", transcript=DAILY, family_id=fam["id"])
    real, made = pipeline._make_order, []

    def crash_on_second(*a, **kw):  # 첫 주문(결제까지)을 만든 뒤 서버가 꺼진 상황
        if made:
            raise RuntimeError("server stopped")
        made.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "_make_order", crash_on_second)
    with pytest.raises(RuntimeError):
        pipeline.process_call("c-crash")
    assert store.get("call", "c-crash")["status"] == "extracted"
    (first,) = orders_of("c-crash")

    monkeypatch.setattr(pipeline, "_make_order", real)
    monkeypatch.setattr(extract, "extract", lambda *a, **kw: pytest.fail("추출을 다시 하면 안 돼요"))
    assert pipeline.resume_interrupted() == ["c-crash"]
    call, orders = store.get("call", "c-crash"), orders_of("c-crash")
    assert call["status"] == "processed" and len(orders) == 3 and first["id"] in call["order_ids"]
    assert [o["request"]["item"] for o in orders].count(first["request"]["item"]) == 1
    assert store.get("order", first["id"])["status"] == first["status"]  # 이미 한 결제는 그대로 (다시 결제 안 함)


def test_old_stuck_call_is_not_ordered_late(client):
    c, _ = client
    fam = login(c)
    call = pipeline.register_call("c-old", [], caller="엄마", source="테스트", transcript=DAILY, family_id=fam["id"])
    store.put("call", "c-old", {**call, "received_at": "2026-01-01T09:00:00"})
    assert pipeline.resume_interrupted() == []
    assert store.get("call", "c-old")["status"] == "interrupted" and not orders_of("c-old")
    assert any("다시 보내" in e["text"] for e in c.get("/api/state").json()["events"])


def test_startup_hook_resumes_in_background(client):
    from oba_hack import app as app_module

    c, _ = client
    fam = login(c)
    pipeline.register_call("c-boot", [], caller="엄마", source="테스트", transcript=DAILY, family_id=fam["id"])
    asyncio.run(app_module._resume_interrupted())
    assert store.get("call", "c-boot")["status"] == "processed"


def test_finished_order_explains_why_instead_of_not_found(client):
    """결제가 끝난 주문을 vooy가 다시 결제·승인하려 하면 '못 찾음'이 아니라 '이미 결제함(주문번호)'으로 답한다."""
    c, _ = client
    fam = login(c, address="서울 강남구 강남대로 374")
    c.put("/api/policy", json={"payment_executor": "vooy"})
    c.post("/api/simulate/call-ended", json={"transcript": DAILY})
    (ok,) = mcp(fam["id"], ("approve_order", {"order_id": "전기요"}))
    tok, oid = ok["checkout"]["payment_token"], ok["purchase"]["order_id"]
    paid, by_name, approve_again, by_id, report = mcp(
        fam["id"],
        ("checkout_order", {"order_id": "전기요", "payment_token": tok}),
        ("checkout_order", {"order_id": "전기요", "payment_token": tok}),
        ("approve_order", {"order_id": "전기요"}),
        ("checkout_order", {"order_id": oid, "payment_token": tok}),
        ("report_purchase", {"order_id": oid, "success": True}),
    )
    assert paid["ok"]
    for r in (by_name, approve_again, by_id, report):
        assert r["ok"] is False and "이미 결제한 주문" in r["message"] and paid["order_number"] in r["message"]
    (nothing,) = mcp(fam["id"], ("approve_order", {"order_id": "냉장고"}))
    assert nothing["ok"] is False and "찾지 못했어요" in nothing["message"]


def test_pick_for_me_is_passed_to_the_llm(client, monkeypatch):
    from oba_hack import llm

    c, _ = client
    login(c)
    seen = []

    def fake(system, user, timeout=40.0, **kw):
        if "쇼핑 비서" in system:  # 상품 선택 요청
            seen.append(json.loads(user))
            return None  # 규칙 선택으로
        return {"summary": "", "requests": [{"item": "콘드로이친", "detail": "", "quantity": 1,
                                             "evidence": "요즘 관절이 아파서 콘도로이친 좀 사려고."}]}

    monkeypatch.setattr(llm, "chat_json", fake)
    c.post("/api/calls", json={"transcript": "엄마: 요즘 관절이 아파서 콘도로이친 좀 사려고.\n엄마: 니가 알아서 해줘!",
                               "caller": "엄마"})
    c.post("/api/calls", json={"transcript": "엄마: 콘드로이친 사줘.", "caller": "엄마"})
    assert [s["mom_asked_you_to_pick"] for s in seen] == [True, False]
