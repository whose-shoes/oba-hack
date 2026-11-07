"""상품 조회와 선택. 실제 쇼핑몰 대신 모의 카탈로그를 쓴다 (과제 조건: 결제는 테스트)."""

from __future__ import annotations

import json
import re
from urllib.parse import quote
from functools import lru_cache
from pathlib import Path


@lru_cache
def _data() -> dict:
    return json.loads((Path(__file__).parent / "catalog.json").read_text())


def products() -> list[dict]:
    return _data()["products"]


def product(pid: str) -> dict | None:
    return next((p for p in products() if p["id"] == pid), None)


def history(caller: str) -> list[dict]:
    return [h for h in _data()["purchase_history"] if h["caller"] == caller]


def all_keywords() -> list[tuple[str, dict]]:
    """(키워드, 상품) 목록, 긴 키워드 우선 (‘주방세제’가 ‘세제’보다 먼저 잡히도록)."""
    pairs = [(k, p) for p in products() for k in p["keywords"]]
    return sorted(pairs, key=lambda kp: -len(kp[0]))


def search(item: str, detail: str = "", caller: str = "") -> list[dict]:
    """요청 품목에 맞는 후보를 점수순으로 돌려준다. 각 후보에 선택 근거(reasons)를 붙인다."""
    text = f"{item} {detail}".strip()
    bought = {h["product_id"]: h["date"] for h in history(caller)}
    numbers = set(re.findall(r"\d+", detail or ""))
    out = []
    for p in products():
        # 품목명과 맞는 키워드가 설명 문구(예: '무릎 관절에 좋은')보다 훨씬 중요하다
        item_hits = [k for k in p["keywords"] if item and (k in item or item in k)]
        detail_hits = [k for k in p["keywords"] if detail and k in detail and k not in item_hits]
        if not item_hits and not detail_hits and not (item and item in p["name"]):
            continue
        score = 25 * len(item_hits) + 4 * len(detail_hits) + (10 if item and item in p["name"] else 0)
        reasons = []
        if p["id"] in bought:
            score += 8
            reasons.append(f"지난번({bought[p['id']]})에 엄마가 받으신 상품")
        if numbers and any(n in re.findall(r"\d+", p["name"]) for n in numbers):
            score += 6
            reasons.append(f"말씀하신 규격({detail})과 일치")
        score += p["rating"]
        reasons.append(f"평점 {p['rating']} · {p['delivery']}")
        out.append({**p, "score": round(score, 2), "reasons": reasons})
    out.sort(key=lambda c: (-c["score"], c["price"]))
    if len(out) > 1 and len(out[0]["reasons"]) == 1:  # 이력·규격 근거가 없으면 '알아서 골라 달라'는 부탁에 맞춘 이유
        out[0]["reasons"].insert(0, "같은 품목 중 평점이 가장 높아요")
    return out


def naver_link(product: dict, keyword: str = "") -> tuple[str, str]:
    """(링크, 종류). 상품에 지정된 네이버 상품 링크가 있으면 그것, 없으면 네이버쇼핑 검색 링크."""
    if product.get("naver_url"):
        return product["naver_url"], "product"
    q = (keyword or (product.get("keywords") or [""])[0] or product.get("name", "")).strip()
    return f"https://search.shopping.naver.com/search/all?query={quote(q)}", "search"
