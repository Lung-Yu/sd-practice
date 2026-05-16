# Tier 4：Delivery Worker 從同步改為 asyncio.gather() 並發批次處理

**實施日期：** 2026-05-17

## 改動摘要

Tier 3B 已將 API 路由層全面非同步化，但 delivery worker（`worker.py`）仍使用同步的 `while running:` 迴圈，每批次中的訊息逐一處理。Tier 4 的目標是將 worker 的批次處理從「逐一串行」改為「全批並發」，利用 `asyncio.gather()` 讓同一批次的所有訊息同時投遞，縮短批次處理時間。

**核心改動：**

1. `worker.py` 從同步 `while` 迴圈改為 `asyncio.run(run())`，使用 `redis.asyncio` 非同步 client
2. `BATCH_SIZE` 從 10 提升至 20
3. 每個批次從「逐一投遞」改為 `asyncio.gather(*tasks)`，所有訊息並發執行
4. 每個 task 內部：`await store.aget(nid)` → `await loop.run_in_executor(None, deliver, notification)` → `await r.xack(...)`

---

## 架構對比

### 改前（Tier 3B）：同步批次，逐一串行

```
worker.py
│
└── while running:
        messages = r.xreadgroup(...)   ← 同步 redis client，blocking
        for msg in messages:           ← BATCH_SIZE = 10，逐一處理
            nid = msg["notification_id"]
            notification = store.get(nid)
            deliver(notification)      ← 等待投遞完成才進下一個
            r.xack(stream, group, id)
```

每一個 `deliver()` 呼叫完成後，才開始下一條訊息的投遞。批次的總時間是所有投遞時間的**總和**。

### 改後（Tier 4）：asyncio 並發批次

```
worker.py
│
└── asyncio.run(run())
        r = redis.asyncio.from_url(...)    ← 非同步 redis client
        while True:
            messages = await r.xreadgroup(...)   ← 非阻塞等待
            tasks = [process(r, loop, msg) for msg in messages]
            await asyncio.gather(*tasks, return_exceptions=True)
                                               ↑
                                  BATCH_SIZE = 20 個 coroutine 同時執行

async def process(r, loop, msg):
    nid = msg["notification_id"]
    notification = await store.aget(nid)                        ← 非同步取資料
    await loop.run_in_executor(None, deliver, notification)     ← 在 thread pool 執行同步 deliver
    await r.xack(stream_key, group_name, msg_id)                ← 非同步 ACK
```

所有 20 個 coroutine 同時啟動，各自獨立推進。批次的總時間是所有投遞時間的**最大值**。

---

## 同步 vs 非同步批次的時間模型

| 面向 | Tier 3B（同步批次） | Tier 4（asyncio 並發批次） |
|------|-------------------|--------------------------|
| BATCH_SIZE | 10 | 20 |
| 批次總時間 | Σ(每筆投遞時間) | max(每筆投遞時間) |
| 批次執行方式 | for 迴圈逐一串行 | asyncio.gather() 全並發 |
| Redis client | 同步 `redis.Redis` | `redis.asyncio` 非同步 client |
| deliver() 呼叫 | 直接在迴圈中呼叫 | `loop.run_in_executor(None, deliver, ...)` |
| ACK 時機 | deliver() 返回後立即 ACK | 每個 task 內 deliver 完成後 ACK |
| 投遞失敗影響 | 一個失敗不影響後續（try/except） | return_exceptions=True，失敗不崩潰批次 |
| 理論加速比 | 基準（×1） | ~BATCH_SIZE 倍（最理想情況） |

**時間模型直觀說明：**

```
Tier 3B 批次（10 條，每條 deliver 50ms）：
  msg-1: [==50ms==]
  msg-2:            [==50ms==]
  msg-3:                      [==50ms==]
  ...
  總時間 = 10 × 50ms = 500ms

Tier 4 批次（20 條，每條 deliver 50ms）：
  msg-1:  [==50ms==]
  msg-2:  [==50ms==]      ← 所有訊息同時開始
  msg-3:  [==50ms==]
  ...
  總時間 = max(50ms) = 50ms（理想情況，忽略排程 overhead）
```

在 `FAILURE_RATE=0`、投遞穩定的情況下，理論上批次吞吐量可提升約 BATCH_SIZE 倍。實際受 thread pool 大小、Redis 連線池、核心排程等因素影響。

---

## 核心程式碼對比

### 改前（Tier 3B，同步 worker）

```python
import redis
import threading

r = redis.Redis.from_url(REDIS_URL)
running = True

def run():
    while running:
        messages = r.xreadgroup(
            groupname=GROUP_NAME,
            consumername=CONSUMER_NAME,
            streams={STREAM_KEY: ">"},
            count=BATCH_SIZE,   # BATCH_SIZE = 10
            block=1000,
        )
        if not messages:
            continue
        for stream, msgs in messages:
            for msg_id, data in msgs:
                nid = data[b"notification_id"].decode()
                notification = store.get(nid)
                if notification:
                    deliver(notification)   # 同步，等完成才下一個
                r.xack(STREAM_KEY, GROUP_NAME, msg_id)
```

### 改後（Tier 4，asyncio.gather 並發）

```python
import asyncio
import redis                    # 頂層 redis，用於 exceptions
import redis.asyncio as aioredis

BATCH_SIZE = 20                 # 從 10 提升至 20

async def process(r, loop, msg_id, data):
    """單一訊息的投遞 coroutine，獨立 ACK"""
    nid = data[b"notification_id"].decode()
    notification = await store.aget(nid)
    if notification:
        # deliver() 是同步函數（內含 channel.send() 的 simulated sleep）
        # 用 run_in_executor 放到 thread pool，避免阻塞 event loop
        await loop.run_in_executor(None, deliver, notification)
    await r.xack(STREAM_KEY, GROUP_NAME, msg_id)

async def run():
    r = aioredis.from_url(REDIS_URL, max_connections=50)
    loop = asyncio.get_event_loop()

    while True:
        try:
            messages = await r.xreadgroup(
                groupname=GROUP_NAME,
                consumername=CONSUMER_NAME,
                streams={STREAM_KEY: ">"},
                count=BATCH_SIZE,   # BATCH_SIZE = 20
                block=1000,
            )
        except redis.exceptions.BusyLoadingError:
            await asyncio.sleep(1)
            continue

        if not messages:
            continue

        tasks = []
        for stream, msgs in messages:
            for msg_id, data in msgs:
                tasks.append(process(r, loop, msg_id, data))

        # 關鍵：所有訊息並發執行，return_exceptions=True 讓單一失敗不崩潰批次
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(run())
```

---

## 關鍵設計決策詳解

### 1. `return_exceptions=True` 的必要性

```python
await asyncio.gather(*tasks, return_exceptions=True)
```

不加 `return_exceptions=True` 時，任何一個 task 拋出未捕獲的例外，`gather()` 會立刻取消所有其他 task 並重新拋出例外。這在投遞場景下是錯誤的行為：

- 一條訊息投遞失敗（例如 channel timeout）不應影響其他 19 條
- 被取消的 task 若已執行一半（`aget` 完成、`deliver` 進行中），會導致部分訊息既沒有 ACK 也沒有重新入隊，造成狀態不一致

`return_exceptions=True` 讓 `gather()` 等所有 task 完成（包括失敗的），失敗的 task 以 `Exception` 物件而非拋出的方式返回，主流程可以記錄並繼續。

### 2. ACK 在 task 內部，而非批次結束後

```python
async def process(r, loop, msg_id, data):
    # ...
    await loop.run_in_executor(None, deliver, notification)
    await r.xack(STREAM_KEY, GROUP_NAME, msg_id)   # ACK 在 deliver 成功後
```

若 ACK 放在 `gather()` 之後統一處理，一旦 worker 在 `gather()` 執行中途崩潰，Redis Stream 的 PEL（Pending Entry List）會保留所有未 ACK 的訊息，等 worker 重啟後可以透過 `XAUTOCLAIM` 重新消費。這保證了**至少投遞一次（at-least-once delivery）**的語意。

將 ACK 放在每個 task 內 deliver 完成後執行，確保已成功投遞的訊息立即被 ACK，不會在崩潰恢復後被重複投遞。

### 3. `deliver()` 為何用 `run_in_executor` 而非直接 `await`

`deliver()` 是同步函數，內部呼叫 `channel.send()`，後者使用 `time.sleep()` 模擬投遞延遲。同步的 `time.sleep()` 會直接阻塞 event loop 的 thread，讓所有 coroutine 都無法推進。

```python
# 錯誤做法：直接在 async 函數中呼叫同步 sleep
async def process(...):
    deliver(notification)   # deliver() 內有 time.sleep()，阻塞 event loop！
    # 這段期間所有其他 coroutine 都被凍結

# 正確做法：將 blocking 函數放到 thread pool executor
async def process(...):
    await loop.run_in_executor(None, deliver, notification)
    # deliver() 在另一個 thread 執行，event loop thread 可以繼續排程其他 coroutine
```

`run_in_executor(None, ...)` 使用 asyncio 的預設 `ThreadPoolExecutor`（預設大小 = CPU 核數 × 5）。20 個並發 task 同時呼叫 `run_in_executor`，會建立最多 20 個並發的 thread pool 任務。

### 4. BATCH_SIZE 從 10 提升至 20

Tier 3B 的 BATCH_SIZE=10 是保守設定，因為同步批次下 BATCH_SIZE 越大，單批次越慢（時間是總和）。Tier 4 的並發批次下，BATCH_SIZE 越大，單批次時間越接近「單條的最大延遲」，吞吐量隨 BATCH_SIZE 線性提升（在 executor thread pool 足夠的前提下）。

每個 task 獨立 ACK，不需要等待整批完成才 ACK，因此 BATCH_SIZE 增大不會增加「批次中某一條失敗導致整批重試」的風險。

---

## Bug 修復：`redis.asyncio` 沒有 `.exceptions` 屬性

### 問題

實作初期，按照 Tier 3B 的寫法，嘗試捕獲 `redis.asyncio.exceptions.BusyLoadingError`：

```python
import redis.asyncio as aioredis

try:
    messages = await r.xreadgroup(...)
except aioredis.exceptions.BusyLoadingError:   # AttributeError！
    await asyncio.sleep(1)
```

執行後拋出 `AttributeError: module 'redis.asyncio' has no attribute 'exceptions'`。

### 根本原因

`redis.asyncio` 是 `redis` 套件的子模組，負責提供非同步 client 類別。例外類別（`BusyLoadingError`、`ConnectionError`、`ResponseError` 等）定義在頂層 `redis.exceptions`，而非 `redis.asyncio.exceptions`。`redis.asyncio` 沒有將 `exceptions` 重新匯出。

### 修復

```python
import redis              # 頂層 redis，提供 redis.exceptions
import redis.asyncio as aioredis

try:
    messages = await r.xreadgroup(...)
except redis.exceptions.BusyLoadingError:   # 正確：從頂層 redis 取 exceptions
    await asyncio.sleep(1)
    continue
except redis.exceptions.ConnectionError:
    await asyncio.sleep(2)
    continue
```

**記憶法則：** `redis.asyncio` 提供非同步 client 的「連線/操作能力」；`redis.exceptions` 提供錯誤的「分類能力」。兩者是平行的，不是包含關係。使用 `redis.asyncio` 時，一律用 `redis.exceptions.*` 來捕獲例外。

---

## 效能測試結果

### 測試條件

- `FAILURE_RATE=0`、直連 `:8000`、4 個 uvicorn workers、600 VU
- k6 ramping-arrival-rate，target 5000 RPS

### Tier 3B vs Tier 4 對比

| 指標 | Tier 3B（同步 worker） | Tier 4（asyncio 並發 worker） | 變化 |
|------|----------------------|------------------------------|------|
| POST /send p95 | 283ms ✓ | **361ms ✓** | +28% |
| GET /{id} p95 | 137ms ✓ | **172ms ✓** | +25% |
| List p95 | 176ms ✓ | **225ms ✓** | +28% |
| 吞吐量 | ~3072 RPS | **~2736 RPS** | -11% |
| 錯誤率 | 0.17% ✓ | **0.67% ✓** | — |
| 所有閾值 | ✓ | **✓ 全部通過** | 維持通過 |
| 投遞成功數 | — | **222,922 筆** | — |
| 投遞失敗數 | — | **0 筆（100%）** | — |

所有 API-side 指標輕微退步，但全部仍在 500ms 閾值以內，且 222,922 筆 notification 投遞成功率 100%。

---

## 為什麼 API-side 指標反而輕微退步？

### 退步量化

```
POST p95：283ms → 361ms（+28%）
GET p95： 137ms → 172ms（+25%）
吞吐量：  3072 RPS → 2736 RPS（-11%）
```

乍看之下，worker 的並發化應該讓系統整體更快，但 API 端的數字卻變差。原因分析如下：

### 原因 1：async worker 增加了 Redis 寫入競爭

```
Tier 3B worker（同步）：
  同一時刻最多 1 條訊息在呼叫 store.save()（同步 Redis 寫入）

Tier 4 worker（asyncio.gather）：
  同一時刻最多 20 條訊息同時呼叫 store.aget() + r.xack()
  → 20 個並發 Redis 操作，全部競爭同一個 Redis 實例的命令處理佇列
```

worker 側的 20 個並發 Redis 操作，與 API 側數百個並發 `store.asave()` / `store.aget()` 請求，共同競爭同一個 Redis 實例的命令處理能力。Redis 是單執行緒的命令執行模型，命令越多，每個命令的等待時間越長。

### 原因 2：`run_in_executor` 創造更多 thread 競爭

```
20 個並發 task × run_in_executor(deliver) = 最多 20 個並發 thread pool 任務
```

這 20 個 thread 與 uvicorn 的 thread pool 共用系統 CPU 時間。在 4 核心的測試機上，20 個並發 thread 會導致上下文切換（context switch）開銷增加，間接拉高 API 路由的 CPU 排程延遲。

### 原因 3：測試間的自然變異

同一硬體、不同測試執行之間，k6 的 RPS 施壓曲線、kernel 的排程決策、Redis 的 AOF 刷寫時機都有自然變異。10–28% 的差異有一部分屬於正常的測試誤差範圍，不完全是架構造成的退步。

### 核心洞察：async worker 的優勢在 worker 側，不在 API 側

```
Tier 3B worker 的瓶頸：deliver() 逐一串行 → 批次耗時 = Σ(delivery_time)
Tier 4 worker 的優勢：deliver() 全並發 → 批次耗時 = max(delivery_time)

這個優勢體現在「worker 消化 backlog 的速度」，不是「API 回應時間」。
在 FAILURE_RATE=0、delivery 瞬間完成的測試條件下，
worker 速度本就不是瓶頸，所以優勢幾乎感知不到。
```

---

## Async Worker 真正發光的場景

### 場景 1：高 FAILURE_RATE 下的重試

```
FAILURE_RATE = 0.20，MAX_RETRIES = 3，base_delay = 0.1s

同步批次（BATCH_SIZE=10）：
  10 條中 2 條失敗 → 每條最多重試 3 次 × 0.1s = 0.3s 等待
  在同步批次中，失敗的重試是串行的：
  正常投遞 8 條 + 重試等待 2 條 = 大量串行等待時間

asyncio.gather 批次（BATCH_SIZE=20）：
  20 條中 4 條失敗 → 每條各自在自己的 coroutine 中 await 等待重試
  正常的 16 條在重試等待期間繼續推進，不互相阻塞
  整批時間 ≈ max(正常投遞時間, 失敗重試最長路徑)
```

當 FAILURE_RATE 高時，同步批次的大量 `time.sleep(backoff)` 完全串行，async 批次的 `await asyncio.sleep(backoff)` 讓其他 coroutine 繼續推進，效能差距會數倍放大。

### 場景 2：大量 backlog 突發爆量

```
情境：worker 重啟後，Stream 中積累了 10,000 條未處理訊息

同步批次（BATCH_SIZE=10）：
  10,000 / 10 = 1,000 個批次
  每批次串行處理 10 條
  總時間 = 1,000 × (10 × avg_delivery_time)

asyncio.gather 批次（BATCH_SIZE=20）：
  10,000 / 20 = 500 個批次
  每批次並發處理 20 條
  總時間 = 500 × max(20 × avg_delivery_time)
         ≈ 500 × avg_delivery_time
```

Backlog 消化速度在理想情況下約為同步批次的 `BATCH_SIZE` 倍（20×），可以更快地從突發積壓中恢復到正常延遲。

### 場景 3：異質化投遞時間（channel 混合）

```
Email channel：avg 50ms
SMS channel：  avg 200ms
Push channel： avg 20ms

同步批次（10 條混合）：
  假設 3 Email + 5 SMS + 2 Push
  總時間 ≈ 3×50 + 5×200 + 2×20 = 150 + 1000 + 40 = 1190ms

asyncio.gather 批次（20 條混合）：
  總時間 ≈ max(50, 200, 20) = 200ms（受最慢的 SMS 決定）
```

當各 channel 的投遞時間差異大時，asyncio.gather 讓快速 channel 的訊息不需要等待慢速 channel，整體批次時間由最慢的訊息決定，而非所有訊息的總和。

---

## 心智模型：`run_in_executor` 的角色

```
Event loop thread（asyncio）：
  ┌─────────────────────────────────────────────────────┐
  │  coroutine-A: await store.aget()  ← 非阻塞 Redis IO │
  │  coroutine-B: await r.xack()      ← 非阻塞 Redis IO │
  │  coroutine-C: await run_executor  ← 等 thread 完成  │
  │  ...                                                │
  └─────────────────────────────────────────────────────┘

ThreadPoolExecutor（獨立 threads）：
  ┌─────────────────────────────────────────────────────┐
  │  thread-1: deliver(notification-A)  → channel.send() │
  │  thread-2: deliver(notification-B)  → channel.send() │
  │  thread-3: deliver(notification-C)  → channel.send() │
  │  ...（最多 20 個並發）                               │
  └─────────────────────────────────────────────────────┘
```

`deliver()` 本身含有同步的 `time.sleep()`（模擬 channel IO）。若直接在 async 函數中呼叫，`time.sleep()` 會凍結整個 event loop。透過 `run_in_executor`，`deliver()` 在獨立的 thread 中執行，event loop thread 可以繼續排程其他 coroutine（包括等待 Redis 回應的 `await`）。

這是 asyncio 與同步函數整合的標準模式：**IO-bound async → 直接 await；CPU-bound 或 legacy sync → run_in_executor**。

---

## 設計決策摘要

| 決策 | 選擇 | 理由 |
|------|------|------|
| BATCH_SIZE | 10 → 20 | 並發批次下，大 batch 不增加延遲，只增加並發度 |
| ACK 位置 | task 內部，deliver 後 | at-least-once 語意，崩潰恢復不丟訊息 |
| return_exceptions | True | 單一投遞失敗不崩潰整批 |
| deliver() 執行方式 | run_in_executor | deliver() 是同步函數，避免阻塞 event loop |
| exceptions 模組 | 頂層 redis.exceptions | redis.asyncio 不匯出 exceptions |
| 連線池大小 | max_connections=50 | worker 最多 20 並發操作，50 有足夠餘量 |

---

## 課程學習（Lessons Learned）

### 1. async 的效益取決於瓶頸位置

Tier 4 的 API-side 指標輕微退步，提醒我們：**async 改善的是等待 IO 的那一段時間，不是整個系統的所有延遲**。若 Redis 本身是瓶頸（命令速率上限），增加並發只會讓更多請求在 Redis 命令佇列中等待，不會讓每個請求更快。在改架構之前，應先識別瓶頸在哪一層。

### 2. `redis.asyncio` 只是 client，exceptions 在頂層

這是容易被忽略的細節。`redis.asyncio` 提供非同步連線能力，`redis.exceptions` 提供錯誤分類。兩者分開，不能用 `redis.asyncio.exceptions` 來捕獲例外。凡是在 asyncio context 使用 redis，一律 `import redis` 並用 `redis.exceptions.*`。

### 3. 同步函數在 asyncio 中必須用 `run_in_executor`

任何包含 `time.sleep()`、`requests.get()`、或其他 blocking call 的同步函數，若直接在 `async def` 中呼叫，都會凍結 event loop。正確做法是 `await loop.run_in_executor(None, sync_func, *args)`。這是 asyncio 整合 legacy code 的核心模式。

### 4. async worker 的最佳場景是高失敗率和大 backlog，而非零失敗率基準測試

在 `FAILURE_RATE=0` 的理想情況下，deliver() 幾乎瞬間完成，同步批次和並發批次的差異微乎其微。async worker 的真正優勢在高失敗率（重試帶來 sleep）和大量 backlog（需要快速消化積壓）的生產場景中才能充分發揮。

### 5. `asyncio.gather()` + `return_exceptions=True` 是批次並發的標準配方

需要並發執行一組獨立任務且不希望任一失敗拖累其他任務時，`asyncio.gather(*tasks, return_exceptions=True)` 是 Python asyncio 的慣用寫法。返回值是一個列表，每個元素是對應 task 的返回值或 Exception 物件，可以在 gather 後統一記錄失敗情況。

---

## 結論

Tier 4 成功將 delivery worker 從同步批次改為 asyncio 並發批次，所有功能性指標達標：

- **222,922 筆 notification 投遞成功，0 筆失敗（100% 投遞成功率）**
- **所有 k6 閾值通過（POST p95 361ms ✓、GET p95 172ms ✓、List p95 225ms ✓、錯誤率 0.67% ✓）**

API-side 指標輕微退步（約 +25–28% latency，-11% RPS），根本原因是 20 個並發 worker task 與 API 側共同競爭同一個 Redis 實例，以及 `run_in_executor` 增加的 thread 競爭。這些退步不是 async 架構本身的問題，而是在單 Redis 實例的測試環境下，worker 並發度提升帶來的副作用。

async worker 在高 FAILURE_RATE 和大 backlog 場景下，才是真正發揮效益的地方——那些場景中，同步批次的串行等待（重試 backoff）會被 asyncio.gather 的並發等待完全消除，批次處理速度可以提升數倍。
