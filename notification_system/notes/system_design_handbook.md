# System Design 經驗手冊 — Notification System 實戰總結

> 基於 Notification System Tier 1 → Tier 3 的實戰經驗彙整（2026-05-14）  
> 所有公式均以本專案實測數據驗算

---

## 目錄

1. [核心計算公式](#1-核心計算公式)
2. [同步 vs 異步交付的取捨](#2-同步-vs-異步交付的取捨)
3. [IO-bound vs CPU-bound 擴展策略](#3-io-bound-vs-cpu-bound-擴展策略)
4. [Redis 使用模式決策樹](#4-redis-使用模式決策樹)
5. [熔斷器設計要點](#5-熔斷器設計要點)
6. [Redis 啟動競態條件](#6-redis-啟動競態條件)
7. [Nginx LB 效益評估](#7-nginx-lb-效益評估)
8. [可觀測性黃金法則](#8-可觀測性黃金法則)
9. [各 Tier 結果快速參照表](#9-各-tier-結果快速參照表)

---

## 本專案硬體與架構基準

```
硬體：Podman VM（宿主機 Apple Silicon）
Tier 1 架構：1 container（4 uvicorn workers）+ in-memory store
Tier 2A 架構：同上 + Redis store（AOF）
Tier 2C 架構：同上 + delivery-worker container（Redis Streams）
Tier 3A 架構：4 app containers + nginx（port 8080）+ Redis + delivery-worker
Tier 3B 架構：1 container（4 workers，async def + redis.asyncio pool=100）
```

| 指標 | 數值（最佳 Tier 3B 單容器） |
|------|--------------------------|
| 總 worker 數 | 4 uvicorn workers |
| Redis 連線池 | 100 async connections / worker |
| POST /send p95 | 283ms |
| GET /{id} p95 | 137ms |
| 吞吐量 | ~3072 RPS |
| 目標吞吐量 | 5000 RPS |
| VU 上限 | 600 |

---

## 1. 核心計算公式

### Little's Law：系統容量上限

```
L = λ × W
```

- `L`：系統中同時存在的請求數 ← k6 VU 上限（600）
- `λ`：到達率（RPS）
- `W`：平均服務時間（秒）

**實測驗算：**

```
Tier 2A（sync routes）：
  W = 0.244s（avg latency under load）
  λ_max = L / W = 600 / 0.244 ≈ 2459 RPS（理論上限）
  實際 = ~1750 RPS（因高負載下 latency 爬升，queueing 效應）

Tier 3B（async routes）：
  W ≈ 0.195s（avg latency under load）
  λ_max = 600 / 0.195 ≈ 3077 RPS（理論上限）
  實際 = ~3072 RPS（接近理論，async 幾乎無 queueing overhead）
```

### 為什麼 p95 > p50 在高負載下差距會擴大

低負載時：請求直接被處理，p95 ≈ p50 × 1.1（只有自然變異）  
高負載時：佇列開始形成，隊末請求等待更多 → tail latency 因 **queueing effect** 指數放大。

公式直覺：排隊論中，utilization → 1 時，平均等待時間 → ∞。p95 承受最嚴重的排隊，因此在 utilization > 0.8 後明顯拉開。

**實測（Tier 3B，單容器，~3072 RPS）：**

| 端點 | p50 | p95 | 比值 |
|------|-----|-----|------|
| POST /send | ~120ms | 283ms | 2.4× |
| GET /{id} | ~60ms | 137ms | 2.3× |

---

## 2. 同步 vs 異步交付的取捨

### 三種模式比較

| 模式 | Tier | 描述 | 結果 |
|------|------|------|------|
| 同步交付（Sync HTTP path） | 1 / 2A | `deliver()` 在 HTTP handler 內執行，返回 200 時已交付 | POST p95 = 544ms ❌ |
| FastAPI BackgroundTasks | 2B | `deliver()` 移到 response 之後，但仍在同一 thread pool | POST p95 = 579ms ❌（更差）|
| 獨立 delivery worker（Redis Streams） | 2C | HTTP 只做 XADD，delivery 在另一容器 | POST p95 = 466ms ✓ |

### BackgroundTasks 的反直覺行為

**原始假設：** 把 deliver() 移到 response 後執行 → HTTP 路徑縮短 → p95 改善

**實際結果：** p95 上升 35ms，GET p95 也上升 54ms

**原因：** FastAPI 的 sync `BackgroundTask` 使用與 sync route handler 相同的 `anyio` thread pool（預設 ~40 threads/worker）。在系統已飽和時（600 VU cap），thread pool 100% 佔用——把 deliver() 推到 response 後只是「延後搶佔」，並不減少搶佔，反而因更多並發 delivery 任務加劇競爭。

```
過載下的 thread pool（Tier 2B，per worker）：

  Thread 1–30：route handler（等 Redis）
  Thread 31–35：GET 的 route handler（等 Redis）
  Thread 36–40：BackgroundTask deliver()（也在等 Redis）
  ────────────────────────────────────────
  新 POST 請求到達：找不到空閒 thread → 排隊 → latency 上升
```

**決策規則：** 若 `deliver()` p50 > 10ms，且系統已接近飽和，必須移出 HTTP 進程（不只 BackgroundTasks）。

### Redis Streams 分離的效益

HTTP 路徑縮減為：
```
POST /send → validate → idempotency → HSET(PENDING) → XADD → return 202
```
兩個廉價 Redis 操作，延遲從 ~244ms 降至 ~70ms（delivery 開銷完全移除）。

---

## 3. IO-bound vs CPU-bound 擴展策略

### 核心區分

| 特性 | IO-bound（本專案：Redis round-trip） | CPU-bound（加密、圖片處理） |
|------|--------------------------------------|---------------------------|
| 瓶頸所在 | 等待網路 / 磁碟 | 等待 CPU 計算完成 |
| async 效益 | 高：await 時 event loop 可服務其他請求 | 無（計算期間 event loop 被阻塞） |
| 更多 process 效益 | 有限（瓶頸在 Redis，不在 CPU） | 線性（每個 process 多一份 CPU） |
| 推薦策略 | async def + 連線池 | 多 worker / 多容器 |

### nginx 擴展的 IO-bound 弔詭

**實測（Tier 3B，async routes）：**

| 架構 | 吞吐量 | POST p95 |
|------|--------|----------|
| 1 container × 4 workers（async） | **~3072 RPS** | **283ms ✓** |
| 4 containers × 4 workers + nginx（async） | ~2060 RPS | 596ms ❌ |

加了 4 倍硬體卻得到更少吞吐量，原因：

1. **nginx 增加每請求延遲**：nginx → backend 的額外 hop，高並發下 queueing 讓此延遲放大至 50–100ms
2. **Redis 連線池競爭**：4 容器 × 4 workers = 16 個進程，每個 pool=100 → 最多 1600 個 async connections；Redis 連線管理開銷顯著
3. **Little's Law 倒打**：nginx 延遲升高 → 需要更多 VU 維持相同 RPS → 600 VU cap 提前被佔滿

**結論：** 對於 IO-bound + 單 Redis 的架構，**1 個大容器 > 4 個小容器 + nginx**。

### async 連線池大小公式

```
pool_size = 峰值並發 coroutine 數 × 平均 IO 等待比例
```

實作上：`max_connections = 100`（每 uvicorn worker），適用於 600 VU / 4 workers ≈ 150 VU/worker 的場景。

---

## 4. Redis 使用模式決策樹

```
需要什麼？
├── 分散式鎖（防止 race condition）
│     → SET NX EX（原子操作，NX = 不存在時才設，EX = 自動過期）
│
├── 計數器（rate limiting、統計）
│     → INCR + EXPIRE（INCR 是原子操作，搭配 TTL 實現滑動窗口）
│
├── 訊息佇列（至少一次交付，需要 ACK）
│     → XADD / XREADGROUP / XACK（Redis Streams）
│     → 提供 PEL：consumer crash 後可用 XCLAIM 重新認領
│
├── 訊息佇列（簡單 FIFO，不需 ACK）
│     → RPUSH / LPOP（List）
│     → 或 RPOPLPUSH 做輕量 reliable queue
│
├── Dead-Letter Queue（失敗後暫存）
│     → RPUSH / LRANGE / LPOP（List）
│     → LRANGE 可非破壞性 peek，LPOP 重試時消費
│
├── 複雜物件（多欄位讀寫）
│     → HSET / HGETALL（Hash）
│     → Pipeline 多個 HSET 成一次 round-trip
│
├── 有序集合（時間序列、排行榜）
│     → ZADD / ZRANGE（ZSET，score = timestamp 或 score）
│     → 本專案：user:{user_id}:notifications，score = created_at
│
└── 快取（短期存放，TTL 控制）
      → SET EX / GET（String + TTL）
      → 本專案：idempotency:{sha256}，TTL = 86400s
```

### Pipeline 減少 round-trip

```python
# 壞：3 次 round-trip
r.hset(f"notification:{id}", mapping=data)
r.set(f"idempotency:{key}", id, ex=86400)
r.zadd(f"user:{user_id}:notifications", {id: ts})

# 好：1 次 round-trip（pipeline）
pipe = r.pipeline()
pipe.hset(f"notification:{id}", mapping=data)
pipe.set(f"idempotency:{key}", id, ex=86400)
pipe.zadd(f"user:{user_id}:notifications", {id: ts})
pipe.execute()
```

---

## 5. 熔斷器設計要點

### 狀態機

```
CLOSED ─(N 次連續失敗)→ OPEN ─(recovery_seconds 後)→ HALF_OPEN
  ↑                                                        │
  └──────────(1 次成功)──────────────────────────────────┘
                                    │
                          (1 次失敗)→ OPEN（重置計時）
```

### 關鍵設計決策

**狀態存放位置：** module-level 單例（`_BREAKERS` dict），不是 per-request 物件。

```python
# registry.py（正確做法）
_BREAKERS: dict[str, CircuitBreaker] = {}

def get_channel(name: str) -> BaseChannel:
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name)
    return _ProtectedChannel(channel=_REGISTRY[name], breaker=_BREAKERS[name])
```

若放在 per-request 物件，每次請求都是全新 breaker → 永遠不會積累失敗計數 → 永遠不會 trip。

**觸發條件：** N 次**連續**失敗（而非百分比）

- 優點：邏輯簡單，行為可預測
- 百分比觸發需要滑動窗口（更複雜），且低流量時單次失敗也可能觸發

**沒有熔斷器的後果：**

```
1 個 degraded channel（90% failure rate）
  × MAX_RETRIES（3）
  × ATTEMPT_TIMEOUT_S（5.0s）
= 每次交付最多等待 15 秒
→ delivery worker 完全停滯
```

有熔斷器後，5 次連續失敗後 OPEN → 後續請求微秒級 fail-fast，worker 繼續處理其他 notification。

**監控要點：** `circuit_breaker_trips_total{channel="email"} > 0` 的 rate > 0（per minute）= channel 正在降級，應立即告警。

---

## 6. Redis 啟動競態條件

### 問題描述

Redis 開啟 AOF（`--appendonly yes`）後，每次重啟都需要 replay AOF log 來恢復狀態。

**Replay 期間：** 所有 Redis 指令返回 `LOADING` 錯誤（`BusyLoadingError`）

### 影響鏈

```
Redis 重啟
  │
  ├─ AOF replay 開始（可能需要數秒）
  │
  ├─ podman-compose up 同時啟動 API workers
  │
  ├─ API worker 啟動完成，開始接受請求
  │
  ├─ k6 setup() 執行 → POST /send × 200 筆
  │     ↓（全部打到正在 replay 的 Redis）
  │     → BusyLoadingError → HTTP 500
  │     → seedIds = []（空）
  │
  └─ 正式測試開始
        → GET /{id} 使用 fallback UUID
        → 0% 通過率（UUID 不存在於 Redis）
```

### 修復模式

**HTTP workers 和 delivery workers 兩者都需要：**

```python
# app/main.py（startup event）
@app.on_event("startup")
async def wait_for_redis():
    max_retries = 30
    for attempt in range(max_retries):
        try:
            await redis_client.ping()
            return   # Redis 就緒
        except (ConnectionError, BusyLoadingError):
            await asyncio.sleep(1)
    raise RuntimeError("Redis not ready after 30s")
```

**驗證：**
```bash
podman exec notification_system_redis_1 redis-cli ping
# → PONG = 就緒；任何其他輸出 = 仍在 replay
```

---

## 7. Nginx LB 效益評估

### 本專案實測結果

| 工作負載特性 | Nginx 效益 | 原因 |
|------------|----------|------|
| CPU-bound（計算密集） | ✓ 線性擴展 | 更多容器 = 更多 CPU，nginx 分流 |
| READ-heavy（Redis/DB reads） | ✓ 讀取並行 | GET p95 降 26%（450ms → 332ms）|
| WRITE-heavy（single Redis） | ❌ nginx 延遲 > 容器並行效益 | POST p95 升 27%（466ms → 590ms）|
| Stateless（any） | ✓ Zero-downtime restart | nginx 在容器重啟時自動 re-route |
| IO-bound（async，單 Redis） | ❌ 純量不等比 | 1 大容器 > 4 小 + nginx（3072 vs 2060 RPS）|

### 為什麼 nginx 仍值得加

即使 POST p95 在 nginx scale 下失敗，nginx 仍帶來：

1. **Zero-downtime 部署**：nginx 在後端容器重啟時自動 re-route，外部端點不間斷
2. **TLS termination**：生產環境必備，集中在 nginx 處理
3. **keepalive 連線復用**：`keepalive 64` 設定讓 nginx → backend TCP 連線可複用，減少 60–80% handshake overhead
4. **`/nginx-health` 端點**：供 LB / 健康監控探測
5. **關注點分離**：日誌、rate limiting、請求過濾都在 nginx 層處理，不污染 app 層

### 正確的 nginx 配置（IO-bound 場景）

```nginx
upstream notification_backends {
    least_conn;              # 取代 round-robin，避免 head-of-line blocking
    keepalive 64;            # 複用 TCP 連線到後端
    server notification-api-1:8000;
    server notification-api-2:8000;
    server notification-api-3:8000;
    server notification-api-4:8000;
}
```

`least_conn` vs `round-robin`：round-robin 在某個後端暫時慢時會把新請求送進去堆積；least_conn 優先送到活躍連線數最少的後端，天然避開過載節點。

---

## 8. 可觀測性黃金法則

### 指標類型選擇

| 情境 | 推薦類型 | 原因 |
|------|----------|------|
| 請求延遲 | Histogram | 可計算任意 percentile；Counter 只能告訴你次數 |
| 事件計數（送出、失敗） | Counter | 永遠遞增，rate() 計算速率 |
| 當前狀態（DLQ 深度、連線數） | Gauge | 可增可減 |

**每個關鍵路徑都要有 Histogram，不只 Counter。** Counter 只能回答「發生了多少次」，Histogram 才能回答「第 95 百分位的用戶等多久」。

### 每個外部呼叫的標配

```python
# 每個 Redis / channel 呼叫都要有：
with histogram.time():           # 計時
    try:
        result = await redis_call()
    except Exception as e:
        error_counter.inc()      # 計錯
        raise
```

### k6 per-endpoint Trend 優於 http_req_duration

```javascript
// 推薦：每個端點獨立 Trend + 獨立 threshold
const sendDuration = new Trend('notification_send_duration', true);
const getDuration  = new Trend('notification_get_duration',  true);

// 可以為每個端點設不同的 SLO
thresholds: {
  'notification_send_duration': ['p(95)<500'],
  'notification_get_duration':  ['p(95)<300'],   // 讀取應更嚴格
}
```

若只用 `http_req_duration`，一個端點的 spike 會拉高整體 p95，無法定位是哪個端點出問題。

### 必須有 alert 的指標

| Metric | Alert 條件 | 意義 |
|--------|-----------|------|
| `circuit_breaker_trips_total` | rate > 0 / min | channel 正在降級 |
| `dlq_depth` | > 100 | 大量交付失敗積壓 |
| `rate_limit_hits_total` | rate > 10 / min | 用戶濫用或客戶端 bug |
| `notification_delivery_duration_seconds{quantile="0.99"}` | > 10s | 交付嚴重延遲 |

---

## 9. 各 Tier 結果快速參照表

### 效能數據

| Tier | 架構 | POST p95 | GET p95 | 吞吐量 | All pass? |
|------|------|----------|---------|--------|-----------|
| Tier 1 | 1 container, in-memory, sync | bounded 5s | bounded 5s | ~1750 RPS | ❌ |
| Tier 2A | 1 container, Redis store, sync | 544ms | 462ms | ~1750 RPS | ❌ |
| Tier 2B | 1 container, BackgroundTasks | 579ms | 516ms | ~1767 RPS | ❌ |
| Tier 2C | 1 container + delivery worker | **466ms ✓** | 450ms | ~2070 RPS | **✓** |
| Tier 3A | 4 containers + nginx（sync） | 590ms | 332ms | ~2362 RPS | ❌ |
| Tier 3B（single） | 1 container, async routes | **283ms ✓** | **137ms ✓** | **~3072 RPS** | **✓** |
| Tier 3B（nginx） | 4 containers + nginx, async | 596ms | 234ms | ~2060 RPS | ❌ |

### NFR 達成狀況

| NFR | Tier 1 | Tier 2A | Tier 2B | Tier 2C | Tier 3A | Tier 3B |
|-----|--------|---------|---------|---------|---------|---------|
| POST p95 < 500ms | ❌ | ❌ | ❌ | ✓ | ❌ | ✓ |
| GET p95 < 500ms | ❌ | ✓ | ❌ | ✓ | ✓ | ✓ |
| 錯誤率 < 1% | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cross-worker 404 = 0 | ❌（~10–20%） | ✓ | ✓ | ✓ | ✓ | ✓ |
| 全局 idempotency | ❌ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 重啟後資料不遺失 | ❌ | ✓（AOF） | ✓ | ✓ | ✓ | ✓ |
| Delivery 隔離 | ❌ | ❌ | ❌（partial） | ✓ | ✓ | ✓ |
| 熔斷器 | ❌ | ❌ | ❌ | ❌ | ✓（Tier 3） | ✓ |
| DLQ | ❌ | ❌ | ❌ | ❌ | ✓（Tier 3） | ✓ |
| Rate limiting | ❌ | ❌ | ❌ | ❌ | ✓（Tier 3） | ✓ |
| Prometheus metrics | ❌ | ✓（Tier 1） | ✓ | ✓ | ✓ | ✓ |

### 關鍵教訓一句話摘要

| 教訓 | 核心洞察 |
|------|---------|
| BackgroundTasks 陷阱 | 同一 thread pool → 飽和時無效；需要 **process 隔離** |
| IO-bound 擴展牆 | 瓶頸是 Redis，加容器不等於加吞吐；async 才是解 |
| nginx 非萬靈丹 | READ 水平擴展有效；WRITE-heavy + single Redis 下負效益 |
| async 的真正效益 | 每個 coroutine 在 await 時放行 event loop → thread-free IO |
| Little's Law 必用 | 在改動前先算 VU 上限對應的理論 RPS，避免無謂優化 |
| Redis 啟動競態 | AOF replay 期間 = 全服務 500；需要 readiness gate |
| 熔斷器存放位置 | module-level singleton，絕不 per-request |
