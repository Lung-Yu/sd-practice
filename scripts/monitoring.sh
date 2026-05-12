#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

GRAFANA_URL="http://localhost:3000"
COMPOSE_FILE="docker-compose.monitoring.yml"

usage() {
  echo "Usage: $0 [start|stop|status]"
  echo ""
  echo "  start   - start shared Prometheus + Grafana"
  echo "  stop    - stop monitoring stack"
  echo "  status  - show running monitoring containers"
  exit 1
}

print_qr() {
  local url="$1"
  echo ""
  if command -v qrencode &>/dev/null; then
    echo "Scan to open Grafana:"
    qrencode -t ansiutf8 "$url"
  else
    echo "  brew install qrencode  for QR code"
  fi
  echo "  Grafana:    $url"
  echo "  Prometheus: http://localhost:9090"
  echo ""
}

ensure_network() {
  podman network exists sd_monitoring 2>/dev/null || \
    podman network create sd_monitoring &>/dev/null || true
}

CMD=${1:-start}

case "$CMD" in
  start)
    ensure_network
    echo "Starting shared monitoring stack..."
    podman-compose -f "$COMPOSE_FILE" up -d
    echo "Waiting for Grafana..."
    for i in $(seq 1 20); do
      curl -sf "$GRAFANA_URL/api/health" &>/dev/null && break
      sleep 1
    done
    print_qr "$GRAFANA_URL"
    echo "  Dashboards:"
    echo "    QR Code Generator  : ${GRAFANA_URL}/d/qr-code-gen"
    echo "    Notification (k6)  : ${GRAFANA_URL}/d/k6-notification"
    ;;

  stop)
    echo "Stopping monitoring stack..."
    podman-compose -f "$COMPOSE_FILE" down
    echo "Done."
    ;;

  status)
    podman ps --filter "name=sd-practice" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ;;

  *)
    usage
    ;;
esac
