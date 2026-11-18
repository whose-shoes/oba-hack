"""LLM 호출. 키가 있는 엔진을 SKT A.X → OpenAI → Anthropic Claude 순으로 쓰고, 실패하면 다음 엔진으로 넘어간다.

모두 없거나 모두 실패하면 None을 돌려주고, 호출자는 규칙 기반 폴백을 쓴다. 데모가 키 없이도 끝까지 돌도록 하기 위함.
A.X와 OpenAI는 같은 OpenAI 호환 Chat Completions 형식이라 주소·모델만 다르다.
"""

from __future__ import annotations

import json
import os
import re

import httpx

AX_BASE = "https://agent-os-biz.adot.ai/oba/v1"   # 해커톤 때 받은 주소. SKT가 준 주소로 OBA_AX_BASE_URL을 바꾼다
AX_MODEL = "skt/a.ll/A.X-K2-ADOT"
OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_MODEL = "gpt-4o-mini"
CLAUDE_MODEL = "claude-sonnet-5-5"

# (이름, 화면 표시, 키 환경변수) — 이 순서대로 시도한다
ENGINES = (("ax", "SKT A.X", "OBA_AX_API_KEY"), ("openai", "OpenAI", "OPENAI_API_KEY"),
           ("claude", "Claude", "ANTHROPIC_API_KEY"))
LABELS = {name: label for name, label, _ in ENGINES}


def providers() -> list[str]:
    """키가 설정된 엔진들 (시도 순서)."""
    return [name for name, _, key in ENGINES if os.environ.get(key)]


def provider() -> str | None:
    ps = providers()
    return ps[0] if ps else None


def provider_label() -> str:
    """화면에 보일 엔진 이름. 폴백이 있으면 'SKT A.X → OpenAI'처럼 순서를 보여 준다."""
    ps = providers()
    return " → ".join(LABELS[p] for p in ps) if ps else "규칙 기반(키 없음)"


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def _openai_compatible(base: str, key: str, model: str, system: str, user: str, timeout: float) -> dict:
    r = httpx.post(
        base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return _parse_json(r.json()["choices"][0]["message"]["content"])


def _claude(system: str, user: str, timeout: float) -> dict:
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": os.environ.get("OBA_CLAUDE_MODEL", CLAUDE_MODEL),
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return _parse_json("".join(b.get("text", "") for b in r.json()["content"]))


def _call(p: str, system: str, user: str, timeout: float) -> dict:
    if p == "ax":
        return _openai_compatible(os.environ.get("OBA_AX_BASE_URL", AX_BASE), os.environ["OBA_AX_API_KEY"],
                                  os.environ.get("OBA_AX_MODEL", AX_MODEL), system, user, timeout)
    if p == "openai":
        return _openai_compatible(os.environ.get("OBA_OPENAI_BASE_URL", OPENAI_BASE), os.environ["OPENAI_API_KEY"],
                                  os.environ.get("OBA_OPENAI_MODEL", OPENAI_MODEL), system, user, timeout)
    return _claude(system, user, timeout)


def chat_json(system: str, user: str, timeout: float = 40.0) -> dict | None:
    """JSON 응답. 실제로 답한 엔진 이름을 '_engine' 키에 넣어 돌려준다 (호출자가 꺼내 쓴다)."""
    for p in providers():
        try:
            out = _call(p, system, user, timeout)
        except Exception as e:  # 네트워크/파싱 실패 → 다음 엔진
            print(f"[llm] {p} failed: {e!r}", flush=True)
            continue
        if isinstance(out, dict):
            out["_engine"] = LABELS[p]
            return out
    return None


def engine_of(result: dict) -> str:
    """chat_json 결과에서 답한 엔진 이름을 꺼낸다 (테스트 대역처럼 표시가 없으면 첫 엔진)."""
    p = provider()
    return result.pop("_engine", None) or (LABELS[p] if p else "규칙 기반")
