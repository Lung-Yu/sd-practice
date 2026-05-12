# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

A collection of independent system design exercises. Each exercise is a self-contained directory with its own infrastructure, implementation, and load tests. Exercises are built with Python + FastAPI and run via Podman + podman-compose.

## Key Commands

### Shared Monitoring (start this first)
```bash
# From repo root
./scripts/monitoring.sh start    # Prometheus :9090 + Grafana :3000
./scripts/monitoring.sh stop
```

### notification_system
```bash
cd notification_system
./scripts/start.sh start         # start service (auto-creates sd_monitoring network)
./scripts/start.sh rebuild       # rebuild image + start
./scripts/start.sh stop

# Load test (with live Grafana output)
K6_PROMETHEUS_RW_SERVER_URL=http://localhost:9090/api/v1/write \
K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM=false \
k6 run -o experimental-prometheus-rw k6s/k6.js

# Load test in 4-worker high-throughput mode (FAILURE_RATE=0)
podman-compose -f docker-compose.yml -f k6s/docker-compose.loadtest.yml up -d --build
```

### qr_code_generator
```bash
cd qr_code_generator
./scripts/start.sh               # full stack (postgres, redis, nginx, varnish, 4 app instances)
./scripts/k6s.sh                 # k6 load test → Prometheus remote write
```

### Adding a new exercise
```
/new-exercise <topic_name>
```
This slash command scaffolds `PROMPT.md`, `README.md`, updates the root README table, and commits.

## Architecture

### Project-level Shared Monitoring

All exercises share a single Prometheus + Grafana instance via the `sd_monitoring` Docker network:

```
docker-compose.monitoring.yml     ← creates sd_monitoring network + Prometheus + Grafana
monitoring/
  prometheus.yml                  ← scrapes qr_code app1-4 via sd_monitoring; accepts k6 remote write
  grafana/dashboards/             ← one JSON per exercise (qr-code-gen, k6-notification)
```

Exercise docker-composes reference `sd_monitoring` as an external network. Each `start.sh` auto-creates the network (`podman network create sd_monitoring`) if monitoring isn't running yet, so exercises can start independently.

- **notification_system** pushes k6 metrics via Prometheus remote write (`--out experimental-prometheus-rw`)
- **qr_code_generator** app instances expose `/metrics` scraped by Prometheus; app1–4 join `sd_monitoring` so Prometheus can reach them by service name

### notification_system

Sync delivery pipeline — `POST /send` blocks until done:

```
routes.py → idempotency.py (sha256 hash) → store.py (in-memory, thread-safe)
         → delivery.py (retry loop, 1–3 attempts) → channels/registry.py
         → EmailChannel / SMSChannel / PushChannel (stdout simulation)
```

- **Idempotency key**: `sha256(user_id|topic|message)` — duplicate POST returns existing record without re-delivering
- **State machine**: `PENDING → SENT | FAILED` (transitions happen synchronously within the request)
- **Failure simulation**: `FAILURE_RATE` env var (default 0.20); `MAX_RETRIES` (default 3)
- **Store**: module-level singleton with `threading.Lock` over three dicts (`_by_id`, `_by_key`, `_by_user`)
- **Adding a channel**: one line in `channels/registry.py._REGISTRY`; implement `BaseChannel.send()`

### qr_code_generator

Multi-tier production-like stack:

```
nginx-global (:8100) → nginx-site1/site2 → app1–4 (FastAPI)
                                                  ↓
varnish (:8200) → nginx-origin → app1–4     postgres (primary + replica via pgbouncer)
                                                  ↓
                                             redis (cache)
```

App code is in `scaffold/app/`. Core logic lives in `routes.py` (redirect flow), `token_gen.py`, `url_validator.py`, `cache.py`, and `metrics.py` (Prometheus instrumentation).

## Exercise Template

Every exercise follows this pattern:
- `PROMPT.md` — design questions (answered in-place) + curl verification tests
- `docker-compose.yml` — full infrastructure; joins `sd_monitoring` external network
- `scripts/start.sh` — start/stop helpers
- `k6s/` or `k6/` — load test scripts targeting 5000 RPS, p95 < 500ms

## Podman Notes

The repo uses `podman-compose` (not `docker-compose`). The `qr_code_generator/scripts/start.sh` bridges to `docker-compose` CLI via `DOCKER_HOST` pointing at the Podman socket — this is already handled in that script. All other scripts use `podman-compose` directly.
