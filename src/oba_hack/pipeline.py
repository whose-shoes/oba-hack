"""통화 한 건을 끝까지 처리하는 파이프라인.

맥락(통화) → 조회 → 선택 → 결제(위임 정책) → 결과 반환(엄마·자식에게 알림), 그리고 취소·환불 복구.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from . import catalog, choose, extract, family, notify, payment, policy, store
from .config import data_dir, josa

TEXT_EXT = {".txt", ".md", ".text", ".csv", ".json"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".amr", ".mp4", ".ogg", ".webm"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def won(n: int) -> str:
    return f"{n:,}원"


# ---------- 1. 전사 ----------

def _stt(path: Path) -> str | None:
    """OpenAI 호환 음성 인식 엔드포인트가 설정돼 있으면 사용. 없으면 None (붙여넣기 대기)."""
    key = os.environ.get("OBA_STT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("OBA_STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    try:
        with path.open("rb") as f:
            r = httpx.post(
                f"{base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, f)},
                data={"model": os.environ.get("OBA_STT_MODEL", "whisper-1"), "language": "ko"},
                timeout=120,
            )
        r.raise_for_status()
        return r.json().get("text")
    except Exception as e:
        print(f"[stt] failed: {e!r}", flush=True)
        return None


def transcript_from_files(call_dir: Path, files: list[dict]) -> tuple[str | None, str]:
    texts = [call_dir / f["name"] for f in files if Path(f["name"]).suffix.lower() in TEXT_EXT
             or f.get("content_type", "").startswith("text/")]
    if texts:
        return "\n".join(p.read_text(errors="replace") for p in texts).strip(), "공유된 텍스트"
    audios = [call_dir / f["name"] for f in files if Path(f["name"]).suffix.lower() in AUDIO_EXT
              or f.get("content_type", "").startswith("audio/")]
    for a in audios:
        t = _stt(a)
        if t:
            return t, "음성 인식"
    return None, "녹음만 있음 (전사 필요)"


# ---------- 2. 통화 등록 ----------

def register_call(call_id: str, files: list[dict], *, caller: str, source: str, transcript: str | None = None,
                  family_id: str | None = None) -> dict:
    call = {
        "id": call_id, "caller": caller, "source": source, "received_at": now(), "family_id": family_id,
        "files": files, "transcript": transcript, "status": "received", "order_ids": [],
    }
    store.put("call", call_id, call)
    store.log("step", call_id, f"{josa(caller, '과', '와')}의 통화 기록 수신 ({source})")
    return call


def new_call_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


# ---------- 3. 처리 ----------

def process_call(call_id: str) -> dict:
    call = store.get("call", call_id)
    if call is None:
        raise KeyError(call_id)

    if not call.get("transcript"):
        text, how = transcript_from_files(data_dir() / "calls" / call_id, call.get("files", []))
        if text is None:
            call["status"] = "needs_transcript"
            store.put("call", call_id, call)
            store.log("push_to_child", call_id, "통화 녹음을 받았어요. 에이닷 요약/텍스트를 붙여 넣어 주세요.")
            return call
        call["transcript"] = text
        store.log("step", call_id, f"전사 확보: {how}")

    result, engine = extract.extract(call["transcript"], context=family.notes_for(call.get("family_id")))
    call.update(extraction=result, engine=engine, status="extracted")
    store.log("step", call_id, f"부탁 추출({engine}): {result.get('summary', '')}")
    store.put("call", call_id, call)

    orders = [_make_order(call, req, result.get("scam_signals", [])) for req in result.get("requests", [])]
    call["order_ids"] = [o["id"] for o in orders]
    call["status"] = "processed"
    store.put("call", call_id, call)

    for t in result.get("other_tasks", []):
        store.log("push_to_child", call_id, f"구매 외 부탁도 있어요: “{t}”")
    if result.get("scam_signals") and not orders:
        store.log("push_to_child", call_id, "⚠️ 사칭 의심 표현이 있는 통화예요: " + ", ".join(result["scam_signals"]))

    _report_to_mom(call, orders)
    notify.on_call_processed(call, orders)
    return call


def _make_order(call: dict, req: dict, scam_signals: list[str]) -> dict:
    caller = call["caller"]
    oid = f"o-{uuid.uuid4().hex[:8]}"
    candidates = catalog.search(req.get("item", ""), req.get("detail", ""), caller)
    order = {
        "id": oid, "call_id": call["id"], "caller": caller, "request": req, "created_at": now(),
        "timeline": [], "receipt": None, "attempts": [], "ship_to": _ship_to(call),
    }
    if not candidates:
        order.update(status="not_found", product=None, alternatives=[], quantity=req.get("quantity", 1), total=0,
                     decision="ask", decision_reason="카탈로그에서 맞는 상품을 못 찾았어요.")
        _tl(order, f"‘{req.get('item')}’ 상품을 찾지 못함")
        store.put("order", oid, order)
        store.log("push_to_child", oid, f"{josa(caller, '이', '가')} {josa(req.get('item') or '물건', '을', '를')} 부탁하셨는데 맞는 상품을 못 찾았어요.")
        return order

    pick = candidates[0]
    ai = choose.choose(req, candidates, family.notes_for(call.get("family_id")),
                       [{**h, "name": (catalog.product(h["product_id"]) or {}).get("name")} for h in catalog.history(caller)],
                       policy.get()["max_per_order"])
    if ai:  # SKT A.X K2가 엄마 상황을 보고 고른 상품
        pick = next(c for c in candidates if c["id"] == ai["product_id"])
        pick = {**pick, "reasons": ([f"{ai['engine']} 추천: {ai['reason']}"] if ai["reason"] else []) + pick["reasons"]}
        order["ai_pick"] = ai
    alts = [c for c in candidates if c["id"] != pick["id"]][:3]
    qty = max(1, int(req.get("quantity") or 1))
    total = pick["price"] * qty
    budget = req.get("budget_krw")
    bought = any(h["product_id"] == pick["id"] for h in catalog.history(caller))
    decision, reason = policy.decide(
        caller=caller, product=pick, total=total, previously_bought=bought, scam_signals=scam_signals,
    )
    if budget and total > int(budget) and decision == "auto":
        decision, reason = "ask", f"{josa(caller, '이', '가')} 말한 예산 {won(int(budget))}을 넘어요."
    order.update(product=pick, alternatives=alts, quantity=qty, total=total,
                 decision=decision, decision_reason=reason)
    _tl(order, f"선택: {pick['name']} x{qty} = {won(total)} ({'; '.join(pick['reasons'])})")
    _tl(order, f"위임 판단: {decision} — {reason}")

    if decision == "auto":
        if policy.get().get("payment_executor") == "vooy":
            order["status"] = "ready_for_vooy"
            _tl(order, "자동결제 범위 → vooy 에이전트가 구매 승인 후 바로 주문·결제")
        else:
            _pay(order)
    elif decision == "ask":
        order["status"] = "awaiting_approval"
        store.log("push_to_child", oid,
                  f"{josa(caller, '이', '가')} “{req.get('evidence', '')}”라고 하셨어요. {pick['name']} x{qty} {won(total)} 결제할까요? ({reason})")
    else:
        order["status"] = "blocked"
        store.log("push_to_child", oid, f"🚫 결제 차단: {pick['name']} — {reason}")
    store.put("order", oid, order)
    return order


def _ship_to(call: dict) -> str:
    fam = store.get("family", call.get("family_id")) if call.get("family_id") else family.current()
    for p in (fam or {}).get("parents", []):
        if p["label"] == call["caller"] and p.get("address"):
            return p["address"]
    return ""


def _tl(order: dict, text: str) -> None:
    order["timeline"].append({"at": now(), "text": text})


def _pay(order: dict) -> bool:
    receipt, attempts = payment.pay(order["total"])
    order["attempts"] += attempts
    for a in attempts:
        _tl(order, f"결제 시도 — {a}")
    if receipt:
        order.update(status="paid", receipt=receipt, paid_at=now())
        store.log("push_to_child", order["id"],
                  f"✅ {order['product']['name']} x{order['quantity']} {won(order['total'])} 테스트 결제 완료 ({receipt['method']})")
        if len(attempts) > 1:
            store.log("push_to_child", order["id"], "첫 결제수단이 실패해서 다음 수단으로 결제했어요.")
        return True
    order["status"] = "payment_failed"
    store.log("push_to_child", order["id"], f"❌ 모든 결제수단 실패: {order['product']['name']}. 결제수단을 확인해 주세요.")
    return False


def _report_to_mom(call: dict, orders: list[dict]) -> None:
    paid = [o for o in orders if o["status"] == "paid"]
    pending = [o for o in orders if o["status"] in ("awaiting_approval", "not_found", "ready_for_vooy")]
    if not paid and not pending:
        return
    parts = []
    if paid:
        names = ", ".join(f"{o['product']['name']}" for o in paid)
        parts.append(f"{call['caller']}, 말씀하신 {names} 주문했어요. {paid[0]['product']['delivery']} 예정이에요.")
    if pending:
        parts.append(f"{josa(', '.join(o['request'].get('item', '') for o in pending), '은', '는')} 제가 확인하고 주문할게요.")
    store.log("sms_to_mom", call["id"], " ".join(parts))


# ---------- 4. 자식의 개입 (승인/거절/교체/취소) ----------

def _load(oid: str) -> dict:
    o = store.get("order", oid)
    if o is None:
        raise KeyError(oid)
    return o


def approve(oid: str) -> dict:
    o = _load(oid)
    if o["status"] not in ("awaiting_approval", "payment_failed"):
        raise ValueError(f"승인할 수 없는 상태: {o['status']}")
    _tl(o, "자식이 승인")
    if _pay(o):
        store.log("sms_to_mom", o["id"], f"{o['caller']}, {o['product']['name']} 주문했어요. {o['product']['delivery']} 예정이에요.")
    store.put("order", oid, o)
    return o


def reject(oid: str) -> dict:
    o = _load(oid)
    if o["status"] not in ("awaiting_approval", "not_found", "payment_failed", "blocked"):
        raise ValueError(f"거절할 수 없는 상태: {o['status']}")
    o["status"] = "rejected"
    _tl(o, "자식이 주문하지 않기로 함")
    store.put("order", oid, o)
    store.log("push_to_child", oid, f"주문하지 않음: {o['request'].get('item')}")
    return o


def swap(oid: str, product_id: str) -> dict:
    o = _load(oid)
    if o["status"] not in ("awaiting_approval", "payment_failed", "ready_for_vooy"):
        raise ValueError(f"상품을 바꿀 수 없는 상태: {o['status']}")
    alt = next((a for a in o["alternatives"] if a["id"] == product_id), None)
    if alt is None:
        raise ValueError("후보에 없는 상품")
    o["alternatives"] = [a for a in o["alternatives"] if a["id"] != product_id] + [o["product"]]
    o["product"], o["total"] = alt, alt["price"] * o["quantity"]
    _tl(o, f"자식이 상품 교체 → {alt['name']} ({won(o['total'])})")
    store.put("order", oid, o)
    return o


def cancel(oid: str) -> dict:
    o = _load(oid)
    if o["status"] != "paid":
        raise ValueError(f"취소할 수 없는 상태: {o['status']}")
    rf = payment.refund(o["receipt"])
    o.update(status="cancelled", refund=rf)
    _tl(o, f"주문 취소·테스트 환불 {won(rf['amount'])} ({rf['refund_id']})")
    store.put("order", oid, o)
    store.log("push_to_child", oid, f"↩️ {o['product']['name']} 취소하고 {won(rf['amount'])} 환불했어요.")
    store.log("sms_to_mom", oid, f"{o['caller']}, {o['product']['name']} 주문은 취소했어요. 다른 걸로 다시 알아볼게요.")
    return o



# ---------- vooy 에이전트가 결제를 실행하는 경우 ----------

def authorize(oid: str) -> dict:
    """구매 승인(위임 증서) 발급. 결제는 vooy가 하고, 우리 서버는 한도·차단을 검사해 승인만 한다."""
    o = _load(oid)
    if not o.get("ship_to"):  # 승인 사이에 배송지가 등록됐으면 반영
        call = store.get("call", o["call_id"]) or {"caller": o["caller"], "family_id": None}
        o["ship_to"] = _ship_to(call)
    if o["status"] not in ("ready_for_vooy", "awaiting_approval", "payment_failed", "authorized"):
        raise ValueError(f"승인할 수 없는 상태: {o['status']}")
    from datetime import timedelta
    import secrets as _secrets
    mandate = {"id": f"m-{uuid.uuid4().hex[:10]}", "max_amount": o["total"], "issued_at": now(),
               # 이 주문·이 금액·30분·1회만 쓸 수 있는 결제 토큰 (카드번호 대신 넘긴다)
               "payment_token": f"ptok_{_secrets.token_urlsafe(12)}",
               "expires_at": (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds"), "used": False}
    o.update(status="authorized", mandate=mandate)
    _tl(o, f"구매 승인 {mandate['id']} 발급 (최대 {won(o['total'])}) → vooy 에이전트가 주문·결제")
    store.put("order", oid, o)
    store.log("push_to_child", oid, f"🛒 {o['product']['name']} 구매를 승인했어요. vooy가 주문·결제를 진행해요.")
    return o


def record_purchase(oid: str, success: bool, merchant: str = "", paid_amount: int = 0,
                    order_number: str = "", note: str = "", message_to_mom: str = "",
                    product_name: str = "", product_url: str = "") -> dict:
    """vooy 에이전트가 (네이버 스마트스토어 등에서) 주문·결제한 결과를 받는다. 성공하면 엄마에게 문자."""
    o = _load(oid)
    if o["status"] != "authorized":
        raise ValueError("먼저 approve_order로 구매 승인을 받아야 해요.")
    amount = int(paid_amount or o["total"])
    name = (product_name or (note if success else "")).strip()
    if success and name and name != o["product"]["name"]:  # vooy가 고른 실제 상품으로 기록
        o["catalog_product"] = o["product"]
        o["product"] = {**o["product"], "name": name, "url": product_url, "source": merchant or "쇼핑몰",
                        "reasons": [f"vooy가 {merchant or '쇼핑몰'}에서 고른 상품"]}
        o["total"] = amount
    if success:
        o.update(status="paid", paid_at=now(), receipt={
            "tx_id": order_number or f"vooy_{uuid.uuid4().hex[:10]}",
            "method": f"vooy 에이전트 결제 · {merchant or '쇼핑몰'}", "amount": amount,
            "approved_at": now(), "executor": "vooy"})
        _tl(o, f"vooy가 주문·결제 완료: {merchant or '쇼핑몰'} {won(amount)} (주문번호 {order_number or '-'})")
        if amount > o["mandate"]["max_amount"] * 1.1:
            _tl(o, "⚠️ 승인 금액보다 크게 결제됨")
            store.log("push_to_child", oid, f"⚠️ 승인 금액({won(o['mandate']['max_amount'])})보다 큰 {won(amount)}이 결제됐어요. 확인해 주세요.")
        store.log("push_to_child", oid, f"✅ vooy가 {o['product']['name']} {won(amount)} 주문·결제를 마쳤어요.")
        sms = (message_to_mom or "").strip() or f"{o['caller']}, {o['product']['name']} 주문했어요. {o['product']['delivery']} 예정이에요."
        store.log("sms_to_mom", oid, sms)
    else:
        o["status"] = "payment_failed"
        _tl(o, f"vooy 주문·결제 실패: {note or '사유 없음'}")
        store.log("push_to_child", oid, f"❌ vooy가 {o['product']['name']} 결제에 실패했어요: {note or '사유 없음'}")
    store.put("order", oid, o)
    return o



def checkout(oid: str, payment_token: str) -> dict:
    """vooy가 테스트몰에서 1회용 결제 토큰으로 결제한다 (가맹점·PG 역할은 우리 서버의 모의 구현)."""
    o = _load(oid)
    m = o.get("mandate") or {}
    if o["status"] != "authorized":
        raise ValueError("먼저 approve_order로 구매 승인을 받아야 해요.")
    if not m.get("payment_token") or payment_token.strip() != m["payment_token"]:
        raise ValueError("결제 토큰이 맞지 않아요. approve_order로 받은 payment_token을 그대로 넣어 주세요.")
    if m.get("used"):
        raise ValueError("이미 사용한 결제 토큰이에요.")
    if datetime.now() > datetime.fromisoformat(m["expires_at"]):
        raise ValueError("결제 토큰이 만료됐어요. approve_order로 다시 승인을 받아 주세요.")
    pol = policy.get()
    card = pol["family_payment_method"]
    if o["total"] > m["max_amount"]:
        raise ValueError("승인 금액을 넘어요.")
    order_no = f"TM-{datetime.now():%m%d}-{uuid.uuid4().hex[:6].upper()}"
    m["used"] = True
    o.update(status="paid", paid_at=now(), receipt={
        "tx_id": f"test_{uuid.uuid4().hex[:12]}", "order_number": order_no, "amount": o["total"],
        "method": f"{pol['test_merchant']} · {card['label']} {card['brand']} •••• {card['last4']}",
        "approved_at": now(), "executor": "vooy", "test": True})
    _tl(o, f"vooy가 {pol['test_merchant']}에서 결제: {won(o['total'])} (주문번호 {order_no}, 토큰 1회 사용)")
    store.put("order", oid, o)
    store.log("push_to_child", oid, f"✅ vooy가 {o['product']['name']} {won(o['total'])} 결제를 마쳤어요 (주문번호 {order_no}).")
    store.log("sms_to_mom", oid, f"{o['caller']}, {o['product']['name']} 주문했어요. {o['product']['delivery']} 예정이에요.")
    return o
