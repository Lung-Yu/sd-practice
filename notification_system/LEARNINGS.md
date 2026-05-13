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
