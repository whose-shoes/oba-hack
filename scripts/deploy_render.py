"""Render 무료 웹 서비스(Docker)에 팀별 데모 서버를 만들거나 갱신한다.

    uv run --no-project python scripts/deploy_render.py skt [branch]    # 또는 vooy

- 공개 GitHub 저장소(whose-shoes/oba-hack)의 Dockerfile로 빌드한다. 서비스 이름: dont-call-mama-<팀>.
- 비밀값은 `.env.fly.<팀>`(gitignored)에서, OPENAI_API_KEY는 `.env`에서 읽어 넣는다. 화면에는 이름만 찍는다.
- RENDER_API_KEY는 `.env`에 둔다.
- 무료 플랜: 15분 요청이 없으면 잠들고(다음 요청에 30~60초 뒤 깨어남), 디스크가 없어 잠들거나 재배포하면 데이터가 지워진다.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.render.com/v1"
REPO = "https://github.com/whose-shoes/oba-hack"
SECRET_KEYS = ("OBA_MCP_TOKEN", "OBA_UPLOAD_TOKEN", "OBA_ADMIN_TOKEN", "OBA_TELCO_TOKEN",
               "OBA_TELCO_SIGNING_SECRET", "OBA_AX_API_KEY", "OBA_AX_BASE_URL")


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


BASE_ENV = read_env(ROOT / ".env")
KEY = BASE_ENV.get("RENDER_API_KEY")


def call(method: str, path: str, body: object | None = None) -> object:
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"Render API {method} {path} → {e.code}: {e.read().decode()[:400]}")


def env_vars(team: str, url: str | None) -> list[dict]:
    team_env = read_env(ROOT / f".env.fly.{team}")
    values = {"OBA_AUDIENCE": team, "OBA_DEMO_MODE": "1", "TZ": "Asia/Seoul", "OBA_LLM_DAILY_USD_LIMIT": "5",
              "OBA_PORT": "10000", "PORT": "10000", "OBA_DATA_DIR": "/data"}
    values |= {k: team_env[k] for k in SECRET_KEYS if team_env.get(k)}
    if team != "skt":  # 통신사 웹훅은 SKT용에만
        values.pop("OBA_TELCO_TOKEN", None)
        values.pop("OBA_TELCO_SIGNING_SECRET", None)
    if BASE_ENV.get("OPENAI_API_KEY"):
        values["OPENAI_API_KEY"] = BASE_ENV["OPENAI_API_KEY"]
    if url:
        values["OBA_PUBLIC_URL"] = url
    missing = [k for k in ("OBA_MCP_TOKEN", "OBA_ADMIN_TOKEN", "OPENAI_API_KEY") if k not in values]
    if missing:
        sys.exit(f"비밀값이 없어요: {missing}")
    return [{"key": k, "value": v} for k, v in values.items()]


def main(team: str, branch: str) -> None:
    if not KEY:
        sys.exit(".env에 RENDER_API_KEY가 없어요")
    name = f"dont-call-mama-{team}"
    owner = call("GET", "/owners?limit=20")[0]["owner"]["id"]
    found = [s["service"] for s in call("GET", f"/services?name={name}&limit=20") if s["service"]["name"] == name]
    if found:
        svc = found[0]
        call("PATCH", f"/services/{svc['id']}", {"branch": branch})
    else:
        svc = call("POST", "/services", {
            "type": "web_service", "name": name, "ownerId": owner, "repo": REPO, "branch": branch,
            "autoDeploy": "no",
            "serviceDetails": {"runtime": "docker", "plan": "free", "region": "singapore",
                               "healthCheckPath": "/health",
                               "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."}},
            "envVars": env_vars(team, None),
        })["service"]
    url = svc["serviceDetails"]["url"]
    vars_ = env_vars(team, url)
    call("PUT", f"/services/{svc['id']}/env-vars", vars_)
    call("POST", f"/services/{svc['id']}/deploys", {"clearCache": "do_not_clear"})  # 202, 본문 없음

    # 로컬 기록의 공개 주소도 새 주소로 (값은 찍지 않음)
    env_path = ROOT / f".env.fly.{team}"
    lines = [f"OBA_PUBLIC_URL={url}" if ln.startswith("OBA_PUBLIC_URL=") else ln
             for ln in env_path.read_text().splitlines()]
    env_path.write_text("\n".join(lines) + "\n")
    print(f"{team}: {name} ({svc['id']}) → {url}  branch={branch}")
    print("  환경 변수:", ", ".join(v["key"] for v in vars_))

    time.sleep(5)
    for _ in range(90):  # 가장 최근 배포가 끝날 때까지 기다리기 (최대 15분)
        status = call("GET", f"/services/{svc['id']}/deploys?limit=1")[0]["deploy"]["status"]
        if status in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            break
        time.sleep(10)
    print(f"  배포 상태: {status}")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in ("skt", "vooy"):
        sys.exit("사용법: deploy_render.py skt|vooy [branch]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "main")
