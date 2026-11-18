"""LLM 호출. 키가 있는 엔진을 SKT A.X → OpenAI → Anthropic Claude 순으로 쓰고, 실패하면 다음 엔진으로 넘어간다.

모두 없거나 모두 실패하면 None을 돌려주고, 호출자는 규칙 기반 폴백을 쓴다. 데모가 키 없이도 끝까지 돌도록 하기 위함.
A.X와 OpenAI는 같은 OpenAI 호환 Chat Completions 형식이라 주소·모델만 다르다.
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

from . import usage

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


def _model(p: str) -> str:
    return {"ax": os.environ.get("OBA_AX_MODEL", AX_MODEL), "openai": os.environ.get("OBA_OPENAI_MODEL", OPENAI_MODEL),
            "claude": os.environ.get("OBA_CLAUDE_MODEL", CLAUDE_MODEL)}[p]


def _openai_compatible(base: str, key: str, model: str, system: str, user: str, timeout: float) -> tuple[str, dict]:
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
    body = r.json()
    u = body.get("usage") or {}
    meta = {"model": body.get("model") or model, "prompt_tokens": u.get("prompt_tokens", 0),
            "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0), "request_id": r.headers.get("x-request-id", "")}
    return body["choices"][0]["message"]["content"], meta


def _claude(system: str, user: str, timeout: float) -> tuple[str, dict]:
    model = _model("claude")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={"model": model, "max_tokens": 2000, "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    u = body.get("usage") or {}
    cached = u.get("cache_read_input_tokens", 0) or 0
    meta = {"model": body.get("model") or model,
            "prompt_tokens": (u.get("input_tokens", 0) or 0) + cached + (u.get("cache_creation_input_tokens", 0) or 0),
            "cached_tokens": cached, "completion_tokens": u.get("output_tokens", 0),
            "request_id": r.headers.get("request-id", "")}
    return "".join(b.get("text", "") for b in body["content"]), meta


def _request(p: str, system: str, user: str, timeout: float) -> tuple[str, dict]:
    """(응답 텍스트, 사용량 메타 {model, prompt_tokens, cached_tokens, completion_tokens, request_id})."""
    if p == "ax":
        return _openai_compatible(os.environ.get("OBA_AX_BASE_URL", AX_BASE), os.environ["OBA_AX_API_KEY"],
                                  _model("ax"), system, user, timeout)
    if p == "openai":
        return _openai_compatible(os.environ.get("OBA_OPENAI_BASE_URL", OPENAI_BASE), os.environ["OPENAI_API_KEY"],
                                  _model("openai"), system, user, timeout)
    return _claude(system, user, timeout)


def chat_json(system: str, user: str, timeout: float = 40.0, purpose: str = "") -> dict | None:
    """JSON 응답. 실제로 답한 엔진 이름을 '_engine' 키에 넣어 돌려준다 (호출자가 꺼내 쓴다).

    시도할 때마다 사용량(토큰·지연·결과·추정 비용)을 기록한다. 하루 비용 한도를 넘었으면 부르지 않는다.
    """
    for p in providers():
        if usage.over_limit():
            return None
        t0 = time.monotonic()
        meta: dict = {"model": _model(p)}
        try:
            text, meta = _request(p, system, user, timeout)
            out = _parse_json(text)
            err = "" if isinstance(out, dict) else "JSON 객체가 아님"
        except Exception as e:  # 네트워크·인증·한도·파싱 실패 → 기록하고 다음 엔진
            out, err = None, f"{type(e).__name__}: {e}"
        usage.record(p, meta.get("model", ""), purpose, ok=not err, latency_ms=int((time.monotonic() - t0) * 1000),
                     prompt_tokens=meta.get("prompt_tokens", 0), cached_tokens=meta.get("cached_tokens", 0),
                     completion_tokens=meta.get("completion_tokens", 0), error=err,
                     request_id=meta.get("request_id", ""))
        if not err:
            out["_engine"] = LABELS[p]
            return out
        print(f"[llm] {p} failed: {err[:200]}", flush=True)
    return None


def engine_of(result: dict) -> str:
    """chat_json 결과에서 답한 엔진 이름을 꺼낸다 (테스트 대역처럼 표시가 없으면 첫 엔진)."""
    p = provider()
    return result.pop("_engine", None) or (LABELS[p] if p else "규칙 기반")
