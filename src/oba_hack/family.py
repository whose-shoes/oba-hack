"""가족 연동: 자식이 가입하고 부모님 번호를 등록·동의받는다.

처리 대상은 '등록되고 동의한 부모님 번호'와의 통화(또는 문자)뿐이다.
그 밖의 통화는 내용·번호를 저장하지 않고 버린다.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime

from . import policy, store
from .config import josa


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_phone(raw: str | None) -> str:
    """'+82 10-1234-5678', '010.1234.5678' → '01012345678'"""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("82") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def mask_phone(raw: str | None) -> str:
    d = normalize_phone(raw)
    return f"{d[:3]}-****-{d[-4:]}" if len(d) >= 9 else d


def families() -> list[dict]:
    return store.all_docs("family")


def current() -> dict | None:
    """데모 화면은 가족 하나를 다룬다 (가장 최근 가입)."""
    fs = families()
    return fs[0] if fs else None


def by_token(token: str) -> dict | None:
    for fam in families():
        if secrets.compare_digest(token.encode(), fam["upload_token"].encode()):
            return fam
    return None


def register(child_name: str, child_phone: str, parents: list[dict]) -> dict:
    child = normalize_phone(child_phone)
    if len(child) < 9:
        raise ValueError("내 번호를 확인해 주세요.")
    if not parents:
        raise ValueError("부모님을 한 분 이상 등록해 주세요.")
    ps = []
    for p in parents:
        num = normalize_phone(p.get("phone"))
        if len(num) < 9:
            raise ValueError("부모님 번호를 확인해 주세요.")
        if num == child:
            raise ValueError("부모님 번호가 내 번호와 같아요.")
        ps.append({
            "id": f"p-{uuid.uuid4().hex[:6]}",
            "label": (p.get("label") or "엄마").strip(),
            "name": (p.get("name") or "").strip(),
            "phone": num,
            "address": (p.get("address") or "").strip(),
            "birthday": (p.get("birthday") or "").strip(),
            "notes": [n.strip() for n in (p.get("notes") or []) if n and n.strip()],
            "consent": "pending",
            "consent_requested_at": now(),
        })
    name = child_name.strip() or "자식"
    fam = {
        "id": f"f-{uuid.uuid4().hex[:8]}",
        "child_name": name,
        "child_phone": child,
        "upload_token": secrets.token_urlsafe(24),
        "created_at": now(),
        "parents": ps,
    }
    store.put("family", fam["id"], fam)
    for p in ps:
        _ask_consent(fam, p)
    return fam


def _ask_consent(fam: dict, p: dict) -> None:
    name = fam["child_name"]
    store.log("sms_to_mom", fam["id"],
              f"{p['label']}, {josa(name, '이', '가')} 통화 속 부탁을 대신 주문해 드리는 ‘don't call me, Mama’에 "
              f"{p['label']} 번호를 등록했어요. 동의하시면 ‘예’라고 답장해 주세요.")
    store.log("push_to_child", fam["id"], f"{p['label']}({mask_phone(p['phone'])})께 동의 요청 문자를 보냈어요.")


def update(family_id: str, *, child_name: str | None = None, child_phone: str | None = None,
           parent_id: str | None = None, label: str | None = None, name: str | None = None,
           phone: str | None = None, address: str | None = None, birthday: str | None = None,
           note: str | None = None) -> tuple[dict, list[str]]:
    """말로 알려 준 정보를 덮어쓰거나 덧붙인다. (가족, 바뀐 항목 목록)"""
    fam = store.get("family", family_id)
    if fam is None:
        raise KeyError(family_id)
    changed: list[str] = []
    if child_name and child_name.strip() and child_name.strip() != fam["child_name"]:
        fam["child_name"] = child_name.strip(); changed.append("내 이름")
    if child_phone:
        num = normalize_phone(child_phone)
        if len(num) < 9:
            raise ValueError("내 번호를 확인해 주세요.")
        if num != fam["child_phone"]:
            fam["child_phone"] = num; changed.append("내 번호")
    p = next((x for x in fam["parents"] if x["id"] == parent_id), fam["parents"][0])
    for key, val, title in (("label", label, "호칭"), ("name", name, "이름"), ("address", address, "배송지"),
                            ("birthday", birthday, "생일")):
        if val and val.strip() and val.strip() != p.get(key, ""):
            p[key] = val.strip(); changed.append(title)
    new_phone = False
    if phone:
        num = normalize_phone(phone)
        if len(num) < 9:
            raise ValueError("부모님 번호를 확인해 주세요.")
        if num != p["phone"]:
            p.update(phone=num, consent="pending", consent_requested_at=now())
            changed.append("전화번호(동의 다시 요청)"); new_phone = True
    if note and note.strip():
        p.setdefault("notes", []).append(note.strip()); changed.append("메모")
    store.put("family", family_id, fam)
    if new_phone:
        _ask_consent(fam, p)
    return fam, changed


def summary(fam: dict | None) -> dict | None:
    """외부(vooy)에 돌려줄 요약: 토큰은 빼고 번호는 가린다."""
    if fam is None:
        return None
    return {
        "child_name": fam["child_name"],
        "child_phone": mask_phone(fam["child_phone"]),
        "parents": [{
            "label": p["label"], "name": p.get("name", ""), "phone": mask_phone(p["phone"]),
            "address": p.get("address", ""), "birthday": p.get("birthday", ""), "notes": p.get("notes", []),
            "consent": {"granted": "동의 완료", "pending": "동의 대기", "declined": "동의 안 함"}[p["consent"]],
        } for p in fam["parents"]],
    }


def notes_for(family_id: str | None) -> str:
    """추출 단계에 넘길 가족 메모 (부모님 정보·선호)."""
    fam = store.get("family", family_id) if family_id else current()
    if not fam:
        return ""
    lines = []
    for p in fam["parents"]:
        bits = [f"{p['label']}" + (f"({p['name']})" if p.get("name") else "")]
        if p.get("birthday"):
            bits.append(f"생일 {p['birthday']}")
        bits += p.get("notes", [])
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def consent(family_id: str, parent_id: str, granted: bool = True) -> dict:
    fam = store.get("family", family_id)
    if fam is None:
        raise KeyError(family_id)
    p = next((x for x in fam["parents"] if x["id"] == parent_id), None)
    if p is None:
        raise KeyError(parent_id)
    p["consent"] = "granted" if granted else "declined"
    p["consent_at"] = now()
    store.put("family", family_id, fam)
    if granted:
        pol = policy.get()
        if p["label"] not in pol["trusted_callers"]:
            policy.update({"trusted_callers": pol["trusted_callers"] + [p["label"]]})
        store.log("push_to_child", family_id,
                  f"✅ {josa(p['label'], '이', '가')} 동의했어요. 이제 {josa(p['label'], '과', '와')} 통화가 끝나면 알아서 처리해요.")
    else:
        store.log("push_to_child", family_id, f"{josa(p['label'], '이', '가')} 동의하지 않았어요. 통화는 처리하지 않아요.")
    return fam


def delete(family_id: str) -> None:
    with store._lock:
        db = store._db()
        db.execute("delete from docs where kind='family' and id=?", (family_id,))
        db.commit()


def match_call(number_a: str, number_b: str) -> tuple[dict, dict] | None:
    """통화 양쪽 번호 중 하나가 자식, 다른 하나가 동의한 부모님이면 (가족, 부모님). 방향은 상관없다."""
    a, b = normalize_phone(number_a), normalize_phone(number_b)
    for fam in families():
        for child, other in ((a, b), (b, a)):
            if child != fam["child_phone"]:
                continue
            for p in fam["parents"]:
                if p["phone"] == other and p["consent"] == "granted":
                    return fam, p
    return None


def match_sender(raw: str, fam: dict | None = None) -> tuple[dict, dict] | None:
    """단축어가 보낸 발신자(번호 또는 연락처 이름)가 동의한 부모님인지."""
    num = normalize_phone(raw)
    for f in [fam] if fam else families():
        for p in f["parents"]:
            if p["consent"] != "granted":
                continue
            if (num and p["phone"] == num) or (not num and raw.strip() == p["label"]):
                return f, p
    return None


def public(fam: dict | None) -> dict | None:
    """화면용: 번호는 가린다."""
    if fam is None:
        return None
    return {
        "id": fam["id"],
        "child_name": fam["child_name"],
        "child_phone": mask_phone(fam["child_phone"]),
        "upload_token": fam["upload_token"],
        "parents": [{**p, "phone": mask_phone(p["phone"])} for p in fam["parents"]],
    }
