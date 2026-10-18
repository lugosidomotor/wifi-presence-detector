# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm AS base
ARG APP_UID=1000
ARG APP_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WIFI_DATA_DIR=/data HOME=/data
WORKDIR /app
# iw: SSID/BSSID/bitrate and power-save readout of the host's WiFi link (optional, read-only).
RUN apt-get update && apt-get install -y --no-install-recommends iw ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$APP_GID" wifi \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home --home-dir /data wifi \
    && mkdir -p /data && chown wifi:wifi /data /app
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt 'pyserial==3.5'
COPY --chown=wifi:wifi wifisense ./wifisense
COPY --chown=wifi:wifi config.yaml ./config.yaml
USER wifi
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('WIFI_WEB_PORT','8010')+'/health',timeout=4)" || exit 1
CMD ["python", "-m", "wifisense.main", "--config", "/app/config.yaml"]

FROM base AS test
USER root
RUN python -m pip install 'pytest==9.1.1' 'httpx==0.28.1'
COPY --chown=wifi:wifi pyproject.toml ./
COPY --chown=wifi:wifi tests ./tests
USER wifi
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
