# 오바란 (oba-hack)

**엄마가 딸·아들에게 전화로 "그거 좀 사 줘" 하면, 통화가 끝난 뒤 에이전트가 자식 대신 찾아서 결제하고 엄마에게 알려 줍니다.**

OBA.RUN 2nd 해커톤 SKT Agentic Pay 트랙 출품작. 결제는 모두 **테스트 결제**이며 실제 금융 거래는 일어나지 않습니다.

## 흐름

```
[가입] 자식이 vooy에 말로 엄마 정보 입력(또는 대시보드) → 엄마에게 동의 문자 → 엄마 '예' → 연동 완료 (한 번만)

엄마 ⇄ 자식 통화 (SKT 에이닷 녹음·요약)
   │  ① 0탭: 통화 종료 시 에이닷이 웹훅 호출   POST /v1/telco/call-ended   (SKT 연동 가정, 데모는 시뮬레이터)
   │  ② 1탭: 에이닷 공유 → iOS 단축어        POST /v1/calls              (지금 바로 가능)
   │  ③ 0탭: 엄마 문자 → 단축어 메시지 자동화  POST /v1/calls (caller_number)
   ▼
 [필터] 등록·동의된 부모님 번호가 아니면 번호·내용을 저장하지 않고 폐기
 [맥락] 부탁 추출: 무엇·몇 개·근거 문장·사칭 정황      ← SKT A.X K2 → Claude → 규칙 기반
 [조회] 카탈로그 검색 + 엄마 구매 이력
 [선택] 늘 사던 상품·규격 일치·평점으로 고르고 이유 설명
 [위임] 정책: auto(바로 결제) / ask(자식 승인) / block(결제 안 함)
 [결제] 테스트 결제, 첫 수단 실패 시 다음 수단으로 복구
 [결과] 엄마에게 문자(모의), 취소·환불
 [vooy] 정리본을 자식의 vooy로 먼저 보냄(웹훅 또는 vooy 크론잡) → 자식이 "응 결제해" → vooy가 MCP approve_order 호출
```

**vooy 연결(MCP)과 레시피:** [docs/VOOY_RECIPE.md](docs/VOOY_RECIPE.md)

### 위임 정책 (자식이 대시보드에서 조절)

| 상황 | 결과 |
| --- | --- |
| 늘 사던 상품 + 자동결제 한도(기본 3만원) 이내 | 묻지 않고 결제 → 엄마에게 "주문했어요" |
| 처음 사는 상품, 한도 초과, 월 예산 초과, 엄마가 말한 예산 초과 | 자식에게 근거 문장과 함께 승인 요청 (상품 교체 가능) |
| 상품권·기프트카드, 사칭 정황("폰이 고장", "인증번호"), 등록 안 된 발신자 | 결제 차단 + 경고 |
| 등록·동의되지 않은 번호와의 통화 | 처리 대상 아님. 저장하지 않고 폐기 |
| 결제 실패 | 다음 결제수단 재시도, 모두 실패하면 자식에게 알림 |
| 원치 않은 결제 | 취소·환불 버튼 → 엄마에게 안내 문자 |

## 에이닷을 붙이는 방법

### ① SKT 연동 (0탭, 목표 구조)

에이닷은 SKT 망에서 통화를 녹음·요약합니다. 자식이 등록하고 엄마가 동의한 번호와의 통화가 끝나면, 에이닷이 아래 웹훅을 호출해 주면 됩니다. 다른 통화는 SKT 밖으로 나오지 않습니다. 공개 API가 아니라 SKT 협력이 필요하며, 해커톤에서는 대시보드 **통화 시뮬레이터**가 같은 처리 함수를 호출합니다.

```http
POST /v1/telco/call-ended
Authorization: Bearer <OBA_TELCO_TOKEN>
Content-Type: application/json

{
  "call_id": "skt-20261107-0001",       // 중복 전달 방지 (선택)
  "from_number": "010-2345-6789",       // 건 사람
  "to_number": "010-1234-5678",         // 받은 사람 (방향 무관: 엄마→자식, 자식→엄마 모두 처리)
  "ended_at": "2026-11-07T14:10:00+09:00",
  "summary": "휴지·계란 주문 부탁",       // 에이닷 요약 (선택)
  "transcript": "엄마: 휴지가 다 떨어졌어...\n나: 알았어"
}
```

응답 `202 {"accepted": true, "call_id": ...}` 또는 `202 {"accepted": false, "reason": "not a registered family call"}` (아무것도 저장하지 않음).

### ② 에이닷 공유 → 단축어 (1탭, 지금 가능)

1. **단축어** 앱 → `+` → 이름 `oba로 보내기`
2. ⓘ(세부사항) → **공유 시트에서 보기** 켜기 → 받는 유형: **파일, 텍스트, 미디어**
3. 동작 **URL 콘텐츠 가져오기**
   - URL `https://<서버 주소>/v1/calls`, 방법 `POST`
   - 헤더 `Authorization` = `Bearer <가족 업로드 토큰>` (대시보드 상단 "지금 바로 쓰는 아이폰 연결"에 표시) 또는 `OBA_UPLOAD_TOKEN`
   - 요청 본문 **양식**: 유형 **파일**, 키 `file`, 값 **단축어 입력**
4. 동작 **알림 보기** → `엄마 통화 전송 완료`

사용: 통화 종료 → 에이닷 통화기록 → 공유 → **oba로 보내기**. 에이닷 공유가 iOS 기본 공유 시트가 아니라 "복사"만 된다면, 복사 후 **클립보드 가져오기** → 같은 업로드를 하는 단축어로 바꿉니다.

### ③ 엄마 문자 → 단축어 메시지 자동화 (0탭, 기기 확인 필요)

1. 단축어 → **자동화** → `+` → **메시지** → 보낸 사람: **엄마** → **즉시 실행**
2. 동작 **URL 콘텐츠 가져오기**: URL `https://<서버 주소>/v1/calls`, `POST`, 헤더 `Authorization: Bearer <가족 업로드 토큰>`, `X-Source: sms-shortcut`
   - 양식: 텍스트 `text` = **단축어 입력**(메시지 내용), 텍스트 `caller_number` = 단축어 입력의 **보낸 사람**
3. 서버는 `caller_number`가 등록·동의된 부모님 번호(또는 연락처 이름)가 아니면 저장하지 않고 버립니다.

iOS 17부터 메시지 자동화를 "즉시 실행"할 수 있지만, 확인 창이 뜬다는 사례도 있어 실제 기기에서 확인해야 합니다.

## 실행

```bash
cp .env.example .env              # 토큰 바꾸기, A.X K2 키가 있으면 OBA_AX_API_KEY
set -a; source .env; set +a
uv run oba-hack                   # http://localhost:8000 대시보드
ngrok http 8000                   # 아이폰 단축어가 접근할 주소
uv run pytest                     # 테스트
```

키가 없어도 규칙 기반 추출로 끝까지 동작합니다. 분석 엔진은 대시보드 상단에 표시됩니다.

| 환경변수 | 설명 |
| --- | --- |
| `OBA_UPLOAD_TOKEN` | 단축어 업로드 공용 토큰 (가족별 토큰도 사용 가능) |
| `OBA_TELCO_TOKEN` | 통신사 웹훅 `/v1/telco/call-ended` 인증 토큰 |
| `OBA_AX_API_KEY` | SKT A.X K2 키 (있으면 1순위) |
| `ANTHROPIC_API_KEY` | Claude 키 (2순위) |
| `OBA_STT_API_KEY` / `OPENAI_API_KEY` | 녹음 파일만 올 때 음성 인식 (없으면 대시보드에서 텍스트 붙여넣기) |
| `OBA_DEFAULT_CALLER` | 공유 경로에서 발신자 정보가 없을 때 기본값 (기본 `엄마`) |
| `OBA_MCP_TOKEN` | vooy용 MCP 주소 `/mcp/<토큰>`의 비밀 경로 (없으면 실행마다 임시 생성) |
| `OBA_PUBLIC_URL` | 외부 https 주소 (대시보드에 vooy 연결 주소를 보여 줄 때 사용) |
| `OBA_VOOY_PUSH_URL` · `OBA_VOOY_PUSH_TOKEN` · `OBA_VOOY_USER_ID` · `OBA_VOOY_SIGNING_SECRET` | vooy 웹훅 기본값 (대시보드에서 바꿀 수 있음). 비어 있으면 보낼 함에 쌓고 크론이 가져감 |
| `OBA_VOOY_RETRY_SEC` | 웹훅 재시도 확인 주기 (기본 10초) |

## 데모 순서 (대시보드)

1. **가입:** 미리 채워진 정보로 "가입하고 엄마 연동하기" → 엄마에게 동의 요청 문자 → "엄마가 '예'라고 답장" → ✅ 연동됨
2. **통화 시뮬레이터 → 장보기 부탁 → 📞 통화 종료:** 탭 없이 몇 초 만에 휴지·계란은 자동 결제와 엄마 문자, 전기요는 승인 요청, 병원 예약은 "구매 외 부탁" 알림
3. **엄마 사칭 + 모르는 번호 → 통화 종료:** 🔒 저장하지 않고 폐기. 같은 내용을 **엄마 번호**로 보내면 상품권이라 결제 차단
4. **건강식품 부탁:** 홍삼·쌀 승인 요청. 정책에서 "카드 결제 실패시키기"를 켜고 승인 → SK pay로 복구 → 취소·환불

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `POST` | `/mcp/<OBA_MCP_TOKEN>` | vooy용 원격 MCP (Streamable HTTP). 도구 8개: [docs/VOOY_RECIPE.md](docs/VOOY_RECIPE.md) |
| `POST` | `/v1/telco/call-ended` | 통신사 통화 종료 웹훅 (`OBA_TELCO_TOKEN`) |
| `GET` · `POST` | `/v1/vooy/digests` · `/v1/vooy/digests/ack` | vooy HTTP 크론용 정리본 조회·확인 (가족 업로드 토큰) |
| `GET` · `PUT` | `/api/vooy` | vooy 웹훅 설정 (토큰·서명 비밀은 응답에 안 나옴) |
| `POST` | `/api/vooy/test` · `/mock/vooy/webhook` | 웹훅 테스트 발송 · 데모용 모의 vooy 수신함 |
| `POST` | `/v1/calls` | 단축어 업로드 (공용 또는 가족 토큰). multipart/urlencoded 양식 `file`·`text`·`caller`·`caller_number`, 또는 본문 그대로 |
| `POST` | `/api/families` | 가입: `{child_name, child_phone, parents:[{label, phone, address}]}` |
| `POST` | `/api/families/{id}/parents/{pid}/consent` | 부모님 동의 `{granted}` |
| `DELETE` | `/api/families/{id}` | 연동 해제 |
| `POST` | `/api/simulate/call-ended` | 데모: 통화 종료 시뮬레이션 `{transcript, who: parent/unknown, direction}` |
| `GET` | `/api/state` | 통화·주문·이벤트·정책·가족(번호 가림) |
| `POST` | `/api/orders/{id}/approve` · `reject` · `cancel` · `swap` | 자식의 개입 |
| `PUT` | `/api/policy` | 위임 정책 변경 |

대시보드·`/api/*`는 인증이 없습니다(해커톤 데모용, 결제는 모의). 외부에 공개할 때는 주의하세요. 데모 화면은 가족 한 곳을 기준으로 하고, 정책도 하나를 공유합니다.

## 구조

```
src/oba_hack/
  mcp_server.py vooy용 MCP 도구 (말로 엄마 등록, 정리본, 결제·거절·취소, 위임 범위)
  notify.py    통화 정리본 생성, vooy 웹훅 발송 / 크론잡용 보낼 함
  family.py    가입·부모님 번호 등록·동의·정보 수정, 번호 정규화, 통화/발신자 매칭
  telco.py     통신사(에이닷) 통화 종료 웹훅, 번호 필터
  ingest.py    단축어 업로드 수신 (공유 1탭, 문자 자동화)
  pipeline.py  전사 → 추출 → 조회·선택 → 위임 → 결제 → 결과, 승인/거절/교체/취소
  extract.py   부탁 추출 (LLM + 규칙 기반 폴백, 사칭 탐지)
  llm.py       SKT A.X K2(OpenAI 호환) / Claude 호출
  catalog.py   모의 카탈로그 검색과 선택 이유 (catalog.json, 구매 이력 포함)
  policy.py    위임 정책 (auto / ask / block)
  payment.py   테스트 결제 게이트웨이, 결제수단 폴백, 환불
  store.py     SQLite 저장소와 이벤트 로그
  app.py       API + 대시보드(static/index.html), 데모 샘플(samples/)
```
