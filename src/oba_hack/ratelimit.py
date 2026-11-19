"""공개 데모 서버용 간단한 요청 제한 (메모리, 프로세스 하나 기준)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def allow(key: str, limit: int, window_sec: float) -> bool:
    """window_sec 동안 key별 limit번까지 허용."""
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window_sec:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(headers, fallback: str = "") -> str:
    """Fly.io 프록시가 넣는 Fly-Client-IP → X-Forwarded-For 첫 값 → 직접 연결 주소."""
    ip = headers.get("fly-client-ip") or (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return ip or fallback or "unknown"


def reset() -> None:
    with _lock:
        _hits.clear()
