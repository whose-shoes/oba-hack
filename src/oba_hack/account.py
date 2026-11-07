"""요청이 어느 계정(가족)의 것인지 — MCP 도구·테스트에서 쓰는 계정 문맥."""

from contextvars import ContextVar

# 테스트나 내부 호출에서 MCP 도구를 특정 계정으로 실행할 때 쓴다 (HTTP 요청은 개인 주소 토큰으로 판별)
override: ContextVar[str | None] = ContextVar("account_override", default=None)

# 지금 처리 중인 계정 — store.log가 이벤트에 family_id를 자동으로 붙인다
log_family: ContextVar[str | None] = ContextVar("log_family", default=None)
