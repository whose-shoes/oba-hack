import json

from conftest import AUTH

from oba_hack import family


def fam_auth(c):
    fam = family.register("지누", "010-1234-5678", [{"label": "엄마", "phone": "010-2345-6789"}])
    c.headers["X-Account"] = fam["dashboard_token"]
    return {"Authorization": f"Bearer {fam['upload_token']}"}


def test_rejects_bad_token(client):
    c, _ = client
    r = c.post("/v1/calls", content=b"hi", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_multipart_audio_and_text(client):
    c, tmp = client
    r = c.post(
        "/v1/calls",
        headers=fam_auth(c),
        files={"file": ("../../엄마 통화.m4a", b"\x00audio", "audio/x-m4a")},
        data={"text": "엄마: 휴지 다 떨어졌어. 하나 시켜줘."},
    )
    assert r.status_code == 201, r.text
    meta = r.json()
    assert {f["name"] for f in meta["files"]} == {"엄마 통화.m4a", "text.txt"}
    call_dir = tmp / "calls" / meta["call_id"]
    assert (call_dir / "엄마 통화.m4a").read_bytes() == b"\x00audio"
    assert json.loads((call_dir / "meta.json").read_text())["caller"] == "엄마"
    # 백그라운드 처리까지 끝나 주문이 생겼는지
    state = c.get("/api/state").json()
    assert state["calls"][0]["status"] == "processed"
    assert state["orders"][0]["product"]["id"] == "p-tissue-30"


def test_raw_text_body_with_caller_header(client):
    c, _ = client
    r = c.post(
        "/v1/calls",
        headers={**fam_auth(c), "Content-Type": "text/plain; charset=utf-8", "X-Caller": "%EC%97%84%EB%A7%88"},
        content="엄마: 김 좀 사줘".encode(),
    )
    assert r.status_code == 201
    assert r.json()["files"][0]["name"] == "transcript.txt"
    assert r.json()["caller"] == "엄마"


def test_audio_only_waits_for_transcript(client):
    c, _ = client
    r = c.post("/v1/calls", headers=fam_auth(c), files={"file": ("call.m4a", b"\x00", "audio/x-m4a")})
    cid = r.json()["call_id"]
    call = next(x for x in c.get("/api/state").json()["calls"] if x["id"] == cid)
    assert call["status"] == "needs_transcript"
    c.post(f"/api/calls/{cid}/transcript", json={"text": "엄마: 계란 한 판 사줘."})
    st = c.get("/api/state").json()
    assert st["orders"][0]["product"]["id"] == "p-egg-30"


def test_empty_upload(client):
    c, _ = client
    assert c.post("/v1/calls", headers=AUTH, content=b"").status_code == 400


def test_global_token_without_parent_is_rejected(client):
    c, _ = client
    r = c.post("/v1/calls", headers={**AUTH, "Content-Type": "text/plain"}, content="엄마: 김 사줘".encode())
    assert r.status_code == 400
