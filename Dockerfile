ARG DENO_VERSION=2.9.8
FROM docker.io/denoland/deno:bin-${DENO_VERSION} AS deno

FROM python:3.12-slim-bookworm

ARG YTDLP_VERSION=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    DATA_DIR=/data

# Keep package postinst scripts from trying to start services during build.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        icecast2 \
        media-types \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/* /usr/sbin/policy-rc.d

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ -n "$YTDLP_VERSION" ]; then \
        pip install --no-cache-dir "yt-dlp==$YTDLP_VERSION"; \
    fi

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
COPY --from=deno /deno /usr/local/bin/deno
RUN chmod +x /entrypoint.sh /usr/local/bin/deno \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin radio \
    && mkdir -p /data \
    && chown -R radio:radio /app /data

USER radio

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
