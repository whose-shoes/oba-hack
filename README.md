# oba-hack

엄마가 전화로 "이거 사줘"라고 하면, 통화 기록을 받아 주문·결제까지 대신 해주는 에이전트.

```
엄마 → 자식 iPhone (에이닷 자동 녹음)
     → [모듈 1] 공유 → iOS 단축어 → 수신 서버   ← 현재 구현
     → [모듈 2] Claude로 주문 정보 추출
     → [모듈 3] 자식 승인 → 주문·결제
```

## 모듈 1: 통화 기록 수신 서버

### 실행

```bash
cp .env.example .env            # OBA_UPLOAD_TOKEN을 긴 랜덤 문자열로 바꾸기
set -a; source .env; set +a
uv run oba-hack                 # http://0.0.0.0:8000
```

아이폰이 접근할 수 있도록 터널을 엽니다.

```bash
ngrok http 8000                 # 출력된 https://xxxx.ngrok-free.app 주소를 단축어에 넣기
```

받은 기록은 `data/calls/<call_id>/`에 원본 파일과 `meta.json`으로 저장됩니다.

### API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/health` | 상태 확인 |
| `POST` | `/v1/calls` | 통화 기록 업로드 (`Authorization: Bearer <토큰>`) |
| `GET` | `/v1/calls` | 받은 기록 목록 |

`POST /v1/calls`는 두 형식을 받습니다.
- `multipart/form-data`: 파일 필드(여러 개 가능) + 선택 텍스트 필드 `text`
- 그 외: 본문 전체를 파일 하나로 저장 (파일명은 `X-Filename` 헤더, 텍스트면 `transcript.txt`)

```bash
curl -H "Authorization: Bearer $OBA_UPLOAD_TOKEN" -F "file=@call.m4a" http://localhost:8000/v1/calls
```

### 아이폰 단축어 설정 ("oba로 보내기")

1. **단축어** 앱 → `+` → 이름 `oba로 보내기`
2. 하단 ⓘ(세부사항) → **공유 시트에서 보기** 켜기 → 받는 유형: **파일, 텍스트, 미디어**
3. 동작 추가: **URL 콘텐츠 가져오기**
   - URL: `https://<ngrok 주소>/v1/calls`
   - 방법: `POST`
   - 헤더: `Authorization` = `Bearer <OBA_UPLOAD_TOKEN>`
   - 요청 본문: **양식** → 필드 추가 → 유형 **파일**, 키 `file`, 값 **단축어 입력**
4. 동작 추가: **알림 보기** → `엄마 통화 전송 완료`

사용: 통화 종료 → 에이닷 통화기록 → 해당 통화 → 공유 → **oba로 보내기**

(선택) 공유를 잊지 않도록: 단축어 → **자동화** → **앱** → 에이닷 선택, **닫힘** → **즉시 실행** → 동작 **알림 보기** "엄마 통화였다면 oba로 보내기".
