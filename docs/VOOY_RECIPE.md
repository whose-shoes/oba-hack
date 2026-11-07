# vooy 연결과 레시피

vooy(자녀의 메신저 에이전트)에 우리 MCP 서버를 연결하면, 자녀는 vooy 대화로 엄마 정보를 등록하고
통화 정리본을 받아 "응 결제해"로 승인합니다. 결제는 모두 **테스트 결제**입니다.

## 1. 연결

1. 서버 실행: `set -a; source .env; set +a; uv run oba-hack`
2. 외부 https 주소 열기: `ngrok http 8000` (ngrok 3.28 이상 필요: `brew upgrade --cask ngrok`)
3. `.env`의 `OBA_PUBLIC_URL`에 ngrok 주소를 넣고 서버 재시작 → 대시보드 상단 "vooy 연결 (MCP)"에 전체 주소가 보입니다.
4. vooy **서비스 연결 → 사용자 지정 MCP**에 아래 주소를 넣습니다.

```
https://<공개 주소>/mcp/<OBA_MCP_TOKEN>
```

경로의 토큰이 접근 키 역할을 합니다. 주소를 외부에 공유하지 마세요.

## 2. 도구

| 도구 | 언제 | 확인 |
|---|---|---|
| `get_family_status` | 처음 연결, "연동 됐어?" | 조회 |
| `setup_parent` | "엄마 번호는 …, 내 번호는 …", "엄마 무릎이 안 좋으셔" (말로 등록·수정) | 등록 |
| `list_parent_requests` | "엄마 부탁 뭐 있어?" | 조회 |
| `get_new_parent_requests` | 크론잡: 새 통화 정리본을 한 번씩 가져옴 | 조회(전달 표시) |
| `approve_order` | "응 결제해", "전기요 사줘" | **결제 — 사용자 확인 필수** |
| `reject_order` | "그건 빼줘" | 변경 |
| `cancel_order` | "그거 취소해줘" | **환불 — 사용자 확인 필수** |
| `update_delegation` | "자동결제 한도 5만원으로" | 변경 |

차단된 주문(상품권, 사칭 의심)은 vooy가 `approve_order`를 불러도 서버가 거절합니다.

## 3. 레시피 지침 (Recipe Studio에 붙여 넣기)

```
너는 바쁜 자녀를 대신해 엄마(부모님)의 부탁을 챙기는 비서다. 오바란 도구를 쓴다.

[처음 연결]
- 먼저 get_family_status를 부른다. 등록 전이면 엄마 전화번호와 내 전화번호부터 묻고 setup_parent로 등록한다.
- 이어서 배송지, 엄마 성함, 생신, 늘 쓰시는 물건(브랜드·규격), 건강·알레르기를 편하게 물어보고,
  들은 내용은 그때그때 setup_parent로 저장한다(선호·건강은 note에 한 문장씩). 한 번에 다 묻지 않는다.
- 등록하면 엄마께 동의 문자가 간다. 동의 전에는 통화를 처리하지 않는다고 알려 준다.

[통화 정리본]
- 정리본(get_new_parent_requests 결과나 서버가 보낸 메시지)은 text를 그대로 보여 준다.
- 🟡 확인 필요 주문은 상품·수량·금액과 엄마가 한 말을 보여 주고 "결제할까요?"라고 묻는다.

[결제·취소]
- approve_order와 cancel_order는 사용자가 "응", "결제해", "취소해"처럼 분명히 동의한 뒤에만 부른다.
- "다른 거 없어?"라고 하면 list_parent_requests의 alternatives를 보여 주고, 고르면 product_id로 approve_order를 부른다.
- 🚫 결제 안 함 항목은 결제를 제안하지 않고, 엄마께 직접 전화해 확인해 보라고 권한다.
- 결과를 전할 때 엄마께 보낸 문자 내용도 알려 준다. 결제는 테스트 결제라고 밝힌다.

[맡기는 범위]
- "자동결제 한도 올려줘", "처음 사는 것도 알아서 사줘" 같은 말은 update_delegation으로 처리하고 바뀐 범위를 알려 준다.
```

## 4. 예시 대화

**처음 연결 (말로 엄마 정보 입력)**
> 자녀: 엄마 등록하고 싶어. 엄마 번호는 010-2345-6789, 내 번호는 010-1234-5678이야
> vooy: (setup_parent) 엄마 정보를 등록하고 엄마께 동의 요청 문자를 보냈어요. 엄마 배송지도 알려 주실래요?
> 자녀: 서울 마포구 월드컵로 1. 엄마는 무릎이 안 좋으시고 휴지는 늘 깨끗한나라 30롤 쓰셔
> vooy: (setup_parent ×2) 기억해 둘게요: 배송지, 메모. 엄마가 동의하시면 통화가 끝날 때마다 정리해 드릴게요.

**통화가 끝난 뒤 (서버가 먼저 보냄)**
> vooy: 엄마와 통화에서 부탁 4건을 정리했어요.
> ✅ 늘 사던 거라 주문했어요: 깨끗한나라 30롤 18,900원, 특란 30구 9,480원 (엄마께 문자 보냄)
> 🟡 확인 필요: 한일 전기요 59,000원 — 엄마 말씀 “요즘 밤에 너무 추워서 전기요 하나 있으면 좋겠어.”
> 📌 구매 외 부탁: 다음 주 화요일 병원 예약 알아보기
> 결제할까요?
> 자녀: 응 결제해
> vooy: (approve_order) 전기요 59,000원 테스트 결제 완료, 모레 도착. 엄마께 ‘주문했어요’ 문자를 보냈어요.

**취소·범위 변경**
> 자녀: 아 전기요 취소해줘 → (cancel_order) 취소하고 59,000원 환불했어요. 엄마께 안내 문자를 보냈어요.
> 자녀: 앞으로 5만원까지는 그냥 사줘 → (update_delegation) 자동결제 한도를 50,000원으로 바꿨어요.

## 5. 정리본을 vooy로 먼저 보내는 방법 — 웹훅과 크론 둘 다 지원

통화가 끝나면 서버가 정리본을 만듭니다. **웹훅과 크론을 함께 켜도 같은 정리본이 두 번 가지 않습니다**
(전달됨 표시를 공유). 웹훅이 실패한 정리본은 재시도되고, 그 사이 크론이 먼저 가져갈 수도 있습니다.

| | 웹훅 (우리 → vooy) | 크론 (vooy → 우리) |
|---|---|---|
| 설정 | 대시보드 "vooy로 먼저 보내기" 또는 `.env` | vooy 반복 작업 또는 HTTP 크론 |
| 시점 | 통화 처리 직후 즉시 | 크론 주기마다 |
| 실패 시 | 백오프 재시도 15·30·60·120초 (최대 5회), 보낼 함에 남음 | 다음 주기에 다시 가져감 |

### A. 웹훅

대시보드 **vooy로 먼저 보내기**에 vooy가 준 웹훅 주소·토큰·사용자 ID·서명 비밀을 넣고 저장합니다
(`.env`의 `OBA_VOOY_PUSH_URL`·`OBA_VOOY_PUSH_TOKEN`·`OBA_VOOY_USER_ID`·`OBA_VOOY_SIGNING_SECRET`도 기본값으로 쓰입니다).
켜는 순간 보낼 함에 쌓여 있던 정리본도 바로 나갑니다. **테스트 발송** 버튼으로 연결을 확인할 수 있습니다.

```http
POST <웹훅 주소>
Content-Type: application/json
Authorization: Bearer <토큰>                 # 토큰을 넣은 경우
X-Oba-Event: digest.created                  # 테스트 발송은 test
X-Oba-Delivery: 20261107-151932-ab12         # 정리본 ID — 같은 값이 다시 오면 중복이니 무시
X-Oba-Timestamp: 1794031010
X-Oba-Signature: sha256=<HMAC-SHA256(서명 비밀, "<timestamp>." + 본문)>   # 서명 비밀을 넣은 경우

{
  "event": "digest.created",
  "user_id": "<vooy 사용자 ID>",
  "text": "엄마와 통화에서 부탁 4건을 정리했어요.\n✅ …\n🟡 …\n결제할까요?",
  "data": {
    "digest_id": "20261107-151932-ab12", "parent": "엄마", "created_at": "2026-11-07T15:19:32",
    "items": [{"order_id": "o-1a2b3c4d", "status": "awaiting_approval", "item": "전기요",
               "product": "한일 전기요 싱글 (탄소매트)", "quantity": 1, "total": 59000,
               "mom_said": "요즘 밤에 너무 추워서 전기요 하나 있으면 좋겠어.", "reason": "처음 사는 상품이라 확인이 필요해요.",
               "alternatives": []}],
    "other_tasks": ["다음 주 화요일 병원 예약 알아보기"],
    "reply_tools": {"approve": "approve_order(order_id)", "reject": "reject_order(order_id)", "list": "list_parent_requests()"}
  }
}
```

2xx 응답이면 전달 완료, 그 외에는 재시도합니다. 받는 쪽 서명 검증 예 (Python):

```python
expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(request.headers["X-Oba-Signature"], expected)
```

vooy가 다른 형식을 원하면 `src/oba_hack/notify.py`의 `_payload()`만 맞추면 됩니다.

**데모용 모의 vooy:** 대시보드의 **모의 vooy로 연결**을 누르면 웹훅 주소가 이 서버의 `/mock/vooy/webhook`으로 설정됩니다.
모의 vooy는 토큰·서명을 실제로 검증하고, 도착한 메시지를 알림 피드에 "📱 vooy(모의)에 도착"으로 보여 줍니다.

### B. 크론

**vooy 반복 작업 (MCP 도구):** "5분마다 `get_new_parent_requests`를 실행하고, 새 정리본이 있으면 text를 그대로 나에게 보내고
결제할지 물어봐. 없으면 아무 말도 하지 마." 가져간 정리본은 전달됨으로 표시됩니다.

**HTTP 크론 (REST):** 가족 업로드 토큰(대시보드 "지금 바로 쓰는 아이폰 연결")으로 호출합니다.

```http
GET  /v1/vooy/digests                 # 새 정리본을 가져가며 전달됨 표시
GET  /v1/vooy/digests?mark=false      # 보기만 (전달 확인은 아래 ack로)
POST /v1/vooy/digests/ack             # {"ids": ["20261107-151932-ab12"]}
Authorization: Bearer <가족 업로드 토큰>
```
