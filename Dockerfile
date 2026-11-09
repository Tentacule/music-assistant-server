# syntax=docker/dockerfile:1
#
# Two build modes (selected by build-docker.ps1):
#
#   Default — uses local deps image (fast after first build):
#     docker build --build-arg DEPS_IMAGE=tentacule/music-assistant:deps ...
#
#   UseOfficialBase — reuses the official MA image (fastest, ~30 s):
#     docker build --build-arg DEPS_IMAGE=ghcr.io/music-assistant/server:stable ...
#
# In both cases only the MA + frontend wheels are reinstalled, so the
# expensive requirements_all.txt download is never repeated.

ARG BASE_IMAGE_VERSION=latest
ARG DEPS_IMAGE=tentacule/music-assistant:deps
FROM --platform=$BUILDPLATFORM ghcr.io/music-assistant/base:$BASE_IMAGE_VERSION AS cliairplay-download

# Bump the version and checksum-manifest hash together.
ARG CLIAIRPLAY_VERSION=v0.5.9
ARG CLIAIRPLAY_CHECKSUMS_SHA256=b007597b927ae5c88bcf568b055b3d94647f9efa2e02249749e45e48c0861bc8
ARG TARGETARCH

# Download the cliairplay release asset for this image architecture.
RUN set -eu \
    && case "$TARGETARCH" in \
        amd64) CLIAIRPLAY_ARCH="x86_64" ;; \
        arm64) CLIAIRPLAY_ARCH="aarch64" ;; \
        *) echo "Unsupported cliairplay architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && CLIAIRPLAY_BINARY="cliairplay-linux-${CLIAIRPLAY_ARCH}" \
    && RELEASE_URL="https://github.com/music-assistant/airplay-cli/releases/download/${CLIAIRPLAY_VERSION}" \
    && wget -q "${RELEASE_URL}/SHA256SUMS" -O /tmp/SHA256SUMS \
    && wget -q "${RELEASE_URL}/${CLIAIRPLAY_BINARY}" -O "/tmp/${CLIAIRPLAY_BINARY}" \
    && echo "${CLIAIRPLAY_CHECKSUMS_SHA256}  /tmp/SHA256SUMS" | sha256sum --check - \
    && mkdir -p /cliairplay \
    && mv "/tmp/${CLIAIRPLAY_BINARY}" "/cliairplay/${CLIAIRPLAY_BINARY}" \
    && awk -v filename="$CLIAIRPLAY_BINARY" \
        '$2 == filename || $2 == "*" filename' \
        /tmp/SHA256SUMS > /tmp/cliairplay.sha256 \
    && test "$(wc -l < /tmp/cliairplay.sha256)" -eq 1 \
    && (cd /cliairplay && sha256sum --check /tmp/cliairplay.sha256) \
    && chmod 755 "/cliairplay/${CLIAIRPLAY_BINARY}" \
    && rm /tmp/SHA256SUMS /tmp/cliairplay.sha256

FROM scratch AS cliairplay
COPY --from=cliairplay-download /cliairplay /cliairplay

##############################################################################
# Builder — starts from the deps image (all Python packages pre-installed).
# When DEPS_IMAGE is the official MA image all system deps and cliairplay are
# already there; when it is our local deps image only the Python deps are.
##############################################################################
FROM $DEPS_IMAGE AS builder
ARG TARGETARCH

# uv might not be present in the official MA image — bring it unconditionally.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV VIRTUAL_ENV=/app/venv
ENV URLLIB3_NO_OVERRIDE=1

# Local wheels — the only layer that changes on every dev build.
ADD dist dist

# Install Music Assistant from prebuilt wheel.
ARG MASS_VERSION
RUN uv pip install \
    --no-cache \
    "music-assistant@dist/music_assistant-${MASS_VERSION}-py3-none-any.whl"

# Override the frontend with a local build when one is present in dist/.
RUN ls dist/music_assistant_frontend-*.whl 1>/dev/null 2>&1 && \
    uv pip install --no-deps --force-reinstall dist/music_assistant_frontend-*.whl || true

# Install cliairplay into the MA providers directory.
# Skip individual binaries that are already present (official-image base case).
COPY --from=cliairplay /cliairplay /tmp/cliairplay
RUN SITE_PACKAGES="$("$VIRTUAL_ENV/bin/python" -c \
        'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && CLIAIRPLAY_BIN_DIR="${SITE_PACKAGES}/music_assistant/providers/airplay/bin" \
    && mkdir -p "$CLIAIRPLAY_BIN_DIR" \
    && for f in /tmp/cliairplay/*; do \
         dest="$CLIAIRPLAY_BIN_DIR/$(basename "$f")"; \
         [ -f "$dest" ] || mv "$f" "$dest"; \
       done \
    && rm -rf /tmp/cliairplay \
    && chmod 755 "$CLIAIRPLAY_BIN_DIR"/cliairplay-linux-* \
    && "$CLIAIRPLAY_BIN_DIR"/cliairplay-linux-* --check

# Pre-compile Python bytecode for faster startup.
RUN $VIRTUAL_ENV/bin/python -m compileall -q $VIRTUAL_ENV/lib/python*/site-packages/music_assistant

# Identifies the official release image, see music_assistant/helpers/build_info.py
ARG MASS_REVISION
ARG MASS_WHEEL_SHA256
RUN printf '{"version": "%s", "revision": "%s", "wheel_sha256": "%s"}\n' \
    "$MASS_VERSION" "$MASS_REVISION" "$MASS_WHEEL_SHA256" > /app/build_info.json

# Permissive permissions so the container can run as non-root.
# IMPORTANT: do this in the builder stage, not the final stage, to avoid extra layers.
RUN chmod -R 777 /app \
    && chmod 755 \
        "$VIRTUAL_ENV"/lib/python*/site-packages/music_assistant/providers/airplay/bin/cliairplay-linux-*

##############################################################################
# Final image
##############################################################################
FROM ghcr.io/music-assistant/base:$BASE_IMAGE_VERSION

ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# tini as PID 1 reaps orphaned subprocesses; -s keeps that working under docker --init.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Copy the already-built /app dir from the builder stage.
COPY --from=builder /app /app

# /app itself needs 777 even though its contents already have it.
RUN chmod 777 /app

ARG MASS_VERSION
ARG TARGETPLATFORM
LABEL \
    org.opencontainers.image.title="Music Assistant Server" \
    org.opencontainers.image.description="Music Assistant is a free, opensource Media library manager that connects to your streaming services and a wide range of connected speakers. The server is the beating heart, the core of Music Assistant and must run on an always-on device like a Raspberry Pi, a NAS or an Intel NUC or alike." \
    org.opencontainers.image.source="https://github.com/music-assistant/server" \
    org.opencontainers.image.authors="The Music Assistant Team" \
    org.opencontainers.image.documentation="https://music-assistant.io" \
    org.opencontainers.image.licenses="Apache License 2.0" \
    io.hass.version="${MASS_VERSION}" \
    io.hass.type="addon" \
    io.hass.name="Music Assistant Server" \
    io.hass.description="Music Assistant Server" \
    io.hass.platform="${TARGETPLATFORM}" \
    io.hass.type="addon"

VOLUME [ "/data" ]
EXPOSE 8095

WORKDIR $VIRTUAL_ENV

# Entrypoint script that enables jemalloc for the main process only.
# MALLOC_CONF enables jemalloc's background thread so freed pages are returned to
# the OS while the process is idle; without it an idle server holds onto the peak
# RSS reached during startup (db migration, provider setup, metadata, image decode).
RUN printf '#!/bin/sh\n\
for path in /usr/lib/*/libjemalloc.so.2; do\n\
    [ -f "$path" ] && export LD_PRELOAD="$path" MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000" && break\n\
done\n\
exec mass "$@"\n' > /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/usr/local/bin/entrypoint.sh", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
