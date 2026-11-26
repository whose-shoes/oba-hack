"""통화 정리본(digest)을 만들어 자녀의 vooy로 먼저 보낸다. 웹훅과 크론 두 경로를 함께 지원한다.

- 웹훅(우리 → vooy): 설정된 주소로 즉시 POST. 실패하면 백오프로 재시도(최대 5회).
  Authorization(Bearer), X-Oba-Delivery(중복 방지 ID), X-Oba-Signature(HMAC-SHA256) 헤더를 붙인다.
- 크론(vooy → 우리): vooy 반복 작업이 MCP 도구 get_new_parent_requests 또는 REST /v1/vooy/digests로 가져간다.
- 두 경로가 같은 '전달됨' 표시를 공유하므로 둘 다 켜도 중복으로 가지 않고, 웹훅이 실패한 건은 크론이 가져간다.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from . import catalog, store
from . import tasks as _tasks
from .config import josa

MAX_ATTEMPTS = 5
BASE_BACKOFF_SEC = 15
FIELDS = ("push_url", "push_token", "user_id", "signing_secret")
_lock = threading.RLock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def won(n: int) -> str:
    return f"{n:,}원"


# ---------- 정리본 ----------

def _item(o: dict) -> dict:
    p = o.get("product") or {}
    return {
        "order_id": o["id"],
        "status": o["status"],
        "item": o["request"].get("item"),
        "product": p.get("name"),
        "quantity": o.get("quantity"),
        "total": o.get("total"),
        "mom_said": o["request"].get("evidence", ""),
        "reason": o.get("decision_reason", ""),
        "buy_link": (catalog.buy_link(p, (o.get("ai_pick") or {}).get("search_keyword", ""))[0] if p else ""),
        "alternatives": [{"product_id": a["id"], "name": a["name"], "total": a["price"] * o.get("quantity", 1)}
                         for a in o.get("alternatives", [])[:3]],
    }


def make_digest(call: dict, orders: list[dict]) -> dict | None:
    label = call["caller"]
    ex = call.get("extraction") or {}
    paid = [o for o in orders if o["status"] == "paid"]
    ask = [o for o in orders if o["status"] in ("awaiting_approval", "payment_failed")]
    ready = [o for o in orders if o["status"] == "ready_for_vooy"]
    nf = [o for o in orders if o["status"] == "not_found"]
    blocked = [o for o in orders if o["status"] == "blocked"]
    tasks = ex.get("other_tasks", [])
    todos = [t for t in (store.get("task", i) for i in call.get("task_ids", [])) if t]
    if not (orders or tasks):
        return None

    lines = [f"{josa(label, '과', '와')} 통화에서 부탁 {len(orders) + len(tasks)}건을 정리했어요."]
    if ex.get("scam_signals"):
        lines.append(f"⚠️ 사칭 의심 표현이 있어요({', '.join(ex['scam_signals'])}). {label} 번호로 왔어도 "
                     f"{label}께 직접 전화해서 맞는지 확인해 보세요.")
    if paid:
        lines.append("✅ 늘 사던 거라 주문했어요: " + ", ".join(
            f"{o['product']['name']} {won(o['total'])}" for o in paid) + f" ({label}께 문자 보냄)")
    if ready:
        lines.append("🛒 늘 사던 소액이라 바로 주문할게요: " + ", ".join(
            f"{o['product']['name']} {won(o['total'])}" for o in ready))
    for o in ask:
        why = (o.get("ai_pick") or {}).get("reason")
        if why:
            o = {**o, "decision_reason": f"{o['decision_reason']} 추천 이유: {why}"}
        lines.append(f"🟡 확인 필요: {o['product']['name']} ×{o['quantity']} {won(o['total'])} — "
                     f"{label} 말씀 “{o['request'].get('evidence', '')}” ({o['decision_reason']})")
    for o in nf:
        lines.append(f"🔎 못 찾음: ‘{o['request'].get('item')}’ — 어떤 걸 원하시는지 알려 주세요.")
    for o in blocked:
        lines.append(f"🚫 결제 안 함: {o['product']['name']} — {o['decision_reason']} {label}께 직접 확인해 보세요.")
    for t in todos or [{"text": t} for t in tasks]:
        lines.append(_tasks.line(t) if t.get("id") else f"📌 구매 외 부탁: {t['text']}")
    if todos:
        lines.append("구매 외 부탁은 “응 해줘”라고 하시면 approve_task로 승인하고 알맞은 Vooy 서비스에 맡길게요.")
    if ask:
        name = (ask[0].get("ai_pick") or {}).get("search_keyword") or ask[0]["request"].get("item") or "그것"
        lines.append(f"결제할까요? (예: “응 사줘”, “{josa(name, '은', '는')} 빼줘”)")

    return store.put("digest", call["id"], {
        "id": call["id"],
        "family_id": call.get("family_id"),
        "parent": label,
        "created_at": now(),
        "call": {"received_at": call.get("received_at"), "source": call.get("source"),
                 "summary": ex.get("summary", "")},
        "text": "\n".join(lines),
        "items": [_item(o) for o in orders],
        "other_tasks": tasks,
        "tasks": [_tasks.brief(t) for t in todos],
        "delivered": False,
        "delivered_via": None,
        "in_flight": False,
        "attempts": [],
    })


def public(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "parent", "created_at", "call", "text", "items", "other_tasks", "tasks")}


# ---------- 설정 (계정별. 환경변수는 단일 계정 운영용 기본값) ----------

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def settings(family_id: str | None = None) -> dict:
    """그 계정의 웹훅 설정. 계정에 저장한 값이 없으면 환경변수 기본값을 쓴다."""
    env = {
        "push_url": os.environ.get("OBA_VOOY_PUSH_URL", ""),
        "push_token": os.environ.get("OBA_VOOY_PUSH_TOKEN", ""),
        "user_id": os.environ.get("OBA_VOOY_USER_ID", ""),
        "signing_secret": os.environ.get("OBA_VOOY_SIGNING_SECRET", ""),
    }
    saved = (store.get("settings", f"vooy:{family_id}") or {}) if family_id else {}
    return {k: saved[k] if k in saved else env[k] for k in FIELDS}


def _dev_mode() -> bool:
    """공개 주소가 없거나 localhost면 로컬 개발 — 이때만 http://localhost 웹훅을 허용한다."""
    base = os.environ.get("OBA_PUBLIC_URL", "")
    return not base or urlparse(base).hostname in LOCAL_HOSTS


def _own_origin(u) -> bool:
    """이 서버 자신의 공개 주소(모의 vooy 수신함)인지."""
    base = urlparse(os.environ.get("OBA_PUBLIC_URL", ""))
    return bool(base.hostname) and (u.scheme, u.hostname, u.port) == (base.scheme, base.hostname, base.port)


def _check_url(url: str) -> None:
    """웹훅 주소 검사: https만, 그리고 내부망(사설·루프백·링크로컬) 주소로는 보내지 않는다 (SSRF 방지)."""
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ValueError("웹훅 주소는 https:// 로 시작해야 해요 (로컬 테스트만 http://localhost 허용).")
    if u.hostname in LOCAL_HOSTS:
        if _dev_mode():
            return
        raise ValueError("이 서버 내부 주소로는 보낼 수 없어요. 공개 https 주소를 넣어 주세요.")
    if u.scheme != "https":
        raise ValueError("웹훅 주소는 https:// 로 시작해야 해요 (로컬 테스트만 http://localhost 허용).")
    if _own_origin(u):
        return
    try:
        ips = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(u.hostname, u.port or 443)}
    except (socket.gaierror, ValueError):
        raise ValueError("웹훅 주소의 도메인을 찾을 수 없어요.")
    if not ips or any(not ip.is_global for ip in ips):
        raise ValueError("내부망 주소로는 보낼 수 없어요. 공개 https 주소를 넣어 주세요.")


def update_settings(changes: dict, family_id: str) -> dict:
    if changes.get("push_url"):
        _check_url(changes["push_url"].strip())
    saved = store.get("settings", f"vooy:{family_id}") or {}
    saved.update({k: (changes[k] or "").strip() for k in FIELDS if k in changes and changes[k] is not None})
    store.put("settings", f"vooy:{family_id}", saved)
    return public_settings(family_id)


def stats(family_id: str | None = None) -> dict:
    ds = [d for d in store.all_docs("digest") if family_id is None or d.get("family_id") == family_id]
    via = [d.get("delivered_via") for d in ds if d["delivered"]]
    return {
        "pending": sum(1 for d in ds if not d["delivered"]),
        "webhook": via.count("webhook"),
        "cron": sum(1 for v in via if v in ("cron", "rest")),
        "gave_up": sum(1 for d in ds if not d["delivered"] and len(d.get("attempts", [])) >= MAX_ATTEMPTS),
    }


def _poll_id(family_id: str | None) -> str:
    return f"poll:{family_id}" if family_id else "poll"


def record_poll(count: int, via: str, family_id: str | None = None) -> dict:
    """vooy가 폴링할 때마다 계정별로 기록 (대시보드에서 5분마다 오는지 확인용)."""
    st = store.get("settings", _poll_id(family_id)) or {}
    st.update(last_polled_at=now(), last_count=count, last_via=via,
              polls=st.get("polls", 0) + 1, taken=st.get("taken", 0) + count)
    return store.put("settings", _poll_id(family_id), st)


def poll_status(family_id: str | None = None) -> dict:
    return store.get("settings", _poll_id(family_id)) or {}


def public_settings(family_id: str | None = None) -> dict:
    s = settings(family_id)
    return {
        "poll": poll_status(family_id),
        "push_url": s["push_url"],
        "user_id": s["user_id"],
        "push_token_set": bool(s["push_token"]),
        "signing_secret_set": bool(s["signing_secret"]),
        "webhook_on": bool(s["push_url"]),
        "stats": stats(family_id),
    }


# ---------- 웹훅 발송 ----------

def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _post(payload: dict, s: dict, delivery_id: str) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Oba-Event": payload["event"],
               "X-Oba-Delivery": delivery_id, "X-Oba-Timestamp": ts}
    if s["push_token"]:
        headers["Authorization"] = f"Bearer {s['push_token']}"
    if s["signing_secret"]:
        headers["X-Oba-Signature"] = sign(s["signing_secret"], ts, body)
    try:
        _check_url(s["push_url"])  # 저장 뒤 DNS가 바뀌었을 수도 있어 보낼 때마다 다시 확인
    except ValueError as e:
        return False, f"blocked: {e}"
    try:
        r = httpx.post(s["push_url"], content=body, headers=headers, timeout=10)  # 리디렉션은 따라가지 않는다 (httpx 기본값)
    except Exception as e:  # 연결 실패 등
        return False, type(e).__name__
    return (r.status_code < 300), f"HTTP {r.status_code}"


def _payload(d: dict, s: dict, event: str = "digest.created") -> dict:
    return {
        "event": event,
        "user_id": s["user_id"],
        "text": d["text"],
        "data": {
            "digest_id": d["id"], "family_id": d.get("family_id"), "parent": d["parent"], "created_at": d["created_at"],
            "items": d["items"], "other_tasks": d["other_tasks"],
            "reply_tools": {"approve": "approve_order(order_id)", "reject": "reject_order(order_id)",
                            "list": "list_parent_requests()"},
        },
    }


def push(digest: dict) -> bool:
    """그 계정에 웹훅이 설정돼 있으면 보낸다. 성공하면 True. 설정이 없으면 보낼 함에 남겨 크론이 가져가게 한다."""
    s = settings(digest.get("family_id"))
    if not s["push_url"]:
        if not digest.get("attempts"):
            store.log("vooy_outbox", digest["id"], "💬 vooy로 보낼 정리본 준비됨 (크론이 가져가거나 웹훅을 켜면 전송): " + digest["text"])
        return False
    with _lock:
        d = store.get("digest", digest["id"]) or digest
        if d["delivered"] or d.get("in_flight"):
            return bool(d["delivered"])
        d["in_flight"] = True
        store.put("digest", d["id"], d)
    ok, info = _post(_payload(d, s), s, d["id"])
    with _lock:
        d = store.get("digest", d["id"])
        d["in_flight"] = False
        d.setdefault("attempts", []).append({"at": now(), "result": info})
        if ok:
            d.update(delivered=True, delivered_via="webhook", delivered_at=now())
        store.put("digest", d["id"], d)
    n = len(d["attempts"])
    if ok:
        store.log("vooy_sent", d["id"], f"💬 vooy 웹훅으로 보냄 ({info}): " + d["text"])
    elif n == 1:
        store.log("vooy_outbox", d["id"], f"💬 vooy 웹훅 실패({info}) — 재시도하고, 그 사이 크론이 가져갈 수 있어요: " + d["text"])
    else:
        store.log("vooy_retry", d["id"], f"vooy 웹훅 재시도 {n}/{MAX_ATTEMPTS} 실패 ({info})")
    return ok


def retry_pending(family_id: str | None = None) -> int:
    """보낼 함의 정리본을 백오프에 맞춰 다시 보낸다. 웹훅을 나중에 켜도 쌓인 정리본이 나간다."""
    sent = 0
    for d in pending(family_id):
        if not settings(d.get("family_id"))["push_url"]:
            continue
        att = d.get("attempts", [])
        if len(att) >= MAX_ATTEMPTS:
            continue
        if att:
            last = datetime.fromisoformat(att[-1]["at"]).timestamp()
            if time.time() - last < BASE_BACKOFF_SEC * 2 ** (len(att) - 1):
                continue
        sent += push(d)
    return sent


def send_test(family_id: str) -> dict:
    s = settings(family_id)
    if not s["push_url"]:
        return {"ok": False, "result": "웹훅 주소가 없어요."}
    sample = {"id": f"test-{int(time.time())}", "family_id": family_id, "parent": "엄마", "created_at": now(),
              "text": "[테스트] don't call Mama 웹훅 연결 확인 메시지예요.", "items": [], "other_tasks": []}
    ok, info = _post(_payload(sample, s, event="test"), s, sample["id"])
    store.log("vooy_sent" if ok else "vooy_retry", "test", f"vooy 웹훅 테스트 발송: {'성공' if ok else '실패'} ({info})")
    return {"ok": ok, "result": info}


# ---------- 크론 (vooy가 가져감) ----------

def pending(family_id: str | None = None) -> list[dict]:
    return sorted((d for d in store.all_docs("digest") if not d["delivered"] and not d.get("in_flight")
                   and (family_id is None or d.get("family_id") == family_id)),
                  key=lambda d: d["created_at"])


def mark_delivered(ids: list[str], via: str) -> int:
    n = 0
    with _lock:
        for i in ids:
            d = store.get("digest", i)
            if d and not d["delivered"]:
                d.update(delivered=True, delivered_via=via, delivered_at=now())
                store.put("digest", i, d)
                n += 1
    return n


def take_pending(via: str, family_id: str | None = None) -> list[dict]:
    """그 계정의 아직 안 간 정리본을 가져가며 전달됨으로 표시한다 (웹훅 발송과 겹치지 않게 잠금)."""
    with _lock:
        items = pending(family_id)
        mark_delivered([d["id"] for d in items], via)
    record_poll(len(items), via, family_id)
    if items:
        store.log("vooy_polled", via, f"💬 vooy가 폴링으로 정리본 {len(items)}건을 가져감", family_id=family_id)
    return items


def on_call_processed(call: dict, orders: list[dict]) -> dict | None:
    digest = make_digest(call, orders)
    if digest:
        push(digest)
    return digest
