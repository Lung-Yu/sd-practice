# Tier 3B：Async Routes + Redis 就緒等待

**實施日期：** 2026-05-14

## 改動摘要

Tier 3A 揭示了兩個問題：round-robin 無法感知後端負載，以及同步 routes 在 IO-bound 場景下浪費 uvicorn thread。Tier 3B 同時修復這兩個問題：

1. 所有路由從 `def` 改為 `async def`，搭配 `redis.asyncio` 非阻塞 client
2. `@app.on_event("startup")` 在 Redis 就緒前阻塞 worker 初始化（修復 `BusyLoadingError`）
3. nginx upstream 改用 `least_conn` 取代 round-robin

---

## 實作細節

### 1. 路由層：`def` → `async def`

```python
# 改前（同步）
@router.post("/send")
def send_notification(req: SendRequest):
    notification = store.save(...)       # 阻塞 uvicorn thread
    queue.enqueue(notification)          # 阻塞 uvicorn thread
    return notification

# 改後（非同步）
@router.post("/send")
async def send_notification(req: SendRequest):
    notification = await store.asave(...)    # 釋放 event loop
    await queue.aenqueue(notification)       # 釋放 event loop
    return notification
```

`async def` 路由在 uvicorn 的 event loop 中執行（非 thread pool）。每當遇到 `await`，event loop 可以切換去處理其他 coroutine，讓單一 uvicorn worker 在 IO-bound 場景中同時服務數百個並發請求。

### 2. `store_redis.py`：加入 asyncio client

```python
import redis.asyncio as aioredis

class RedisNotificationStore:
    def __init__(self, redis_url: str):
        self._r = redis.Redis.from_url(redis_url)             # 同步 client（舊介面保留）
        self._async_r = aioredis.from_url(
            redis_url,
            max_connections=100                                # 預設 10，遠不夠用
        )

    async def asave(self, notification: Notification) -> None:
        pipe = self._async_r.pipeline()
        pipe.hset(f"notification:{notification.id}", mapping=asdict(notification))
        pipe.set(f"idempotency:{notification.idempotency_key}",
                 notification.id, ex=86400)
        pipe.zadd(f"user:{notification.user_id}:notifications",
                  {notification.id: notification.created_at})
        await pipe.execute()

    async def aget(self, notification_id: str) -> Notification | None:
        data = await self._async_r.hgetall(f"notification:{notification_id}")
        if not data:
            return None
        return Notification(**data)

    async def alist_for_user(self, user_id: str) -> list[Notification]:
        ids = await self._async_r.zrange(f"user:{user_id}:notifications", 0, -1)
        if not ids:
            return []
        pipe = self._async_r.pipeline()
        for nid in ids:
            pipe.hgetall(f"notification:{nid}")
        results = await pipe.execute()
        return [Notification(**r) for r in results if r]
```

`pipeline()` 在 asyncio 版本中同樣可用，批次操作只需一次 TCP 往返，與同步版本的語意相同。

### 3. `store.py`（in-memory fallback）：async shim

In-memory store 的操作是純 CPU，不涉及 IO，不需要真正的 async：

```python
class NotificationStore:
    async def asave(self, notification: Notification) -> None:
        return self.save(notification)     # 直接呼叫同步版本

    async def aget(self, notification_id: str) -> Notification | None:
        return self.get(notification_id)   # in-memory dict，不需要 await

    async def alist_for_user(self, user_id: str) -> list[Notification]:
        return self.list_for_user(user_id)
```

這樣路由層可以統一用 `await store.asave()`，不需要判斷底層是 Redis 還是 in-memory。

### 4. `queue.py`：加入 async enqueue

```python
class RedisQueue:
    def _get_async_client(self):
        if not hasattr(self, "_async_r"):
            self._async_r = aioredis.from_url(self._redis_url, max_connections=50)
        return self._async_r

    async def aenqueue(self, notification: Notification) -> str:
        r = self._get_async_client()
        entry_id = await r.xadd(
            self.stream_key,
            {"notification_id": notification.id}
        )
        return entry_id
```

### 5. `main.py`：Redis 就緒等待

```python
@app.on_event("startup")
async def wait_for_redis():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return
    r = aioredis.from_url(redis_url)
    for attempt in range(30):
        try:
            await r.ping()
            logger.info("Redis ready")
            return
        except Exception as e:
            logger.warning(f"Redis not ready (attempt {attempt+1}/30): {e}")
            await asyncio.sleep(1)
    raise RuntimeError("Redis did not become ready in 30 seconds")
```

這個 startup hook 在第一個請求到達之前阻塞 uvicorn 的 lifespan 初始化。在 Redis 還在重播 AOF log 的時候，`ping()` 會拋出 `BusyLoadingError`，不會被誤判為就緒，保證所有 worker 在 Redis 完全準備好後才開始接受請求。

---

## 效能測試結果

### 單容器測試（4 workers，直連 :8000）

**測試條件：** FAILURE_RATE=0、target 5000 RPS、600 VU、k6 ramping-arrival-rate

| 指標 | Tier 2A Sync | Tier 3B Async | 變化 |
|------|------------|--------------|------|
| POST /send p95 | 466ms ✓ | **283ms ✓** | -39% |
| GET /{id} p95 | 450ms ✓ | **137ms ✓** | -69% |
| List p95 | 455ms ✓ | **176ms ✓** | -61% |
| 吞吐量 | ~2070 RPS | **~3072 RPS** | +48% |
| 錯誤率 | 0.00% ✓ | 0.17% ✓ | — |
| 所有閾值 | ✓ | **✓** | 維持通過 |

GET p95 從 450ms 降至 137ms（-69%），因為單一 uvicorn worker 的 event loop 現在可以同時並發數十個 `HGETALL` 而不是排隊等待。POST 同樣大幅改善，從 466ms 降至 283ms（-39%）。

### nginx-scale 測試（4 容器 × 4 workers，:8080，least_conn）

| 指標 | Tier 3A（sync，round-robin） | Tier 3B（async，least_conn） |
|------|---------------------------|--------------------------|
| POST p95 | 590ms ❌ | 596ms ❌ |
| GET p95 | 332ms ✓ | **234ms ✓** |
| 錯誤率 | 0.00% ✓ | **0.00% ✓** |
| 吞吐量 | ~2362 RPS | ~2060 RPS |

POST p95 在 nginx-scale 模式下仍然超出 500ms 閾值。

---

## 反直覺發現：async + 4 容器 < async + 1 容器

**單容器 async：3072 RPS**
**4 容器 async + nginx：2060 RPS**

加了 3 個容器，吞吐量反而下降 33%。原因分析：

### 1. nginx 跳躍的延遲放大效應

在單容器模式，k6 直連 :8000，沒有中間層。在 nginx-scale 模式，每個請求多一個 nginx→backend 的網路跳躍。這 1–2ms 的靜態開銷在 600 VU 高並發下被 Little's Law 放大：

```
Little's Law：L = λ × W

單容器：  W = 100ms（平均）→ λ = 600 / 0.100 = 6000 RPS（理論上限）
nginx-scale：W = 150ms（多了 nginx 延遲 + 排隊）→ λ = 600 / 0.150 = 4000 RPS（理論上限）

實際 k6 VU 上限 600 → 更高的 W 直接壓低理論最大 λ
```

### 2. Redis 連線池爆炸性增長

```
單容器 async：4 workers × 100 connections = 400 個潛在 Redis 連線
4 容器 async：16 workers × 100 connections = 1,600 個潛在 Redis 連線
```

Redis 的連線管理（TCP accept、心跳、connection state）需要消耗 Redis server 自身的 CPU。在 IO-bound 場景中，所有 1,600 個連線都在等同一個 Redis 實例的回應，連線管理 overhead 本身變成新的瓶頸。

### 3. IO-bound 工作的水平擴展極限

```
16 個 worker → 16 個 event loop → 每個都等 Redis
                                          ↓
                              Redis 每秒能處理的命令數固定
                              更多 waiter ≠ 更多 throughput
```

在 CPU-bound 場景，更多 worker 直接等於更多並行計算。在 IO-bound 場景，瓶頸在 IO 端（Redis），加 worker 只是增加了更多「在排隊等 Redis」的 coroutine，不能突破 Redis 本身的命令處理速率上限。

**垂直擴展在 IO-bound 場景往往優於水平擴展：** 1 個大容器 + 8 uvicorn workers，比 4 個小容器 × 2 workers + nginx 更高效，因為前者沒有 nginx 跳躍，且 Redis 連線數是後者的一半。

---

## BusyLoadingError 偵錯故事

這是 Tier 3B 最重要的學習，完整還原偵錯過程。

### 現象：第一次 nginx-scale 測試，GET 通過率 0%

```
✗ get /{id} status 200
    ↳ 0.00% — 0 / 2670

http_req_failed: 6.92%
```

GET 請求「全軍覆沒」，一個都沒成功。但 POST 還有部分成功。

### 假設 1：nginx 路由設定錯誤

檢查 nginx access log，GET 請求確實抵達了後端，後端回傳了 500，不是 404。502 Bad Gateway 也沒有。排除 nginx 路由問題。

### 假設 2：k6 腳本 check 邏輯錯誤

```javascript
// k6 setup()：建立 200 個 notification，收集 IDs
export function setup() {
    let seedIds = [];
    for (let i = 0; i < 200; i++) {
        let res = http.post(`${BASE_URL}/send`, payload);
        if (res.status === 200) {
            seedIds.push(res.json().id);
        }
    }
    return { seedIds };
}

// 主測試：用 seedIds 做 GET
export default function (data) {
    let id = data.seedIds[Math.floor(Math.random() * data.seedIds.length)]
        || "00000000-0000-0000-0000-000000000000";
    let res = http.get(`${BASE_URL}/${id}`);
    check(res, { "get /{id} status 200": (r) => r.status === 200 });
}
```

問題找到了！`setup()` 在測試開始時執行，此時 Redis 可能還在重播 AOF log。如果 200 個 setup POST 全部返回 500（Redis 還在 loading），`seedIds = []`。

### 假設 3：fallback UUID 導致鏈式失敗

`seedIds` 為空時，`|| "00000000-0000-0000-0000-000000000000"` 這個 fallback UUID 被使用。此時 Redis 還在 loading，GET `/00000000-0000-0000-0000-000000000000` 返回的不是 404（找不到），而是 500（`BusyLoadingError`）。

```
BusyLoadingError: Redis is loading the dataset in memory
```

k6 的 check `"get /{id} status 200"` 對每個 GET 都呼叫，**包括**回傳 500 的那些。check 次數 2670，成功 0 次，通過率 0%。

**根本原因鏈：**

```
Redis 重啟 → 重播 AOF log（數秒）
    → setup() 在此窗口執行
    → 200 個 POST 全部返回 500
    → seedIds = []
    → 所有 GET 使用 fallback UUID
    → GET 返回 500（非 404，因為 Redis 還在 loading）
    → check "status 200" 對 2670 次 GET 全部失敗
    → 通過率 0%
```

### 修復

在 `main.py` 加入 Redis 就緒等待（如前述 startup hook）。`BusyLoadingError` 在 `ping()` 時就會拋出，被 except 捕獲後等待 1 秒再試，直到 Redis 完全就緒後 worker 才開始接受請求。

**修復後：錯誤率 = 0.00%，GET 通過率恢復正常。**

### 為什麼 delivery-worker 沒有這個問題？

delivery-worker 從一開始就有 Redis 就緒等待（因為它的工作完全依賴 Redis Stream，如果 Redis 沒準備好根本無法消費）。API 層沒有這個保護是個遺漏，`BusyLoadingError` 才得以穿透到 HTTP response。**這個 bug 在單容器測試時從未出現，因為單容器測試不需要重啟 Redis。**

---

## async Redis 連線池大小的重要性

### 第一次 async 測試：pool 預設 10，p95 = 517ms

```
max_connections = 10（redis.asyncio 預設值）
測試結果：POST p95 = 517ms ❌，吞吐量 = 2247 RPS
```

10 個連線在 3000+ RPS 下是嚴重的瓶頸：每秒有數百個 coroutine 同時等待 Redis 回應，但連線池只有 10 個槽位。超過 10 個並發 coroutine 需要 Redis 連線時，必須等待其他 coroutine 釋放連線，形成內部排隊，p95 因此超標。

### 修正：`max_connections=100`，p95 = 283ms

```python
self._async_r = aioredis.from_url(redis_url, max_connections=100)
```

```
修正後：POST p95 = 283ms ✓，吞吐量 = 3072 RPS
```

**連線池大小的估算原則：**

```
peak_concurrent_coroutines_per_worker ≈ max_connections

在 3072 RPS、4 workers、平均延遲 100ms 下：
每個 worker 的並發 coroutine ≈ 3072 / 4 × 0.100 = ~77 個

max_connections = 100 > 77，有足夠餘量，不排隊
max_connections = 10 << 77，嚴重不足，大量排隊
```

Async IO 的優勢（大量並發 coroutine）必須搭配足夠的連線池大小才能發揮。連線池太小，coroutine 全部在等連線槽位，async 的優勢反而放大了排隊問題。

---

## Sync vs Async Thread Pool 的心智模型

| 面向 | Sync（`def` 路由） | Async（`async def` 路由） |
|------|------------------|------------------------|
| 執行環境 | uvicorn thread pool（預設 40 threads/worker） | uvicorn event loop（單執行緒，多 coroutine） |
| IO 等待期間 | Thread 被佔用，阻塞 | coroutine 被暫停，thread 空閒 |
| 並發上限 | threads 數量（40）| 連線池大小（100）× event loop 效率 |
| Redis 呼叫 | `r.hgetall()`：blocking syscall | `await r.hgetall()`：non-blocking，event loop 可切換 |
| 適合場景 | CPU-bound（計算密集） | IO-bound（等網路、等 DB） |
| 資源浪費 | Thread 等 IO 時完全閒置 | 幾乎無浪費（等 IO 期間服務其他 coroutine） |

**為什麼 IO-bound 應該用 async：**

```
Sync 路由（1 thread = 1 請求）：
  t=0ms：thread-1 接受請求，呼叫 Redis
  t=0ms~2ms：thread-1 阻塞，等 Redis 回應（IDLE）
  t=2ms：thread-1 收到回應，繼續處理
  → 40 threads × 每次阻塞 2ms = 在 5000 RPS 下遠不夠用

Async 路由（1 event loop = N 個 coroutine）：
  t=0ms：coroutine-A 接受請求，await Redis（暫停）
  t=0ms：event loop 切換到 coroutine-B（接受下一個請求）
  t=0ms：event loop 切換到 coroutine-C...
  t=2ms：Redis 回應 A，event loop 喚醒 coroutine-A 繼續
  → 單一 event loop 可以同時管理數百個 pending IO 操作
```

---

## 實際修復 POST p95 的正確路徑

Tier 3B 在 nginx-scale 模式下，POST p95 = 596ms，仍然超出 500ms 閾值。真正的修復方向：

| 方案 | 說明 | 預期效果 |
|------|------|---------|
| Redis Cluster | 將寫入壓力分散到多個 Redis 節點 | 突破單 Redis 實例的命令處理速率上限 |
| 跳過 nginx 的 client-side LB | gRPC + service discovery，client 直連後端 | 消除 nginx 跳躍延遲，特別對寫入路徑 |
| 垂直擴展取代水平擴展 | 1 個大容器 + 更多 uvicorn workers | IO-bound 場景下，減少 Redis 連線開銷 |
| POST 完全非同步化 | POST 只 enqueue，立即回傳 202，不等 deliver 結果 | 消除 POST path 的第二次 Redis 往返 |

---

## 結論

Tier 3B 在單容器場景下帶來了顯著的效能提升：

- POST p95：466ms → 283ms（-39%）
- GET p95：450ms → 137ms（-69%）
- 吞吐量：2070 RPS → 3072 RPS（+48%）

核心原因是 `async def` + `redis.asyncio` 讓單一 uvicorn worker 在等待 Redis IO 的期間不再阻塞 thread，可以同時服務數百個並發 coroutine。

nginx-scale 模式下，POST p95 仍然超標（596ms），根本原因是：在 IO-bound 的寫入路徑上，nginx 的連線跳躍延遲 + Redis 單節點的命令速率上限，形成了水平擴展的上限，不是靠加容器或改用 least_conn 就能解決的。

**最重要的兩個學習：**

1. **`BusyLoadingError` 是 Redis restart 的必然現象**，必須在 startup hook 裡阻塞直到 `ping()` 成功，否則早期流量（包括 k6 的 `setup()`）會打到還在 loading 的 Redis，造成一連串難以追蹤的假性失敗。

2. **Async 的連線池大小必須匹配峰值並發 coroutine 數**。預設的 `max_connections=10` 在 3000+ RPS 的 IO-bound 場景下是嚴重瓶頸，pool 裡的每個槽位都對應一個「可以真正向 Redis 發出請求的 coroutine」，不夠多時 async 的優勢反而被連線池排隊抵消。
