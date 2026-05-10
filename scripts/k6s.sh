#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8100}"
PROM_URL="${K6_PROMETHEUS_RW_SERVER_URL:-http://localhost:9190/api/v1/write}"
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/k6/load_test.js"

if ! command -v k6 &>/dev/null; then
  echo "k6 not found. Install with: brew install k6"
  exit 1
fi

echo "Running k6 load test against $BASE_URL"
echo "Pushing metrics to Prometheus at $PROM_URL"
echo ""

K6_PROMETHEUS_RW_SERVER_URL="$PROM_URL" \
K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM=false \
K6_PROMETHEUS_RW_TREND_STATS="p(50),p(95),p(99)" \
k6 run \
  --out experimental-prometheus-rw \
  -e BASE_URL="$BASE_URL" \
  "$SCRIPT" "$@"
