"""통화 정리본(digest)을 만들어 자녀의 vooy로 먼저 보낸다.

- vooy에 외부 발송 창구(API·웹훅)가 있으면 OBA_VOOY_PUSH_URL로 바로 보낸다.
- 없으면 정리본을 '보낼 함'에 쌓아 두고, vooy 반복 작업이 MCP 도구 get_new_parent_requests로 가져간다.
어느 쪽이든 정리본에는 order_id가 들어 있어, 자녀가 "응 결제해"라고 답하면 vooy가 approve_order를 부를 수 있다.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from . import store
from .config import josa


def won(n: int) -> str:
    return f"{n:,}원"


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
    nf = [o for o in orders if o["status"] == "not_found"]
    blocked = [o for o in orders if o["status"] == "blocked"]
    tasks = ex.get("other_tasks", [])
    if not (orders or tasks):
        return None

    lines = [f"{josa(label, '과', '와')} 통화에서 부탁 {len(orders) + len(tasks)}건을 정리했어요."]
    if paid:
        lines.append(f"✅ 늘 사던 거라 주문했어요: " + ", ".join(
            f"{o['product']['name']} {won(o['total'])}" for o in paid) + f" ({label}께 문자 보냄)")
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

    digest = {
        "id": call["id"],
        "parent": label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "text": "\n".join(lines),
        "items": [_item(o) for o in orders],
        "other_tasks": tasks,
        "delivered": False,
        "delivered_via": None,
    }
    return store.put("digest", digest["id"], digest)


def push(digest: dict) -> bool:
    """vooy 발송 창구가 설정돼 있으면 보낸다. 성공하면 True."""
    url = os.environ.get("OBA_VOOY_PUSH_URL", "")
    if not url:
        store.log("vooy_outbox", digest["id"], "💬 vooy로 보낼 정리본 준비됨 (발송 창구 확인 전): " + digest["text"])
        return False
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {os.environ.get('OBA_VOOY_PUSH_TOKEN', '')}"},
            json={"user_id": os.environ.get("OBA_VOOY_USER_ID", ""), "text": digest["text"],
                  "data": {"digest_id": digest["id"], "items": digest["items"]}},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        store.log("vooy_outbox", digest["id"], f"💬 vooy 발송 실패({type(e).__name__}) — 보낼 함에 보관: " + digest["text"])
        return False
    mark_delivered([digest["id"]], via="push")
    store.log("vooy_sent", digest["id"], "💬 vooy로 보냄: " + digest["text"])
    return True


def pending() -> list[dict]:
    return sorted((d for d in store.all_docs("digest") if not d["delivered"]), key=lambda d: d["created_at"])


def mark_delivered(ids: list[str], via: str) -> None:
    for i in ids:
        d = store.get("digest", i)
        if d and not d["delivered"]:
            d.update(delivered=True, delivered_via=via, delivered_at=datetime.now().isoformat(timespec="seconds"))
            store.put("digest", i, d)


def on_call_processed(call: dict, orders: list[dict]) -> dict | None:
    digest = make_digest(call, orders)
    if digest:
        push(digest)
    return digest
