# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock requirements.lock README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels --no-deps . \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

RUN groupadd --gid 1000 argus \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/argus argus \
    && mkdir -p /app/data /app/artifacts /app/logs \
    && chown -R 1000:1000 /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

WORKDIR /app
COPY --chown=argus:argus configs ./configs
COPY --chown=argus:argus scripts ./scripts
COPY --chown=argus:argus docs ./docs
COPY --chown=argus:argus README.md report.md plan.md wallex.md ./

USER argus
EXPOSE 8080
CMD ["python", "-m", "wallex_trader", "run"]
