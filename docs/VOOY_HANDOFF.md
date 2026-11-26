# 엄마 부탁을 Vooy 커넥터로 넘기기 (주문 · 택시 · 숙소 …)

엄마와의 통화에서 "사줘", "택시 좀 불러줘", "그날 묵을 데 알아봐줘" 같은 부탁이 나오면, 실제 처리는 **Vooy가 가진 커넥터**(마켓컬리, Swing Taxi, NOL 숙소 …)가 맡는다.
don't call Mama 서버는 **판단과 승인**(엄마가 정말 부탁했나, 사칭은 아닌가, 자녀가 맡긴 범위 안인가, 얼마까지 쓰나)을 맡고, **실행은 하지 않는다.**

```
엄마 통화 종료
  → 서버: 통화 정리 (부탁 추출 · 사칭 차단 · 위임 범위 확인)
  → Vooy 반복 작업: get_new_parent_requests  →  자녀에게 "엄마가 ○○ 부탁하셨어요. 할까요?"
  → 자녀: "응 해줘"
  → Vooy: approve_order / approve_task  →  서버가 승인서(handoff)를 돌려줌
  → Vooy: handoff.connector에 작업을 맡김 (마켓컬리 · Swing Taxi · NOL 숙소 …)
  → Vooy: report_purchase / report_task 로 결과 보고  →  서버가 엄마께 "해놨어~" 문자
```

## 1. 꼭 알아야 할 vooy 동작 (2026-11-25 실험으로 확인)

- 대화하는 Vooy는 사용자 지정 MCP 도구를 **직접 부르지 못한다.** 대신 **작업을 맡기면** 맡은 쪽이 우리 MCP를 부른다.
  - "엄마가 뭐 사달라고 한 거 있어?"처럼 자연스럽게 물어도 Vooy가 알아서 작업을 맡겼다(서버 기록 확인).
  - 공개 MCP(DeepWiki)로 비교해도 같았다. vooy 구조이지, 우리 서버 문제가 아니다.
- Vooy의 다른 서비스(쇼핑·택시·숙소)도 같은 방식, 곧 "작업을 맡겨서" 처리한다.
  - 그래서 우리 응답에 **어느 커넥터에 무엇을 맡길지**를 서비스 이름으로 분명히 적어 주면, Vooy가 그대로 위임한다.
- 반복 작업의 최소 주기는 **15분**이다. 5분으로 걸 수 없다.
- 서버에 도구를 새로 추가하면 vooy 연결 설정에 **꺼진 상태로** 들어온다(2026-11-26 확인). 연결 → don't call Mama → 설정에서
  새 도구를 체크하고 "도구 설정 저장"을 눌러야 Vooy가 부를 수 있다. 저장해야 도구 목록도 새로 읽는다.

## 2. 부탁 종류 → Vooy 커넥터

vooy 계정의 커넥터 목록(`api.vooy.com/v1/connectors`)에서 확인한 실제 이름이다. "연결"은 지금 테스트 계정에 연결돼 있다는 뜻이다.

| 엄마 부탁 | 1순위 커넥터 (도구) | 대안 | 연결 | 서버가 넘길 값 |
|---|---|---|---|---|
| 장보기·생필품 (휴지, 계란) | 마켓컬리 `market-kurly` (search-products) | 쿠팡·네이버 등 (Vooy 브라우저 작업), 다이소 `daiso` | 컬리·다이소 연결 | 품목, 수량, 최대 금액, 배송지, 늘 사던 상품 |
| 가전·건강식품 (전기요, 콘드로이친) | 다나와 가격비교 `danawa` → 네이버 스마트스토어 (브라우저) | 오늘의집 `ohouse` | 다나와 연결 | 품목, 고르는 기준, 최대 금액, 참고 상품 링크 |
| 택시 (병원 갈 때 등) | Swing Taxi `swing_taxi` (estimate_fare → dispatch_taxi → get_ride_status) | 카카오맵 `kakaomap` 길찾기(요금·시간만) | **미연결** | 출발지(엄마 집), 도착지, 시각, 탑승자, 최대 요금 |
| 숙소 | NOL 숙소 `nol` (create-quote → 예약) | 여기어때 `goodchoice`, 마이리얼트립 `myrealtrip` (검색) | NOL·여기어때 **미연결(수동 인증)** | 지역, 날짜, 인원, 최대 금액, 조건(엘리베이터 등) |
| 음식 배달 | 요기요 `yogiyo` (order-preview → place-order) | — | **미연결** | 메뉴, 배달 주소, 최대 금액 |
| 병원·가게 찾기·전화 | 카카오맵 `kakaomap` (place) + Vooy 전화 걸기 | Google Maps | 연결 | 장소 종류, 동네, 원하는 시각 |
| 주차 | 모두의주차장 `modu_parking` | — | 미연결 | 위치, 시간 |

서버는 커넥터를 **추천만** 한다. 연결 여부와 가능 여부는 Vooy가 판단한다. 미연결이면 Vooy가 자녀에게 연결을 안내하거나 대안 커넥터로 처리하게 한다.

## 3. 서버가 넘기는 승인서(handoff) 형식

구매는 이미 있다. `approve_order`가 `vooy_checkout=external`일 때 `handoff`를 돌려준다(`src/oba_hack/mcp_server.py` `_vooy_purchase`). 구매가 아닌 부탁에도 같은 모양을 쓴다.

```json
{
  "handoff": {
    "kind": "taxi",                         // purchase | taxi | stay | food | reservation
    "connector": "swing_taxi",              // 1순위 vooy 커넥터 이름
    "fallback": ["kakaomap"],
    "action": "dispatch_taxi",              // 먼저 estimate_fare로 요금 확인
    "params": {"from": "엄마 집 주소", "to": "○○정형외과", "when": "2026-11-27 09:30", "rider": "엄마"},
    "limits": {"max_amount": 20000, "confirm_with_user": true},
    "mom_said": "목요일 아침에 정형외과 가야 하는데 택시 좀 불러줘",
    "after": "report_task(task_id, success, provider, amount, reference, message_to_mom)"
  },
  "message": "Swing Taxi에 작업을 맡겨 … 요금이 2만원을 넘으면 먼저 물어보세요. 끝나면 report_task로 알려 주세요."
}
```

- `message`에 **커넥터 이름 + "작업을 맡겨"**를 적는다. Vooy는 이 문장을 보고 위임한다.
- 돈이 나가거나 사람이 움직이는 일(결제, 택시 호출, 예약)은 `confirm_with_user: true`이고, 자녀가 한 번 더 확인한다.
- 최대 금액과 위임 범위는 서버 정책(`update_delegation`)에서 온다. 사칭이 의심되거나 상품권 같은 건 handoff 자체를 주지 않는다.

## 4. 만들어야 할 것

| 단계 | 내용 | 지금 |
|---|---|---|
| 추출 | 부탁 문장마다 할 일(task)을 만들고 종류(taxi·stay·food·reservation·other)를 붙인다. 날짜·장소는 문장에 남기고 Vooy가 통화 날짜 기준으로 해석 (`tasks.py`) | ✅ |
| 정리본 | "🚕 … — Swing Taxi로 부를까요? (task_id …)"처럼 실행 제안을 넣는다 | ✅ |
| 승인 | `approve_task(task_id)` → 위 handoff를 돌려준다 (택시 출발지·배달지·검색 기준에 엄마 주소를 채운다) | ✅ |
| 보고 | `report_task(task_id, success, provider, amount, reference, details, message_to_mom)` → 상태 저장, 엄마께 문자 | ✅ |
| 구매 | 이미 있는 `approve_order` handoff에 `connector`(market-kurly/danawa 등)를 추가한다 | 쇼핑몰 링크만 |
| 기억 규칙 | 처음 연결할 때 "엄마 관련 부탁·통화·주문은 don't call Mama에 작업을 맡겨 처리"를 Vooy 기억에 남긴다 | 없음 |

## 5. 테스트 순서

1. 데모 통화 시나리오 추가: "목요일 아침 병원 택시", "다음 달 부산 1박 숙소".
2. 택시: `estimate_fare`(요금 조회)까지만 진행하고 `dispatch_taxi`는 하지 않는다. 실제 택시가 오기 때문이다.
3. 숙소: 견적(`create-quote`)까지만 진행하고 예약은 하지 않는다.
4. 장보기: 마켓컬리 검색 결과와 최대 금액 비교까지 진행한다. 실제 결제는 테스트몰 `checkout_order`로 한다.
5. 서버 기록에서 `approve_task` → `report_task` 순서와 엄마 문자 발송을 확인한다.
