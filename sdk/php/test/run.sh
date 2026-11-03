#!/usr/bin/env bash
# Starts `keel serve --transport http`, runs the PHP end-to-end test (natively, or in docker when php is missing), stops the server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PORT=$((20000 + RANDOM % 20000))
SECRET=s3cret
(cd "$ROOT" && KEEL_SECRET=$SECRET exec uv run keel serve examples/fleet --transport http --host 0.0.0.0 --port "$PORT" >/dev/null 2>&1) &
PID=$!
# a second server whose cartridge stores agents (registry: store), for the stored-agents calls
CART="$(mktemp -d)/fleet"
cp -R "$ROOT/examples/fleet" "$CART"
printf '\nregistry: store\nagents:\n  team-member:\n    writes: [task, agent]\n    reads: { kinds: [unit, tire, inspection] }\n' >> "$CART/cartridge.yaml"
PORT2=$((PORT + 1))
(cd "$ROOT" && KEEL_SECRET=$SECRET exec uv run keel serve "$CART" --transport http --host 0.0.0.0 --port "$PORT2" --events examples/fleet/events.jsonl >/dev/null 2>&1) &
PID2=$!
trap 'for p in $PID $PID2; do pkill -P $p 2>/dev/null || true; kill $p 2>/dev/null || true; done' EXIT
for port in $PORT $PORT2; do
  for _ in $(seq 1 200); do curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1 && break; sleep 0.1; done
done
if command -v php >/dev/null 2>&1; then
  php "$ROOT/sdk/php/test/fleet.php" "http://127.0.0.1:$PORT" "$SECRET" "http://127.0.0.1:$PORT2"
else
  docker run --rm -v "$ROOT/sdk/php:/app" -w /app php:8.3-cli php test/fleet.php "http://host.docker.internal:$PORT" "$SECRET" "http://host.docker.internal:$PORT2"
fi
