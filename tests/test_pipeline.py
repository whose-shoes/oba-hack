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
