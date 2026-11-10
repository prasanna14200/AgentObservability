FROM python:3.12-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements-container.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-container.lock

COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/agentsentinel-entrypoint

RUN chmod 0555 /usr/local/bin/agentsentinel-entrypoint \
    && groupadd --system --gid 10001 agentsentinel \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin agentsentinel \
    && mkdir -p /app/data/paypal_sandbox \
    && chown -R 10001:10001 /app/data

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/agentsentinel-entrypoint"]
