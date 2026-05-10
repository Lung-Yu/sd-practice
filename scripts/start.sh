#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Wire docker-compose to Podman's socket when Docker daemon is absent
if ! docker info &>/dev/null 2>&1; then
  PODMAN_SOCK=$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)
  if [ -z "$PODMAN_SOCK" ]; then
    echo "ERROR: Docker daemon not running and no Podman machine socket found."
    echo "  Start Podman machine with: podman machine start"
    exit 1
  fi
  export DOCKER_HOST="unix://$PODMAN_SOCK"
  echo "Using Podman socket: $PODMAN_SOCK"
fi

echo "Starting QR Code Generator stack..."
echo "  App:        http://localhost:8100"
echo "  App UI:     http://localhost:8100/static/index.html"
echo "  Prometheus: http://localhost:9190"
echo "  Grafana:    http://localhost:3100  (admin / admin)"
echo ""

docker-compose up --build "$@"
