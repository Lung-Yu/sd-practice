# Tier 3：可靠性強化 — 熔斷器 + 死信佇列 + 速率限制

## 背景

Tier 2C 完成了核心架構目標（p95 全通過、2,070 RPS）。
Tier 3 在此基礎上加入三個生產環境必備的可靠性機制：

| 機制 | 解決的問題 |
|------|-----------|
| Circuit Breaker（熔斷器） | 下游 channel 持續失敗時，避免每次 delivery 都等滿 retry timeout |
| Dead-Letter Queue（死信佇列） | 耗盡重試仍失敗的通知不要直接丟棄，保留供人工介入 |
| Per-User Rate Limiting（速率限制） | 防止單一用戶的爆量請求耗盡系統資源 |

---

## 一、Circuit Breaker（熔斷器）

### 實作位置

- `app/circuit_breaker.py`：狀態機核心邏輯（~60 行，無外部依賴）
- `channels/registry.py`：`_BREAKERS` dict + `_ProtectedChannel` wrapper

### 狀態機

```
            N 次連續失敗
  CLOSED ─────────────────▶ OPEN
    ▲                          │
    │                          │ recovery_seconds 後
    │  成功                    ▼
    └─────────────── HALF_OPEN
                       │
                       │ 失敗
                       ▼
                      OPEN（重新計時）
```

| 狀態 | 行為 | 轉換條件 |
|------|------|---------|
| CLOSED | 正常放行所有請求 | 連續失敗達 `CB_FAILURE_THRESHOLD` 次 → OPEN |
| OPEN | 快速失敗（不呼叫 channel），拋出 `CircuitOpenError` | 等待 `CB_RECOVERY_SECONDS` 秒 → HALF_OPEN |
| HALF_OPEN | 放行一個探測請求 | 成功 → CLOSED；失敗 → OPEN |

### 設定

| 環境變數 | 預設值 | 說明 |
|---------|-------|------|
| `CB_FAILURE_THRESHOLD` | 5 | 連續失敗幾次後跳閘 |
| `CB_RECOVERY_SECONDS` | 30.0 | OPEN 維持幾秒後允許探測 |

### 呼叫路徑

```python
# channels/registry.py

_BREAKERS: dict[str, CircuitBreaker] = {}   # module-level，跨請求共用

def get_channel(name: str) -> BaseChannel:
    if name not in _REGISTRY:
        raise UnknownChannelError(name)
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name)
    return _ProtectedChannel(_REGISTRY[name], _BREAKERS[name])

class _ProtectedChannel(BaseChannel):
    def send(self, user_id: str, message: str) -> None:
        try:
            self._breaker.call(self._inner.send, user_id, message)
        except CircuitOpenError:
            metrics.circuit_breaker_trips_total.labels(channel=self._name).inc()
            raise ChannelDeliveryError(f"Circuit open for {self._name}")
```

### 為什麼 breaker 必須在 module-level，而非 per-request？

這是熔斷器最常見的實作錯誤。

**per-request（錯誤）：**

```python
# 每個請求都建立新的 CircuitBreaker
def get_channel(name: str):
    return _ProtectedChannel(_REGISTRY[name], CircuitBreaker(name))  # ← 每次都是全新狀態
```

每個請求看到的 breaker 都是初始 CLOSED 狀態，
即使 Email channel 剛才連續失敗了 100 次，下一個請求仍然不知道，仍然嘗試呼叫。
熔斷器完全失去意義。

**module-level（正確）：**

```python
_BREAKERS: dict[str, CircuitBreaker] = {}   # process 啟動時建立，跨所有請求共用
```

所有請求共用同一個 breaker 實例。
第 5 次連續失敗後，`_BREAKERS["email"].state` 變為 OPEN，
之後所有請求都看到 OPEN 狀態，立刻快速失敗。

**類比：** 家裡的電路斷路器（實體）是整棟房子共用的，不是每次插電器才新建一個。

### 熔斷器的效能意義

**無 CB，channel 持續 90% 失敗：**

```
每次 deliver() 最壞情況：
  attempt 1 → 失敗，等 backoff
  attempt 2 → 失敗，等 backoff
  attempt 3 → 失敗
  MAX_RETRIES × ATTEMPT_TIMEOUT_S ≈ 15 秒
```

**有 CB，circuit OPEN 後：**

```
get_channel("email").send(...)
  → _breaker.call(...)
  → state == OPEN → 直接拋出 CircuitOpenError（微秒級）
```

---

## 二、Dead-Letter Queue（死信佇列）

### 實作位置

- `app/queue.py`：`DLQ_KEY = "notifications:dlq"` + `push_to_dlq()` / `pop_from_dlq()`
- `app/delivery.py`：耗盡重試後呼叫 `push_to_dlq()`
- `app/routes.py`（admin）：`GET /admin/dlq`、`POST /admin/dlq/retry`

### 資料流

```
delivery-worker
  │
  ├── deliver() 執行 retry loop
  │     attempt 1 → 失敗
  │     attempt 2 → 失敗
  │     attempt 3 → 失敗（MAX_RETRIES 耗盡）
  │
  ├── Redis HSET notification FAILED
  └── Redis RPUSH notifications:dlq <notification_id>   ← 進入 DLQ

管理員
  ├── GET /admin/dlq
  │     └── LLEN + LRANGE 0 9（非破壞性 peek）
  └── POST /admin/dlq/retry?count=10
        ├── LPOP notifications:dlq（取出 N 個 ID）
        └── XADD notifications:delivery（重新入隊）
```

### Redis 資料結構選擇：List（而非 Stream）

DLQ 選用 Redis List（RPUSH + LPOP）而非 Stream，原因：

| 面向 | List | Stream |
|------|------|--------|
| 語意 | 簡單 FIFO 佇列 | append-only log + consumer group |
| 重試 | 直接 LPOP + XADD 回主 stream | 需要額外管理 group state |
| 複雜度 | 低 | 高 |
| 可觀測性 | `LLEN`（長度）| 可查 consumer lag |

DLQ 的消費是**人工觸發**（admin 呼叫 retry API），不需要 consumer group 的自動分配機制，
List 的簡單性更合適。

### Admin 端點設計

```
GET /admin/dlq
{
  "depth": 42,
  "sample": ["notif-id-1", "notif-id-2", ...]   ← LRANGE 0 9，非破壞性
}

POST /admin/dlq/retry?count=10
{
  "requeued": 10,
  "ids": ["notif-id-1", ...]
}
```

`GET /admin/dlq` 使用 `LRANGE`（非破壞性）而非 `LPOP`，確保觀察不影響狀態。
`POST /admin/dlq/retry` 才真正 `LPOP` 並重新入隊。

---

## 三、Per-User Rate Limiting（速率限制）

### 實作位置

`app/routes.py`，POST /send handler 的前置檢查。

### 演算法：Fixed-Window Counter

```python
# app/routes.py

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_S", "60"))

def _check_rate_limit(r: Redis, user_id: str) -> bool:
    window = int(time.time()) // RATE_LIMIT_WINDOW_S
    key = f"ratelimit:{user_id}:{window}"
    count = r.incr(key)              # 原子遞增
    if count == 1:
        r.expire(key, RATE_LIMIT_WINDOW_S * 2)   # 第一次寫入才設 TTL
    return count <= RATE_LIMIT_REQUESTS
```

Key 格式：`ratelimit:{user_id}:{epoch // window_s}`

| user_id | window (60s bucket) | key |
|---------|---------------------|-----|
| user-123 | 第 0 分鐘 | `ratelimit:user-123:0` |
| user-123 | 第 1 分鐘 | `ratelimit:user-123:1` |

### 為什麼只用 2 個 Redis round-trip（不需要 Lua）

1. `INCR key`：原子操作，多 worker 下計數正確
2. `EXPIRE key TTL`：只在 `count == 1` 時執行（第一次建立 key 才設 TTL）

`INCR` 是原子的，不需要 Lua script 來保護「讀 → 判斷 → 寫」的臨界區。
`EXPIRE` 不是臨界區操作（最壞情況：兩個 worker 都對 count==1 的 key 執行 EXPIRE，結果相同）。

### 與 ip-based rate limit 的比較

| 面向 | Per-IP（Nginx） | Per-User（Redis） |
|------|---------------|-----------------|
| 精度 | IP 共享（NAT 後多人共用同 IP） | 用戶個別計數 |
| 繞過難度 | 換 IP 即可 | 需要新帳號 |
| 維度擴展 | 只能 per-IP | 可改為 per-plan、per-topic |
| 位置 | Nginx 層，Python 之前 | App 層，需要 Redis |

### 驗證

```
110 個循序請求（同 user_id，60 秒內）
→ 前 100 個：202 Accepted ✓
→ 後 10 個：429 Too Many Requests ✓
```

超出限制時，同時遞增 `rate_limit_hits_total{user_id}` Prometheus counter。

---

## NFR（Non-Functional Requirements）Scorecard

| NFR | 原始版本 | Tier 2C | Tier 3 |
|-----|----------|---------|--------|
| POST p95 延遲 | 無界限 | 466ms ✓ | 466ms ✓ |
| Retry 策略 | 無 jitter（thundering herd） | exp. backoff + jitter | 同左 |
| 熔斷器 | 無 | 無 | ✓ per-channel |
| 死信佇列 | 無 | 無 | ✓ Redis List + admin API |
| Per-user 速率限制 | 無 | 無 | ✓ fixed-window Redis |
| 可觀測性 | 無 | Prometheus 基礎指標 | ✓ + CB + DLQ + RL 指標 |

---

## 可觀測性指標設計

### 新增的 Prometheus Counters

| 指標 | Labels | 觸發時機 |
|------|--------|---------|
| `circuit_breaker_trips_total` | `channel` | circuit OPEN 時每次快速失敗 |
| `dlq_push_total` | — | 通知進入 DLQ |
| `rate_limit_hits_total` | `user_id` | 請求被 rate limit 拒絕 |

### 監控告警原則：「出現就告警」

```
# 健康狀態：counter 永遠為 0
# 異常：任何非零值都需要關注

circuit_breaker_trips_total > 0   → 某 channel 持續失敗，需調查
dlq_push_total > 0                → 通知無法交付，需人工介入
rate_limit_hits_total > 0         → 用戶行為異常或需要調整限制
```

**為什麼用「rate > 0」而非設閾值？**

這三個指標在正常運作下應該永遠為 0（或極低）。
設定如「rate > 100/min」的閾值會掩蓋早期問題：
- 熔斷器每分鐘跳閘 99 次，但在閾值之下，沒有告警
- 1 條通知進入 DLQ 就代表交付失敗，應立刻知道

「任何非零值就告警」讓問題在萌芽期就可見。

---

## 學到的系統設計概念

### 1. 熔斷器的 Module-Level 原則

Circuit Breaker 的狀態必須是**進程內共享的單例**，絕對不能 per-request 建立。
這是熔斷器最常見的實作錯誤——看起來程式碼正確，但完全不生效。

### 2. DLQ 是「可靠性」而非「效能」的元件

DLQ 不加速任何東西。它的價值是：**失敗的工作不被靜默丟棄**。
沒有 DLQ 的系統：通知交付失敗 → `FAILED` 狀態 → 無法追蹤、無法重試。
有 DLQ 的系統：交付失敗 → 進入 DLQ → 管理員可見、可批量重試。

### 3. Rate Limiting 的層次選擇

| 層次 | 工具 | 適合場景 |
|------|------|---------|
| 網路層 | iptables / WAF | IP blocking，DDoS 防護 |
| LB 層 | Nginx limit_req | Per-IP 粗粒度保護 |
| App 層 | Redis counter | Per-user、per-plan 精細控制 |
| 業務層 | Token bucket / sliding window | 複雜的速率策略 |

本系統選擇 App 層（Redis fixed-window）是因為需要 per-user 精度，
且 Redis 已經是 critical path 的依賴，不增加新的基礎設施。

### 4. Fixed-Window 的已知缺陷：邊界爆量

```
window 1：第 59 秒發送 100 個請求 ✓（填滿）
window 2：第 61 秒發送 100 個請求 ✓（新 window）

跨兩個 window 邊界，2 秒內發送了 200 個請求，超過名義上的 100/60s 限制。
```

解法（本系統未實作，記錄於此）：
- **Sliding Window Log**：記錄每次請求的時間戳，精確但記憶體消耗大
- **Sliding Window Counter**：用上一個 window 的比例估算，近似但節省記憶體
- 對本系統（通知送出）的影響：可接受，邊界爆量最多 2x，不影響核心可靠性
