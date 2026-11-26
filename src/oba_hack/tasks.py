"""구매가 아닌 엄마 부탁(택시 · 숙소 · 병원 예약 · 배달 …)을 Vooy 커넥터에 맡길 '할 일'로 만든다.

서버는 판단과 승인만 하고 실행은 하지 않는다 (docs/VOOY_HANDOFF.md).
  통화 처리 → make_for_call: 부탁 문장마다 할 일(task)을 만들고 종류·추천 커넥터를 붙인다
  자녀 승인 → approve: 승인서(handoff)를 돌려준다. Vooy가 handoff.connector에 작업을 맡긴다
  Vooy 보고 → report: 결과를 저장하고 엄마께 문자
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from . import policy, store


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def won(n: int) -> str:
    return f"{n:,}원"


# 앞에서부터 먼저 맞는 종류로 본다 ("병원 갈 때 택시 불러줘"는 택시)
KINDS: list[tuple[str, re.Pattern]] = [
    ("taxi", re.compile(r"택시|태워|데려다|콜택시|차 좀 불러|차 불러")),
    ("stay", re.compile(r"숙소|호텔|펜션|묵을|1박|하룻밤|모텔|리조트|민박")),
    ("food", re.compile(r"배달|시켜\s*먹|치킨|짜장|피자|족발")),
    ("reservation", re.compile(r"병원|예약|접수|진료|미용실|식당|예매|신청")),
]

SPEC: dict[str, dict] = {
    "taxi": {
        "label": "택시", "emoji": "🚕", "connector": "swing_taxi", "service": "Swing Taxi", "fallback": ["kakaomap"],
        "action": "dispatch_taxi", "default_max": 30000, "moves_money": True,
        "steps": "estimate_fare로 요금을 먼저 확인하고, 사용자에게 요금·시각을 보여 주고 동의를 받은 뒤 dispatch_taxi로 부르세요. "
                 "부른 뒤 get_ride_status로 배차 결과를 확인하세요.",
        "ask": "Swing Taxi로 부를까요?",
    },
    "stay": {
        "label": "숙소", "emoji": "🏨", "connector": "nol", "service": "NOL 숙소", "fallback": ["goodchoice", "myrealtrip"],
        "action": "book_stay", "default_max": None, "moves_money": True,
        "steps": "조건에 맞는 숙소를 찾아 create-quote로 견적을 받고, 사용자에게 후보·금액을 보여 주고 동의를 받은 뒤 예약하세요.",
        "ask": "NOL 숙소에서 알아볼까요?",
    },
    "food": {
        "label": "배달", "emoji": "🍲", "connector": "yogiyo", "service": "요기요", "fallback": [],
        "action": "place_order", "default_max": 30000, "moves_money": True,
        "steps": "order-preview로 메뉴·금액을 확인하고, 사용자 동의를 받은 뒤 place-order로 주문하세요.",
        "ask": "요기요로 시킬까요?",
    },
    "reservation": {
        "label": "예약", "emoji": "📅", "connector": "kakaomap", "service": "카카오맵", "fallback": ["google_maps"],
        "action": "find_and_reserve", "default_max": 0, "moves_money": False,
        "steps": "카카오맵으로 엄마 집 근처 후보(이름·전화번호·영업시간·거리)를 2~3곳 찾아 사용자에게 보여 주세요. "
                 "전화 예약은 사용자가 고르고 '전화해서 예약해줘'라고 할 때만 하세요.",
        "ask": "카카오맵으로 엄마 집 근처를 찾아볼까요?",
    },
    "other": {
        "label": "부탁", "emoji": "📌", "connector": "", "service": "Vooy", "fallback": [],
        "action": "do_task", "default_max": 0, "moves_money": False,
        "steps": "Vooy가 할 수 있는 방법을 사용자에게 제안하고, 동의를 받은 뒤 처리하세요.",
        "ask": "처리할까요?",
    },
}


def kind_of(text: str) -> str:
    return next((k for k, pat in KINDS if pat.search(text)), "other")


def _address(fid: str | None, caller: str) -> str:
    fam = store.get("family", fid) if fid else None
    for p in (fam or {}).get("parents", []):
        if p["label"] == caller and p.get("address"):
            return p["address"]
    return ""


def make_for_call(call: dict, texts: list[str]) -> list[dict]:
    """통화의 '구매 외 부탁' 문장마다 할 일을 만든다. 재시작으로 다시 불려도 같은 문장은 한 번만 만든다."""
    have = {t["text"]: t for t in store.all_docs("task") if t.get("call_id") == call["id"]}
    out = []
    for text in dict.fromkeys(str(x).strip() for x in texts):
        if not text:
            continue
        if text in have:
            out.append(have[text])
            continue
        tid = f"t-{uuid.uuid4().hex[:8]}"
        caller = call.get("caller") or "엄마"
        out.append(store.put("task", tid, {
            "id": tid, "family_id": call.get("family_id"), "call_id": call["id"], "caller": caller,
            "text": text, "kind": kind_of(text), "status": "proposed", "created_at": now(),
            "call_date": (call.get("received_at") or now())[:10],
            "address": _address(call.get("family_id"), caller),
        }))
    return out


def line(t: dict) -> str:
    """정리본·목록에 넣는 한 줄: 무엇을, 어느 서비스로 할지 제안."""
    s = SPEC[t["kind"]]
    tail = {"proposed": s["ask"], "approved": f"{s['service']}에 맡김, 결과 대기", "done": "완료",
            "failed": "못 함", "skipped": "안 하기로 함"}.get(t["status"], "")
    return f"{s['emoji']} {t['text']} — {tail} (task_id {t['id']})"


def brief(t: dict) -> dict:
    s = SPEC[t["kind"]]
    return {"task_id": t["id"], "request": t["text"], "kind": t["kind"], "kind_label": s["label"],
            "status": t["status"], "suggested_connector": s["connector"], "suggested_service": s["service"],
            "result": t.get("result")}


def of_family(fid: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    return [t for t in store.all_docs("task") if t.get("family_id") == fid
            and (statuses is None or t["status"] in statuses)]


def find(ref: str, fid: str, statuses: tuple[str, ...] | None = None) -> dict | None:
    """task_id로 찾고, 없으면 부탁 문장·종류 이름 일부로 찾는다 (vooy가 '병원 예약'처럼 말로 넘겨도 되게)."""
    t = store.get("task", ref.strip())
    if t and t.get("family_id") == fid:
        return t if statuses is None or t["status"] in statuses else None
    key = ref.strip().lower()
    if not key:
        return None
    for t in of_family(fid, statuses):
        s = SPEC[t["kind"]]
        if key in t["text"].lower() or key in (s["label"], t["kind"], s["service"].lower()):
            return t
    return None


def _max_amount(t: dict) -> int:
    s = SPEC[t["kind"]]
    if s["default_max"] is None:
        return int(policy.get(t["family_id"])["max_per_order"])
    return int(s["default_max"])


def approve(t: dict) -> dict:
    """자녀가 승인한 할 일의 승인서(handoff). Vooy는 message를 보고 handoff.connector에 작업을 맡긴다."""
    s = SPEC[t["kind"]]
    t.update(status="approved", approved_at=now())
    store.put("task", t["id"], t)
    store.log("push_to_child", t["id"], f"{s['emoji']} ‘{t['text']}’ 승인했어요. Vooy가 {s['service']}로 진행해요.",
              family_id=t["family_id"])
    params = {"request": t["text"], "who": t["caller"], "said_on": t["call_date"]}
    addr = t.get("address") or ""
    if t["kind"] == "taxi":
        params |= {"from": addr, "to": "", "when": "", "rider": t["caller"]}
    elif t["kind"] in ("food",):
        params |= {"deliver_to": addr}
    elif t["kind"] in ("reservation", "other"):
        params |= {"near": addr}
    elif t["kind"] == "stay":
        params |= {"where": "", "check_in": "", "nights": 1, "guests": 1}
    cap = _max_amount(t)
    limits = {"max_amount": cap, "confirm_with_user": s["moves_money"] or t["kind"] == "reservation"}
    handoff = {
        "kind": t["kind"], "connector": s["connector"], "service": s["service"], "fallback": s["fallback"],
        "action": s["action"], "params": params, "limits": limits, "mom_said": t["text"],
        "after": "report_task(task_id, success, provider, amount, reference, details, message_to_mom)",
    }
    blanks = [k for k, v in params.items() if v in ("", None)]
    msg = (f"{s['service']}에 작업을 맡겨 ‘{t['text']}’을(를) 처리하세요. {s['steps']} "
           + (f"{t['caller']} 집 주소는 {addr}이에요. " if addr else f"{t['caller']} 주소가 등록돼 있지 않아요. 필요하면 사용자에게 물어 setup_parent(address=...)로 저장하세요. ")
           + (f"통화 날짜는 {t['call_date']}이니 '다음 주 화요일' 같은 말은 이 날짜 기준으로 계산하세요. ")
           + (f"handoff.params의 빈 칸({', '.join(blanks)})은 엄마 부탁 문장에서 읽어 채우고, 문장에 없으면 사용자에게 물어보세요. "
              if blanks else "")
           + (f"금액이 {won(cap)}을 넘으면 진행하지 말고 먼저 물어보세요. " if cap else "")
           + (f"{s['service']}가 연결돼 있지 않으면 {', '.join(s['fallback'])}로 하거나 사용자에게 연결을 안내하세요. " if s["fallback"] else "")
           + f"끝나면 report_task(task_id=\"{t['id']}\", success, provider, amount, reference, details, message_to_mom)로 결과를 알려 주세요. "
           "message_to_mom에는 엄마께 보낼 문자를 자식 말투로 1~2문장 적어 주세요.")
    return {"ok": True, "task": brief(t), "handoff": handoff, "message": msg}


def report(t: dict, success: bool, provider: str = "", amount: int = 0, reference: str = "",
           details: str = "", message_to_mom: str = "") -> dict:
    """Vooy가 커넥터로 처리한 결과. 성공이면 엄마께 문자를 보낸다."""
    s = SPEC[t["kind"]]
    t["result"] = {"success": bool(success), "provider": provider or s["service"], "amount": int(amount or 0),
                   "reference": reference, "details": details, "reported_at": now()}
    t["status"] = "done" if success else "failed"
    store.put("task", t["id"], t)
    fid = t["family_id"]
    if success:
        cost = f" {won(int(amount))}" if amount else ""
        store.log("push_to_child", t["id"],
                  f"✅ {provider or s['service']}로 ‘{t['text']}’ 처리했어요{cost}" + (f" ({reference})" if reference else "")
                  + (f": {details}" if details else ""), family_id=fid)
        sms = (message_to_mom or "").strip() or f"{t['caller']}, 말씀하신 {s['label']} 처리해 뒀어요." + (f" {details}" if details else "")
        store.log("sms_to_mom", t["id"], sms, family_id=fid)
        return {"ok": True, "task": brief(t), "message_to_mom": sms,
                "message": f"결과를 기록하고 {t['caller']}께 ‘{sms}’ 문자를 보냈어요."}
    store.log("push_to_child", t["id"], f"❌ ‘{t['text']}’ 처리 못 함: {details or '사유 없음'}", family_id=fid)
    return {"ok": True, "task": brief(t), "message": "못 한 것으로 기록했어요. 엄마께는 문자를 보내지 않았어요."}


def skip(t: dict) -> dict:
    t["status"] = "skipped"
    store.put("task", t["id"], t)
    return {"ok": True, "task": brief(t), "message": f"‘{t['text']}’은(는) 하지 않을게요."}


def why_not(t: dict) -> str:
    return {"done": "이미 처리를 마친 부탁이에요" + (f" ({t['result'].get('reference')})" if (t.get("result") or {}).get("reference") else "") + ".",
            "skipped": "하지 않기로 한 부탁이에요.",
            "approved": "이미 승인했어요. Vooy가 처리한 뒤 report_task로 결과를 알려 주세요.",
            "failed": "지난번에 처리하지 못한 부탁이에요."}.get(t["status"], "")

