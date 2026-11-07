"""vooy에 연결할 원격 MCP 서버 (Streamable HTTP).

vooy(자녀의 메신저 에이전트)가 이 도구들을 불러 엄마 정보를 말로 등록하고,
통화 정리본을 받아 보고, 결제·거절·취소를 처리한다. 결제는 모두 테스트 결제다.

vooy는 사용자 말투와 도구 설명을 맞춰 도구를 찾기 때문에(도구 검색), 설명에 실제로 할 법한 말을 넣었다.
위험한 실행(결제·취소)은 서버가 최종 판단한다: 차단 주문은 누가 불러도 결제하지 않는다.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from . import account, catalog, family, notify, pipeline, policy, store
from .config import josa

INSTRUCTIONS = """don't call me, Mama: 엄마(부모님)와 자녀가 통화를 마치면 엄마의 부탁을 정리해 주문까지 돕는 서비스입니다.
- 처음 연결했거나 상태가 궁금하면 get_family_status를 먼저 부르세요. 등록 전이면 사용자에게 엄마 호칭·엄마 전화번호·
  내(사용자) 전화번호·배송지·엄마의 선호/건강 메모를 대화로 물어 setup_parent로 등록하세요. 한 번에 다 묻지 말고 필요한 것부터.
- 공용(가입용) 주소로 가입하면 setup_parent가 사용자 전용 연결 주소(mcp_url)를 돌려줍니다. 사용자에게 그 주소를 보여 주고
  vooy 서비스 연결의 MCP 주소를 그 개인 주소로 바꾸라고 안내하세요. 개인 주소로 바꿔야 그 사용자 데이터만 다룹니다.
- 결제(approve_order)와 취소(cancel_order)는 반드시 상품·수량·금액을 보여 주고 사용자가 분명히 동의한 뒤에만 부르세요.
- 🚫(결제 안 함) 항목은 결제를 제안하지 말고 엄마께 직접 확인하라고 안내하세요.
- 결과를 전할 때 엄마께 보낸 문자 내용도 함께 알려 주세요. 결제는 모두 테스트 결제입니다.
- [자동 확인] 등록이 끝났거나 응답에 auto_check_needed=true가 있으면, vooy의 반복 작업(일정) 기능으로
  5분마다 get_new_parent_requests를 실행하는 반복 작업을 만드세요(setup_auto_check가 문구를 줍니다).
  반복 실행 때는 has_new가 true일 때만 사용자에게 말하고, false면 조용히 있으세요."""

server = MCPServer(name="dont-call-me-mama", title="don't call me, Mama · 엄마 부탁 대신 처리", instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
PAY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


def won(n: int | None) -> str:
    return f"{(n or 0):,}원"


SHARED_MSG = ("이 연결 주소는 가입용 공용 주소예요. 엄마 번호와 내 번호를 알려 주시면 계정을 만들고 개인 연결 주소를 드릴게요. "
              "이미 가입했다면 vooy 서비스 연결의 MCP 주소를 개인 주소로 바꿔 주세요.")


def _account(ctx: Context | None) -> dict | None:
    """이 요청이 어느 계정 것인지: 개인 주소(/mcp/<계정 토큰>)로 판별. 공용(가입용) 주소면 None."""
    fid = account.override.get()
    fam = family.get(fid) if fid else None
    if fam is None and not fid:
        req = None
        try:
            req = ctx.request_context.request if ctx is not None else None
        except Exception:
            req = None
        if req is not None:
            token = (getattr(req, "path_params", None) or {}).get("token") \
                or str(getattr(getattr(req, "url", None), "path", "")).rstrip("/").rsplit("/", 1)[-1]
            if token and token != MCP_TOKEN:
                fam = family.by_mcp_token(token)
    account.log_family.set(fam["id"] if fam else None)
    return fam


def _need_account() -> dict[str, Any]:
    return {"ok": False, "need_personal_url": True, "message": SHARED_MSG}


def _policy_brief(fid: str) -> dict:
    p = policy.get(fid)
    return {
        "자동결제_한도": won(p["auto_approve_limit"]),
        "처음_사는_상품도_자동결제": p["auto_approve_new_items"],
        "1회_최대": won(p["max_per_order"]),
        "월_예산": won(p["monthly_budget"]),
        "이번_달_사용": won(policy.spent_this_month(fid)),
        "차단_품목": p["blocked_categories"],
    }


AUTO_CHECK = {
    "interval_minutes": 5,
    "tool": "get_new_parent_requests",
    "instruction": ("5분마다 get_new_parent_requests를 실행해. has_new가 true면 message를 그대로 나에게 보내고, "
                    "확인이 필요한 주문은 결제할지 물어봐. has_new가 false면 아무 말도 하지 마."),
}


def _auto_check_on(fid: str) -> bool:
    """vooy가 최근 10분 안에 이 계정을 폴링했으면 자동 확인이 켜진 것으로 본다."""
    last = notify.poll_status(fid).get("last_polled_at")
    return bool(last) and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < 600


def _with_auto_check(out: dict[str, Any], fid: str) -> dict[str, Any]:
    if not _auto_check_on(fid):
        out["auto_check_needed"] = True
        out["next_action"] = ("vooy 반복 작업(일정) 기능으로 5분마다 get_new_parent_requests를 실행하는 반복 작업을 만들어 주세요. "
                              "그래야 엄마와 통화가 끝나면 5분 안에 자녀에게 정리본이 갑니다.")
        out["auto_check"] = AUTO_CHECK
    return out


def _find_order(ref: str, statuses: tuple[str, ...], fid: str) -> dict | None:
    """이 계정의 주문을 order_id로 찾고, 없으면 상품명·품목명 일부로 찾는다 (vooy가 이름으로 넘겨도 동작하도록)."""
    o = store.get("order", ref.strip())
    if o and o.get("family_id") == fid:
        return o
    key = ref.strip().lower()
    for o in store.all_docs("order"):
        if o.get("family_id") != fid or o["status"] not in statuses:
            continue
        names = [(o["request"].get("item") or ""), ((o.get("product") or {}).get("name") or "")]
        if any(key and (key in n.lower() or n.lower() in key) for n in names if n):
            return o
    return None


def _brief(o: dict) -> dict:
    return notify._item(o) | {"ship_to": o.get("ship_to", "")}


@server.tool(
    name="get_family_status",
    title="엄마 연결 상태 확인",
    description=(
        "엄마(부모님) 연결 상태와 등록된 정보, 자동결제 범위를 확인합니다. 처음 연결했을 때 가장 먼저 부르세요. "
        "예: '설정 확인해줘', '엄마 정보 뭐 등록돼 있어?', '연동 됐어?'"
    ),
    annotations=READ,
)
def get_family_status(ctx: Context) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return {
            "set_up": False, "shared_url": True,
            "message": "아직 엄마 정보가 없어요. 엄마 전화번호와 내 전화번호를 알려 주시면 연결할게요. "
                       "배송지, 엄마가 늘 쓰시는 물건, 건강 정보도 말로 알려 주시면 함께 기억해요.",
        }
    p = fam["parents"][0]
    msg = f"{josa(p['label'], '과', '와')} 연결돼 있어요. 통화가 끝나면 부탁을 정리해서 먼저 알려 드릴게요."
    return _with_auto_check({"set_up": True, "message": msg,
                             "auto_check_on": _auto_check_on(fam["id"]), "family": family.summary(fam),
                             "delegation": _policy_brief(fam["id"])}, fam["id"])


@server.tool(
    name="setup_parent",
    title="엄마 정보 말로 등록·수정",
    description=(
        "엄마(부모님) 기본 정보를 대화로 등록하거나 고칩니다. 사용자가 말한 내용에서 값을 뽑아 넣으세요. "
        "예: '우리 엄마 번호는 010-2345-6789고 내 번호는 010-1234-5678이야', '엄마는 서울 마포구 월드컵로 1에 사셔', "
        "'엄마 무릎이 안 좋으셔', '휴지는 늘 깨끗한나라 30롤 쓰셔', '엄마 성함은 김순자', '엄마 생신은 3월 5일'. "
        "처음 등록할 때는 parent_phone(엄마 번호)과 child_phone(사용자 번호)이 꼭 필요하고, 등록하면 바로 연결됩니다. "
        "그 뒤로는 알려 준 항목만 고치거나 note로 덧붙입니다. 선호·건강·알레르기·자주 사는 물건은 note에 한 문장으로 넣으세요."
    ),
    annotations=WRITE,
)
def setup_parent(
    parent_phone: str | None = None,
    child_phone: str | None = None,
    parent_label: str | None = None,
    parent_name: str | None = None,
    address: str | None = None,
    birthday: str | None = None,
    note: str | None = None,
    child_name: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    fam = _account(ctx)
    try:
        if fam is None:  # 공용(가입용) 주소 → 새 계정
            need = [n for n, v in (("엄마 전화번호", parent_phone), ("내 전화번호", child_phone)) if not v]
            if need:
                return {"ok": False, "need": need,
                        "message": f"처음 연결이라 {', '.join(need)}가 필요해요. 알려 주시면 바로 등록할게요."}
            if any(f["child_phone"] == family.normalize_phone(child_phone) for f in family.families()):
                return {"ok": False, "already_registered": True,
                        "message": "이미 가입된 번호예요. 가입할 때 받은 개인 연결 주소로 vooy를 연결해 주세요 "
                                   "(개인 대시보드에서도 확인할 수 있어요)."}
            fam = family.register(child_name or "자녀", child_phone, [{
                "label": parent_label or "엄마", "phone": parent_phone, "name": parent_name, "address": address,
                "birthday": birthday, "notes": [note] if note else [],
            }])
            account.log_family.set(fam["id"])
            label = fam["parents"][0]["label"]
            links = family.links(fam)
            return {"ok": True, "created": True, "family": family.summary(fam), **links,
                    "message": (f"{label} 정보를 등록하고 계정을 만들었어요. 이제 vooy 서비스 연결의 MCP 주소를 "
                                f"이 개인 주소로 바꿔 주세요: {links['mcp_url']} — 그 뒤로 {josa(label, '과', '와')} "
                                f"통화가 끝날 때마다 부탁을 정리해 드려요. 내 대시보드: {links['dashboard_url']}")}
        fam, changed = family.update(
            fam["id"], child_name=child_name, child_phone=child_phone, label=parent_label, name=parent_name,
            phone=parent_phone, address=address, birthday=birthday, note=note,
        )
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "created": False, "changed": changed, "family": family.summary(fam),
            "message": ("기억해 둘게요: " + ", ".join(changed)) if changed else "바뀐 내용이 없어요."}


@server.tool(
    name="list_parent_requests",
    title="엄마 부탁 목록",
    description=(
        "엄마가 통화에서 부탁한 일을 보여 줍니다: 확인이 필요한 주문(order_id 포함, 엄마가 한 말 인용), 이미 주문한 것, "
        "결제하지 않은 것(차단), 구매 외 부탁. 예: '엄마 부탁 뭐 있어?', '엄마랑 통화한 거 정리해줘', '통화 내용 뭐였어?', '결제할 거 있어?', "
        "'엄마 주문 어떻게 됐어?'. include_done=true면 지난 주문·취소 내역까지 보여 줍니다."
    ),
    annotations=READ,
)
def list_parent_requests(include_done: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    orders = [o for o in store.all_docs("order") if o.get("family_id") == fam["id"]]
    today = datetime.now().strftime("%Y-%m-%d")
    ask = [o for o in orders if o["status"] in ("awaiting_approval", "payment_failed", "not_found")]
    ready = [o for o in orders if o["status"] in ("ready_for_vooy", "authorized")]
    recent = [o for o in orders if include_done or o["created_at"].startswith(today)]
    paid = [o for o in recent if o["status"] == "paid"]
    blocked = [o for o in recent if o["status"] == "blocked"]
    done = [o for o in recent if o["status"] in ("cancelled", "rejected")] if include_done else []
    tasks = [t for c in store.all_docs("call") if c.get("family_id") == fam["id"]
             and (include_done or c["received_at"].startswith(today))
             for t in (c.get("extraction") or {}).get("other_tasks", [])]
    lines = []
    for o in ask:
        if o["status"] == "not_found":
            lines.append(f"🔎 ‘{o['request'].get('item')}’ 상품을 못 찾았어요. 어떤 걸 원하시는지 알려 주세요.")
        else:
            lines.append(f"🟡 {o['product']['name']} ×{o['quantity']} {won(o['total'])} — "
                         f"{o['caller']} 말씀 “{o['request'].get('evidence', '')}” ({o['decision_reason']})")
    lines += [f"🛒 바로 주문할 것: {o['product']['name']} {won(o['total'])}" + (" (승인됨, 결제 결과 대기)" if o["status"] == "authorized" else "")
              for o in ready]
    lines += [f"✅ 주문함: {o['product']['name']} {won(o['total'])}" for o in paid]
    lines += [f"🚫 결제 안 함: {o['product']['name']} — {o['decision_reason']}" for o in blocked]
    lines += [f"📌 구매 외 부탁: {t}" for t in tasks]
    return {
        "message": "\n".join(lines) if lines else "지금은 엄마 부탁이 없어요.",
        "needs_approval": [_brief(o) for o in ask],
        "ready_to_buy": [_brief(o) for o in ready],
        "ordered": [_brief(o) for o in paid],
        "blocked": [_brief(o) for o in blocked],
        "closed": [_brief(o) for o in done],
        "other_tasks": tasks,
    }


@server.tool(
    name="setup_auto_check",
    title="5분 자동 확인 켜기",
    description=(
        "통화가 끝나면 자동으로 알림을 받도록 5분 자동 확인을 켭니다. 예: '5분마다 엄마 부탁 확인해줘', '자동 확인 켜줘', "
        "'통화 끝나면 바로 알려줘'. 이 도구가 돌려주는 instruction 문구로 vooy의 반복 작업(일정)을 5분 주기로 만들어 "
        "get_new_parent_requests를 실행하세요. 이미 켜져 있으면 그렇다고 알려 줍니다."
    ),
    annotations=WRITE,
)
def setup_auto_check(ctx: Context) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    st = notify.poll_status(fam["id"])
    store.log("push_to_child", "auto-check", "⏱️ vooy에 5분 자동 확인(반복 작업) 설정을 요청했어요.")
    if _auto_check_on(fam["id"]):
        return {"ok": True, "already_on": True, "last_polled_at": st.get("last_polled_at"),
                "message": "이미 5분마다 확인하고 있어요."}
    return {"ok": True, "already_on": False, "auto_check": AUTO_CHECK,
            "message": "vooy 반복 작업으로 5분마다 get_new_parent_requests를 실행하도록 만들어 주세요. "
                       "첫 실행이 들어오면 자동 확인이 켜진 것으로 표시돼요."}


POLL_INTERVAL_SEC = 300


@server.tool(
    name="get_new_parent_requests",
    title="새 통화 정리본 가져오기 (5분 폴링)",
    description=(
        "vooy가 5분마다 폴링하는 도구입니다. 엄마와의 통화가 끝난 뒤 새로 정리된 통화 데이터(정리본)가 있으면 넘겨주고, "
        "넘겨준 건 전달됨으로 표시해 다음 폴링에는 다시 나오지 않습니다. "
        "has_new가 true면 message를 사용자에게 그대로 보내고 확인이 필요한 주문은 결제할지 물어보세요. "
        "has_new가 false면 사용자에게 아무것도 보내지 마세요. 사용자가 '통화 저장됐어', '엄마랑 통화 끝났어', "
        "'방금 엄마 전화 끊었어', '엄마한테 새로 온 부탁 있어?'라고 하면 바로 부르세요."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=False),
)
def get_new_parent_requests(ctx: Context) -> dict[str, Any]:
    fam = _account(ctx)
    base = {"polled_at": datetime.now().isoformat(timespec="seconds"), "poll_interval_sec": POLL_INTERVAL_SEC}
    if fam is None:  # 공용 주소로 폴링하면 조용히 (사용자에게 보낼 것 없음)
        return base | {"has_new": False, "count": 0, "notify_user": False, "message": "", "new": [],
                       "need_personal_url": True, "warning": SHARED_MSG}
    new = notify.take_pending("cron", fam["id"])  # 웹훅으로 이미 간 정리본은 제외, 가져간 건 전달됨 표시
    if not new:
        return base | {"has_new": False, "count": 0, "notify_user": False, "message": "", "new": []}
    return base | {
        "has_new": True,
        "count": len(new),
        "notify_user": True,
        "message": "\n\n".join(d["text"] for d in new),
        "new": [notify.public(d) for d in new],
    }


def _result(fn, ref: str, statuses: tuple[str, ...], fid: str, *args) -> dict[str, Any]:
    o = _find_order(ref, statuses, fid)
    if o is None:
        return {"ok": False, "message": f"‘{ref}’에 해당하는 주문을 찾지 못했어요. list_parent_requests로 확인해 주세요."}
    try:
        return {"ok": True, "order": _brief(fn(o["id"], *args))}
    except ValueError as e:
        return {"ok": False, "order": _brief(o), "message": f"처리할 수 없어요: {e} ({o.get('decision_reason', '')})"}


def _vooy_pays(fid: str) -> bool:
    return policy.get(fid).get("payment_executor") == "vooy"


@server.tool(
    name="approve_order",
    title="구매 승인 (결제 전 필수)",
    description=(
        "엄마 부탁 주문을 승인합니다. 사용자가 상품·수량·금액을 본 뒤 '응 결제해', '그거 사줘', '주문해'처럼 분명히 동의했을 때 부르세요. "
        "🛒(늘 사던 소액, 자동결제 범위) 주문은 확인 없이 바로 불러도 됩니다. order_id(또는 상품 이름)를 넣고, 대안 상품으로 바꾸려면 product_id를 넣으세요. "
        "승인되면 구매 정보(상품·수량·최대 금액·배송지)를 돌려줍니다. 그 범위 안에서 vooy가 직접 주문·결제하고, 끝나면 report_purchase로 결과를 알려 주세요. "
        "상품권·사칭 의심 등 차단된 주문은 서버가 승인하지 않습니다."
    ),
    annotations=PAY,
)
def approve_order(order_id: str, product_id: str = "", ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    fid = fam["id"]
    allowed = ("awaiting_approval", "payment_failed", "ready_for_vooy", "authorized")
    if product_id:
        swapped = _result(pipeline.swap, order_id, allowed, fid, product_id)
        if not swapped["ok"]:
            return swapped
        order_id = swapped["order"]["order_id"]
    if _vooy_pays(fid):
        r = _result(pipeline.authorize, order_id, allowed, fid)
        if r["ok"]:
            o = store.get("order", r["order"]["order_id"])
            r["purchase"] = {
                "order_id": o["id"], "mandate_id": o["mandate"]["id"], "product": o["product"]["name"],
                "search_keywords": f"{o['request'].get('item', '')} {o['request'].get('detail', '')}".strip(),
                "quantity": o["quantity"], "max_amount": o["total"], "ship_to": o.get("ship_to", ""),
                "recipient": o["caller"],
            }
            pol = policy.get(fid)
            card = pol["family_payment_method"]
            r["checkout"] = {
                "merchant": pol["test_merchant"], "tool": "checkout_order",
                "payment_token": o["mandate"]["payment_token"], "expires_at": o["mandate"]["expires_at"],
                "payment_method": f"{card['label']} {card['brand']} •••• {card['last4']}",
                "max_amount": o["mandate"]["max_amount"], "single_use": True,
            }
            ai = o.get("ai_pick") or {}
            item = ai.get("search_keyword") or (o["product"].get("keywords") or [None])[0] \
                or o["request"].get("item") or o["product"]["name"]
            link, link_type, shop = catalog.buy_link(o["product"], item)
            if link_type != "product":  # 기본: 링크 없이 vooy가 네이버 스마트스토어에서 직접 찾고 산다
                link, shop = "", "네이버 스마트스토어"
            r["handoff"] = {
                "buy_here": link, "link_type": link_type, "shop": shop,
                "product_on_link": o["product"].get("link_name", ""), "price_on_link": o["product"].get("link_price", 0),
                "what": item, "quantity": o["quantity"], "max_amount": o["mandate"]["max_amount"],
                "buy_criteria": ai.get("buy_criteria", []), "why": ai.get("reason", ""),
                "ship_to": o.get("ship_to", ""), "recipient": o["caller"],
                "where": shop, "reference_product": o["product"]["name"],
                "mom_said": o["request"].get("evidence", ""),
                "after": "report_purchase(order_id, success, merchant, paid_amount, order_number, product_name, message_to_mom)",
            }
            r["message"] = ((f"이 {shop} 상품 링크에서 1개 구매하세요(테스트 결제): {link} — " if link_type == "product" else "")
                            + f"구매 승인: {item} ×{o['quantity']}, 최대 {won(o['mandate']['max_amount'])}"
                            + (f", 배송지 {o['ship_to']}" if o.get("ship_to") else "")
                            + (f". 고를 때 기준: {', '.join(ai['buy_criteria'])}" if ai.get("buy_criteria") else "")
                            + (f". 위 {shop} 링크의 상품을 주문·결제하세요(테스트 결제). " if link_type == "product" else
                               f". vooy가 네이버 스마트스토어에서 '{item}'을(를) 찾아 최대 금액 이하의 평점 높은 상품을 골라 주문·결제하세요(테스트 결제). ")
                            + f"끝나면 report_purchase로 성공 여부, merchant(예: {shop if link_type == 'product' else '네이버 스마트스토어'}), "
                            "paid_amount, order_number, 산 상품명(product_name — 없으면 note에), 엄마께 보낼 문자(message_to_mom)를 알려 주세요. "
                            f"참고 상품: {o['product']['name']}. 우리 테스트몰로 결제하려면 checkout_order(order_id, payment_token)도 쓸 수 있어요.")
            if not o.get("ship_to"):
                r["missing"] = ["ship_to"]
                r["message"] += (f" 단, {o['caller']} 배송지가 등록돼 있지 않아요. 주문 전에 사용자에게 배송지를 물어 "
                                 "setup_parent(address=...)로 저장한 뒤 그 주소로 주문하세요.")
        return r
    r = _result(pipeline.approve, order_id, allowed, fid)
    if r["ok"]:
        o = store.get("order", r["order"]["order_id"])
        if o["status"] == "paid":
            r["message"] = (f"{o['product']['name']} ×{o['quantity']} {won(o['total'])} 테스트 결제 완료 "
                            f"({o['receipt']['method']}), {o['product']['delivery']}. {o['caller']}께 ‘주문했어요’ 문자를 보냈어요.")
        else:
            r.update(ok=False, message="모든 결제수단이 실패했어요: " + " / ".join(o["attempts"]))
    return r


@server.tool(
    name="checkout_order",
    title="테스트몰에서 결제",
    description=(
        "approve_order로 받은 1회용 payment_token으로 don't call me, Mama 테스트몰에서 주문·결제합니다. "
        "카드번호나 쇼핑몰 로그인 없이 토큰만 넣으면 됩니다. 결제가 끝나면 주문번호를 돌려주고 엄마께 '주문했어요' 문자가 갑니다."
    ),
    annotations=PAY,
)
def checkout_order(order_id: str, payment_token: str, ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    o = _find_order(order_id, ("authorized",), fam["id"])
    if o is None:
        return {"ok": False, "message": f"‘{order_id}’에 해당하는 승인된 주문이 없어요. 먼저 approve_order로 승인을 받아 주세요."}
    try:
        o = pipeline.checkout(o["id"], payment_token)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    rc = o["receipt"]
    return {"ok": True, "order": _brief(o), "order_number": rc["order_number"], "paid_amount": rc["amount"],
            "message": f"{o['product']['name']} {won(rc['amount'])} 결제 완료 (주문번호 {rc['order_number']}, {rc['method']}), "
                       f"{o['product']['delivery']}. {o['caller']}께 ‘주문했어요’ 문자를 보냈어요."}


@server.tool(
    name="report_purchase",
    title="vooy 주문·결제 결과 알리기",
    description=(
        "approve_order로 승인받은 주문을 vooy가 직접 주문·결제한 뒤 결과를 알려 줍니다. 성공했으면 엄마께 보낼 문자를 "
        "message_to_mom에 자식 말투로 적어 주세요(예: '엄마, 휴지 시켜놨어~ 내일 와!'). 서명/제목 없이 1~2문장으로, "
        "상품·배송 요점은 넣고 주문번호나 결제수단 같은 내부 정보는 빼세요. 비워 두면 서버가 기본 문구로 대신 보냅니다. "
        "success(성공 여부), merchant(쇼핑몰, 예: 네이버 스마트스토어), paid_amount(결제 금액, 원), order_number(주문번호), "
        "product_name(실제로 산 상품명 — 없으면 note에), product_url(상품 링크), 실패면 note(사유)를 넣으세요."
    ),
    annotations=WRITE,
)
def report_purchase(order_id: str, success: bool = True, merchant: str = "", paid_amount: int = 0,
                    order_number: str = "", note: str = "", message_to_mom: str = "",
                    product_name: str = "", product_url: str = "", ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    o = _find_order(order_id, ("authorized",), fam["id"])
    if o is None:
        return {"ok": False, "message": f"‘{order_id}’에 해당하는 승인된 주문이 없어요. 먼저 approve_order로 승인을 받아 주세요."}
    try:
        o = pipeline.record_purchase(o["id"], success, merchant, paid_amount, order_number, note, message_to_mom,
                                     product_name, product_url)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    if o["status"] == "paid":
        return {"ok": True, "order": _brief(o),
                "message": f"{o['product']['name']} {won(o['receipt']['amount'])} 주문·결제 완료를 기록했어요. {o['caller']}께 ‘주문했어요’ 문자를 보냈어요."}
    return {"ok": True, "order": _brief(o), "message": "결제 실패를 기록했어요. 다른 상품이나 결제수단으로 다시 시도할 수 있어요."}


@server.tool(
    name="reject_order",
    title="이번엔 안 사기",
    description="주문하지 않기로 합니다. 예: '그건 사지 마', '전기요는 빼줘', '다음에 할게', '그건 내가 직접 살게'.",
    annotations=WRITE,
)
def reject_order(order_id: str, ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    r = _result(pipeline.reject, order_id, ("awaiting_approval", "not_found", "payment_failed", "blocked"), fam["id"])
    if r["ok"]:
        r["message"] = f"‘{r['order']['item']}’은(는) 주문하지 않을게요."
    return r


@server.tool(
    name="cancel_order",
    title="결제 취소·환불",
    description=(
        "이미 결제한 주문을 취소하고 테스트 환불합니다. 예: '그거 취소해줘', '환불해줘', '휴지 주문 취소'. "
        "사용자 확인 후 부르세요. 엄마께 취소 안내 문자가 갑니다."
    ),
    annotations=PAY,
)
def cancel_order(order_id: str, ctx: Context | None = None) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    r = _result(pipeline.cancel, order_id, ("paid",), fam["id"])
    if r["ok"]:
        o = store.get("order", r["order"]["order_id"])
        r["message"] = f"{o['product']['name']} 주문을 취소하고 {won(o['refund']['amount'])} 환불했어요. {o['caller']}께 안내 문자를 보냈어요."
    return r


@server.tool(
    name="update_delegation",
    title="자동결제 범위 바꾸기",
    description=(
        "자녀가 맡기는 범위(위임 정책)를 바꿉니다. 예: '자동결제 한도 5만원으로 올려줘', '이번 달 예산 20만원으로', "
        "'처음 사는 것도 알아서 사줘', '한 번에 10만원 넘으면 꼭 물어봐'. 바꾼 뒤 현재 범위를 돌려줍니다."
    ),
    annotations=WRITE,
)
def update_delegation(
    auto_approve_limit: int | None = None,
    monthly_budget: int | None = None,
    max_per_order: int | None = None,
    auto_approve_new_items: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    fam = _account(ctx)
    if fam is None:
        return _need_account()
    changes = {k: v for k, v in {
        "auto_approve_limit": auto_approve_limit, "monthly_budget": monthly_budget,
        "max_per_order": max_per_order, "auto_approve_new_items": auto_approve_new_items,
    }.items() if v is not None}
    # vooy가 입력 형식을 단순화하면서 말하지 않은 항목을 0으로 채워 보낼 수 있다.
    # 예산·1회 최대 0원(이하)과 음수 한도는 '안 바꿈'으로 본다 (자동결제 한도 0원 = 자동결제 끄기는 허용).
    for k in ("monthly_budget", "max_per_order"):
        if k in changes and int(changes[k]) <= 0:
            changes.pop(k)
    if "auto_approve_limit" in changes and int(changes["auto_approve_limit"]) < 0:
        changes.pop("auto_approve_limit")
    for k in ("auto_approve_limit", "monthly_budget", "max_per_order"):
        if k in changes and int(changes[k]) > 5_000_000:
            return {"ok": False, "message": "금액은 500만원 이하로 알려 주세요."}
    if changes:
        policy.update(changes, fam["id"])
        store.log("push_to_child", "policy", "맡기는 범위를 바꿨어요: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    return {"ok": True, "delegation": _policy_brief(fam["id"]),
            "message": "맡기는 범위를 바꿨어요." if changes else "바꿀 값을 알려 주세요."}


# ---------- HTTP 연결 ----------

MCP_TOKEN = os.environ.get("OBA_MCP_TOKEN") or secrets.token_urlsafe(18)
if not os.environ.get("OBA_MCP_TOKEN"):
    print("[mcp] OBA_MCP_TOKEN이 없어 임시 경로를 만들었습니다 (재시작하면 바뀜).", flush=True)
MCP_PATH = f"/mcp/{MCP_TOKEN}"

# 경로의 토큰이 계정을 가린다: 공용(가입용) 토큰 또는 계정별 개인 토큰만 통과.
# 외부(ngrok 등) Host로 들어오므로 host는 0.0.0.0으로 둔다.
http_app = server.streamable_http_app(streamable_http_path="/mcp/{token}", stateless_http=True, json_response=True,
                                      host="0.0.0.0")


def _gate(inner):
    from starlette.responses import PlainTextResponse

    async def app(scope, receive, send):
        token = (scope.get("path_params") or {}).get("token") or scope.get("path", "").rstrip("/").rsplit("/", 1)[-1]
        if token != MCP_TOKEN and family.by_mcp_token(token) is None:
            await PlainTextResponse("not found", status_code=404)(scope, receive, send)
            return
        await inner(scope, receive, send)
    return app


for _r in http_app.routes:
    if getattr(_r, "path", "") == "/mcp/{token}":
        _r.app = _gate(_r.app)


def public_url() -> str:
    """가입용 공용 주소 (계정별 개인 주소는 family.links)."""
    base = os.environ.get("OBA_PUBLIC_URL", "").rstrip("/")
    return f"{base}{MCP_PATH}" if base else MCP_PATH
