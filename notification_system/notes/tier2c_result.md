# Tier 2C：獨立交付 Worker — Redis Streams 解耦

## 背景

Tier 2A 把 in-memory store 換成 Redis，Tier 2B 嘗試用 FastAPI `BackgroundTasks` 非同步化交付。
兩者都未能通過所有 p95 閾值——根本原因是交付邏輯仍在 HTTP worker 進程內執行，
搶佔同一個 thread pool，高負載下互相干擾。

Tier 2C 的核心改動：**把交付 (`deliver()`) 完全移出 HTTP 進程**，
放進獨立的 `delivery-worker` container，透過 Redis Streams 溝通。

---

## 架構

### Tier 2C 之前（2A / 2B）

```
POST /send
  │
  ├── validate + idempotency check
  ├── Redis HSET (PENDING)
  └── deliver() ← 在 HTTP worker 線程內執行，retry loop + sleep 佔用 thread
      └── return 200/207
```

### Tier 2C：Redis Streams 解耦

```
HTTP workers (uvicorn)            delivery-worker (獨立 container)
─────────────────────────         ─────────────────────────────────
POST /send                        XREADGROUP GROUP delivery-workers
  │                                 CONSUMER <hostname> > notifications:delivery
  ├── validate
  ├── idempotency check             ↓
  ├── Redis HSET (PENDING)        store.get(notification_id)
  ├── Redis XADD                    ↓
  │   notifications:delivery      deliver()  ← retry loop 在這裡執行
  └── return 202                    ↓
                                  Redis HSET (SENT | FAILED)
                                    ↓
                                  XACK notifications:delivery delivery-workers <id>
```

**HTTP 路徑現在只有兩個廉價的 Redis 操作，然後立刻返回 202。**
`deliver()` 的 retry loop、channel timeout、sleep 全都在 worker container 的獨立進程中執行。

---

## 關鍵設計決策

### Consumer Group：exactly-once 消費

```
Stream:  notifications:delivery
Group:   delivery-workers
Consumer: <socket.gethostname()>  ← 每個 worker container 唯一
```

Redis consumer group 確保每條訊息只被一個 consumer 消費。即使水平擴展到多個 `delivery-worker` container，每個 notification 仍只被交付一次。

`socket.gethostname()` 在 container 內回傳 container ID，天然唯一，無須額外設定。

### XADD / XREADGROUP / XACK 語意

| 命令 | 作用 |
|------|------|
| `XADD notifications:delivery * notification_id <id>` | 把 notification ID 寫入 stream，`*` 讓 Redis 自動產生 entry ID（毫秒時間戳 + 序號） |
| `XREADGROUP GROUP delivery-workers CONSUMER <name> COUNT 10 BLOCK 5000 STREAMS notifications:delivery >` | `>` 代表「只讀還沒被分配給任何 consumer 的新訊息」；BLOCK 5000 = 最多等 5 秒 |
| `XACK notifications:delivery delivery-workers <entry-id>` | 告知 Redis 此訊息已成功處理，從 PEL（Pending Entry List）移除 |

**為什麼 XACK 至關重要？**

Redis 在訊息被 `XREADGROUP` 讀取後，會把該訊息放入 consumer 的 **PEL（Pending Entry List）**，但不會刪除它。
只有 `XACK` 之後，Redis 才從 PEL 移除訊息。
若 worker 在 deliver 途中 crash：
- PEL 仍保留這條訊息
- 其他 worker 可用 `XAUTOCLAIM` 或 `XCLAIM` 重新認領並重試

這提供了 **at-least-once 語意**；加上 HTTP 層的 idempotency key，整體效果接近 exactly-once。

### `id=">"` vs `id="0"` 的差異

| `id` 值 | 行為 |
|---------|------|
| `">"` | 只讀新訊息（未分配給任何 consumer） |
| `"0"` | 讀取所有訊息，**包括**此 consumer 的 PEL（未 ACK 的待處理訊息） |

Worker 啟動時用 `"0"` 先清掉上次 crash 留下的 pending 訊息，
之後切換 `">"` 正常消費。本實作在 `_drain_pending()` 函式中處理此邏輯。

---

## 關鍵檔案

### `app/queue.py`

```python
STREAM_KEY  = "notifications:delivery"
GROUP_NAME  = "delivery-workers"

def enqueue(r: Redis, notification_id: str) -> str:
    """XADD — 把 notification_id 推入 delivery stream。"""
    entry_id = r.xadd(STREAM_KEY, {"notification_id": notification_id})
    return entry_id

def create_group_if_missing(r: Redis) -> None:
    """建立 consumer group；若已存在則忽略 BUSYGROUP 錯誤。"""
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
```

### `app/worker.py`

```python
def _wait_for_redis(r: Redis, retries: int = 15, delay: float = 1.0) -> None:
    """
    等 Redis 就緒。處理 AOF replay 期間的 BusyLoadingError。
    重試最多 15 次，每次間隔 1 秒。
    """
    for attempt in range(retries):
        try:
            r.ping()
            return
        except BusyLoadingError:
            logger.warning(f"Redis 仍在載入資料集，等待... ({attempt+1}/{retries})")
            time.sleep(delay)
        except ConnectionError:
            logger.warning(f"Redis 連線失敗，等待... ({attempt+1}/{retries})")
            time.sleep(delay)
    raise RuntimeError("Redis 在等待期限內未就緒")
```

**POST /send 完整路徑（Tier 2C）：**

```
routes.py
  │
  ├── validate payload
  ├── idempotency.check(sha256 hash)     ← Redis HGETALL
  ├── store.create(PENDING)              ← Redis HSET
  ├── queue.enqueue(notification_id)     ← Redis XADD
  └── return 202 Accepted
```

---

## Tier 2C k6 壓測結果

目標：p95 < 500ms，error rate = 0%，三個端點全部通過。

| 指標 | 2A（Redis store） | 2B（BackgroundTasks） | 2C（Stream worker） |
|------|-----------------|----------------------|--------------------|
| POST /send p95 | 544ms ❌ | 579ms ❌ | **466ms ✓** |
| GET /{id} p95 | 462ms ✓ | 516ms ❌ | **450ms ✓** |
| List p95 | 466ms ✓ | 519ms ❌ | **455ms ✓** |
| 錯誤率 | 0.00% ✓ | 0.00% ✓ | **0.00% ✓** |
| 吞吐量 | ~1,750 RPS | ~1,767 RPS | **~2,070 RPS** |
| 閾值全通過 | ❌ 2 項未通過 | ❌ 4 項未通過 | **✓ 全通過** |

---

## 為什麼有效：真正的進程隔離

### 2A / 2B 失敗的根本原因

| 階段 | deliver() 執行位置 | 問題 |
|------|-------------------|------|
| 2A | HTTP request 內（同步） | blocking — 直接佔用 uvicorn worker |
| 2B | `BackgroundTasks` | 仍在同一個 uvicorn process 的 event loop / thread pool |
| 2C | 獨立 container | **完全隔離** |

`BackgroundTasks` 並非真正獨立的進程——它在同一個 uvicorn worker 的 asyncio event loop 排程執行。
高負載下，大量積壓的 background task 仍然消耗 HTTP worker 的 CPU 和 thread pool，
造成 p95 比 2A 更差（579ms vs 544ms）。

### Tier 2C 的 HTTP 路徑

```
POST /send → validate → idempotency check → Redis HSET (PENDING) → Redis XADD → return 202
```

兩次廉價的 Redis 操作（各 ~0.5ms），然後立刻返回。
`deliver()` 的 retry loop（最多 15 秒）**永遠不會觸碰 HTTP thread pool**。

---

## 遇到的 Bug：BusyLoadingError

### 症狀

`delivery-worker` 在啟動時 crash：

```
redis.exceptions.BusyLoadingError: Redis is loading the dataset in memory
```

### 根因

Redis 啟用了 AOF 持久化（`--appendonly yes`）。
Container 重啟後，Redis 需要 **replay AOF log** 才能恢復資料集到記憶體。
在 replay 完成前，Redis **拒絕所有寫入操作**，並回傳 `LOADING` 錯誤。

Worker 在 Redis 還沒就緒時就嘗試建立 consumer group（`XGROUP CREATE`），觸發 `BusyLoadingError`。

### 修正：`_wait_for_redis()` retry loop

```python
def _wait_for_redis(r: Redis, retries: int = 15, delay: float = 1.0) -> None:
    for attempt in range(retries):
        try:
            r.ping()
            return                         # 成功就緒
        except BusyLoadingError:
            time.sleep(delay)              # AOF 還在 replay，等待
        except ConnectionError:
            time.sleep(delay)              # Redis 還沒啟動，等待
    raise RuntimeError("Redis 未在期限內就緒")
```

用 `r.ping()` 探測是因為 `PING` 是 Redis 在 loading 狀態下**唯一會正常回應**的命令（回傳 `PONG`）。

### BusyLoadingError 是 AOF/RDB 的常見模式

任何啟用了 AOF 或 RDB 持久化的 Redis，在重啟後都需要 replay。
**依賴 Redis 的服務必須處理這個窗口期。** 正確做法：

1. **retry + backoff**（本實作）：簡單、適合 container 啟動順序不確定的場景
2. **`depends_on: condition: service_healthy`**（docker-compose）：health check 確保 Redis 就緒才啟動 worker
3. **兩者並用**（最穩健）：health check 處理正常啟動順序；retry loop 處理 health check 誤判或 failover 場景

---

## Redis Streams 核心概念摘要

```
Stream: notifications:delivery
│
├── Entry 1: {notification_id: "abc123"}  ← XADD 寫入
├── Entry 2: {notification_id: "def456"}
└── ...

Consumer Group: delivery-workers
  ├── Consumer: worker-a (container hostname)
  │   └── PEL: [Entry 1]  ← XREADGROUP 後、XACK 前
  └── Consumer: worker-b
      └── PEL: [Entry 2]
```

| 概念 | 說明 |
|------|------|
| Stream | append-only log；訊息永久保留直到明確刪除（XDEL / MAXLEN） |
| Consumer Group | 多個 consumer 協作消費同一個 stream，每條訊息只分配給一個 consumer |
| PEL（Pending Entry List） | 已讀取但尚未 ACK 的訊息清單；crash 後可由其他 consumer 重新認領 |
| XACK | 從 PEL 移除訊息；代表「已成功處理，不需要重試」 |
| XAUTOCLAIM | 認領超過指定時間未 ACK 的訊息（用於 crash recovery） |

---

## 學到的系統設計概念

### 1. 非同步化要真正隔離才有效

「非同步」不等於「隔離」。`BackgroundTasks` 在同一個進程執行，
仍然共用 CPU、memory、thread pool。
真正的解耦需要**獨立的進程或 container**，透過訊息佇列溝通。

### 2. 訊息佇列的核心價值：削峰填谷

HTTP 層的工作從「validate + store + deliver（最多 15s）」縮減為「validate + store + enqueue（~1ms）」。
交付壓力由 delivery worker 按自己的節奏消化，HTTP 層不再受到交付速度的影響。

### 3. Redis Streams vs Redis Lists（RPUSH/BLPOP）

| 面向 | Redis List | Redis Streams |
|------|-----------|---------------|
| 消費後訊息狀態 | 刪除（pop） | 保留（需 XDEL） |
| 多 consumer 協作 | 需自行實作鎖 | Consumer Group 原生支援 |
| Crash recovery | 訊息遺失 | PEL 保留，可重新認領 |
| 可觀測性 | 只知道 queue 長度 | 可查 PEL、consumer lag |

本系統選擇 Streams 正是因為需要 **at-least-once** 語意與 crash recovery。
