# don't call me, Mama — 데모 서버 이미지 (Fly.io 등 컨테이너 호스팅용)
FROM python:3.11-slim

# 주문·폴링 시각을 한국 시간으로 기록·표시
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# SQLite·통화 파일은 볼륨(/data)에 둔다
ENV OBA_DATA_DIR=/data OBA_HOST=0.0.0.0 OBA_PORT=8080
EXPOSE 8080
CMD ["/app/.venv/bin/oba-hack"]
