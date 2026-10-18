#!/bin/sh
# Smoke test of a Keel image: health, ingest over HTTP, a rule evaluated by context, auth enforced.
#   deploy/smoke.sh ghcr.io/angelkurten/keel:0.27.1
set -eu
IMAGE=${1:-keel:local}
PORT=${PORT:-18000}
NAME=keel-smoke-$$
URL=http://127.0.0.1:$PORT
docker run -d --name "$NAME" -p "$PORT:8000" -e KEEL_SECRET=smoke -e KEEL_DB=/data/keel.db \
  -v "$(pwd)/examples/fleet:/cartridge:ro" "$IMAGE" >/dev/null
trap 'docker rm -f "$NAME" >/dev/null' EXIT
status=starting
for _ in $(seq 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
  [ "$status" = healthy ] && break
  sleep 2
done
[ "$status" = healthy ] || { docker logs "$NAME"; echo "not healthy: $status"; exit 1; }

auth="Authorization: Bearer smoke"
tenant="X-Keel-Tenant: acme-logistics"
events=$(python3 -c "import json; print(json.dumps([json.loads(l) for l in open('examples/fleet/events.jsonl') if l.strip() and not l.startswith('#')]))")
curl -fsS -X POST "$URL/v1/events" -H "$auth" -H "$tenant" -H 'Content-Type: application/json' -d "$events" | grep -q '"processed":12'
curl -fsS -X POST "$URL/v1/context" -H "$auth" -H "$tenant" -H 'Content-Type: application/json' -d '{"subject":"tire:T-9001"}' | grep -q '"tire_critical":true'
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/v1/context" -H "$tenant" -H 'Content-Type: application/json' -d '{"subject":"tire:T-9001"}')
[ "$code" = 401 ] || { echo "expected 401 without the secret, got $code"; exit 1; }
[ "$(docker exec "$NAME" id -u)" != 0 ] || { echo "runs as root"; exit 1; }
echo "smoke ok: $IMAGE"
