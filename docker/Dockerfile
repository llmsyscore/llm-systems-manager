# Control-plane images (#384): build with --target manager or --target alarm-engine,
# published as ghcr.io/llmsyscore/llm-systems-manager/{manager,alarm-engine}.
FROM python:3.14-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/bash llmsys

WORKDIR /opt/llm-systems-manager
ENV PYTHONUNBUFFERED=1 \
    LLM_SYSTEMS_CONFIG=/opt/llm-systems-manager/config/llm-systems.toml \
    LSM_CONTAINERIZED=1


FROM base AS manager
# gcc: some pinned deps (e.g. psutil) ship no cp314 wheel and build from source.
COPY llm-systems-manager/backend/requirements.txt /tmp/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && apt-get purge -y gcc libc6-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY . .
# unified_config.py is gitignored; images build it from the sanitized example.
RUN cp config/unified_config.py.example config/unified_config.py \
    && mkdir -p data llm-systems-alarm-engine/data /var/log/llm-systems-manager \
    && chown -R llmsys:llmsys /opt/llm-systems-manager /var/log/llm-systems-manager \
    && chmod +x docker/*.sh
USER llmsys
WORKDIR /opt/llm-systems-manager/llm-systems-manager
# 5000 HTTP, 5443 HTTPS, 5444 WS proxy, 5445 SSE daemon
EXPOSE 5000 5443 5444 5445
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1
ENTRYPOINT ["/opt/llm-systems-manager/docker/entrypoint-manager.sh"]
CMD ["python3", "backend/llm-systems-manager.py"]


FROM base AS alarm-engine
COPY llm-systems-alarm-engine/requirements.txt /tmp/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && apt-get purge -y gcc libc6-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY . .
RUN cp config/unified_config.py.example config/unified_config.py \
    && mkdir -p data llm-systems-alarm-engine/data /var/log/llm-systems-manager \
    && chown -R llmsys:llmsys /opt/llm-systems-manager /var/log/llm-systems-manager \
    && chmod +x docker/*.sh
USER llmsys
WORKDIR /opt/llm-systems-manager/llm-systems-alarm-engine
EXPOSE 8081
# start-period covers the entrypoint's cert wait (up to LSM_AE_TLS_WAIT_S=90s).
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
    CMD curl -fsSk https://localhost:8081/health || curl -fsS http://localhost:8081/health || exit 1
ENTRYPOINT ["/opt/llm-systems-manager/docker/entrypoint-alarm-engine.sh"]
CMD ["python3", "-m", "backend.alarm_engine"]
