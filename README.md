# don't call Mama (돈콜 마마)

> **엄마가 전화로 "그거 좀 사 줘" 하면, 통화가 끝난 뒤 에이전트가 자식 대신 찾아서 결제하고 엄마에게 알려 줍니다.**

OBA.RUN 2nd 해커톤(SKT Agentic Pay 트랙) 출품작을 해커톤 이후 완성한 버전입니다.
엄마 쪽 접점은 **SKT 에이닷 통화 요약·할일**, 자식 쪽 접점은 **vooy(자녀의 메신저 에이전트)용 원격 MCP 서버**입니다.
결제·문자는 모두 **테스트(모의)** 이며 실제 금융 거래는 일어나지 않습니다.

## 바로 체험하기

팀마다 **배포를 따로** 했습니다. 서버·데이터·토큰이 완전히 분리되고, 첫 화면이 그 팀의 시작 페이지입니다.

| 누구를 위한 | 주소 | 무엇을 볼 수 있나 |
| --- | --- | --- |
| **SKT** (에이닷 연동) | https://dont-call-mama-skt.fly.dev | 에이닷 통화 종료 → 웹훅 → 부탁 추출·상품 선택·위임 판단·테스트 결제·엄마 문자까지 단계별로. 연동 명세와 curl 예시 |
| **vooy 팀** (MCP) | https://dont-call-mama-vooy.fly.dev | vooy에 넣을 MCP 주소·레시피·도구 목록, 체험 계정, vooy가 부른 도구가 서버에서 처리되는 실시간 기록 |

두 서버 모두 자녀 대시보드는 `/dashboard`, 운영자용 LLM 사용량은 `/admin`입니다.
방문할 때마다 **체험 계정**(데모 번호·엄마 메모·위임 범위가 채워진 샌드박스)이 만들어져 다른 사람의 체험과 섞이지 않습니다.
자세한 안내: [에이닷 연동 (SKT)](docs/SKT.md) · [vooy 연결과 레시피](docs/VOOY_RECIPE.md) · [배포](docs/DEPLOY.md)

## 왜 필요한가요?

부모님의 장보기 부탁은 대부분 전화로 옵니다. "휴지 다 떨어졌다", "쌀 좀 시켜 줘" 같은 말은 통화가 끝나면 자식의 할 일 목록으로 넘어가고, 바쁜 자식은 잊거나 늦게 처리합니다. 부모님은 앱 주문이 어려워 다시 부탁하기를 미안해합니다. 반대로 전화 속 부탁을 에이전트가 그대로 결제해 버리면 사칭 보이스피싱에 돈이 나갈 수 있고, 매번 확인을 받으면 대행의 의미가 없어집니다. 우리는 **통화라는 가장 자연스러운 맥락**에서 구매를 시작하되, **어디까지 맡길지를 자식이 정하고**, 그 범위 안에서는 묻지 않고 끝까지 처리합니다.

SKT는 2026년 6월 에이닷 업데이트에서 통화 요약 중 장보기 같은 후속 조치를 '할일'에 자동 등록한다고 발표했습니다([SKT 뉴스룸](https://news.sktelecom.com/227226)). 이 프로젝트는 그다음 단계 — 그 장보기 할일을 실제 구매·결제·부모님 안내까지 끝내는 에이전트입니다.

## 흐름

```
[가입]  자식이 vooy에 말로 엄마 번호·정보 입력(또는 대시보드) → 바로 연결 (한 번만)

엄마 ⇄ 자식  통화 (SKT 에이닷 녹음·요약·할일)
   │  ① 0탭: 통화 종료 시 에이닷이 웹훅 호출   POST /v1/telco/call-ended   (SKT 협력 필요, 데모는 시뮬레이터)
   │  ② 1탭: 에이닷 공유 → iOS 단축어       POST /v1/calls              (지금 바로 가능)
   │  ③ 0탭: 엄마 문자 → 단축어 메시지 자동화 POST /v1/calls (caller_number)
   ▼
 [필터]  등록된 부모님 번호가 아니면 번호·내용을 저장하지 않고 폐기
 [맥락]  부탁 추출: 무엇·몇 개·근거 문장·사칭 정황 (에이닷 할일을 힌트로)   ← SKT A.X → OpenAI → Claude → 규칙 기반
 [조회]  카탈로그 검색 + 엄마 구매 이력 (이 서비스로 산 것도 쌓임)
 [선택]  늘 사던 상품·규격 일치·평점·엄마 메모로 고르고 이유 설명
 [위임]  정책: auto(바로 결제) / ask(자식 승인) / block(결제 안 함)
 [전달]  정리본을 자식의 vooy로 먼저 보냄 (웹훅 또는 vooy 5분 반복 작업, 중복 없음)
 [승인]  자식이 "응 결제해" → vooy가 MCP approve_order
 [결제]  서버가 테스트 결제(실패 시 다음 결제수단으로 복구)
         또는 vooy가 1회용·금액 상한 토큰으로 테스트몰 결제 (checkout_order)
 [알림]  엄마께 "엄마 시켰어~" 문자 (vooy가 자식 말투로 작성, 비면 서버 기본 문구)
 [사후]  취소·환불·대체 상품 교체도 vooy로 처리
```

## 위임 정책 (자식이 대시보드나 vooy에서 조절)

| 상황 | 결과 |
| --- | --- |
| 늘 사던 상품 + 자동결제 한도(기본 3만원) 이내 | 묻지 않고 결제 → 엄마에게 "주문했어요" |
| 처음 사는 상품, 한도 초과, 월 예산 초과, 엄마가 말한 예산 초과 | 자식에게 근거 문장과 함께 승인 요청 (상품 교체 가능) |
| 한 번 승인해서 산 상품 | 다음부터 '늘 사던 상품' — 한도 안이면 바로 결제 |
| 상품권·기프트카드, 사칭 정황("폰이 고장", "인증번호"), 등록 안 된 발신자 | 결제 차단 + 경고. 누가 승인해도 서버가 거절 |
| 등록되지 않은 번호와의 통화 | 처리 대상 아님. 저장하지 않고 폐기 |
| 결제 실패 | 다음 결제수단(SK pay) 재시도, 모두 실패하면 자식에게 알림 |
| 원치 않은 결제 | 취소·환불 → 엄마에게 안내 문자 |

## 에이닷 연결 (SKT)

자식이 동의한 부모님 번호와의 통화가 끝나면, 에이닷이 요약·할일·대화 텍스트를 한 번 POST 하면 됩니다. 계약·보안·응답 형식은 [docs/SKT.md](docs/SKT.md).

```http
POST /v1/telco/call-ended
Authorization: Bearer <OBA_TELCO_TOKEN>
Content-Type: application/json

{"call_id": "skt-20261117-0001", "from_number": "010-2345-6789", "to_number": "010-1234-5678",
 "ended_at": "2026-11-17T14:10:00+09:00", "summary": "엄마가 휴지·계란과 전기요를 부탁함",
 "todos": ["장보기: 휴지 30롤, 계란 한 판", "장보기: 전기요"],
 "transcript": "엄마: 휴지가 다 떨어졌어...\n나: 알았어"}
```

`OBA_TELCO_SIGNING_SECRET`을 설정하면 `X-Telco-Timestamp`·`X-Telco-Signature`(HMAC-SHA256)도 요구합니다. SKT 협력 없이 지금 되는 길은 에이닷 공유 → iOS 단축어(1탭)입니다 ([docs/SKT.md](docs/SKT.md#6-지금-바로-되는-연결-에이닷-공유--ios-단축어)).

## vooy 연결 (MCP)

vooy의 서비스 연결에 MCP 주소를, Recipe Studio에 [레시피](src/oba_hack/static/vooy-recipe.txt)를 넣습니다. 도구 12개(체험 서버 기준), 설계 원칙, 예시 대화는 [docs/VOOY_RECIPE.md](docs/VOOY_RECIPE.md).

- **사용자마다 개인 MCP 주소** `/mcp/<계정 토큰>` — vooy는 MCP 요청에 사용자 정보를 보내지 않으므로 주소로 계정을 가릅니다. 공용 주소 `/mcp/<OBA_MCP_TOKEN>`은 가입만 합니다.
- **결제는 위임 증서 + 1회용 토큰** — `approve_order`가 상품·수량·최대 금액·배송지와 이 주문에만 30분 동안 한 번 쓰는 토큰을 주고, vooy가 `checkout_order`로 테스트몰 결제를 마칩니다. 실제 쇼핑몰(네이버·11번가) 구매 위임은 대시보드에서 명시적으로 켜야 합니다.
- **정리본은 먼저 보냄** — 웹훅(HMAC 서명·재시도) 또는 vooy 5분 반복 작업(`get_new_parent_requests`). 둘 다 켜도 중복 없음.

## 실행

```bash
cp .env.example .env              # 토큰 바꾸기, LLM 키가 있으면 OPENAI_API_KEY 등
set -a; source .env; set +a
uv run oba-hack                   # http://localhost:8000 — / (대시보드), /skt, /vooy (OBA_AUDIENCE가 비어 있을 때)
uv run pytest                     # 테스트 79개
```

키가 없어도 **규칙 기반 추출**로 끝까지 동작합니다. 지금 쓰는 분석 엔진은 화면 상단에 표시됩니다. 배포는 [docs/DEPLOY.md](docs/DEPLOY.md) (Fly.io, 팀별 앱 2개).

### 환경변수

| 변수 | 설명 |
| --- | --- |
| `OBA_AUDIENCE` | 이 배포가 누구용인지: `skt`(첫 화면 = 에이닷 연동 데모) · `vooy`(첫 화면 = vooy MCP 데모). 비우면 로컬 개발 — 첫 화면이 대시보드이고 `/skt`·`/vooy`를 모두 엶 |
| `OBA_PUBLIC_URL` | 외부 https 주소. vooy 연결 주소·대시보드 링크를 만들 때 씀 |
| `OBA_MCP_TOKEN` | 공용(가입용) MCP 주소 `/mcp/<토큰>`의 비밀 경로 (없으면 실행마다 임시 생성) |
| `OBA_TELCO_TOKEN` · `OBA_TELCO_SIGNING_SECRET` | 통신사 웹훅 인증 토큰 · (선택) HMAC 서명 비밀 |
| `OBA_UPLOAD_TOKEN` | 단축어 업로드 공용 토큰 (가족별 토큰도 사용 가능) |
| `OBA_AX_API_KEY` · `OBA_AX_BASE_URL` · `OBA_AX_MODEL` | SKT A.X (OpenAI 호환). 키가 있으면 1순위 |
| `OPENAI_API_KEY` · `OBA_OPENAI_MODEL` | OpenAI (기본 `gpt-4o-mini`). 2순위 |
| `ANTHROPIC_API_KEY` · `OBA_CLAUDE_MODEL` | Claude. 3순위. 앞 엔진이 실패하면 다음 엔진, 모두 실패하면 규칙 기반 |
| `OBA_STT_API_KEY` | 녹음 파일만 올 때 음성 인식 (없으면 `OPENAI_API_KEY`, 그것도 없으면 대시보드에서 붙여넣기) |
| `OBA_DEMO_MODE` | 체험 서버 기능(체험 계정, `demo_parent_call` 도구, 통화 결과 조회). 기본 `1`, 끄려면 `0` |
| `OBA_ADMIN_TOKEN` | 운영자용 LLM 사용량 화면 `/admin`·`GET /admin/usage` 토큰 (없으면 두 주소 모두 404) |
| `OBA_LLM_DAILY_USD_LIMIT` | 하루 LLM 추정 비용 한도(USD). 넘으면 그날은 LLM을 쉬고 규칙 기반 (데모 서버 $5) |
| `OBA_LLM_PRICES` | 단가 추가·변경 JSON `{"모델": [입력, 캐시 입력, 출력]}` (100만 토큰당 USD) |
| `OBA_DEFAULT_CALLER` | 공유 경로에서 발신자 정보가 없을 때 기본값 (기본 `엄마`) |
| `OBA_VOOY_PUSH_URL` · `…_TOKEN` · `…_USER_ID` · `…_SIGNING_SECRET` | vooy 웹훅 기본값 (단일 계정 운영용). 보통은 계정별로 대시보드에서 설정 |

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/` · `/dashboard` | 팀 시작 페이지(`OBA_AUDIENCE`) · 자녀 대시보드 (로컬 개발이면 `/`가 대시보드, `/skt`·`/vooy`도 열림) |
| `POST` | `/mcp/<토큰>` | vooy용 원격 MCP (Streamable HTTP, stateless, JSON 응답) |
| `POST` | `/v1/telco/call-ended` | 통신사 통화 종료 웹훅 (`OBA_TELCO_TOKEN`, 선택 HMAC) |
| `GET` | `/v1/telco/calls/{call_id}` | 웹훅으로 보낸 통화의 처리 결과 (통신사 토큰, 데모 모드) |
| `POST` | `/v1/calls` | 단축어 업로드 (공용 또는 가족 토큰) |
| `GET` · `POST` | `/v1/vooy/digests` · `/v1/vooy/digests/ack` | vooy HTTP 크론용 정리본 조회·확인 (가족 업로드 토큰) |
| `POST` | `/api/demo` | 체험 계정 만들기 `{audience: "skt" \| "vooy"}` (요청 제한 있음) |
| `GET` | `/api/public` | 시작 페이지용 공개 정보 (분석 엔진, 공용 MCP 주소, 도구 목록) |
| `POST` | `/api/families` | 가입: `{child_name, child_phone, parents:[{label, phone, address}]}` |
| `GET` | `/api/state` | 내 계정의 통화·주문·이벤트·정책 (`X-Account: <대시보드 토큰>`) |
| `POST` | `/api/simulate/call-ended` | 통화 종료 시뮬레이션 `{transcript, who, direction, summary, todos}` |
| `GET` | `/api/calls/{id}/outcome` | 통화 한 건의 추출·선택·판단·결제·문자 결과 |
| `POST` | `/api/orders/{id}/approve` · `reject` · `cancel` · `swap` | 자식의 개입 |
| `GET` · `PUT` | `/api/policy` · `/api/vooy` | 위임 정책 · vooy 웹훅 설정 (계정별, 토큰·서명 비밀은 응답에 안 나옴) |
| `GET` | `/admin` · `/admin/usage` | 운영자용 LLM 사용량 (오늘·일별·팀별·계정별·용도별·모델별, 최근 호출) — `OBA_ADMIN_TOKEN` |
| `POST` | `/api/vooy/test` · `/mock/vooy/webhook` | 웹훅 테스트 발송 · 데모용 모의 vooy 수신함 |

`/api/*`는 `X-Account` 헤더(개인 대시보드 토큰)가 필요합니다. 가입·체험 계정·샘플·공개 정보만 열려 있습니다.

## LLM 사용량 기록

LLM을 부를 때마다(실패·폴백 포함) 엔진·모델·용도(부탁 찾기/상품 고르기)·계정과 그 계정의 출처(SKT 체험, vooy 체험, vooy 가입, 대시보드 가입)·통화 ID·입력/캐시/출력 토큰·지연·결과·추정 비용·요청 ID를 SQLite `llm_usage` 표에 남깁니다. **프롬프트·응답 내용은 남기지 않습니다.**

- 운영자 화면: `/admin` (토큰 입력) — 팀별로 SKT·vooy가 얼마나 썼는지 바로 보입니다
- 명령: `uv run oba-usage [일수]` (서버: `fly ssh console -C /app/.venv/bin/oba-usage`)
- 로그: 호출마다 `[llm-usage] openai/gpt-4o-mini extract ok 812ms in=1234 cached=0 out=210 $0.000311 …` 한 줄
- 비용은 OpenAI 공식 단가표(2026-11) 기준 추정치입니다. 단가를 모르는 모델은 비용을 비워 두고 `OBA_LLM_PRICES`로 넣을 수 있습니다.

## 보안·개인정보

- 등록된 부모님 번호와의 통화만 처리하고, 나머지는 내용·번호를 저장하지 않습니다.
- 외부 LLM에 보내기 전에 전화번호·계좌번호를 가리고, 화면·vooy 응답의 번호도 가립니다 (체험 계정의 데모 번호만 그대로 보임).
- 계정마다 개인 MCP 주소·대시보드 토큰·업로드 토큰을 따로 발급하고, 남의 주문은 승인할 수 없습니다.
- vooy에는 카드번호 대신 주문별 1회용·금액 상한·30분 만료 토큰만 넘깁니다.
- 웹훅 주소는 https 공개 주소만 받습니다 (사설망·메타데이터 주소 차단). 가입·체험 계정·시뮬레이터에는 요청 제한이 있습니다.

## 사용한 API / 기술 스택

- **SKT 에이닷** — 통화 녹음·요약·할일 (통화 종료 웹훅 0탭, 지금은 공유 1탭)
- **SKT A.X** (OpenAI 호환) → **OpenAI** → **Anthropic Claude** → 규칙 기반 — 부탁 추출·상품 선택 (실패 시 다음 엔진)
- **vooy** — 원격 **MCP (Streamable HTTP)** 서버, 레시피, 5분 반복 작업, 웹훅 Push
- **iOS 단축어** — 에이닷 공유·엄마 문자 자동화 → `/v1/calls`
- **테스트 결제 게이트웨이·테스트몰(모의)** — 결제수단 폴백, 1회용 결제 토큰, 환불
- 런타임: **Python 3.11**, **uv**, **FastAPI**, **Uvicorn**, **httpx**, **MCP Python SDK 2.x**, **SQLite**, **pytest**, **Fly.io**
- 프런트: 정적 HTML/CSS/JS (Montage/Wanted 디자인 토큰, 모바일 우선, 다크 모드)

## 구조

```
src/oba_hack/
  app.py         API, 팀별 시작 페이지(OBA_AUDIENCE), 대시보드, 체험 계정·요청 제한
  mcp_server.py  vooy용 MCP 도구 (엄마 등록, 정리본, 승인·테스트몰 결제·보고, 취소, 위임 범위, 체험 통화)
  telco.py       통신사(에이닷) 통화 종료 웹훅: 번호 필터, 할일, HMAC 서명, 처리 결과 조회
  notify.py      통화 정리본, vooy 웹훅 발송(계정별 설정·SSRF 방지) / 반복 작업용 보낼 함
  pipeline.py    전사 → 추출 → 조회·선택 → 위임 → 결제 → 결과, 승인/거절/교체/취소, 계정별 구매 이력
  extract.py     부탁 추출 (LLM + 규칙 기반 폴백, 사칭 탐지, 번호 가림)
  choose.py      LLM이 엄마 상황을 보고 상품 선택
  llm.py         SKT A.X → OpenAI → Claude 순서와 폴백
  usage.py       LLM 사용량 기록·추정 비용·하루 한도·요약(/admin, oba-usage)
  family.py      가입·체험 계정·번호 정규화·통화/발신자 매칭
  ingest.py      단축어 업로드 수신 (공유 1탭, 문자 자동화)
  policy.py · payment.py · catalog.py · store.py · samples.py · ratelimit.py
  static/        index.html(대시보드), skt.html, vooy.html, admin.html, site.css/js, vooy-recipe.txt
  samples/       데모 통화 (에이닷 요약·할일 포함)
```

## 라이선스·면책

데모 코드입니다. 모든 결제·문자는 **모의**입니다. 실제 서비스로 전개하려면 통신사 제휴(에이닷 연동·이용자 동의), 발신번호 사전등록, 결제사 연동과 PCI DSS, 개인정보 처리 동의 흐름을 별도로 갖춰야 합니다.
