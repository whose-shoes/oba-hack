"""통화 기록 → '자식이 대신 해줘야 할 부탁' 구조화.

LLM(A.X K2 / Claude)이 있으면 쓰고, 없거나 실패하면 규칙 기반으로 추출한다.
모든 요청에는 통화 원문 근거(evidence)를 붙여, 자식이 왜 이 주문이 생겼는지 볼 수 있게 한다.
"""

from __future__ import annotations

import re

from . import catalog, llm

CHILD_LABELS = ("나", "딸", "아들", "자식", "본인", "me", "Me")
SCAM_PATTERNS = [
    "상품권", "기프트", "기프티콘", "송금", "계좌", "이체", "인증번호", "폰이 고장", "액정", "휴대폰이 고장", "급하게 돈",
]
INTENT = re.compile(r"사\s*줘|사다|사\s*줄|시켜|주문|떨어|다 썼|없어|없네|필요|보내\s*줘|사 와|사와|좀 사|챙겨|있으면 좋겠|갖고 싶|바꿔야")
QTY = re.compile(r"(\d+|한|두|세|네|다섯|여섯)\s*(개|봉|봉지|팩|박스|통|병|상자|판|세트|포대|묶음)")
SPEC = re.compile(r"\d+\s*(롤|구|kg|키로|킬로|L|리터|매|포|정|캡슐)")
KOR_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
TASK = re.compile(r"예약|신청|접수|알아봐|알아 봐|예매")

SYSTEM = """너는 '엄마가 자식에게 건 전화' 통화 기록을 읽고, 자식이 대신 처리해야 할 부탁을 뽑는 비서다.
규칙:
- 엄마(발신자)가 사 달라고 하거나, 떨어졌다/다 썼다/필요하다고 말한 물건만 구매 요청으로 뽑는다.
- 자식이 한 말, 이미 해결된 것, 단순 안부는 제외한다.
- evidence에는 근거가 된 엄마의 말을 원문 그대로 인용한다.
- 상품권·기프트카드·송금·계좌·인증번호 요구, '폰이 고장' 같은 사칭 정황은 scam_signals에 적는다.
- 구매가 아닌 부탁(병원 예약 등)은 other_tasks에 적는다.
- '참고 정보'(자녀가 등록한 엄마의 선호·건강 메모)에 늘 쓰는 브랜드·규격이 있으면 detail에 반영한다.
  단, 통화에서 부탁하지 않은 물건을 참고 정보만 보고 requests에 넣지 않는다.
JSON만 출력:
{"summary": "한 문장 요약",
 "requests": [{"item": "일반 품목명(예: 휴지)", "detail": "규격/브랜드/옵션 또는 빈 문자열",
               "quantity": 1, "budget_krw": null, "evidence": "원문 인용"}],
 "other_tasks": ["..."], "scam_signals": ["..."]}"""


def caller_lines(transcript: str) -> list[str]:
    """화자 표기가 있으면 자식 쪽 발화를 뺀다. 표기가 없으면 전체를 쓴다."""
    lines = [ln.strip() for ln in transcript.splitlines() if ln.strip()]
    labeled = [re.match(r"^\[?([^\]:：\d]{1,6})\]?\s*[:：]\s*(.*)$", ln) for ln in lines]
    if sum(1 for m in labeled if m) < max(1, len(lines) // 2):
        return lines
    return [m.group(2) for m in labeled if m and m.group(1).strip() not in CHILD_LABELS]


def _sentences(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        out += [s.strip() for s in re.split(r"(?<=[.!?。~])\s+", ln) if s.strip()]
    return out


def _kw_pattern(kw: str) -> re.Pattern:
    if len(kw) == 1:  # '김', '물' 같은 한 글자 키워드는 단어 경계 필요
        return re.compile(rf"(?:^|[\s,]){kw}(?=$|[\s,.]|이|을|도|좀|하고|은|는|랑|두|한|세)")
    return re.compile(re.escape(kw))


def rule_extract(transcript: str) -> dict:
    lines = caller_lines(transcript)
    sents = _sentences(lines)
    requests: list[dict] = []
    seen: dict[str, dict] = {}
    for i, s in enumerate(sents):
        ctx = s + " " + (sents[i + 1] if i + 1 < len(sents) else "")
        if not INTENT.search(ctx):
            continue
        rest = s
        for kw, prod in catalog.all_keywords():
            m = _kw_pattern(kw).search(rest)
            if not m:
                continue
            rest = rest[: m.start()] + " " * len(m.group(0)) + rest[m.end():]
            item = prod["keywords"][0]
            after = s[m.start(): m.end() + 14]
            q = QTY.search(after)
            spec = SPEC.search(after)
            qty = (int(q.group(1)) if q.group(1).isdigit() else KOR_NUM[q.group(1)]) if q else 1
            if item in seen:  # 같은 품목을 다시 말하며 수량·규격을 밝힌 경우 갱신
                prev = seen[item]
                if q:
                    prev["quantity"] = qty
                if spec:
                    prev["detail"] = spec.group(0)
                if (q or spec) and s not in prev["evidence"]:
                    prev["evidence"] += " " + s
                continue
            seen[item] = {
                "item": item, "detail": spec.group(0) if spec else "", "quantity": qty,
                "budget_krw": None, "evidence": s,
            }
            requests.append(seen[item])
    text = " ".join(lines)
    return {
        "summary": f"구매 부탁 {len(requests)}건" if requests else "구매 부탁 없음",
        "requests": requests,
        "other_tasks": [s for s in sents if TASK.search(s)],
        "scam_signals": [p for p in SCAM_PATTERNS if p in text],
    }


PHONE = re.compile(r"(?:\+?82[-\s.]?|0)1[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
LONG_NUMBER = re.compile(r"\d[\d\s-]{9,}\d")  # 계좌·카드 번호처럼 10자리 넘는 숫자열


def redact(text: str) -> str:
    """외부 LLM으로 보내기 전에 전화번호·계좌번호를 가린다 (A.X 요청은 운영 기록에 남을 수 있음)."""
    return LONG_NUMBER.sub("[번호]", PHONE.sub("[전화번호]", text))


def extract(transcript: str, context: str = "") -> tuple[dict, str]:
    """(추출 결과, 사용한 엔진 이름). context: 자녀가 등록한 가족 메모."""
    prompt = f"통화 기록:\n{redact(transcript)}"
    if context.strip():
        prompt = f"참고 정보(자녀가 등록한 가족 메모):\n{redact(context)}\n\n" + prompt
    result = llm.chat_json(SYSTEM, prompt)
    if result and isinstance(result.get("requests"), list):
        result.setdefault("other_tasks", [])
        result.setdefault("scam_signals", [])
        # LLM이 놓치더라도 사칭 정황은 규칙으로 한 번 더 잡는다
        rule_scam = [p for p in SCAM_PATTERNS if p in transcript]
        result["scam_signals"] = sorted(set(result["scam_signals"]) | set(rule_scam))
        for r in result["requests"]:
            r["quantity"] = int(r.get("quantity") or 1)
            r.setdefault("detail", "")
            # "엄마: ..." 처럼 화자 표기가 붙어 오면 떼어 낸다
            r["evidence"] = re.sub(r"^\s*[^:：\s]{1,6}\s*[:：]\s*", "", r.get("evidence") or "")
        return result, llm.provider_label()
    return rule_extract(transcript), "규칙 기반"
