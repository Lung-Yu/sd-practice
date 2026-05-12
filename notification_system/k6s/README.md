# Load Test — Notification Service

Target: **5 000 RPS**, p95 < 500 ms, error rate < 1%

## Prerequisites

- k6 v0.54+: https://grafana.com/docs/k6/latest/set-up/install-k6/
- Podman + podman-compose

## 1. Start the service in load-test mode

```bash
# From notification_system/
podman-compose \
  -f docker-compose.yml \
  -f load/docker-compose.loadtest.yml \
  up -d --build
```

This starts uvicorn with **4 workers** and `FAILURE_RATE=0` (no simulated failures, no retry overhead).

Verify health:

```bash
curl http://localhost:8000/
# → {"status":"ok"}
```

## 2. Run k6

```bash
k6 run load/k6.js

# Override base URL (remote host):
k6 run --env BASE_URL=http://your-host:8000 load/k6.js

# Write JSON results for CI / Grafana:
k6 run --out json=load/results.json load/k6.js
```

## 3. Tear down

```bash
podman-compose \
  -f docker-compose.yml \
  -f load/docker-compose.loadtest.yml \
  down
```

## Expected output (passing)

```
post_send_duration...:   p(95)=<500ms  p(99)=<1000ms  ✓
get_by_id_duration...:   p(95)=<500ms  p(99)=<1000ms  ✓
list_by_user_duration:   p(95)=<500ms  p(99)=<1000ms  ✓
notification_error_rate: 0.xx% < 1%                   ✓
notification_404_count:  NNN  ← expected in multi-worker mode, not an error
```

## Notes

### Traffic mix
| Endpoint | Weight |
|---|---|
| POST /send | 75% |
| GET /{id} | 20% |
| GET /?user_id= | 5% |

### Why FAILURE_RATE=0?
With `FAILURE_RATE=0.20` and `MAX_RETRIES=3`, up to 3 synchronous retries run
inside each failed request. At 5 000 RPS that's ~3 000 extra channel calls/sec
on the hot path, inflating p99 and obscuring true service capacity. The
loadtest env eliminates this to measure routing + serialisation + store overhead.

### Multi-worker 404s
4 uvicorn workers = 4 separate in-memory stores. A GET hitting a different
worker than the POST returns 404. These are counted in `notification_404_count`
but excluded from `notification_error_rate`. To eliminate them, use a single
worker by removing `--workers 4` from `load/docker-compose.loadtest.yml`.

### Scaling beyond 5 000 RPS
A single-host in-memory service will hit CPU saturation before 5 000 RPS on
most hardware. For higher targets: add nginx upstream pool, or replace the
in-memory store with Redis to support true horizontal scaling.
