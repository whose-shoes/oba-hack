"""통화 정리본(digest)을 만들어 자녀의 vooy로 먼저 보낸다. 웹훅과 크론 두 경로를 함께 지원한다.

- 웹훅(우리 → vooy): 설정된 주소로 즉시 POST. 실패하면 백오프로 재시도(최대 5회).
  Authorization(Bearer), X-Oba-Delivery(중복 방지 ID), X-Oba-Signature(HMAC-SHA256) 헤더를 붙인다.
- 크론(vooy → 우리): vooy 반복 작업이 MCP 도구 get_new_parent_requests 또는 REST /v1/vooy/digests로 가져간다.
- 두 경로가 같은 '전달됨' 표시를 공유하므로 둘 다 켜도 중복으로 가지 않고, 웹훅이 실패한 건은 크론이 가져간다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from . import store
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
    if not (orders or tasks):
        return None

    lines = [f"{josa(label, '과', '와')} 통화에서 부탁 {len(orders) + len(tasks)}건을 정리했어요."]
    if paid:
        lines.append("✅ 늘 사던 거라 주문했어요: " + ", ".join(
            f"{o['product']['name']} {won(o['total'])}" for o in paid) + f" ({label}께 문자 보냄)")
    if ready:
        lines.append("🛒 늘 사던 소액이라 바로 주문할게요: " + ", ".join(
            f"{o['product']['name']} {won(o['total'])}" for o in ready))
    for o in ask:
        lines.append(f"🟡 확인 필요: {o['product']['name']} ×{o['quantity']} {won(o['total'])} — "
                     f"{label} 말씀 “{o['request'].get('evidence', '')}” ({o['decision_reason']})")
    for o in nf:
        lines.append(f"🔎 못 찾음: ‘{o['request'].get('item')}’ — 어떤 걸 원하시는지 알려 주세요.")
    for o in blocked:
        lines.append(f"🚫 결제 안 함: {o['product']['name']} — {o['decision_reason']} {label}께 직접 확인해 보세요.")
    for t in tasks:
        lines.append(f"📌 구매 외 부탁: {t}")
    if ask:
        lines.append("결제할까요? (예: “응 결제해”, “전기요는 빼줘”)")

    return store.put("digest", call["id"], {
        "id": call["id"],
        "parent": label,
        "created_at": now(),
        "call": {"received_at": call.get("received_at"), "source": call.get("source"),
                 "summary": ex.get("summary", "")},
        "text": "\n".join(lines),
        "items": [_item(o) for o in orders],
        "other_tasks": tasks,
        "delivered": False,
        "delivered_via": None,
        "in_flight": False,
        "attempts": [],
    })


def public(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "parent", "created_at", "call", "text", "items", "other_tasks")}


# ---------- 설정 (환경변수 기본값 + 대시보드에서 바꾼 값) ----------

def settings() -> dict:
    env = {
        "push_url": os.environ.get("OBA_VOOY_PUSH_URL", ""),
        "push_token": os.environ.get("OBA_VOOY_PUSH_TOKEN", ""),
        "user_id": os.environ.get("OBA_VOOY_USER_ID", ""),
        "signing_secret": os.environ.get("OBA_VOOY_SIGNING_SECRET", ""),
    }
    saved = store.get("settings", "vooy") or {}
    return {k: saved[k] if k in saved else env[k] for k in FIELDS}


def _check_url(url: str) -> None:
    u = urlparse(url)
    local = u.hostname in ("127.0.0.1", "localhost", "::1")
    if not (u.scheme == "https" or (u.scheme == "http" and local)) or not u.hostname:
        raise ValueError("웹훅 주소는 https:// 로 시작해야 해요 (로컬 테스트만 http://localhost 허용).")


def update_settings(changes: dict) -> dict:
    if changes.get("push_url"):
        _check_url(changes["push_url"].strip())
    saved = store.get("settings", "vooy") or {}
    saved.update({k: (changes[k] or "").strip() for k in FIELDS if k in changes and changes[k] is not None})
    store.put("settings", "vooy", saved)
    return public_settings()


def stats() -> dict:
    ds = store.all_docs("digest")
    via = [d.get("delivered_via") for d in ds if d["delivered"]]
    return {
        "pending": sum(1 for d in ds if not d["delivered"]),
        "webhook": via.count("webhook"),
        "cron": sum(1 for v in via if v in ("cron", "rest")),
        "gave_up": sum(1 for d in ds if not d["delivered"] and len(d.get("attempts", [])) >= MAX_ATTEMPTS),
    }


def record_poll(count: int, via: str) -> dict:
    """vooy가 폴링할 때마다 기록 (대시보드에서 5분마다 오는지 확인용)."""
    st = store.get("settings", "poll") or {}
    st.update(last_polled_at=now(), last_count=count, last_via=via,
              polls=st.get("polls", 0) + 1, taken=st.get("taken", 0) + count)
    return store.put("settings", "poll", st)


def poll_status() -> dict:
    return store.get("settings", "poll") or {}


def public_settings() -> dict:
    s = settings()
    return {
        "poll": poll_status(),
        "push_url": s["push_url"],
        "user_id": s["user_id"],
        "push_token_set": bool(s["push_token"]),
        "signing_secret_set": bool(s["signing_secret"]),
        "webhook_on": bool(s["push_url"]),
        "stats": stats(),
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
        r = httpx.post(s["push_url"], content=body, headers=headers, timeout=10)
    except Exception as e:  # 연결 실패 등
        return False, type(e).__name__
    return (r.status_code < 300), f"HTTP {r.status_code}"


def _payload(d: dict, s: dict, event: str = "digest.created") -> dict:
    return {
        "event": event,
        "user_id": s["user_id"],
        "text": d["text"],
        "data": {
            "digest_id": d["id"], "parent": d["parent"], "created_at": d["created_at"],
            "items": d["items"], "other_tasks": d["other_tasks"],
            "reply_tools": {"approve": "approve_order(order_id)", "reject": "reject_order(order_id)",
                            "list": "list_parent_requests()"},
        },
    }


def push(digest: dict) -> bool:
    """웹훅이 설정돼 있으면 보낸다. 성공하면 True. 설정이 없으면 보낼 함에 남겨 크론이 가져가게 한다."""
    s = settings()
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


def retry_pending() -> int:
    """보낼 함의 정리본을 백오프에 맞춰 다시 보낸다. 웹훅을 나중에 켜도 쌓인 정리본이 나간다."""
    if not settings()["push_url"]:
        return 0
    sent = 0
    for d in pending():
        att = d.get("attempts", [])
        if len(att) >= MAX_ATTEMPTS:
            continue
        if att:
            last = datetime.fromisoformat(att[-1]["at"]).timestamp()
            if time.time() - last < BASE_BACKOFF_SEC * 2 ** (len(att) - 1):
                continue
        sent += push(d)
    return sent


def send_test() -> dict:
    s = settings()
    if not s["push_url"]:
        return {"ok": False, "result": "웹훅 주소가 없어요."}
    sample = {"id": f"test-{int(time.time())}", "parent": "엄마", "created_at": now(),
              "text": "[테스트] don't call me, Mama 웹훅 연결 확인 메시지예요.", "items": [], "other_tasks": []}
    ok, info = _post(_payload(sample, s, event="test"), s, sample["id"])
    store.log("vooy_sent" if ok else "vooy_retry", "test", f"vooy 웹훅 테스트 발송: {'성공' if ok else '실패'} ({info})")
    return {"ok": ok, "result": info}


# ---------- 크론 (vooy가 가져감) ----------

def pending() -> list[dict]:
    return sorted((d for d in store.all_docs("digest") if not d["delivered"] and not d.get("in_flight")),
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


def take_pending(via: str) -> list[dict]:
    """아직 안 간 정리본을 가져가며 전달됨으로 표시한다 (웹훅 발송과 겹치지 않게 잠금)."""
    with _lock:
        items = pending()
        mark_delivered([d["id"] for d in items], via)
    record_poll(len(items), via)
    if items:
        store.log("vooy_polled", via, f"💬 vooy가 폴링으로 정리본 {len(items)}건을 가져감")
    return items


def on_call_processed(call: dict, orders: list[dict]) -> dict | None:
    digest = make_digest(call, orders)
    if digest:
        push(digest)
    return digest
