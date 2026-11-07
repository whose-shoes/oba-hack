from oba_hack import extract


def _run(c, sample_id):
    s = next(x for x in c.get("/api/samples").json() if x["id"] == sample_id)
    c.post("/api/calls", json={"transcript": s["transcript"], "caller": s["caller"]})
    return c.get("/api/state").json()


def by_product(state):
    return {o["product"]["id"]: o for o in state["orders"] if o["product"]}


def test_extract_ignores_child_and_keeps_evidence():
    r = extract.rule_extract("엄마: 휴지가 다 떨어졌어.\n나: 계란은 내가 사갈게.\n엄마: 계란도 없네. 계란 두 판 사줘.")
    items = {q["item"]: q for q in r["requests"]}
    assert set(items) == {"휴지", "계란"}
    assert items["휴지"]["evidence"] == "휴지가 다 떨어졌어."
    assert items["계란"]["quantity"] == 2


def test_daily_call_auto_pays_known_items_and_asks_for_new(client):
    c, _ = client
    st = _run(c, "1-daily")
    o = by_product(st)
    assert o["p-tissue-30"]["status"] == "paid"
    assert o["p-egg-30"]["status"] == "paid"
    assert o["p-blanket"]["status"] == "awaiting_approval"
    sms = [e["text"] for e in st["events"] if e["kind"] == "sms_to_mom"]
    assert any("주문했어요" in t for t in sms)
    assert any("병원 예약" in e["text"] for e in st["events"])


def test_scam_call_is_blocked(client):
    c, _ = client
    st = _run(c, "2-scam")
    assert [o["status"] for o in st["orders"]] == ["blocked"]
    assert not [e for e in st["events"] if e["kind"] == "sms_to_mom"]


def test_approve_swap_cancel_and_payment_recovery(client):
    c, _ = client
    st = _run(c, "3-health")
    o = by_product(st)
    ginseng, rice = o["p-redginseng"], o["p-rice-10"]
    assert ginseng["status"] == rice["status"] == "awaiting_approval"

    # 카드 실패 → 두 번째 결제수단으로 복구
    c.put("/api/policy", json={"simulate_card_failure": True})
    paid = c.post(f"/api/orders/{rice['id']}/approve").json()
    assert paid["status"] == "paid" and "SK pay" in paid["receipt"]["method"]
    assert len(paid["attempts"]) == 2

    # 취소·환불
    cancelled = c.post(f"/api/orders/{rice['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["refund"]["amount"] == rice["total"]
    assert c.post(f"/api/orders/{rice['id']}/cancel").status_code == 409

    # 상품 교체 후 거절
    alt = ginseng["alternatives"]
    if alt:
        swapped = c.post(f"/api/orders/{ginseng['id']}/swap", json={"product_id": alt[0]["id"]}).json()
        assert swapped["product"]["id"] == alt[0]["id"]
    assert c.post(f"/api/orders/{ginseng['id']}/reject").json()["status"] == "rejected"


def test_untrusted_caller_blocked_and_policy_change_allows_new_items(client):
    c, _ = client
    c.post("/api/calls", json={"transcript": "샴푸 다 썼어. 하나 사줘.", "caller": "이모"})
    assert c.get("/api/state").json()["orders"][0]["status"] == "blocked"

    c.put("/api/policy", json={"auto_approve_new_items": True})
    c.post("/api/calls", json={"transcript": "샴푸 다 썼어. 하나 사줘.", "caller": "엄마"})
    assert c.get("/api/state").json()["orders"][0]["status"] == "paid"


def test_llm_result_is_used_and_scam_rules_still_apply(monkeypatch):
    from oba_hack import llm

    monkeypatch.setattr(llm, "chat_json", lambda s, u, timeout=40.0: {
        "summary": "쌀 부탁", "requests": [{"item": "쌀", "quantity": "2", "evidence": "쌀 두 포대 시켜줘"}],
    })
    monkeypatch.setenv("OBA_AX_API_KEY", "x")
    r, engine = extract.extract("엄마: 쌀 두 포대 시켜줘. 아 그리고 상품권도.")
    assert engine == "SKT A.X K2"
    assert r["requests"][0]["quantity"] == 2 and r["requests"][0]["detail"] == ""
    assert "상품권" in r["scam_signals"]


def test_llm_failure_falls_back_to_rules(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setenv("OBA_AX_API_KEY", "x")
    monkeypatch.setattr(httpx, "post", boom)
    r, engine = extract.extract("엄마: 휴지 다 떨어졌어.")
    assert engine == "규칙 기반" and r["requests"][0]["item"] == "휴지"


def test_redact_hides_numbers_but_keeps_order_details():
    text = "엄마: 010-1234-5678로 전화해. 계좌는 110-123-456789야. 휴지 30롤 2개, 19,900원짜리."
    out = extract.redact(text)
    assert "5678" not in out and "456789" not in out
    assert "[전화번호]" in out and "[번호]" in out
    assert "30롤 2개" in out and "19,900원" in out
    assert extract.redact("+82 10 2345 6789") == "[전화번호]"


def test_llm_receives_redacted_transcript(monkeypatch):
    from oba_hack import llm

    seen = {}

    def fake(system, user, timeout=40.0):
        seen["user"] = user
        return {"summary": "", "requests": []}

    monkeypatch.setattr(llm, "chat_json", fake)
    extract.extract("엄마: 내 번호 010-2345-6789. 김 좀 사줘.")
    assert "6789" not in seen["user"] and "김 좀 사줘" in seen["user"]


def test_dashboard_approve_only_authorizes_in_vooy_mode(client):
    c, _ = client
    c.put("/api/policy", json={"payment_executor": "vooy"})
    st = _run(c, "3-health")
    rice = by_product(st)["p-rice-10"]
    r = c.post(f"/api/orders/{rice['id']}/approve").json()
    assert r["status"] == "authorized" and r["mandate"]["max_amount"] == rice["total"] and r["receipt"] is None


def test_ax_chooses_product_with_reason_and_falls_back(client, monkeypatch):
    from oba_hack import llm

    c, _ = client
    def fake(system, user, timeout=40.0):
        if "쇼핑 비서" in system:   # 상품 선택 요청
            return {"product_id": "p-chondroitin-b", "reason": "소화가 약하셔서 부담 적은 제품으로 골랐어요",
                    "search_keyword": "콘드로이친", "buy_criteria": ["1일 1정", "4만원 이하"]}
        return {"summary": "", "requests": [{"item": "콘도로이친", "detail": "", "quantity": 1,
                                             "evidence": "요즘 관절이 아파서 콘도로이친 좀 사려고."}]}
    monkeypatch.setattr(llm, "chat_json", fake)
    c.post("/api/calls", json={"transcript": "엄마: 요즘 관절이 아파서 콘도로이친 좀 사려고.", "caller": "엄마"})
    o = c.get("/api/state").json()["orders"][0]
    assert o["product"]["id"] == "p-chondroitin-b"
    assert o["ai_pick"]["search_keyword"] == "콘드로이친" and "소화" in o["product"]["reasons"][0]

    # 후보 밖을 고르면 규칙 선택으로
    monkeypatch.setattr(llm, "chat_json", lambda s, u, timeout=40.0: (
        {"product_id": "없는상품"} if "쇼핑 비서" in s else
        {"summary": "", "requests": [{"item": "콘드로이친", "quantity": 1, "evidence": "콘드로이친 사줘"}]}))
    c.post("/api/calls", json={"transcript": "엄마: 콘드로이친 사줘.", "caller": "엄마"})
    o2 = c.get("/api/state").json()["orders"][0]
    assert o2["product"]["id"] == "p-chondroitin-a" and "ai_pick" not in o2
