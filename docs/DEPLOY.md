# 배포 — 팀별 서버 2개

SKT용과 vooy용을 **따로 배포**합니다. 서버·토큰·데이터가 모두 분리되고, 첫 화면(`/`)이 그 팀의 시작 페이지입니다.

## 지금: Render 무료 웹 서비스 (2026-11-25~)

| 팀 | 서비스 | 주소 |
| --- | --- | --- |
| SKT | `dont-call-mama-skt` | https://dont-call-mama-skt.onrender.com |
| vooy | `dont-call-mama-vooy` | https://dont-call-mama-vooy.onrender.com |

```bash
# .env에 RENDER_API_KEY, OPENAI_API_KEY / .env.fly.<팀>에 팀별 토큰 (모두 gitignored)
uv run --no-project python scripts/deploy_render.py skt  [branch]   # 기본 main
uv run --no-project python scripts/deploy_render.py vooy [branch]
```

공개 GitHub 저장소의 `Dockerfile`로 싱가포르 리전에 빌드합니다(카드 등록 없음). 무료 플랜의 제약:

- 15분 동안 요청이 없으면 잠들고, 다음 요청에 30~60초 뒤 깨어납니다. vooy 반복 작업(5분)이 돌면 vooy 서버는 깨어 있습니다.
- 디스크가 없어서 **잠들거나 다시 배포하면 체험 계정이 지워집니다** (vooy 개인 MCP 주소도 다시 가입해야 함).
- 무료 사용 시간은 계정 전체 월 750시간 — 두 서버를 계속 켜 두면 모자랍니다.

## 이전: Fly.io (체험 크레딧 소진으로 2026-11-25 중단)

카드를 등록하면 아래 설정으로 되살릴 수 있습니다(SQLite 볼륨이라 데이터가 남음).

| 팀 | 설정 파일 | 앱 | 주소 |
| --- | --- | --- | --- |
| SKT | `fly.skt.toml` (`OBA_AUDIENCE=skt`) | `dont-call-mama-skt` | https://dont-call-mama-skt.fly.dev |
| vooy | `fly.vooy.toml` (`OBA_AUDIENCE=vooy`) | `dont-call-mama-vooy` | https://dont-call-mama-vooy.fly.dev |

도쿄(nrt) 리전에 앱마다 머신 1대 + SQLite 볼륨. vooy 5분 폴링·웹훅 재시도가 끊기지 않도록 자동 정지는 끕니다.

## 처음 한 번 (팀마다)

```bash
brew install flyctl            # macOS에서 소스 빌드가 오래 걸리면: curl -L https://fly.io/install.sh | sh
fly auth login

T=skt                          # vooy용은 T=vooy
APP=dont-call-mama-$T
fly apps create $APP
fly volumes create dcm_data --region nrt --size 1 --app $APP --yes

# 비밀값: 팀마다 새로 만든다 (공유할 팀에만 따로 전달). 통신사 토큰은 SKT용에만
gen() { python3 -c 'import secrets;print(secrets.token_urlsafe(24))'; }
fly secrets set --app $APP --stage OBA_PUBLIC_URL=https://$APP.fly.dev \
  OBA_MCP_TOKEN="$(gen)" OBA_UPLOAD_TOKEN="$(gen)" OBA_ADMIN_TOKEN="$(gen)"
[ $T = skt ] && fly secrets set --app $APP --stage OBA_TELCO_TOKEN="$(gen)"

# LLM 키 (.env에 있는 값을 화면에 찍지 않고 옮기기)
set -a; source .env; set +a
printf 'OPENAI_API_KEY=%s\n' "$OPENAI_API_KEY" | fly secrets import --app $APP --stage
```

선택: `OBA_TELCO_SIGNING_SECRET`(통신사 웹훅 HMAC 서명), `OBA_AX_API_KEY`·`OBA_AX_BASE_URL`(SKT A.X), `ANTHROPIC_API_KEY`.

## 배포

```bash
fly deploy -c fly.skt.toml --ha=false     # SQLite 볼륨이라 머신은 1대
fly deploy -c fly.vooy.toml --ha=false
curl https://dont-call-mama-skt.fly.dev/health    # {"ok": true, "llm": "OpenAI"}
```

## 운영 (APP=dont-call-mama-skt 또는 dont-call-mama-vooy)

- LLM 사용량: `https://$APP.fly.dev/admin`에서 그 앱의 `OBA_ADMIN_TOKEN` 입력, 또는
  `fly ssh console --app $APP -C /app/.venv/bin/oba-usage`, 또는 `fly logs --app $APP | grep llm-usage`
- 하루 비용 한도는 설정 파일의 `OBA_LLM_DAILY_USD_LIMIT` (앱마다 $5). 넘으면 그날은 규칙 기반으로 처리
- 로그: `fly logs --app $APP`
- 처리 도중 서버가 꺼지면(배포·재시작·머신 정지) 다시 켜질 때 멈춘 통화를 이어서 처리합니다(로그 `[resume]`).
  이미 만든 주문·결제는 다시 하지 않고, 2시간 넘게 멈춰 있던 통화는 늦게 주문하지 않고 자녀에게 '다시 보내 주세요'로 알립니다
- 비밀값 확인: `fly ssh console --app $APP -C 'printenv OBA_MCP_TOKEN OBA_TELCO_TOKEN'`
- 데이터 초기화(그 앱의 체험 계정 모두 삭제): `fly ssh console --app $APP -C 'rm -rf /data/oba.db /data/calls'` 후 `fly machine restart --app $APP`
- 비용을 멈추려면: `fly scale count 0 --app $APP` (다시 켜기: `fly scale count 1 --app $APP`)
- 체험 기능을 끄려면 설정 파일의 `OBA_DEMO_MODE`를 `"0"`으로 바꾸고 다시 배포
