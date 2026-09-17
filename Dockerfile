# Multi-stage build. The `builder` stage has a C toolchain and pip's build
# cache; neither exists in the final image (see ADR 0009 Decision 4) — the
# runtime stage copies only the finished virtualenv and the source tree.
#
# One image, three processes: `command` is overridden per ECS task
# definition (infra/terraform/ecs.tf) to run the api, the relay, or the
# worker from this same image. The default CMD below is the api, so
# `docker run` with no override does something sensible.

FROM python:3.12-slim AS builder

# build-essential: some of asyncpg's transitive dependencies build from
# source on platforms with no matching prebuilt wheel. Installed only here —
# the runtime stage never sees apt or a compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# A venv, not `pip install --user` or a global install, so the ENTIRE
# runtime dependency set is one directory (/opt/venv) the next stage can
# copy in a single COPY — nothing to enumerate or miss.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Only requirements.txt, never requirements-dev.txt: pytest, ruff, and httpx
# have no reason to exist in anything that runs in production (ADR 0009).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

# Root only for adduser; every instruction after this runs as `app`.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# alembic.ini + migrations/: the release step (or an init container) runs
# `alembic upgrade head` from this same image, so migrations ship with the
# code that needs them rather than drifting in a separate deploy artifact.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini .

USER app

# Liveness only — the three processes have different real readiness checks
# (the api has /health/ready; the relay and worker have none exposed over
# HTTP), so per-service health lives in infra/terraform/ecs.tf's ALB target
# group / ECS health check config, not a single HEALTHCHECK that would be
# wrong for two of the three commands this image runs.
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
