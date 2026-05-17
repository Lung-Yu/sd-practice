# Notification System — Load Test Learnings

## Tier 1 + Tier 2A Baseline (k6, 4-worker, FAILURE_RATE=0, Redis store)

### What the test showed

| Metric | Result | Target | Pass? |
|--------|--------|--------|-------|
| POST /send p95 | 544 ms | < 500 ms | ❌ |
| POST /send p99 | 738 ms | < 1000 ms | ✓ |
| GET /{id} p95 | 462 ms | < 500 ms | ✓ |
| GET list p95 | 466 ms | < 500 ms | ✓ |
| Error rate | 0.00% | < 1% | ✓ |
| 404s (cross-worker) | 0 | — | ✓ (Redis fixed this) |
| Actual throughput | ~1750 RPS | 5000 RPS | ❌ |
| Dropped iterations | 196,653 | 0 | ❌ |

### Why we only hit ~1750 RPS instead of 5000

**Little's Law**: required VUs = RPS × avg_latency_s

- At 244 ms avg latency → need 5000 × 0.244 = **1220 VUs** to sustain 5000 RPS
- k6 cap is 600 VUs → actual max = 600 / 0.244 ≈ **2459 RPS** theoretical
- We only got 1750 RPS because latency climbs under load (queueing)

The root cause: **POST /send is still synchronous end-to-end.** Each request pays:
1. Redis HSET/SET/ZADD pipeline (save as PENDING) — ~2 ms
2. `channel.send()` in thread pool — ~0.1 ms (no failure simulation)
3. Redis HSET/SET/ZADD pipeline again (save as SENT) — ~2 ms
4. Uvicorn/HTTP overhead

Even with zero simulated failures, 2 Redis round trips per request (both in the HTTP path) cap latency. Under high concurrency, queueing inflates this to 200+ ms median.

### What Redis fixed (vs old in-memory store)

- **Zero 404s** on GET /{id} — previously ~10–20% of cross-worker GETs returned 404 because workers had separate in-memory stores. Redis gives all 4 workers a shared view.
- **Global idempotency** — duplicate POSTs now dedup across all workers, not just within one process.
- **Durability** — restart no longer loses notifications (Redis AOF enabled).

### What's still broken

- **POST /send p95 > 500 ms** — fails the SLO because delivery is synchronous in the request path.
- **5000 RPS target not reachable** — fundamentally blocked until delivery is moved out of the HTTP path.

---

## Tier 2B: BackgroundTasks — What Happened and Why

**Pattern applied:** POST /send saves as PENDING → returns 202 → `deliver()` runs after response via `BackgroundTasks`.

### Tier 2B k6 results (same 4-worker, 5000 RPS target)

| Metric | Before (2A) | After (2B) | Δ |
|--------|-------------|------------|---|
| POST /send p95 | 544 ms | 579 ms | ❌ worse |
| GET /{id} p95 | 462 ms | 516 ms | ❌ worse |
| List p95 | 466 ms | 519 ms | ❌ worse |
| Throughput | ~1750 RPS | ~1767 RPS | same |
| Dropped iterations | 196,653 | 197,086 | same |

### Why BackgroundTasks made things WORSE

This is a classic FAANG trap. **BackgroundTasks uses the same thread pool as request handlers.**

FastAPI runs sync route handlers via `anyio.to_thread.run_sync` (a shared thread pool, default ~40 threads per worker process). BackgroundTasks for sync functions also use this thread pool — they just run *after* the response is written, but the thread is still occupied.

Under overload (1750 RPS, 600 VU cap, system already saturated):
- Each POST /send creates a background delivery task
- That task uses a thread from the same pool that's also serving new incoming requests
- Net effect: MORE contention on the same thread pool → all endpoints queue longer → p95 rises across the board

The key insight: **moving work to BackgroundTasks only helps if the system has spare thread capacity.** At 5000 RPS target (well above our ~2500 RPS ceiling), there is no spare capacity. You're just rearranging debt.

### What actually needs to happen

| Approach | Description | Result |
|----------|-------------|--------|
| FastAPI BackgroundTasks | Same process, same thread pool | ❌ Doesn't help under saturation |
| `asyncio.create_task` | Same process, async (no thread needed) | ✓ If channels are async |
| Separate delivery container | Different process, different CPU | ✓ True isolation |
| Redis Streams + worker | Queue decouples producers from consumers | ✓ Production pattern |

The correct Tier 2B is a **dedicated delivery worker container** reading from a Redis Stream — the HTTP workers only enqueue (`XADD`), the delivery workers only dequeue and call channel.send(). These never share a thread pool.

BackgroundTasks is the right *pattern* but wrong *implementation* when the service is already at thread pool saturation. At lower load (e.g., 2000 RPS on this hardware), BackgroundTasks would show clear improvement.

---

## NFR Improvement Scorecard

## Tier 2C: Separate Delivery Worker Container (Redis Streams)

**Architecture change:**
- HTTP workers: POST /send → save PENDING → `XADD notifications:delivery {notification_id}` → return 202 immediately
- Delivery worker (`delivery-worker` container): `XREADGROUP` → `store.get(id)` → `deliver()` → `XACK`
- Consumer group `delivery-workers`: Redis ensures each message is consumed by exactly one worker
- Worker uses `socket.gethostname()` as consumer name → unique per container

### Tier 2C k6 results

| Metric | 2A (Redis store) | 2B (BackgroundTasks) | 2C (Stream worker) |
|--------|-----------------|----------------------|--------------------|
| POST /send p95 | 544 ms ❌ | 579 ms ❌ | **466 ms ✓** |
| GET /{id} p95 | 462 ms ✓ | 516 ms ❌ | **450 ms ✓** |
| List p95 | 466 ms ✓ | 519 ms ❌ | **455 ms ✓** |
| Error rate | 0.00% ✓ | 0.00% ✓ | **0.00% ✓** |
| Throughput | ~1750 RPS | ~1767 RPS | **~2070 RPS** |
| All thresholds | ❌ 2 fail | ❌ 4 fail | **✓ ALL PASS** |

### Why it worked: true process isolation

The delivery worker is a **separate container** — isolated CPU, memory, and thread pool. HTTP path is now just:
```
POST /send → validate → idempotency check → Redis HSET (PENDING) → Redis XADD → return 202
```
Two cheap Redis ops, then done. `deliver()` never touches the HTTP thread pool.

---

## NFR Scorecard (all tiers)

| NFR | Original | Tier 1 | Tier 2A | Tier 2B | Tier 2C |
|-----|----------|--------|---------|---------|---------|
| POST p95 latency | unbounded | bounded 5s | 544 ms ❌ | 579 ms ❌ | **466 ms ✓** |
| Throughput | ~1750 RPS | ~1750 RPS | ~1750 RPS | ~1767 RPS | **~2070 RPS** |
| All thresholds pass | ❌ | ❌ | ❌ | ❌ | **✓** |
| Cross-worker 404s | ~10–20% | ~10–20% | 0% | 0% | **0%** |
| Idempotency | per-process | per-process | global | global | **global** |
| Durability | lost on restart | lost on restart | Redis AOF | Redis AOF | **Redis AOF** |
| Retry safety | thundering herd | exp. backoff + jitter | ← | ← | **← same** |
| Observability | none | Prometheus | Prometheus | Prometheus | **Prometheus** |
| Delivery isolation | none | none | none | partial | **full (separate process)** |

---

## Tier 3: Circuit Breaker, DLQ, Rate Limiting

### Circuit Breaker (`app/circuit_breaker.py` + `channels/registry.py`)

Hand-rolled state machine — no external dependency needed (~60 lines):

```
CLOSED → (N consecutive failures) → OPEN → (after recovery_seconds) → HALF_OPEN
HALF_OPEN → (success) → CLOSED
HALF_OPEN → (failure) → OPEN
```

Key insight: breakers live at **module level** in `registry.py` (`_BREAKERS` dict), not per-request. Each `get_channel()` call returns a `_ProtectedChannel` wrapping the same stateful breaker. This means state persists across all requests in a worker process.

Without the CB, a channel degraded to 90% failure rate makes every delivery attempt wait for `MAX_RETRIES × ATTEMPT_TIMEOUT_S = 15s` before giving up. With CB, after 5 consecutive failures the circuit trips OPEN: all subsequent calls fail-fast in microseconds instead of seconds, protecting worker thread capacity.

Metric: `circuit_breaker_trips_total{channel}` — alert when rate > 0.

### Dead-Letter Queue (`app/queue.py` + `app/delivery.py`)

When `deliver()` exhausts all retries, it pushes the notification_id to `notifications:dlq` (Redis List). Admin endpoints:
- `GET /admin/dlq` → depth + sample peek (non-destructive LRANGE)
- `POST /admin/dlq/retry?count=N` → pops N from DLQ, XADDs back to delivery stream

This gives ops a requeue button without re-architecting the delivery path.

### Per-User Rate Limiting (`app/routes.py`)

Fixed-window counter in Redis:
- Key: `ratelimit:{user_id}:{epoch // window_s}`
- INCR + conditional EXPIRE (2 round-trips, no Lua)
- Default: 100 requests / 60 seconds per user
- Returns 429 on breach; increments `rate_limit_hits_total` prometheus counter

Verified: 110 rapid requests from one user → 10 rejections (exactly over the 100 limit).

Metric: `rate_limit_hits_total` — alert on sustained 429 rate (signal of abuse or misconfigured client).

---

## Tier 3A: Nginx Load Balancer + Horizontal Scaling

### Architecture

```
k6 (8080) → nginx → notification-api-1..4 (each: 4 uvicorn workers)
                         ↓
                     Redis (shared state)
                         ↑
              delivery-worker (1 container, Redis Stream consumer)
```

nginx config: `keepalive 64` (reuse TCP connections to backends), `proxy_http_version 1.1`, round-robin.

### Benchmark comparison

| Config | Workers | Throughput | POST p95 | GET p95 | All pass? |
|--------|---------|------------|----------|---------|-----------|
| 1 container, 1 worker/container (broken) | 1 | ~1473 RPS | 1.48s ❌ | 1.43s ❌ | No |
| 1 container, 4 workers | 4 | ~2070 RPS | **466ms ✓** | 450ms ✓ | **Yes** |
| 4 containers, 4 workers each + nginx | 16 | ~2362 RPS | 590ms ❌ | **332ms ✓** | No |

### Key insight: READ vs WRITE scaling asymmetry

**GET /{id}** scales linearly with more replicas (−26%: 450ms→332ms). Each replica fetches from Redis independently; more replicas = more parallel reads.

**POST /send** gets WORSE under nginx at high concurrency (+27%: 466ms→590ms) because:
1. nginx adds a connection hop (~1-2ms + queuing under peak load)
2. At 5000 RPS target, all 600 VUs × connection pooling overhead compounds in the nginx→backend path
3. Round-robin can't perfectly balance write pressure; a momentarily slow backend stalls its keepalive connections

### The fix for POST at nginx scale

- **`least_conn` upstream**: route to the backend with fewest active connections — avoids head-of-line blocking from a slow replica
- **Async Python routes** (`async def` + `redis.asyncio`): eliminates thread pool entirely; POST returns in ~0.5ms (just 2 async Redis calls)
- **gRPC internal**: skip nginx for internal paths; use nginx only for the public edge

### Why nginx is still worth adding

Even though POST p95 failed the 500ms threshold, nginx delivered:
- Stable external endpoint regardless of how many backends are running
- Zero-downtime rolling restarts (nginx re-routes while a container restarts)
- `keepalive` reduces TCP handshake overhead by 60–80% vs plain reverse proxy
- Health check endpoint (`/nginx-health`) for load balancer probes
- Separation of concerns: TLS termination, rate limiting, request logging all move to nginx

The right takeaway: nginx LB works well for READ-heavy workloads at these concurrency levels. For write-heavy or latency-critical paths, async code + connection pooling is the next lever.

---

## Tier 3B: Async Routes + Redis Readiness

### Changes
- All route handlers converted to `async def` with `redis.asyncio` client (pool size 100)
- FastAPI startup event waits for Redis readiness (handles `BusyLoadingError` on AOF replay)
- `least_conn` added to nginx scale config (avoids round-robin head-of-line blocking)

### Results: single container, 4 workers, direct port 8000

| Metric | 2C Sync | 3B Async | Change |
|--------|---------|---------|--------|
| POST /send p95 | 466ms ✓ | **283ms ✓** | −39% |
| GET /{id} p95 | 450ms ✓ | **137ms ✓** | −69% |
| List p95 | 455ms ✓ | **176ms ✓** | −61% |
| Throughput | ~2070 RPS | **~3072 RPS** | +48% |
| Error rate | 0.00% ✓ | 0.17% ✓ | — |

Async routes remove the thread pool entirely for IO-bound paths. Each coroutine just suspends at the `await`, freeing the event loop to serve other requests — no thread context-switching overhead.

### Results: 4 containers × 4 workers + nginx + least_conn

| Metric | 3A (sync, round-robin) | 3B (async, least_conn) |
|--------|----------------------|----------------------|
| POST p95 | 590ms ❌ | 596ms ❌ |
| GET p95 | 332ms ✓ | **234ms ✓** |
| Error rate | 0.00% ✓ | **0.00% ✓** |
| Throughput | ~2362 RPS | ~2060 RPS |

### Key insight: async routes help single-container far more than multi-container

Single container + async: **3072 RPS** (best result yet). Nginx-scale + async: **2060 RPS** (FEWER than single container).

Why adding 4× compute with nginx gives FEWER RPS:
1. nginx hop adds ~50–100ms latency to every request under high concurrency
2. More containers = more Redis connection pools (20 uvicorn processes × 100+100 async connections = 4,000 potential connections); Redis connection management overhead grows
3. Little's Law: the nginx latency increase raises VUs-needed above the 600-VU cap more than additional parallelism lowers average latency

**The IO-bound scaling wall**: when the bottleneck is network round-trips to Redis (not CPU), adding more processes doesn't help. All 16 workers are waiting on the same Redis. More waiters = more queueing on the Redis connection pool, not more throughput.

### BusyLoadingError root cause + fix

Redis replays its AOF log on every restart. If API workers start before AOF replay completes, all Redis commands return `LOADING` → 500 errors. k6 `setup()` ran during this window → `seedIds = []` → all GET checks used fallback UUID → 500 (still loading) → 0% GET check success.

Fix: `@app.on_event("startup")` blocks worker initialization until `redis.ping()` succeeds. Delivery worker already had this; now HTTP API workers do too.

### What would actually fix POST p95 under nginx scale

- **Redis cluster**: shard write load across multiple Redis nodes — eliminates single-Redis bottleneck
- **Skip nginx for writes**: use DNS-based client-side load balancing (gRPC + service discovery) — removes nginx hop from the hot path  
- **Vertical scale**: 1 large container + more uvicorn workers outperforms N small containers + nginx for IO-bound workloads at these RPS levels

---

## Tier 4: Async Delivery Worker

**Change:** `worker.py` converted from synchronous blocking loop to `asyncio.gather()` concurrent batch processing. BATCH_SIZE lifted from 10 → 20.

```
# Before: sequential
for msg_id, data in msgs:
    notification = store.get(nid)
    deliver(notification)
    r.xack(...)

# After: concurrent
tasks = [_process_message(r, msg_id, data, loop) for msg_id, data in msgs]
await asyncio.gather(*tasks, return_exceptions=True)
```

Each task: `await store.aget(nid)` → `await loop.run_in_executor(None, deliver, notification)` → `await r.xack(...)`. The executor is needed because `channel.send()` is sync (simulates network latency with sleep).

**Key insight:** batch total time = `max(delivery_time)` instead of `sum(delivery_time)`. Under high failure rate with retries, this is N× faster per batch.

**Bug fixed:** `redis.asyncio` module has no `.exceptions` attribute — must use top-level `redis.exceptions` for `BusyLoadingError`, `ConnectionError`, `ResponseError`.

| Metric | 3B (sync worker) | 4 (async worker) | Δ |
|--------|-----------------|------------------|---|
| POST p95 | 283ms ✓ | 361ms ✓ | +28% |
| GET p95 | 137ms ✓ | 172ms ✓ | +25% |
| Throughput | ~3072 RPS | ~2736 RPS | −11% |
| All pass | ✓ | ✓ | — |

API-side regression: async worker creates 20 concurrent thread-pool tasks per batch, adding Redis write pressure from the worker side. The win is delivery throughput (drains backlogs faster), not API RPS.

---

## Tier 5: Failure Mode Testing (FAILURE_RATE=0.2)

### Connection Pool Exhaustion (first attempt — catastrophic failure)

Running with `max_connections=100` and 600 VUs caused **83% error rate** from `redis.exceptions.ConnectionError: Too many connections` — this is client-side pool exhaustion, NOT a Redis server problem. The two look identical in logs.

**Root cause:** with 600 VUs and async routes, up to 600 coroutines may simultaneously hold an open connection during `await pipeline.execute()`. Pool of 100 < 600 concurrent holders → exception raised immediately (redis-py does NOT block-and-wait by default).

**Fix:** `max_connections=1000` in both `store_redis.py` and `queue.py`.

**Formula:** `required_pool ≥ peak_concurrent_VUs × max_simultaneous_redis_ops_per_request`

### Results (FAILURE_RATE=0.2, pool=1000, 4 uvicorn workers)

All thresholds pass. API is 100% reliable despite 20% channel failure rate — circuit breaker and DLQ are invisible to HTTP clients.

| Metric | Result | Target |
|--------|--------|--------|
| POST p95 | 358ms | <500ms ✓ |
| GET p95 | 169ms | <500ms ✓ |
| Error rate | 0.00% | <1% ✓ |
| All checks | 621,538/621,538 | 100% ✓ |

**Reliability machinery active:**
- Circuit breaker trips: email=3567, sms=2831, push=1633 — CB oscillates OPEN→HALF_OPEN→CLOSED as channel failures are intermittent
- DLQ accumulated: 22,104 entries — CB OPEN fast-fail → DLQ (bypasses all 3 retries), so DLQ >> theoretical 0.8% permanent failure rate
- Total retries: ~7,052 across channels

### DLQ Retry Verification

After restoring FAILURE_RATE=0 (simulating channel recovery), replayed the entire DLQ:

```bash
POST /admin/dlq/retry?count=5000  # × 4 batches + 1 final batch
# DLQ: 22,104 → 11,904 → 6,904 → 1,904 → 0
```

All 22,104 previously FAILED notifications delivered successfully. Status updated to `sent`; `sent_at` timestamp reflects the retry time.

**Design quirk discovered:** `error` field is NOT cleared on successful retry — it preserves the previous circuit breaker message even after status becomes `sent`. Fix: clear `notification.error = None` in the success branch of `deliver()`.

**DLQ drain rate:** ~733/second at FAILURE_RATE=0 (no retries needed, fast delivery).

**Production ops playbook:**
1. DLQ depth alert fires (threshold: > 1000)
2. Diagnose channel health (circuit breaker state, external provider status)
3. Wait for channel recovery (or fix underlying issue)
4. Replay: `POST /admin/dlq/retry?count=N` in batches
5. Monitor: `notifications_sent_total{status="SENT"}` rises, DLQ depth falls to 0

---

## Tier 6: Multi-Worker Delivery Scaling (4 × delivery-worker)

### Setup

Scaled delivery-worker to 4 container instances sharing the `delivery-workers` consumer group:

```bash
FAILURE_RATE=0 MAX_RETRIES=1 podman-compose -f docker-compose.yml \
  -f k6s/docker-compose.loadtest.yml up -d --scale delivery-worker=4
```

**Port conflict fixed first:** removed `ports: ["8001:8001"]` from `docker-compose.yml` — static host port binding prevents scaling. Prometheus reaches workers via `sd_monitoring` network DNS (round-robins across instances).

### Consumer Group Distribution

Each worker uses `socket.gethostname()` as consumer name → unique container ID per instance. Redis XREADGROUP `>` ensures exactly-once delivery across all consumers.

| Worker | Container ID | Messages delivered |
|--------|--------------|--------------------|
| 1 | a2a57a26da65 | 16,423 |
| 2 | 8a68aa5d4ea7 | 16,701 |
| 3 | d7c848441057 | 16,652 |
| 4 | feb87a153fb3 | 16,769 |
| **Total** | | **66,545** |

Distribution: ~25% per worker. No duplicates (consumer group guarantees exactly-once claim per message).

### Latency Regression

| Config | POST p95 | GET p95 | Throughput | All pass? |
|--------|----------|---------|------------|-----------|
| 1 delivery worker (Tier 4) | 361ms ✓ | 172ms ✓ | 2,736 RPS | ✓ |
| 4 delivery workers (Tier 6) | **1,450ms ❌** | **532ms ❌** | **800 RPS** | ❌ |

Error rate stayed 0.00% — no connection errors, pure latency degradation.

### Root Cause: Redis Single-Threaded Command Queue Saturation

Each delivery worker runs `asyncio.gather()` at BATCH_SIZE=20:
- `store.aget(nid)` → Redis HGETALL per message
- `loop.run_in_executor(None, deliver, notification)` → sync `store.save()`: pipeline(HSET + SET + ZADD) per message
- `r.xack()` → XACK per message

4 workers × 20 concurrent delivers = **80 simultaneous Redis pipelines** from workers alone.

Plus API: 600 VUs × 4 async Redis ops each = hundreds of concurrent API commands.

Redis is single-threaded. With 80+ delivery pipelines queued, every Redis command waits proportionally longer. This cascades directly to API latency — each POST /send makes 4 Redis calls, so if each call waits 10× longer, p95 jumps ~4× longer.

**IO-bound scaling wall — advanced edition:** adding more delivery workers adds more Redis contention, not more compute. The bottleneck is the serialization point (Redis), not the workers themselves.

### BATCH_SIZE inverse scaling rule

With a single Redis, the total concurrent delivery pipeline count is the lever:
```
total_concurrent_deliveries = num_workers × BATCH_SIZE
```

To preserve the same Redis command rate when scaling workers:
- 1 worker, BATCH_SIZE=20 → 20 concurrent
- 4 workers, BATCH_SIZE=5  → 20 concurrent (same Redis pressure, 4× delivery containers)

### What would actually fix multi-worker scaling

| Approach | Description |
|----------|-------------|
| Separate delivery Redis | API uses Redis-A (state); worker uses Redis-B (stream + delivery writes) — independent workloads, no cross-contention |
| Redis Cluster | Shard delivery writes to different nodes from API reads |
| Reduce BATCH_SIZE inversely | `BATCH_SIZE = target_concurrency / num_workers` — simple config fix, same Redis load |
| Dedicated delivery store | Worker writes to a separate fast store (e.g., Cassandra) for delivery status; API reads from primary Redis |

The right production pattern: **two Redis instances** — API-facing Redis for idempotency, rate limiting, and notification state reads; delivery Redis for the Stream, ACK, and delivery status writes.

### What This Test Validated

Despite the latency failure, Tier 6 confirmed:
1. **Exactly-once delivery across N workers** — Redis consumer group works correctly at scale
2. **Consumer name = hostname = unique per container** — no coordination needed for unique consumer IDs in docker-compose/Kubernetes
3. **Even load distribution** — ~25% per worker with zero configuration (Redis natural distribution)
4. **Bottleneck is Redis, not workers** — workers are idle-capable; the serialization point is the single Redis instance
