# The support showcase at demo.angelkurten.com (docs/design/showcase.md): the page, both agents, the RAG index and the judge.
#   docker build -f deploy/showcase/Dockerfile -t keel-showcase .
#   docker run -p 8080:8080 -e OFOX_API_KEY=... -e KEEL_URL=https://<account>.keel.angelkurten.com -e KEEL_KEY=kk_... -v showcase:/data keel-showcase
FROM python:3.12-slim
COPY . /src
RUN pip install --no-cache-dir "/src[http]" httpx \
    && mkdir -p /app/examples /data && cp -r /src/showcase /app/ && cp -r /src/examples/relay /app/examples/ \
    && chmod -R a+rX /app && rm -rf /src && useradd --uid 10001 --create-home keel && chown keel /data
USER keel
WORKDIR /app
ENV PYTHONUNBUFFERED=1 SHOWCASE_DATA=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["uvicorn", "showcase.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"]
