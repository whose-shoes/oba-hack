from conftest import AUTH, TELCO

from oba_hack import family

CHILD, MOM, STRANGER = "010-1234-5678", "010-2345-6789", "010-9876-5432"
TALK = "엄마: 휴지가 다 떨어졌어. 하나 시켜줘.\n나: 응 알았어."


def signup(c, consent=True):
    fam = c.post("/api/families", json={
        "child_name": "지누", "child_phone": CHILD,
        "parents": [{"label": "엄마", "phone": MOM}],
    }).json()
    if consent:
        c.post(f"/api/families/{fam['id']}/parents/{fam['parents'][0]['id']}/consent", json={"granted": True})
    return fam


def call_ended(c, frm, to, text=TALK, call_id=None):
    return c.post("/v1/telco/call-ended", headers=TELCO, json={
        "call_id": call_id, "from_number": frm, "to_number": to, "transcript": text,
    })


def test_normalize_and_mask():
    assert family.normalize_phone("+82 10-2345-6789") == "01023456789"
    assert family.normalize_phone("010.2345.6789") == "01023456789"
    assert family.mask_phone("01023456789") == "010-****-6789"


def test_signup_masks_numbers_and_asks_mom_consent(client):
    c, _ = client
    fam = signup(c, consent=False)
    assert fam["parents"][0]["phone"] == "010-****-6789"
    assert fam["parents"][0]["consent"] == "pending"
    sms = [e["text"] for e in c.get("/api/state").json()["events"] if e["kind"] == "sms_to_mom"]
    assert any("동의하시면" in t for t in sms)
    assert c.post("/api/families", json={"child_name": "x", "child_phone": "12", "parents": [{"phone": MOM}]}).status_code == 400


def test_telco_requires_token(client):
    c, _ = client
    signup(c)
    r = c.post("/v1/telco/call-ended", json={"from_number": MOM, "to_number": CHILD, "transcript": TALK})
    assert r.status_code == 401


def test_mom_call_is_processed_both_directions(client):
    c, _ = client
    signup(c)
    r = call_ended(c, "+82-10-2345-6789", CHILD)          # 엄마 → 나
    assert r.status_code == 202 and r.json()["accepted"]
    r = call_ended(c, CHILD, MOM, "엄마: 계란 한 판 사줘.")  # 나 → 엄마
    assert r.json()["accepted"]
    st = c.get("/api/state").json()
    assert {o["product"]["id"] for o in st["orders"]} == {"p-tissue-30", "p-egg-30"}
    assert all(o["status"] == "paid" for o in st["orders"])
    assert {x["source"] for x in st["calls"]} == {"SKT 망 연동 · 수신", "SKT 망 연동 · 발신"}


def test_other_calls_are_dropped_without_storing_anything(client):
    c, tmp = client
    signup(c)
    r = call_ended(c, STRANGER, CHILD, "엄마야 폰이 고장나서. 문화상품권 사줘.")
    assert r.json() == {"accepted": False, "reason": "not a registered family call"}
    st = c.get("/api/state").json()
    assert st["calls"] == [] and st["orders"] == []
    assert not (tmp / "calls").exists() or not any((tmp / "calls").iterdir())
    dropped = [e for e in st["events"] if e["kind"] == "filter"]
    assert len(dropped) == 1 and "9876" not in dropped[0]["text"] and "상품권" not in dropped[0]["text"]


def test_no_processing_before_mom_consents(client):
    c, _ = client
    signup(c, consent=False)
    assert not call_ended(c, MOM, CHILD).json()["accepted"]


def test_duplicate_webhook_is_ignored(client):
    c, _ = client
    signup(c)
    first = call_ended(c, MOM, CHILD, call_id="skt-1").json()
    again = call_ended(c, MOM, CHILD, call_id="skt-1").json()
    assert again["duplicate"] and again["call_id"] == first["call_id"]
    assert len(c.get("/api/state").json()["calls"]) == 1


def test_simulator_uses_same_filter(client):
    c, _ = client
    assert c.post("/api/simulate/call-ended", json={"transcript": TALK}).status_code == 409
    signup(c)
    assert c.post("/api/simulate/call-ended", json={"transcript": TALK}).json()["accepted"]
    assert not c.post("/api/simulate/call-ended", json={"transcript": TALK, "who": "unknown"}).json()["accepted"]


def test_sms_shortcut_with_family_token_and_sender_filter(client):
    c, _ = client
    fam = signup(c)
    tok = {"Authorization": f"Bearer {fam['upload_token']}", "X-Source": "sms-shortcut"}
    ok = c.post("/v1/calls", headers=tok, data={"text": "김 좀 사줘", "caller_number": MOM})
    assert ok.status_code == 201 and ok.json()["caller"] == "엄마"
    by_name = c.post("/v1/calls", headers=tok, data={"text": "물 좀 시켜줘", "caller_number": "엄마"})
    assert by_name.status_code == 201
    no = c.post("/v1/calls", headers=tok, data={"text": "상품권 사줘", "caller_number": STRANGER})
    assert no.status_code == 202 and not no.json()["accepted"]
    st = c.get("/api/state").json()
    assert len(st["calls"]) == 2 and {x["source"] for x in st["calls"]} == {"문자 자동화"}
    assert c.post("/v1/calls", headers={"Authorization": "Bearer nope"}, data={"text": "x"}).status_code == 401
