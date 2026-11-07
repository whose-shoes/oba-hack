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

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import family, notify, pipeline, policy, store
from .config import josa

INSTRUCTIONS = """오바란: 엄마(부모님)와 자녀가 통화를 마치면 엄마의 부탁을 정리해 주문까지 돕는 서비스입니다.
- 처음 연결했거나 상태가 궁금하면 get_family_status를 먼저 부르세요. 등록 전이면 사용자에게 엄마 호칭·엄마 전화번호·
  내(사용자) 전화번호·배송지·엄마의 선호/건강 메모를 대화로 물어 setup_parent로 등록하세요. 한 번에 다 묻지 말고 필요한 것부터.
- 결제(approve_order)와 취소(cancel_order)는 반드시 상품·수량·금액을 보여 주고 사용자가 분명히 동의한 뒤에만 부르세요.
- 🚫(결제 안 함) 항목은 결제를 제안하지 말고 엄마께 직접 확인하라고 안내하세요.
- 결과를 전할 때 엄마께 보낸 문자 내용도 함께 알려 주세요. 결제는 모두 테스트 결제입니다."""

server = MCPServer(name="oba-family-errands", title="오바란 · 엄마 부탁 대신 처리", instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
PAY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


def won(n: int | None) -> str:
    return f"{(n or 0):,}원"


def _policy_brief() -> dict:
    p = policy.get()
    return {
        "자동결제_한도": won(p["auto_approve_limit"]),
        "처음_사는_상품도_자동결제": p["auto_approve_new_items"],
        "1회_최대": won(p["max_per_order"]),
        "월_예산": won(p["monthly_budget"]),
        "이번_달_사용": won(policy.spent_this_month()),
        "차단_품목": p["blocked_categories"],
    }


def _find_order(ref: str, statuses: tuple[str, ...]) -> dict | None:
    """order_id로 찾고, 없으면 상품명·품목명 일부로 찾는다 (vooy가 이름으로 넘겨도 동작하도록)."""
    o = store.get("order", ref.strip())
    if o:
        return o
    key = ref.strip().lower()
    for o in store.all_docs("order"):
        if o["status"] not in statuses:
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
        "예: '설정 확인해줘', '엄마 정보 뭐 등록돼 있어?', '연동 됐어?', '엄마 동의하셨어?'"
    ),
    annotations=READ,
)
def get_family_status() -> dict[str, Any]:
    fam = family.current()
    if fam is None:
        return {
            "set_up": False,
            "message": "아직 엄마 정보가 없어요. 엄마 전화번호와 내 전화번호를 알려 주시면 연결할게요. "
                       "배송지, 엄마가 늘 쓰시는 물건, 건강 정보도 말로 알려 주시면 함께 기억해요.",
        }
    p = fam["parents"][0]
    waiting = p["consent"] != "granted"
    msg = (f"{p['label']}께 동의 요청 문자를 보냈고 답장을 기다리는 중이에요. 동의하시면 통화가 끝날 때마다 자동으로 정리해 드려요."
           if waiting else f"{josa(p['label'], '과', '와')} 연결돼 있어요. 통화가 끝나면 부탁을 정리해서 먼저 알려 드릴게요.")
    return {"set_up": True, "consent_pending": waiting, "message": msg,
            "family": family.summary(fam), "delegation": _policy_brief()}


@server.tool(
    name="setup_parent",
    title="엄마 정보 말로 등록·수정",
    description=(
        "엄마(부모님) 기본 정보를 대화로 등록하거나 고칩니다. 사용자가 말한 내용에서 값을 뽑아 넣으세요. "
        "예: '우리 엄마 번호는 010-2345-6789고 내 번호는 010-1234-5678이야', '엄마는 서울 마포구 월드컵로 1에 사셔', "
        "'엄마 무릎이 안 좋으셔', '휴지는 늘 깨끗한나라 30롤 쓰셔', '엄마 성함은 김순자', '엄마 생신은 3월 5일'. "
        "처음 등록할 때는 parent_phone(엄마 번호)과 child_phone(사용자 번호)이 꼭 필요하고, 등록하면 엄마께 동의 요청 문자가 갑니다. "
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
) -> dict[str, Any]:
    fam = family.current()
    try:
        if fam is None:
            need = [n for n, v in (("엄마 전화번호", parent_phone), ("내 전화번호", child_phone)) if not v]
            if need:
                return {"ok": False, "need": need,
                        "message": f"처음 연결이라 {', '.join(need)}가 필요해요. 알려 주시면 바로 등록할게요."}
            fam = family.register(child_name or "자녀", child_phone, [{
                "label": parent_label or "엄마", "phone": parent_phone, "name": parent_name, "address": address,
                "birthday": birthday, "notes": [note] if note else [],
            }])
            label = fam["parents"][0]["label"]
            return {"ok": True, "created": True, "family": family.summary(fam),
                    "message": f"{label} 정보를 등록하고 {label}께 동의 요청 문자를 보냈어요. "
                               f"{label}께서 ‘예’라고 답장하시면 그때부터 통화가 끝날 때마다 부탁을 정리해 드려요."}
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
        "결제하지 않은 것(차단), 구매 외 부탁. 예: '엄마 부탁 뭐 있어?', '엄마랑 통화한 거 정리해줘', '결제할 거 있어?', "
        "'엄마 주문 어떻게 됐어?'. include_done=true면 지난 주문·취소 내역까지 보여 줍니다."
    ),
    annotations=READ,
)
def list_parent_requests(include_done: bool = False) -> dict[str, Any]:
    orders = store.all_docs("order")
    today = datetime.now().strftime("%Y-%m-%d")
    ask = [o for o in orders if o["status"] in ("awaiting_approval", "payment_failed", "not_found")]
    recent = [o for o in orders if include_done or o["created_at"].startswith(today)]
    paid = [o for o in recent if o["status"] == "paid"]
    blocked = [o for o in recent if o["status"] == "blocked"]
    done = [o for o in recent if o["status"] in ("cancelled", "rejected")] if include_done else []
    tasks = [t for c in store.all_docs("call") if (include_done or c["received_at"].startswith(today))
             for t in (c.get("extraction") or {}).get("other_tasks", [])]
    lines = []
    for o in ask:
        if o["status"] == "not_found":
            lines.append(f"🔎 ‘{o['request'].get('item')}’ 상품을 못 찾았어요. 어떤 걸 원하시는지 알려 주세요.")
        else:
            lines.append(f"🟡 {o['product']['name']} ×{o['quantity']} {won(o['total'])} — "
                         f"{o['caller']} 말씀 “{o['request'].get('evidence', '')}” ({o['decision_reason']})")
    lines += [f"✅ 주문함: {o['product']['name']} {won(o['total'])}" for o in paid]
    lines += [f"🚫 결제 안 함: {o['product']['name']} — {o['decision_reason']}" for o in blocked]
    lines += [f"📌 구매 외 부탁: {t}" for t in tasks]
    return {
        "message": "\n".join(lines) if lines else "지금은 엄마 부탁이 없어요.",
        "needs_approval": [_brief(o) for o in ask],
        "ordered": [_brief(o) for o in paid],
        "blocked": [_brief(o) for o in blocked],
        "closed": [_brief(o) for o in done],
        "other_tasks": tasks,
    }


@server.tool(
    name="get_new_parent_requests",
    title="새 통화 정리본 가져오기",
    description=(
        "엄마와의 통화가 끝난 뒤 새로 정리된 정리본을 한 번씩만 가져옵니다(가져가면 전달됨으로 표시). "
        "반복 작업으로 몇 분마다 확인하거나 '엄마한테 새로 온 부탁 있어?'에 쓰세요. "
        "새 정리본이 있으면 text를 사용자에게 그대로 전하고, 확인이 필요한 주문은 결제할지 물어보세요."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=False),
)
def get_new_parent_requests() -> dict[str, Any]:
    new = notify.take_pending("cron")  # 웹훅으로 이미 간 정리본은 제외, 가져간 건 전달됨 표시
    if not new:
        return {"new": [], "message": "새로 정리된 엄마 부탁이 없어요."}
    return {"new": [{"text": d["text"], "items": d["items"], "created_at": d["created_at"]} for d in new],
            "message": "\n\n".join(d["text"] for d in new)}


def _result(fn, ref: str, statuses: tuple[str, ...], *args) -> dict[str, Any]:
    o = _find_order(ref, statuses)
    if o is None:
        return {"ok": False, "message": f"‘{ref}’에 해당하는 주문을 찾지 못했어요. list_parent_requests로 확인해 주세요."}
    try:
        return {"ok": True, "order": _brief(fn(o["id"], *args))}
    except ValueError as e:
        return {"ok": False, "order": _brief(o), "message": f"처리할 수 없어요: {e} ({o.get('decision_reason', '')})"}


@server.tool(
    name="approve_order",
    title="주문 승인·결제",
    description=(
        "확인이 필요한 주문을 승인하고 테스트 결제합니다. 사용자가 상품·수량·금액을 본 뒤 '응 결제해', '그거 사줘', "
        "'주문해'처럼 분명히 동의했을 때만 부르세요. order_id(또는 상품 이름)를 넣고, 대안 상품으로 바꾸려면 product_id를 넣으세요. "
        "결제가 끝나면 엄마께 '주문했어요' 문자가 갑니다. 차단된 주문은 서버가 거절합니다."
    ),
    annotations=PAY,
)
def approve_order(order_id: str, product_id: str | None = None) -> dict[str, Any]:
    if product_id:
        swapped = _result(pipeline.swap, order_id, ("awaiting_approval", "payment_failed"), product_id)
        if not swapped["ok"]:
            return swapped
        order_id = swapped["order"]["order_id"]
    r = _result(pipeline.approve, order_id, ("awaiting_approval", "payment_failed"))
    if r["ok"]:
        o = store.get("order", r["order"]["order_id"])
        if o["status"] == "paid":
            r["message"] = (f"{o['product']['name']} ×{o['quantity']} {won(o['total'])} 테스트 결제 완료 "
                            f"({o['receipt']['method']}), {o['product']['delivery']}. {o['caller']}께 ‘주문했어요’ 문자를 보냈어요.")
        else:
            r.update(ok=False, message="모든 결제수단이 실패했어요: " + " / ".join(o["attempts"]))
    return r


@server.tool(
    name="reject_order",
    title="이번엔 안 사기",
    description="주문하지 않기로 합니다. 예: '그건 사지 마', '전기요는 빼줘', '다음에 할게', '그건 내가 직접 살게'.",
    annotations=WRITE,
)
def reject_order(order_id: str) -> dict[str, Any]:
    r = _result(pipeline.reject, order_id, ("awaiting_approval", "not_found", "payment_failed", "blocked"))
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
def cancel_order(order_id: str) -> dict[str, Any]:
    r = _result(pipeline.cancel, order_id, ("paid",))
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
) -> dict[str, Any]:
    changes = {k: v for k, v in {
        "auto_approve_limit": auto_approve_limit, "monthly_budget": monthly_budget,
        "max_per_order": max_per_order, "auto_approve_new_items": auto_approve_new_items,
    }.items() if v is not None}
    for k in ("auto_approve_limit", "monthly_budget", "max_per_order"):
        if k in changes and not (0 <= int(changes[k]) <= 5_000_000):
            return {"ok": False, "message": "금액은 0원에서 500만원 사이로 알려 주세요."}
    if changes:
        policy.update(changes)
        store.log("push_to_child", "policy", "맡기는 범위를 바꿨어요: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    return {"ok": True, "delegation": _policy_brief(),
            "message": "맡기는 범위를 바꿨어요." if changes else "바꿀 값을 알려 주세요."}


# ---------- HTTP 연결 ----------

MCP_TOKEN = os.environ.get("OBA_MCP_TOKEN") or secrets.token_urlsafe(18)
if not os.environ.get("OBA_MCP_TOKEN"):
    print("[mcp] OBA_MCP_TOKEN이 없어 임시 경로를 만들었습니다 (재시작하면 바뀜).", flush=True)
MCP_PATH = f"/mcp/{MCP_TOKEN}"

# 추측할 수 없는 경로가 접근 제어 역할을 한다. 외부(ngrok 등) Host로 들어오므로 host는 0.0.0.0으로 둔다.
http_app = server.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
                                      host="0.0.0.0")


def public_url() -> str:
    base = os.environ.get("OBA_PUBLIC_URL", "").rstrip("/")
    return f"{base}{MCP_PATH}" if base else MCP_PATH
