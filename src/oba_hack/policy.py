"""위임 정책: 자식이 '어디까지 맡길지' 정해 두는 규칙.

결정 결과
- auto:  한도 안의 믿을 만한 요청 → 묻지 않고 테스트 결제
- ask:   자식 승인 필요 (금액 초과, 처음 사는 상품, 월 예산 초과)
- block: 결제하지 않음 (차단 품목, 사칭 정황, 모르는 발신자)
"""

from __future__ import annotations

from datetime import datetime

from . import store
from .config import josa

DEFAULT = {
    "trusted_callers": ["엄마"],
    "auto_approve_limit": 30000,      # 이 금액 이하 + 이전 구매 상품이면 자동 결제
    "auto_approve_new_items": False,  # 처음 사는 상품도 자동 결제할지
    "max_per_order": 100000,          # 넘으면 무조건 승인 요청
    "monthly_budget": 300000,
    "blocked_categories": ["상품권"],
    "payment_methods": [
        {"id": "card", "name": "신한카드 (테스트)"},
        {"id": "skpay", "name": "SK pay 포인트 (테스트)"},
    ],
    "simulate_card_failure": False,   # 데모: 카드 결제 실패 → 다음 수단으로 복구
}


def get() -> dict:
    p = store.get("policy", "default")
    if p is None:
        p = store.put("policy", "default", dict(DEFAULT))
    return {**DEFAULT, **p}


def update(changes: dict) -> dict:
    p = get()
    for k, v in changes.items():
        if k in DEFAULT:
            p[k] = v
    return store.put("policy", "default", p)


def spent_this_month() -> int:
    month = datetime.now().strftime("%Y-%m")
    return sum(
        o["total"] for o in store.all_docs("order")
        if o["status"] == "paid" and o.get("paid_at", "").startswith(month)
    )


def decide(*, caller: str, product: dict, total: int, previously_bought: bool, scam_signals: list[str]) -> tuple[str, str]:
    p = get()
    if product["category"] in p["blocked_categories"]:
        return "block", f"{josa(product['category'], '은', '는')} 대신 결제하지 않는 품목이에요 (보이스피싱 단골 수법)."
    if scam_signals:
        return "block", "통화에 사칭 의심 표현이 있어요: " + ", ".join(scam_signals)
    if caller not in p["trusted_callers"]:
        return "block", f"등록되지 않은 발신자({caller})의 요청이에요."
    if total > p["max_per_order"]:
        return "ask", f"1회 한도 {p['max_per_order']:,}원을 넘어요."
    if spent_this_month() + total > p["monthly_budget"]:
        return "ask", f"이번 달 예산 {p['monthly_budget']:,}원을 넘어요."
    if total <= p["auto_approve_limit"] and (previously_bought or p["auto_approve_new_items"]):
        why = "늘 사던 상품" if previously_bought else "자동결제 허용"
        return "auto", f"{why} · 자동결제 한도 {p['auto_approve_limit']:,}원 이내"
    if not previously_bought:
        return "ask", "처음 사는 상품이라 확인이 필요해요."
    return "ask", f"자동결제 한도 {p['auto_approve_limit']:,}원을 넘어요."
