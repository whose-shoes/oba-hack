"""무엇을 살지 — SKT A.X K2에게 묻는다.

규칙 기반 후보(catalog.search) 중에서 엄마의 말·가족 메모·구매 이력·예산을 보고 하나를 고르고,
엄마 상황을 반영한 이유, 네이버 검색용 정식 명칭, 쇼핑몰에서 고를 때 쓸 구매 기준을 만든다.
LLM이 없거나 실패하거나 후보 밖을 고르면 None을 돌려 규칙 선택을 그대로 쓴다.
"""

from __future__ import annotations

import json

from . import llm
from .extract import redact

SYSTEM = """너는 바쁜 자녀를 대신해 엄마의 부탁 물건을 고르는 쇼핑 비서다.
엄마가 통화에서 한 말, 자녀가 등록한 엄마 정보(나이·건강·선호), 지난 구매 이력, 예산을 보고
후보 상품 중 엄마에게 가장 맞는 하나를 고른다.
규칙:
- 반드시 후보 목록의 product_id 중 하나를 고른다. 후보 밖 상품을 지어내지 않는다.
- 늘 쓰던 상품이 있으면 우선한다. 엄마가 "알아서" 골라 달라고 하면 건강 정보와 평점을 근거로 고른다.
- 예산(max_amount)을 넘지 않는 상품을 우선한다.
- reason은 엄마 상황을 반영한 한 문장(40자 안팎, 자녀에게 하는 말투).
  근거는 후보에 주어진 사실(이름·가격·평점·배송)과 엄마 정보만 쓴다. 섭취 횟수·성분·효능처럼 주어지지 않은 정보는 지어내지 않는다.
- search_keyword는 쇼핑몰 검색용 정식 명칭(오타·발음 교정, 예: 콘도로이친 → 콘드로이친).
- buy_criteria는 다른 쇼핑몰에서 직접 고를 때 쓸 기준 2~3개(짧은 구, 예산·평점·배송처럼 확인 가능한 것).
JSON만 출력:
{"product_id": "...", "reason": "...", "search_keyword": "...", "buy_criteria": ["...", "..."]}"""


def choose(req: dict, candidates: list[dict], notes: str, history: list[dict], max_amount: int) -> dict | None:
    if not candidates:
        return None
    cands = [{"product_id": c["id"], "name": c["name"], "price": c["price"], "rating": c["rating"],
              "delivery": c["delivery"], "category": c["category"]} for c in candidates[:6]]
    prompt = json.dumps({
        "mom_request": {"item": req.get("item"), "detail": req.get("detail", ""),
                        "quantity": req.get("quantity", 1), "mom_said": redact(req.get("evidence", ""))},
        "mom_info": redact(notes),
        "purchase_history": [h.get("name") or h.get("product_id") for h in history],
        "max_amount": max_amount,
        "candidates": cands,
    }, ensure_ascii=False)
    out = llm.chat_json(SYSTEM, prompt)
    if not out or out.get("product_id") not in {c["id"] for c in candidates}:
        return None
    crit = out.get("buy_criteria") or []
    return {
        "product_id": out["product_id"],
        "reason": str(out.get("reason") or "").strip()[:80],
        "search_keyword": str(out.get("search_keyword") or req.get("item") or "").strip()[:40],
        "buy_criteria": [str(x).strip()[:40] for x in crit if str(x).strip()][:3],
        "engine": llm.provider_label(),
    }
