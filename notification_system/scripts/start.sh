#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

SERVICE_URL="http://localhost:8000"
GRAFANA_URL="http://localhost:3000"

usage() {
  echo "Usage: $0 [start|rebuild|stop]"
  echo ""
  echo "  start    - start the notification service"
  echo "  rebuild  - rebuild image and start"
  echo "  stop     - stop the notification service"
  echo ""
  echo "  Shared monitoring: $ROOT_DIR/scripts/monitoring.sh start"
  exit 1
}

ensure_network() {
  podman network exists sd_monitoring 2>/dev/null || \
    podman network create sd_monitoring &>/dev/null || true
}

CMD=${1:-start}

case "$CMD" in
  start)
    ensure_network
    echo "Starting notification service..."
    podman-compose up -d
    echo "Service: $SERVICE_URL"
    ;;

  rebuild)
    ensure_network
    echo "Rebuilding and starting notification service..."
    podman-compose up -d --build
    echo "Service: $SERVICE_URL"
    ;;

  stop)
    echo "Stopping notification service..."
    podman-compose down
    echo "Done."
    ;;

  *)
    usage
    ;;
esac
