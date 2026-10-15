#!/usr/bin/env bash
# Starts `keel serve --transport http`, runs the PHP end-to-end test (natively, or in docker when php is missing), stops the server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PORT=$((20000 + RANDOM % 20000))
SECRET=s3cret
(cd "$ROOT" && KEEL_SECRET=$SECRET exec uv run keel serve cartridges/fleet --transport http --host 0.0.0.0 --port "$PORT" >/dev/null 2>&1) &
PID=$!
trap 'pkill -P $PID 2>/dev/null || true; kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 200); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 0.1; done
if command -v php >/dev/null 2>&1; then
  php "$ROOT/sdk/php/test/fleet.php" "http://127.0.0.1:$PORT" "$SECRET"
else
  docker run --rm -v "$ROOT/sdk/php:/app" -w /app php:8.3-cli php test/fleet.php "http://host.docker.internal:$PORT" "$SECRET"
fi
