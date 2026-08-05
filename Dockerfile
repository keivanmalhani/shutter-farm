# shutter-farm: the toolchain as a batch container.
#
# Multi-stage so the runtime image carries no build tooling. Runs as a
# non-root user, works with a read-only root filesystem, needs no
# capabilities, and makes no outbound connections: the only port it opens
# is the metrics endpoint you choose to scrape.
#
# The heavy engines are installed here rather than vendored into the farm,
# because the farm dispatches to them and should never be the reason an
# image fails to build.

# ----------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

# Everything lands in one prefix so the runtime stage copies a single tree.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir .

# The engines. Pinned to tags at build time by ENGINE_REF so an image is
# reproducible; override to test a branch.
ARG ENGINE_REF=v0.1.0
ARG INSTALL_ENGINES=true
RUN if [ "$INSTALL_ENGINES" = "true" ]; then \
      pip install --no-cache-dir \
        "git+https://github.com/keivanmalhani/shutter-cull.git@${ENGINE_REF}" \
        "git+https://github.com/keivanmalhani/shutter-select.git@${ENGINE_REF}" ; \
    fi

# ----------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="shutter-farm" \
      org.opencontainers.image.description="Batch runner for the shutter toolchain: idempotent scheduled culling over a media volume" \
      org.opencontainers.image.source="https://github.com/keivanmalhani/shutter-farm" \
      org.opencontainers.image.licenses="MIT"

# exiftool for sidecar writes, ffmpeg for video analysis. No recommends,
# no docs: this is the difference between a 400MB image and a 1.2GB one.
RUN apt-get update && apt-get install -y --no-install-recommends \
        exiftool ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin farm

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FARM_ROOT=/media \
    FARM_STATE=/state/shutter-farm-state.json \
    FARM_METRICS_PORT=9090

# /media is the mount for the archive and can be read-only for a dry run.
# /state is a separate small volume, because the ledger must survive a
# restart even when the media mount does not allow writes.
RUN mkdir -p /media /state && chown farm:farm /state
VOLUME ["/media", "/state"]

USER farm
EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('FARM_METRICS_PORT','9090')+'/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["shutter-farm"]
CMD ["run"]
