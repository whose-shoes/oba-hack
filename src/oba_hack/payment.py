"""테스트 결제 게이트웨이 (모의). 실제 금융 거래는 일어나지 않는다.

결제 수단을 정책 순서대로 시도하고, 실패하면 다음 수단으로 복구를 시도한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from . import policy


class PaymentError(Exception):
    pass


def _charge(method: dict, amount: int, pol: dict) -> dict:
    if method["id"] == "card" and pol.get("simulate_card_failure"):
        raise PaymentError("카드사 승인 거절 (테스트: 한도 초과)")
    return {
        "tx_id": f"test_{uuid.uuid4().hex[:12]}",
        "method": method["name"],
        "amount": amount,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "test": True,
    }


def pay(amount: int, family_id: str | None = None) -> tuple[dict | None, list[str]]:
    """(성공 영수증 또는 None, 시도 기록)"""
    pol = policy.get(family_id)
    attempts = []
    for m in pol["payment_methods"]:
        try:
            receipt = _charge(m, amount, pol)
            attempts.append(f"{m['name']}: 승인")
            return receipt, attempts
        except PaymentError as e:
            attempts.append(f"{m['name']}: 실패 — {e}")
    return None, attempts


def refund(receipt: dict) -> dict:
    return {
        "refund_id": f"test_rf_{uuid.uuid4().hex[:10]}",
        "tx_id": receipt["tx_id"],
        "amount": receipt["amount"],
        "refunded_at": datetime.now().isoformat(timespec="seconds"),
        "test": True,
    }
