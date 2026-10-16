# Keel: the five tools over HTTP (/v1, /mcp) for one cartridge, on SQLite or MySQL.
#
#   docker build -t keel .                                  # this checkout
#   docker build --build-arg KEEL_VERSION=0.22.0 -t keel .  # a release from PyPI
#   docker run -p 8000:8000 -e KEEL_SECRET=... -v ./examples/fleet:/cartridge:ro -v keel-data:/data keel
#
# The cartridge is not in the image: mount it at /cartridge, or `FROM ghcr.io/angelkurten/keel` and COPY it there.
# /data is the working directory: a SQLite graph, the `keel index` cursor (.keel/) and a .env live there.
FROM python:3.12-slim

ARG KEEL_VERSION=
ARG KEEL_EXTRAS=http2,mysql,chroma
COPY . /src
RUN if [ -n "$KEEL_VERSION" ]; then pip install --no-cache-dir "keel-context[$KEEL_EXTRAS]==$KEEL_VERSION"; \
    else pip install --no-cache-dir "/src[$KEEL_EXTRAS]"; fi \
    && rm -rf /src \
    && useradd --uid 10001 --create-home keel \
    && mkdir -p /data /cartridge && chown keel /data

USER keel
WORKDIR /data
VOLUME /data
EXPOSE 8000
ENV PYTHONUNBUFFERED=1

# no curl in the image: python checks /health (open, no secret needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# serve refuses a non-loopback host without KEEL_SECRET (or --allow-open behind your own auth)
ENTRYPOINT ["keel"]
CMD ["serve", "/cartridge", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
