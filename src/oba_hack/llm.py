"""LLM 호출. SKT A.X K2(OpenAI 호환) → Anthropic Claude 순으로 사용하고, 둘 다 없으면 None.

호출자는 None을 받으면 규칙 기반 폴백을 쓴다. 데모가 키 없이도 끝까지 돌도록 하기 위함.
"""

from __future__ import annotations

import json
import os
import re

import httpx

AX_BASE = "https://agent-os-biz.adot.ai/oba/v1"
AX_MODEL = "skt/a.ll/A.X-K2-ADOT"


def provider() -> str | None:
    if os.environ.get("OBA_AX_API_KEY"):
        return "ax"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def provider_label() -> str:
    return {"ax": "SKT A.X K2", "claude": "Claude"}.get(provider() or "", "규칙 기반(키 없음)")


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def chat_json(system: str, user: str, timeout: float = 40.0) -> dict | None:
    p = provider()
    if p is None:
        return None
    try:
        if p == "ax":
            r = httpx.post(
                os.environ.get("OBA_AX_BASE_URL", AX_BASE).rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OBA_AX_API_KEY']}"},
                json={
                    "model": os.environ.get("OBA_AX_MODEL", AX_MODEL),
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout,
            )
            r.raise_for_status()
            return _parse_json(r.json()["choices"][0]["message"]["content"])
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": os.environ.get("OBA_CLAUDE_MODEL", "claude-sonnet-5-5"),
                "max_tokens": 2000,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return _parse_json(text)
    except Exception as e:  # 네트워크/파싱 실패 → 폴백
        print(f"[llm] {p} failed: {e!r}", flush=True)
        return None
