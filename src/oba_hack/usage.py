"""LLM 사용량 기록 — 호출마다 엔진·모델·용도·계정·토큰·지연·결과·추정 비용을 남긴다.

- 프롬프트·응답 내용은 남기지 않는다 (통화 내용은 개인정보).
- 비용은 단가표로 계산한 추정치다. 단가를 모르는 모델은 비용을 비워 둔다 (OBA_LLM_PRICES로 추가).
- OBA_LLM_DAILY_USD_LIMIT을 정하면, 오늘 추정 비용이 넘을 때 LLM을 부르지 않고 규칙 기반으로 돌린다.
- 보기: 관리자 페이지 /admin (OBA_ADMIN_TOKEN), 명령 `oba-usage`, 로그의 [llm-usage] 줄.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

from . import account, store

# USD / 100만 토큰: (입력, 캐시된 입력, 출력) — OpenAI 공식 가격표(2026-11, Standard) 기준
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}
COLUMNS = ("id", "ts", "day", "provider", "model", "purpose", "family_id", "ref", "ok", "error", "latency_ms",
           "prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens", "cost_usd", "request_id")
_warned_day: str | None = None


def prices() -> dict[str, tuple[float, float, float]]:
    """기본 단가표 + OBA_LLM_PRICES(JSON: {"모델": [입력, 캐시, 출력]})."""
    out = dict(PRICES)
    try:
        for model, v in json.loads(os.environ.get("OBA_LLM_PRICES") or "{}").items():
            out[model] = (float(v[0]), float(v[1]), float(v[2]))
    except (ValueError, TypeError, IndexError, AttributeError):
        print("[llm-usage] OBA_LLM_PRICES 형식이 잘못돼 무시합니다", flush=True)
    return out


def price_for(model: str) -> tuple[float, float, float] | None:
    """정확히 같은 이름, 없으면 가장 긴 접두사 (gpt-4o-mini-2024-07-18 → gpt-4o-mini)."""
    table = prices()
    if model in table:
        return table[model]
    keys = sorted((k for k in table if model.startswith(k)), key=len, reverse=True)
    return table[keys[0]] if keys else None


def cost(model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
    p = price_for(model or "")
    if p is None:
        return None
    uncached = max(0, prompt_tokens - cached_tokens)
    return round((uncached * p[0] + cached_tokens * p[1] + completion_tokens * p[2]) / 1_000_000, 8)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record(provider: str, model: str, purpose: str, ok: bool, latency_ms: int, prompt_tokens: int = 0,
           cached_tokens: int = 0, completion_tokens: int = 0, error: str = "", request_id: str = "") -> dict:
    """LLM 호출 한 번을 기록한다. 계정·통화는 지금 처리 중인 문맥에서 가져온다."""
    row = {
        "ts": time.time(), "day": today(), "provider": provider, "model": model or "", "purpose": purpose or "",
        "family_id": account.log_family.get() or "", "ref": account.log_ref.get() or "", "ok": int(bool(ok)),
        "error": (error or "")[:300], "latency_ms": int(latency_ms), "prompt_tokens": int(prompt_tokens or 0),
        "cached_tokens": int(cached_tokens or 0), "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
        "cost_usd": cost(model, int(prompt_tokens or 0), int(cached_tokens or 0), int(completion_tokens or 0)),
        "request_id": request_id or "",
    }
    with store._lock:
        db = store._db()
        db.execute(f"insert into llm_usage({', '.join(COLUMNS[1:])}) values ({', '.join('?' * (len(COLUMNS) - 1))})",
                   [row[c] for c in COLUMNS[1:]])
        db.commit()
    usd = f"${row['cost_usd']:.6f}" if row["cost_usd"] is not None else "$?"
    print(f"[llm-usage] {provider}/{row['model']} {row['purpose'] or '-'} {'ok' if ok else 'FAIL'} {latency_ms}ms "
          f"in={row['prompt_tokens']} cached={row['cached_tokens']} out={row['completion_tokens']} {usd} "
          f"family={row['family_id'] or '-'} ref={row['ref'] or '-'}" + (f" err={row['error']}" if error else ""),
          flush=True)
    return row


def daily_limit() -> float | None:
    try:
        v = float(os.environ.get("OBA_LLM_DAILY_USD_LIMIT", "") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


def spent_today() -> float:
    with store._lock:
        v = store._db().execute("select coalesce(sum(cost_usd), 0) from llm_usage where day=?", (today(),)).fetchone()[0]
    return float(v or 0)


def over_limit() -> bool:
    """오늘 추정 비용이 하루 한도를 넘었으면 True (그날 처음 한 번만 로그)."""
    global _warned_day
    limit = daily_limit()
    if limit is None or spent_today() < limit:
        return False
    if _warned_day != today():
        _warned_day = today()
        print(f"[llm-usage] 오늘 추정 비용이 하루 한도 ${limit}를 넘어 LLM을 쉬고 규칙 기반으로 처리합니다", flush=True)
    return True


# ---------- 보기 ----------

def _agg(where: str, args: tuple, group: str) -> list[dict]:
    sql = (f"select {group} as k, count(*), sum(ok), sum(prompt_tokens), sum(cached_tokens), sum(completion_tokens), "
           f"sum(total_tokens), sum(coalesce(cost_usd, 0)), sum(case when cost_usd is null then 1 else 0 end), "
           f"cast(avg(latency_ms) as integer) from llm_usage where {where} group by k order by k desc")
    with store._lock:
        rows = store._db().execute(sql, args).fetchall()
    return [{"key": r[0], "calls": r[1], "ok": r[2] or 0, "failed": r[1] - (r[2] or 0), "prompt_tokens": r[3] or 0,
             "cached_tokens": r[4] or 0, "completion_tokens": r[5] or 0, "total_tokens": r[6] or 0,
             "cost_usd": round(r[7] or 0, 6), "unpriced_calls": r[8] or 0, "avg_latency_ms": r[9] or 0} for r in rows]


def _account_info(fid: str) -> dict:
    fam = store.get("family", fid) if fid else None
    if not fam:
        return {"child_name": "", "origin": "계정 없음" if not fid else "삭제된 계정"}
    return {"child_name": fam.get("child_name", ""), "origin": fam.get("origin") or ("체험" if fam.get("demo") else "가입")}


def summary(days: int = 14, recent: int = 50) -> dict:
    since = (datetime.now() - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    total = _agg("day >= ?", (since,), "'all'")
    accounts = _agg("day >= ?", (since,), "family_id")
    for a in accounts:
        fid = a.pop("key") or ""
        a.update(family_id=fid, **_account_info(fid))
    teams: dict[str, dict] = {}
    for a in accounts:  # 팀(유입 경로)별 합계: SKT 체험 / vooy 체험 / vooy 가입 / 대시보드 가입
        t = teams.setdefault(a["origin"], {"key": a["origin"], "accounts": 0, **{k: 0 for k in (
            "calls", "ok", "failed", "prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens", "unpriced_calls")},
            "cost_usd": 0.0})
        t["accounts"] += 1
        for k in ("calls", "ok", "failed", "prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens",
                  "unpriced_calls"):
            t[k] += a[k]
        t["cost_usd"] = round(t["cost_usd"] + a["cost_usd"], 6)
    with store._lock:
        rows = store._db().execute(f"select {', '.join(COLUMNS)} from llm_usage order by id desc limit ?",
                                   (recent,)).fetchall()
    return {
        "since": since, "days": days,
        "limit_usd_per_day": daily_limit(), "spent_today_usd": round(spent_today(), 6),
        "total": total[0] if total else None,
        "today": (_agg("day = ?", (today(),), "'today'") or [None])[0],
        "by_day": _agg("day >= ?", (since,), "day"),
        "by_team": sorted(teams.values(), key=lambda t: -t["cost_usd"]),
        "by_account": sorted(accounts, key=lambda a: -a["cost_usd"]),
        "by_purpose": _agg("day >= ?", (since,), "purpose"),
        "by_model": _agg("day >= ?", (since,), "provider || '/' || model"),
        "recent": [dict(zip(COLUMNS, r)) for r in rows],
        "prices_per_1m_usd": {k: {"input": v[0], "cached_input": v[1], "output": v[2]} for k, v in prices().items()},
    }


def main() -> None:
    """`oba-usage [일수]` — 최근 사용량을 표로 출력한다 (서버에서는 fly ssh console -C /app/.venv/bin/oba-usage)."""
    import sys

    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 14
    s = summary(days)

    def line(label: str, r: dict | None) -> str:
        if not r:
            return f"{label:<24} 호출 0"
        usd = f"${r['cost_usd']:.4f}" + (f" (+단가 없는 {r['unpriced_calls']}건)" if r.get("unpriced_calls") else "")
        return (f"{label:<24} 호출 {r['calls']:>5} (실패 {r['failed']})  입력 {r['prompt_tokens']:>9,} (캐시 {r['cached_tokens']:,})  "
                f"출력 {r['completion_tokens']:>8,}  {usd}")

    print(f"LLM 사용량 — {s['since']}부터 {days}일 · 오늘 ${s['spent_today_usd']:.4f}"
          + (f" / 하루 한도 ${s['limit_usd_per_day']}" if s["limit_usd_per_day"] else " (하루 한도 없음)"))
    print(line("합계", s["total"]))
    print("\n[일별]");    [print(line(r["key"], r)) for r in s["by_day"]]
    print("\n[팀별]");    [print(line(f"{r['key']} ({r['accounts']}계정)", r)) for r in s["by_team"]]
    print("\n[용도별]");  [print(line(r["key"] or "-", r)) for r in s["by_purpose"]]
    print("\n[모델별]");  [print(line(r["key"], r)) for r in s["by_model"]]
    print("\n[계정별 상위 10]")
    for r in s["by_account"][:10]:
        print(line(f"{r['origin']} {r['child_name']} {r['family_id'][:10]}", r))
