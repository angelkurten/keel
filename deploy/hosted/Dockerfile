# Hosted Keel's control plane: sign-up, the usage panel, and a Keel container per account (hosted/README.md).
#   docker build -f deploy/hosted/Dockerfile -t keel-hosted .
FROM python:3.12-slim
COPY . /src
RUN pip install --no-cache-dir "/src[http,mysql]" httpx python-multipart \
    && mkdir -p /app && cp -r /src/hosted /src/examples /app/ && chmod -R a+rX /app && rm -rf /src \
    && useradd --uid 10001 --create-home keel
USER keel
WORKDIR /app
ENV PYTHONUNBUFFERED=1 HOSTED_EXAMPLES_DIR=/app/examples
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["uvicorn", "hosted.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8080"]
