# Official Python base pinned after beta image vulnerability review.
FROM python:3.12.16-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS base
ARG VERSION=0.1.0-beta.5
LABEL org.opencontainers.image.source="https://github.com/onelivewire/plex-bridge" \
      org.opencontainers.image.title="plex-bridge" \
      org.opencontainers.image.version=$VERSION
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY plex_bridge/ /app/plex_bridge/
COPY LICENSE NOTICE /app/
RUN addgroup -g 10001 bridge && adduser -D -u 10001 -G bridge bridge \
    && mkdir -p /data /private /config /exports/playback /exports/topology /handoff /state \
    && chown -R 10001:10001 /data /private /config /exports /handoff /state \
    && chmod 700 /data /private /config /exports/playback /exports/topology /handoff /state
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "plex_bridge.healthcheck"]

FROM base AS setup
ENV BRIDGE_SERVICE=setup
CMD ["python", "-m", "plex_bridge.setup"]

FROM base AS topology
ENV BRIDGE_SERVICE=topology
CMD ["python", "-m", "plex_bridge.runtime"]

FROM base AS playback
ENV BRIDGE_SERVICE=playback
CMD ["python", "-m", "plex_bridge.runtime"]

FROM base AS bundle
ENV BRIDGE_SERVICE=bundle
HEALTHCHECK --interval=15s --timeout=12s --start-period=15s --retries=3 \
    CMD ["python", "-m", "plex_bridge.healthcheck"]
CMD ["python", "-m", "plex_bridge.bundle"]
