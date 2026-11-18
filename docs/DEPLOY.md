# 배포 (Fly.io)

데모 서버는 Fly.io 도쿄(nrt) 리전에 머신 1대 + SQLite 볼륨으로 상시 실행합니다.
vooy 5분 폴링·웹훅 재시도가 끊기지 않도록 자동 정지는 끕니다 (`fly.toml`).

## 처음 한 번

```bash
brew install flyctl
fly auth login                                   # 브라우저 로그인 (결제수단 등록 필요할 수 있음)
fly apps create dont-call-me-mama                # 이름이 이미 있으면 다른 이름으로, fly.toml의 app도 바꾼다
fly volumes create dcmm_data --region nrt --size 1 --app dont-call-me-mama --yes

# 비밀값 (값은 직접 만들고, 공유할 사람에게만 따로 전달)
fly secrets set --app dont-call-me-mama --stage \
  OBA_PUBLIC_URL=https://dont-call-me-mama.fly.dev \
  OBA_MCP_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" \
  OBA_TELCO_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  OBA_UPLOAD_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  OBA_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"

# LLM 키 (.env에 있는 값을 화면에 찍지 않고 옮기기)
set -a; source .env; set +a
printf 'OPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" | fly secrets import --app dont-call-me-mama --stage
```

선택: `OBA_TELCO_SIGNING_SECRET`(통신사 웹훅 HMAC 서명), `OBA_AX_API_KEY`·`OBA_AX_BASE_URL`(SKT A.X), `ANTHROPIC_API_KEY`.

## 배포

```bash
fly deploy --app dont-call-me-mama --ha=false    # SQLite 볼륨이라 머신은 1대
fly status --app dont-call-me-mama
curl https://dont-call-me-mama.fly.dev/health    # {"ok": true, "llm": "OpenAI"}
```

## 운영

- LLM 사용량: `https://dont-call-me-mama.fly.dev/admin`에서 `OBA_ADMIN_TOKEN` 입력, 또는
  `fly ssh console --app dont-call-me-mama -C /app/.venv/bin/oba-usage`, 또는 `fly logs --app dont-call-me-mama | grep llm-usage`
- 하루 비용 한도는 `fly.toml`의 `OBA_LLM_DAILY_USD_LIMIT` (기본 $5). 넘으면 그날은 규칙 기반으로 처리
- 로그: `fly logs --app dont-call-me-mama`
- 공용 MCP 주소·통신사 토큰 확인: `fly ssh console --app dont-call-me-mama -C 'printenv OBA_MCP_TOKEN OBA_TELCO_TOKEN'`
- 데이터 초기화(모든 체험 계정 삭제): `fly ssh console --app dont-call-me-mama -C 'rm -rf /data/oba.db /data/calls'` 후 `fly machine restart`
- 비용을 멈추려면: `fly scale count 0 --app dont-call-me-mama` (다시 켜기: `fly scale count 1`)
- 체험 기능을 끄려면 `fly.toml`의 `OBA_DEMO_MODE`를 `"0"`으로 바꾸고 다시 배포
