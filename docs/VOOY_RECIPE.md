# vooy 연결과 레시피 — vooy 팀 전달용

vooy(자녀의 메신저 에이전트)에 이 원격 MCP 서버를 연결하면, 자녀는 vooy 대화로 엄마 정보를 등록하고,
엄마와 통화가 끝날 때마다 정리본을 먼저 받고, "응 결제해" 한마디로 vooy가 결제를 실행합니다. 결제는 모두 **테스트 결제**입니다.

- 체험: https://dont-call-mama-vooy.fly.dev (vooy 팀 전용 서버) — MCP 주소·레시피·도구 목록·실시간 서버 기록
- 소스: https://github.com/whose-shoes/oba-hack — MCP 서버는 `src/oba_hack/mcp_server.py`
- 전송: Streamable HTTP, stateless, JSON 응답 (MCP Python SDK 2.x)

## 1. 5분 체험 순서

1. 첫 화면에서 **체험 계정 만들기** → 개인 MCP 주소를 vooy **서비스 연결**에 넣습니다.
   (또는 공용 주소를 넣고 vooy에서 "엄마 등록해줘. 엄마 번호는 …, 내 번호는 …"이라고 말해 가입)
2. **Recipe Studio**에 [레시피](../src/oba_hack/static/vooy-recipe.txt)를 붙여 넣습니다.
3. vooy에게 차례로 말해 봅니다 — 첫 화면의 **내 계정에서 일어나는 일**에 서버 처리가 실시간으로 보입니다.

| 말 | vooy가 부르는 도구 | 보이는 것 |
| --- | --- | --- |
| "엄마는 서울 마포구 월드컵로 1에 사셔. 무릎이 안 좋으셔" | `setup_parent` ×2 | 배송지·메모 저장 |
| "5분마다 엄마 부탁 확인해줘" | `setup_auto_check` → vooy 반복 작업 | 대시보드에 vooy 폴링 기록 |
| "데모 통화 하나 만들어줘" | `demo_parent_call` → `get_new_parent_requests` | 정리본: 🛒 휴지·계란, 🟡 전기요, 📌 병원 예약 |
| (🛒 자동결제 범위) | `approve_order` → `checkout_order` | 묻지 않고 1회용 토큰으로 테스트몰 결제, 엄마 문자 |
| "응 전기요도 사줘" | `approve_order` → `checkout_order(message_to_mom)` | 자식 말투 문자가 엄마께 감 |
| "사칭 전화 상황도 보여줘. 그래도 사줘" | `demo_parent_call` → `approve_order` | 🚫 서버가 결제 거절 |
| "앞으로 5만원까지는 그냥 사줘" | `update_delegation` | 자동결제 한도 50,000원 |
| "휴지 주문 취소해줘" | `cancel_order` | 테스트 환불 + 엄마 안내 문자 |

## 2. 연결 — 사용자마다 개인 주소

vooy는 MCP 요청에 사용자 정보를 보내지 않으므로, **사용자마다 다른 MCP 주소**로 계정을 구분합니다.

| 주소 | 용도 |
| --- | --- |
| `https://<서버>/mcp/<OBA_MCP_TOKEN>` | **공용(가입용)** — 엄마 번호와 내 번호를 말하면 계정을 만들고 개인 주소를 돌려줌. 다른 도구는 "개인 주소로 바꿔 주세요"만 답함 |
| `https://<서버>/mcp/<계정 토큰>` | **개인** — 그 사용자의 통화·주문·정리본·위임 범위만 다룸. 모르는 토큰은 404 |

- 이미 가입된 번호로 공용 주소에서 다시 가입하면, 남의 주소가 새지 않도록 개인 주소를 알려 주지 않습니다.
- vooy로 가입한 계정은 **vooy가 결제를 실행**합니다(아래 4). 대시보드에서 가입한 계정은 기본으로 서버가 테스트 결제합니다.

## 3. 도구

| 도구 | 언제 | 힌트 |
| --- | --- | --- |
| `get_family_status` | 처음 연결, "연동 됐어?" | 읽기 전용 |
| `setup_parent` | "엄마 번호는 …, 내 번호는 …", "엄마 무릎이 안 좋으셔" (말로 등록·수정) | 변경 |
| `list_parent_requests` | "엄마 부탁 뭐 있어?" | 읽기 전용 |
| `get_new_parent_requests` | 5분 반복 작업 · "통화 저장됐어" — 새 정리본을 한 번씩만 가져옴 | 변경(전달 표시) |
| `setup_auto_check` | "5분마다 엄마 부탁 확인해줘" — 반복 작업 문구를 돌려줌 | 변경 |
| `approve_order` | "응 결제해" — 구매 승인(위임 증서 + 1회용 결제 토큰) | **결제 — 사용자 확인 필수 (🛒 자동결제 범위는 예외)** |
| `checkout_order` | 승인 직후 — 토큰으로 테스트몰 결제, `message_to_mom` | **결제** |
| `report_purchase` | 외부 쇼핑몰 모드에서 vooy가 산 결과 보고 | 변경 |
| `reject_order` | "그건 빼줘" | 변경 |
| `cancel_order` | "그거 취소해줘" — 테스트 환불 | **환불 — 사용자 확인 필수** |
| `update_delegation` | "자동결제 한도 5만원으로" | 변경 |
| `demo_parent_call` | 체험 서버 전용 — "데모 통화 만들어줘" | 변경 |

차단된 주문(상품권, 사칭 의심)은 vooy가 `approve_order`를 불러도 서버가 거절하고, 이유와 함께 "엄마께 직접 확인해 보세요"라고 답합니다.

## 4. 결제 — 위임 증서와 1회용 토큰

vooy가 결제하는 계정에서 `approve_order`는 결제하지 않고 **구매 승인**을 돌려줍니다.

```json
{
  "ok": true,
  "purchase": {"order_id": "o-048f3764", "mandate_id": "m-…", "product": "한일 전기요 싱글 (탄소매트)",
               "quantity": 1, "max_amount": 59000, "ship_to": "서울 마포구 월드컵로 1", "recipient": "엄마"},
  "checkout": {"merchant": "don't call Mama 테스트몰", "tool": "checkout_order", "payment_token": "ptok_…",
               "expires_at": "…", "payment_method": "가족 공용 카드 (테스트) VISA •••• 4242", "max_amount": 59000, "single_use": true},
  "next_action": {"tool": "checkout_order", "order_id": "o-048f3764", "payment_token": "ptok_…"},
  "message": "구매 승인: … 이제 checkout_order(…)로 테스트몰 결제를 마치세요 — 카드번호 없이, 이 주문·이 금액에 30분 동안 한 번만 쓰는 토큰이에요. …"
}
```

- 토큰은 **이 주문 · 이 금액 이하 · 30분 · 1회**만 유효합니다. 틀리거나 다시 쓰면 거절합니다.
- `checkout_order(order_id, payment_token, message_to_mom)` — 엄마 문자는 vooy가 자식 말투로 씁니다. 비우면 서버 기본 문구.
- **외부 쇼핑몰 모드**(대시보드 'vooy가 실제 쇼핑몰에서 구매', 기본 꺼짐): `approve_order`가 네이버 스마트스토어·11번가 구매 기준(`handoff`)을 주고, vooy가 산 뒤 `report_purchase`로 알립니다. 실제 결제가 될 수 있어 체험 서버 기본값은 테스트몰입니다.

## 5. 레시피

전문: [`src/oba_hack/static/vooy-recipe.txt`](../src/oba_hack/static/vooy-recipe.txt) (첫 화면에서 복사 가능). 요점:

- 처음엔 `get_family_status` → 등록 전이면 번호부터 묻고 `setup_parent`. 선호·건강은 그때그때 note로.
- 정리본의 `message`는 그대로 보여 주고, 🟡는 상품·수량·금액·엄마 말을 보여 주며 "결제할까요?"
- 🛒는 묻지 않고 `approve_order`, 🟡는 분명한 동의 뒤에만. `next_action`이 `checkout_order`면 바로 결제를 마침.
- 🚫는 결제를 제안하지 않고 엄마께 직접 확인하라고 권함.
- 반복 작업 실행 때 `has_new`가 false면 아무 말도 하지 않음.

## 6. 정리본을 vooy로 먼저 보내는 두 경로

| | 웹훅 (우리 → vooy) | 반복 작업 (vooy → 우리) |
| --- | --- | --- |
| 설정 | 대시보드 'vooy 전달'에 주소·토큰·사용자 ID·서명 비밀 (계정별) | vooy 반복 작업 5분 `get_new_parent_requests`, 또는 HTTP 크론 `GET /v1/vooy/digests` |
| 시점 | 통화 처리 직후 즉시 | 주기마다 |
| 실패 시 | 15·30·60·120초 백오프 재시도 (최대 5회) | 다음 주기에 가져감 |

두 경로가 '전달됨' 표시를 공유해 **같이 켜도 같은 정리본이 두 번 가지 않습니다.**

```http
POST <vooy 웹훅 주소>
Authorization: Bearer <토큰>
X-Oba-Event: digest.created
X-Oba-Delivery: 20261117-141032-ab12          # 정리본 ID — 같은 값이 다시 오면 중복
X-Oba-Timestamp: 1794031010
X-Oba-Signature: sha256=<HMAC-SHA256(서명 비밀, "<timestamp>." + 본문)>

{"event": "digest.created", "user_id": "<vooy 사용자 ID>",
 "text": "엄마와 통화에서 부탁 4건을 정리했어요.\n🛒 …\n🟡 …\n결제할까요?",
 "data": {"digest_id": "…", "parent": "엄마", "created_at": "…",
          "items": [{"order_id": "o-…", "status": "awaiting_approval", "item": "전기요", "product": "한일 전기요 싱글 (탄소매트)",
                     "quantity": 1, "total": 59000, "mom_said": "…", "reason": "처음 사는 상품이라 확인이 필요해요.", "alternatives": []}],
          "other_tasks": ["다음 주 화요일 병원 예약 알아보기"],
          "reply_tools": {"approve": "approve_order(order_id)", "reject": "reject_order(order_id)", "list": "list_parent_requests()"}}}
```

받는 쪽 서명 검증 (Python):

```python
expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(request.headers["X-Oba-Signature"], expected)
```

웹훅 주소는 공개 https만 받습니다(사설망·메타데이터 주소 차단). 대시보드의 **모의 vooy로 연결**은 이 서버의 수신함으로 보내 서명까지 실제로 검증해 보여 줍니다.
vooy가 다른 형식을 원하면 `src/oba_hack/notify.py`의 `_payload()`만 맞추면 됩니다.

## 7. 설계 원칙 (왜 좋은 vooy MCP인가)

1. **말로 찾히는 도구** — 설명에 사용자가 실제로 할 말("응 결제해", "그건 빼줘", "통화 저장됐어")을 넣어 vooy 도구 검색이 바로 맞는 도구로 잇습니다.
2. **마지막 안전장치는 서버** — 차단 주문은 누가 불러도 결제하지 않고, 결제·환불 도구에 `destructiveHint`, 조회 도구에 `readOnlyHint`를 답니다.
3. **위임 증서 + 1회용 토큰** — vooy는 카드번호 없이, 서버가 정한 범위(상품·수량·최대 금액·배송지) 안에서만 결제합니다.
4. **먼저 알려 주기** — 통화가 끝나면 서버가 정리본을 만들고, 웹훅·반복 작업 어느 쪽으로든 한 번만 갑니다. 새 것이 없으면 `has_new=false`로 조용히.
5. **사용자 분리** — 개인 주소로 계정을 가르고, 공용 주소는 가입만, 남의 주문은 건드릴 수 없습니다.
6. **대화로 등록·기억** — 번호·배송지·건강·늘 쓰는 물건을 말로 저장하고, 부탁 추출·상품 선택에 반영합니다.
7. **바로 보여 줄 문장 + 구조화 데이터** — 모든 응답에 `message`와 `next_action`/`need`를 담아, 실패해도 다음에 무엇을 할지 알려 줍니다.
8. **자식 말투 문자** — 결제 결과를 엄마께 알리는 문장은 vooy가 씁니다(`message_to_mom`). 비면 서버가 기본 문구로 보내 흐름이 멈추지 않습니다.
