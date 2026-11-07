"""정리본을 vooy로 보내는 두 경로(웹훅 · 크론)와 그 조합을 검증한다."""

import asyncio
import json
from urllib.parse import urlparse

import httpx
from mcp import Client

from oba_hack import family, notify

MOM, CHILD = "010-2345-6789", "010-1234-5678"
TALK = "엄마: 휴지가 다 떨어졌어. 하나 시켜줘.\n엄마: 요즘 밤에 너무 추워서 전기요 하나 있으면 좋겠어."
MOCK = "http://127.0.0.1:8765/mock/vooy/webhook"


def setup_family(c):
    fam = family.register("지누", CHILD, [{"label": "엄마", "phone": MOM}])
    family.consent(fam["id"], fam["parents"][0]["id"])
    return fam


def call_ends(c):
    assert c.post("/api/simulate/call-ended", json={"transcript": TALK}).json()["accepted"]


def route_to_app(c, tamper=False, fail_first=0):
    """notify의 httpx.post를 테스트 앱(모의 vooy 수신함)으로 돌린다."""
    state = {"calls": 0}

    def fake_post(url, content=None, headers=None, timeout=None):
        state["calls"] += 1
        if state["calls"] <= fail_first:
            raise httpx.ConnectError("vooy down")
        body = content + b" " if tamper else content
        return c.post(urlparse(url).path, content=body, headers=headers)

    return fake_post, state


def cron_tool():
    from oba_hack.mcp_server import server

    async def run():
        async with Client(server) as cl:
            r = await cl.call_tool("get_new_parent_requests", {})
            return getattr(r, "structured_content", None) or json.loads(r.content[0].text)

    return asyncio.run(run())


def events(c, kind):
    return [e for e in c.get("/api/state").json()["events"] if e["kind"] == kind]


def test_webhook_with_token_and_signature_reaches_vooy_once(client, monkeypatch):
    c, _ = client
    fake, state = route_to_app(c)
    monkeypatch.setattr(notify.httpx, "post", fake)
    r = c.put("/api/vooy", json={"push_url": MOCK, "push_token": "t0ken-value", "user_id": "child-1",
                                 "signing_secret": "s3cr3t-value"})
    assert r.status_code == 200 and r.json()["webhook_on"] and r.json()["signing_secret_set"]
    shown = json.dumps(r.json()) + json.dumps(c.get("/api/state").json())
    assert "s3cr3t-value" not in shown and "t0ken-value" not in shown   # 토큰·서명 비밀은 화면/API에 안 나온다
    setup_family(c)
    call_ends(c)
    arrived = events(c, "vooy_mock")
    assert len(arrived) == 1 and "서명 확인" in arrived[0]["text"] and "결제할까요" in arrived[0]["text"]
    assert cron_tool()["new"] == []                      # 웹훅으로 간 건 크론이 다시 가져가지 않는다
    assert c.get("/api/vooy").json()["stats"] == {"pending": 0, "webhook": 1, "cron": 0, "gave_up": 0}
    assert state["calls"] == 1


def test_bad_signature_is_rejected_and_cron_picks_it_up(client, monkeypatch):
    c, _ = client
    fake, _ = route_to_app(c, tamper=True)
    monkeypatch.setattr(notify.httpx, "post", fake)
    c.put("/api/vooy", json={"push_url": MOCK, "signing_secret": "sec"})
    setup_family(c)
    call_ends(c)
    assert events(c, "vooy_mock") == []                  # 모의 vooy가 401로 거절
    new = cron_tool()["new"]
    assert len(new) == 1 and "전기요" in new[0]["text"]   # 크론이 대신 가져감
    assert notify.retry_pending() == 0                   # 이미 전달됨 → 다시 보내지 않음
    assert c.get("/api/vooy").json()["stats"]["cron"] == 1


def test_webhook_retries_with_backoff(client, monkeypatch):
    c, _ = client
    fake, state = route_to_app(c, fail_first=1)
    monkeypatch.setattr(notify.httpx, "post", fake)
    c.put("/api/vooy", json={"push_url": MOCK})
    setup_family(c)
    call_ends(c)
    assert c.get("/api/vooy").json()["stats"]["pending"] == 1
    assert notify.retry_pending() == 0                   # 백오프 시간 전이라 아직 안 보냄
    real_time = notify.time.time
    monkeypatch.setattr(notify.time, "time", lambda: real_time() + 60)
    assert notify.retry_pending() == 1
    d = notify.store.all_docs("digest")[0]
    assert d["delivered_via"] == "webhook" and [a["result"] for a in d["attempts"]] == ["ConnectError", "HTTP 200"]


def test_turning_webhook_on_later_flushes_the_outbox(client, monkeypatch):
    c, _ = client
    fake, _ = route_to_app(c)
    monkeypatch.setattr(notify.httpx, "post", fake)
    setup_family(c)
    call_ends(c)                                         # 웹훅 없음 → 보낼 함
    assert c.get("/api/vooy").json()["stats"]["pending"] == 1
    c.put("/api/vooy", json={"push_url": MOCK})          # 켜는 순간 쌓인 정리본 전송
    assert c.get("/api/vooy").json()["stats"] == {"pending": 0, "webhook": 1, "cron": 0, "gave_up": 0}
    assert len(events(c, "vooy_mock")) == 1


def test_rest_cron_poll_and_ack(client):
    c, _ = client
    fam = setup_family(c)
    call_ends(c)
    auth = {"Authorization": f"Bearer {fam['upload_token']}"}
    assert c.get("/v1/vooy/digests").status_code == 401
    peek = c.get("/v1/vooy/digests?mark=false", headers=auth).json()["digests"]
    assert len(peek) == 1 and "upload_token" not in json.dumps(peek)
    assert c.post("/v1/vooy/digests/ack", headers=auth, json={"ids": [peek[0]["id"]]}).json() == {"acked": 1}
    assert c.get("/v1/vooy/digests", headers=auth).json()["digests"] == []


def test_settings_validation_and_test_send(client, monkeypatch):
    c, _ = client
    assert c.put("/api/vooy", json={"push_url": "http://example.com/hook"}).status_code == 400
    assert c.post("/api/vooy/test").json()["ok"] is False
    fake, _ = route_to_app(c)
    monkeypatch.setattr(notify.httpx, "post", fake)
    c.put("/api/vooy", json={"push_url": MOCK, "push_token": "tok"})
    assert c.post("/api/vooy/test").json() == {"ok": True, "result": "HTTP 200"}
    assert "[테스트]" in events(c, "vooy_mock")[0]["text"]
