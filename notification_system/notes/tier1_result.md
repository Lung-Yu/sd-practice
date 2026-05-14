# Tier 1：快速改善 — 逾時保護 + 可觀測性 + 細粒度鎖

**實施日期：** 2026-05-14
**目標：** 不改架構，消除最壞情況（thread 永久阻塞、零可觀測性、全局鎖爭用）

---

## 改動摘要

| 子任務 | 檔案 | 核心改動 |
|--------|------|---------|
| 1A：逾時 + 指數退避 + jitter | `app/delivery.py`, `app/config.py` | ThreadPoolExecutor + future.result(timeout)；backoff = base × 2^attempt × jitter |
| 1B：Prometheus 可觀測性 | `app/metrics.py`, `app/main.py`, `monitoring/prometheus.yml` | Counter + Histogram；`@app.get("/metrics")` 純路由；新增 scrape job |
| 1C：細粒度 Store 鎖 | `app/store.py` | 拆分全局鎖為 `_id_lock` + per-user lock；`_by_user` 從 list 改為 set |

---

## 1A：逾時保護 + 指數退避 + Jitter

### 問題

原始 `deliver()` 的重試迴圈：

```python
# Before（危險）
for attempt in range(1, config.MAX_RETRIES + 1):
    try:
        channel.send(user_id, message)   # 可能永遠不返回
        return notification
    except ChannelDeliveryError:
        time.sleep(0.5)                  # 固定 sleep，無 jitter，thundering herd
```

兩個致命缺陷：
1. `channel.send()` 沒有 timeout 保護——如果下游 channel 掛住，這個 HTTP thread 永久阻塞，永遠不釋放回 thread pool
2. 固定 backoff 沒有 jitter——所有在同一時刻失敗的請求（在 5000 RPS 下可能是數百個）會在完全相同的時間點同時重試，造成 thundering herd，可能讓本已過載的 channel 再次崩潰

### 解法：ThreadPoolExecutor + future.result(timeout)

```python
# app/delivery.py（After）
_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="channel")

def _send_with_timeout(channel, user_id: str, message: str) -> None:
    future = _executor.submit(channel.send, user_id, message)
    try:
        future.result(timeout=config.ATTEMPT_TIMEOUT_S)
    except _FuturesTimeout:
        raise ChannelDeliveryError(f"timed out after {config.ATTEMPT_TIMEOUT_S}s")
```

`future.result(timeout=5.0)` 的機制：
- 呼叫端 thread 等待最多 5 秒；超過則拋出 `concurrent.futures.TimeoutError`
- Channel 的 `send()` 在 executor thread 繼續執行（無法強制停止），但呼叫端 thread 已獲釋，可以處理下一個請求
- 這是 Python 中模擬 per-call timeout 的標準做法（asyncio 有 `asyncio.wait_for`，sync 端只能用 executor）

### 指數退避 + Jitter

```python
# app/delivery.py（After）
if attempt < config.MAX_RETRIES:
    delay = config.RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
    jitter = random.uniform(0, delay * 0.1)
    time.sleep(delay + jitter)
```

| Attempt | Base delay | Jitter 範圍 | 實際 sleep |
|---------|-----------|------------|-----------|
| 1 失敗後 | 0.1s | 0–0.01s | 0.10–0.11s |
| 2 失敗後 | 0.2s | 0–0.02s | 0.20–0.22s |
| 最多重試 2 次 | — | — | 總計最多 ~0.33s |

Jitter ±10% 讓同時失敗的請求分散在不同時間點重試，打散 thundering herd。

### 新增 config.py 參數

```python
# app/config.py
RETRY_BASE_DELAY_S = float(os.getenv("RETRY_BASE_DELAY_S", "0.1"))
ATTEMPT_TIMEOUT_S  = float(os.getenv("ATTEMPT_TIMEOUT_S",  "5.0"))
```

兩個參數都可透過環境變數覆蓋，無需修改程式碼。在高吞吐量測試時可設 `FAILURE_RATE=0` 搭配 `ATTEMPT_TIMEOUT_S=1.0` 做壓測。

### 為什麼這樣還不夠

`time.sleep()` 依然存在於重試迴圈中，意味著這個 HTTP thread 在 backoff 期間仍然被佔用（只是在睡眠，而非執行 channel 呼叫）。真正的解法是把 deliver() 整個移出 request path（Tier 2：異步佇列），讓 HTTP thread 立即釋放。Tier 1 只是把「最壞情況」從「永久阻塞」降低到「最多 ~0.33s + channel 超時」。

---

## 1B：Prometheus 可觀測性

### 問題

初始設計完全沒有 `/metrics` endpoint，Prometheus 無法 scrape。唯一的外部視角是 k6 的 HTTP response time，看不到任何服務內部狀態：

- 哪個 channel 失敗率最高？
- 重試了多少次？
- Idempotency 命中率是多少？
- 交付延遲的 p95 是多少？

### Metrics 設計

```python
# app/metrics.py
from prometheus_client import Counter, Histogram

# 交付結果：按 channel × status（SENT / FAILED）計數
notifications_sent = Counter(
    "notifications_sent_total",
    "Notifications by delivery outcome",
    ["channel", "status"],
)

# 重試次數（不含第一次嘗試）
notification_retries = Counter(
    "notification_retries_total",
    "Retry attempts after first failure, by channel",
    ["channel"],
)

# 逾時計數（future.result(timeout=...) 超時）
delivery_timeouts = Counter(
    "delivery_timeouts_total",
    "Delivery attempts that timed out, by channel",
    ["channel"],
)

# Idempotency 命中（重複請求，不重新交付）
idempotency_hits = Counter(
    "idempotency_hits_total",
    "Requests deduplicated by idempotency key (no re-delivery)",
)

# Circuit breaker 跳閘次數（Tier 3 預留）
circuit_breaker_trips = Counter(
    "circuit_breaker_trips_total",
    "Times a channel circuit breaker rejected a call (OPEN state)",
    ["channel"],
)

# Rate limit 觸發次數（Tier 2 預留）
rate_limit_hits = Counter(
    "rate_limit_hits_total",
    "Requests rejected by per-user rate limiter",
)

# 端到端交付時間（所有 attempt 合計）
notification_delivery_seconds = Histogram(
    "notification_delivery_duration_seconds",
    "End-to-end delivery time per channel (all attempts combined)",
    ["channel"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
```

Histogram buckets 的選擇邏輯：
- `0.005–0.05`：正常的 channel 呼叫（stdout simulate，幾乎零延遲）
- `0.1–0.5`：p95 目標範圍（SLA 是 p95 < 500ms）
- `1.0–5.0`：重試後的最壞情況（ATTEMPT_TIMEOUT_S = 5.0）

### /metrics Endpoint：make_asgi_app() vs 純路由

這裡踩了一個坑，值得詳細記錄。

**錯誤做法（返回 404）：**

```python
# app/main.py — 錯誤版本
from prometheus_client import make_asgi_app

app.mount("/metrics", make_asgi_app())
```

`make_asgi_app()` 返回一個獨立的 ASGI app，透過 `app.mount()` 掛載。問題在於 FastAPI 的路由匹配機制：mount 路徑 `/metrics` 的 ASGI app 期望請求路徑去掉前綴後傳入，但 Prometheus 的 ASGI app 期望接收 `/` 路徑，而不是空路徑 `""`。實際測試結果：`GET /metrics` 返回 **404**，原因是 route 匹配到 mount，但 Prometheus ASGI app 內部找不到對應路徑。

**正確做法（純 FastAPI 路由）：**

```python
# app/main.py — 正確版本
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi import Response

@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`generate_latest()` 直接生成 Prometheus 文字格式的 bytes，`Response` 包裝後返回。這是最簡單且最可靠的做法，完全繞過 ASGI app 的路由問題。

**教訓：** 在 FastAPI 中，`app.mount()` 適合掛載靜態檔案或完整的 sub-application（有自己的 routing logic）。如果只是要從一個函數返回 bytes，用 `@app.get()` 路由就夠了，不需要 ASGI app 的複雜性。

### Prometheus 設定

```yaml
# monitoring/prometheus.yml（新增 scrape job）
scrape_configs:
  - job_name: notification_service
    static_configs:
      - targets: ["notification-api:8000"]
```

`notification-api` 是 `docker-compose.yml` 中 app service 的名稱，Prometheus container 透過 `sd_monitoring` Docker network 可直接解析此 hostname。

### 現在能看到什麼

有了 metrics 之後，Grafana 可以顯示：
- `rate(notifications_sent_total{status="SENT"}[1m])` — 每分鐘成功交付率
- `rate(notifications_sent_total{status="FAILED"}[1m])` — 每分鐘失敗率
- `histogram_quantile(0.95, notification_delivery_duration_seconds_bucket)` — p95 交付延遲（按 channel 分組）
- `rate(notification_retries_total[1m])` — 重試壓力
- `rate(idempotency_hits_total[1m])` — 重複請求比率

---

## 1C：細粒度 Store 鎖

### 問題

原始 store 使用單一全局鎖：

```python
# Before（全局鎖，高爭用）
class NotificationStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_id: dict[str, Notification] = {}
        self._by_key: dict[str, str] = {}
        self._by_user: dict[str, list[str]] = {}  # O(n) membership check

    def save(self, notification):
        with self._lock:                    # 所有 thread 都在等這一把鎖
            self._by_id[...] = notification
            self._by_key[...] = notification_id
            self._by_user.setdefault(..., []).append(notification_id)

    def list_for_user(self, user_id):
        with self._lock:                    # 讀取也要等全局鎖
            return [...]
```

在 5000 RPS 下，4 個 worker 各有多條 thread，全部爭同一把鎖。`list_for_user()` 是 O(n) 操作（掃描 list），在高請求量下鎖持有時間長，爭用嚴重。

### 解法：拆分鎖 + set 代替 list

```python
# After（細粒度鎖）
class NotificationStore:
    def __init__(self):
        # 短暫鎖：只保護兩個全局 dict 的指針操作（極快）
        self._id_lock = threading.Lock()
        self._by_id: dict[str, Notification] = {}
        self._by_key: dict[str, str] = {}

        # Per-user 鎖：不同 user 的操作完全不互相阻塞
        self._user_locks_lock = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        # set 提供 O(1) membership check，而非 list 的 O(n)
        self._by_user: dict[str, set[str]] = {}
```

鎖的架構設計：

| 鎖 | 保護的資料 | 持有時間 | 競爭情況 |
|----|-----------|---------|---------|
| `_id_lock` | `_by_id` + `_by_key` | 極短（dict 賦值，微秒級）| 所有 thread 共用，但持有時間極短 |
| `_user_locks_lock` | `_user_locks` dict 本身 | 極短（只用於取出 per-user lock）| 僅在首次建立 user lock 時競爭 |
| `_user_locks[user_id]` | `_by_user[user_id]` | 短（set 操作）| 只有同一 user_id 的請求競爭 |

**關鍵改善：** 不同 user_id 的請求現在完全不會互相阻塞。在 5000 RPS 的多用戶場景下，鎖爭用從「所有請求爭同一把鎖」降低為「只有同一個 user 的請求互相排隊」。

```python
def save(self, notification: Notification) -> None:
    # 1. 快速更新全局索引（鎖持有時間極短）
    with self._id_lock:
        self._by_id[notification.notification_id] = notification
        self._by_key[notification.idempotency_key] = notification.notification_id
    # 2. 只鎖當前 user 的 set（其他 user 完全不阻塞）
    with self._user_lock(notification.user_id):
        self._by_user.setdefault(notification.user_id, set()).add(notification.notification_id)

def list_for_user(self, user_id: str) -> list[Notification]:
    with self._user_lock(user_id):
        ids = set(self._by_user.get(user_id, set()))  # 持有鎖時做快照
    # 釋放鎖後再做 dict lookups（_by_id 是無鎖讀取，Python dict 讀取是 GIL 保護的）
    return [self._by_id[nid] for nid in ids if nid in self._by_id]
```

`list_for_user()` 的模式：**持有鎖時只做快照（複製 id set），釋放鎖後再做 lookups**。這樣把鎖持有時間壓縮到最小，避免長時間持有 user lock 而阻塞同 user 的其他寫入操作。

---

## Tier 1 完成後的系統狀態

### 已解決的問題

| 問題 | 解決方式 |
|------|---------|
| Thread 永久阻塞（channel hang）| future.result(timeout=5.0) 強制超時 |
| Thundering herd 重試 | Exponential backoff + ±10% jitter |
| 零可觀測性 | /metrics endpoint + 7 個 metrics 指標 |
| 全局鎖爭用 | Per-user lock + set O(1) membership |
| P99 無上限 | Timeout 保護下，最壞情況有界（5s × MAX_RETRIES）|

### 仍然存在的根本問題

| 問題 | 狀態 | 需要 Tier |
|------|------|----------|
| 同步交付阻塞 HTTP thread（backoff sleep 仍佔 thread）| 未解決 | Tier 2（異步佇列）|
| In-memory store 不跨 worker | 未解決 | Tier 2（Redis store）|
| Worker 重啟後資料全失 | 未解決 | Tier 2（Redis 持久化）|
| 無全局 idempotency（4 worker = 4 個獨立 store）| 未解決 | Tier 2（Redis store）|
| 無 rate limiting（無 Redis 就無法做全局計數）| 未解決 | Tier 2（Redis rate limit）|
| 無 circuit breaker | 未解決 | Tier 3 |
| 無 DLQ | 未解決 | Tier 3 |

### 核心結論

Tier 1 把系統從「可能永久失控」的狀態改善為「有界的最壞情況」。但架構上的根本矛盾——同步交付 + in-memory store——依然存在。在 5000 RPS 的目標負載下，真正能突破瓶頸的改善在 Tier 2：把 deliver() 移出 request path，把 store 換成 Redis。

Tier 1 的真正價值：**加了 metrics，現在能看見問題在哪裡**。Tier 2 的每個改善都有了量化的基準可以對比。
