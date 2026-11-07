"""상품 조회와 선택. 실제 쇼핑몰 대신 모의 카탈로그를 쓴다 (과제 조건: 결제는 테스트)."""

from __future__ import annotations

import json
import re
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
        hits = [k for k in p["keywords"] if k in text or (item and item in k)]
        if not hits and item and item not in p["name"]:
            continue
        score = 10 * len(hits) + (5 if item and item in p["name"] else 0)
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
    return sorted(out, key=lambda c: (-c["score"], c["price"]))
