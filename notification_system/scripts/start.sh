#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

GRAFANA_URL="http://localhost:3000"
SERVICE_URL="http://localhost:8000"

usage() {
  echo "Usage: $0 [start|rebuild|monitoring|stop]"
  echo ""
  echo "  start      - start the notification service"
  echo "  rebuild    - rebuild image and start"
  echo "  monitoring - start service + Prometheus + Grafana (with QR code)"
  echo "  stop       - stop all containers"
  exit 1
}

print_qr() {
  local url="$1"
  echo ""
  if command -v qrencode &>/dev/null; then
    echo "Scan to open Grafana dashboard:"
    qrencode -t ansiutf8 "$url"
  else
    echo "Install qrencode for QR code: brew install qrencode"
  fi
  echo "Grafana: $url  (admin access, no login required)"
  echo ""
}

CMD=${1:-start}

case "$CMD" in
  start)
    echo "Starting notification service..."
    podman-compose up -d
    echo "Service: $SERVICE_URL"
    ;;

  rebuild)
    echo "Rebuilding and starting notification service..."
    podman-compose up -d --build
    echo "Service: $SERVICE_URL"
    ;;

  monitoring)
    echo "Starting notification service + Prometheus + Grafana..."
    podman-compose --profile monitoring up -d
    echo ""
    echo "Waiting for Grafana to be ready..."
    for i in $(seq 1 20); do
      if curl -sf "$GRAFANA_URL/api/health" &>/dev/null; then
        break
      fi
      sleep 1
    done
    print_qr "$GRAFANA_URL/d/k6-notification"
    echo "Service:    $SERVICE_URL"
    echo "Prometheus: http://localhost:9090"
    ;;

  stop)
    echo "Stopping all containers..."
    podman-compose --profile monitoring down 2>/dev/null || podman-compose down
    echo "All containers stopped."
    ;;

  *)
    usage
    ;;
esac
