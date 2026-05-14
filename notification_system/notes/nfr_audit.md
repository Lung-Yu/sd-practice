# NFR 初始審計 — FAANG 主任工程師視角

**審計日期：** 2026-05-14
**系統：** Notification Service（FastAPI + Python，in-memory store，同步交付）
**目標負載：** 5,000 RPS，p95 < 500ms，4 uvicorn workers

---

## 背景與問題場景

這個通知服務的初始設計是一個典型的「能跑就好」原型：

```
POST /send
  → 計算 idempotency key（sha256）
  → 查 in-memory store（module-level dict）
  → 同步 deliver()（for 迴圈重試 + time.sleep()）
    → EmailChannel / SMSChannel / PushChannel（stdout）
  → 回傳 HTTP 200
```

整個交付流程同步阻塞在 HTTP request path 上。在 k6 以 5,000 RPS 打入時，4 個 uvicorn worker 的 thread pool 很快會被佔滿，後續請求在 event loop 積壓，p99 無上限。

---

## 6 個 NFR 維度的問題掃描

### 1. Performance（效能）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| 同步 deliver() 阻塞 HTTP thread | 每個 POST /send 的 thread 在 deliver() 返回前都無法釋放 | uvicorn thread pool（預設 40）在高 QPS 下瞬間耗盡 |
| 重試期間 time.sleep() 佔用 thread | `for attempt in range(MAX_RETRIES)` + `time.sleep(backoff)` | 一次失敗重試最多佔用 thread 0.1 + 0.2 + 0.4 = 0.7s，在 5000 RPS 下致命 |
| p99 無上限 | 無 per-attempt timeout | 外部 channel 掛起時 thread 永久阻塞 |
| 無法非同步 | 路由用 `async def`，但 deliver() 是同步的；在 async context 呼叫 sync 阻塞會 block event loop | FastAPI 的 async 能力被白白浪費 |

**量化影響：** 假設 20% FAILURE_RATE，MAX_RETRIES=3，平均每個失敗請求多佔用 ~0.35s thread time。在 5000 RPS × 20% 失敗 = 1000 req/s 失敗，等效 350 條 thread 長期被重試佔用，遠超 4 worker × 40 thread/worker = 160 條上限。

---

### 2. Scalability（可擴展性）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| In-memory store 是 per-process 的 | `store = NotificationStore()` 是 module-level singleton，每個 uvicorn worker 有獨立的記憶體空間 | 4 個 worker = 4 個互不同步的 store |
| 跨 worker 查詢返回 404 | Worker A 建立的 notification，被 worker B 收到 GET 請求時找不到 | 真實系統中會出現神秘 404，難以 debug |
| Idempotency 不是全局的 | sha256 key 存在 module-level dict；worker A 已去重，worker B 完全不知道 | 相同請求可能被重複交付最多 4 次（每個 worker 各一次）|
| 水平擴展會讓問題惡化 | 增加 worker 或增加 replica，store 分裂問題線性放大 | 「擴容」反而增加資料不一致的機率 |

**核心矛盾：** 水平擴展（加 worker）是解決效能問題的標準方法，但在這個架構下，加 worker 會同時讓 scalability 問題惡化。

---

### 3. Reliability（可靠性）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| Naive retry loop，無 per-attempt timeout | 原始設計沒有 timeout 機制 | Channel hang 住時，thread 永久阻塞 |
| 無 circuit breaker | 每次失敗都直接重試打 channel | 下游 channel 崩潰時，所有請求都在重試，加重崩潰系統的負荷（cascade failure）|
| 無 dead letter queue（DLQ） | 重試耗盡後 notification 直接標記 FAILED，不再有機會重試 | 短暫故障（數秒）會永久丟失通知 |
| Backoff 沒有 jitter | 所有在同一時間失敗的請求，會在同一時間重試 | Thundering herd：大量請求同時打 channel，可能讓本已過載的系統再次崩潰 |
| 同步重試阻塞 request path | 重試邏輯在 HTTP thread 上執行 | 重試本身增加了 HTTP response time，使 p99 急劇惡化 |

---

### 4. Durability（持久性）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| In-memory = 重啟後全部消失 | `_by_id`, `_by_key`, `_by_user` 都是 Python dict | 每次 deploy 或 crash，所有 PENDING/SENT/FAILED 記錄全部歸零 |
| PENDING 通知丟失 | 系統重啟時，正在 deliver 的通知沒有任何恢復機制 | 用戶的通知在系統重啟後無聲消失，沒有任何告警 |
| 記憶體無界增長 | store 永遠不刪除條目（無 TTL，無 eviction） | 長期運行後 OOM；每個 notification 在 `_by_id`、`_by_key`、`_by_user` 各佔一份 |
| Idempotency key 也是揮發的 | `_by_key` 存在記憶體中 | 重啟後，重複請求無法被識別，會再次交付 |

**量化影響：** 以 5,000 RPS 計算，每個 Notification 物件約 500 bytes，一小時後 store 大小 = 5000 × 3600 × 500B = **~9GB**，遠超任何合理的 container 記憶體上限。

---

### 5. Observability（可觀測性）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| 無 /metrics endpoint | 初始設計沒有 prometheus_client | Prometheus 無法 scrape，Grafana 無任何服務內部指標 |
| 無交付成功率追蹤 | 沒有 Counter 記錄 SENT/FAILED | 只能靠 k6 的外部視角；看不到每個 channel 的成功率 |
| 無延遲 histogram | 沒有 Histogram 記錄 delivery duration | 無法知道 p50/p95/p99 的分布在哪個 channel 惡化 |
| 無重試計數 | 重試次數沒有任何記錄 | 無法知道系統在靠重試撐著，還是第一次就成功 |
| 唯一的外部視角是 k6 | k6 只測量 HTTP response time | 看不到 idempotency hit rate、channel 失敗率、DLQ 積壓量等關鍵內部狀態 |
| 錯誤只有 logs（stdout）| channels 的失敗是 `raise ChannelDeliveryError()`，上層 log 但不計量 | 無法設定告警閾值；在 5000 RPS 下 log 量太大，實際上看不到 |

---

### 6. Backpressure（背壓）

| 問題 | 根本原因 | 風險 |
|------|---------|------|
| 無 rate limiting | `POST /send` 接受所有請求，無任何節流 | 任何客戶端都可以用 10,000 RPS 打垮服務 |
| 延遲爬升是無聲的 | 無 backpressure 機制，過載時系統只是變慢，不主動拒絕 | p99 從 50ms 爬升到 5,000ms，客戶端持續等待而不知道系統已過載 |
| 無 queue 容量上限 | 無任何工作佇列，所有請求直接進 deliver() | 瞬間流量突增時，所有 thread 同時被佔用，後續請求在 event loop 積壓 |
| 無 admission control | 系統不知道自己的容量上限 | 無法做 graceful degradation（如優先處理 push，降級處理 email）|

---

## 根本原因分析

所有 6 個 NFR 問題都可以追溯到**三個架構決策**：

| 根本原因 | 影響的 NFR | 說明 |
|---------|-----------|------|
| **同步阻塞交付在 request path** | Performance, Reliability, Backpressure | deliver() 佔用 HTTP thread 直到完成，包含所有重試和 sleep |
| **In-memory singleton store** | Scalability, Durability | Module-level dict 不可跨 process 或跨 replica 共享，重啟即失 |
| **零可觀測性** | Observability | 沒有 metrics，問題存在但看不見，無法觸發告警 |

---

## NFR 之間的依賴關係

這是最關鍵的洞察：**NFR 改善有先後順序，不能跳級。**

```
Durability（持久化 store）
    ↓ 必須先有，才能
Scalability（跨 worker 共享 store → 全局 idempotency）
    ↓ 解決之後，才能安全地
Performance（異步交付 → 不怕 worker 重啟丟失 in-flight jobs）
    ↓ 有了異步佇列，才能做
Reliability（DLQ、circuit breaker、獨立重試 worker）
    ↓ 有了可靠性，才有意義去
Backpressure（rate limit 保護服務，而非保護一個隨時會崩的系統）
```

Observability 是橫切關注點，應該在每個 Tier 都加入，而不是留到最後。

---

## 改善路線圖

### Tier 1 — 快速改善（不改架構，降低最壞情況）

| 編號 | 改善項目 | 影響 NFR | 工作量 |
|------|---------|---------|--------|
| 1A | Per-attempt timeout（ThreadPoolExecutor）+ exponential backoff + jitter | Performance, Reliability | 小（改 delivery.py） |
| 1B | Prometheus metrics（/metrics endpoint，Counter + Histogram）| Observability | 小（新增 metrics.py）|
| 1C | Store 細粒度鎖（per-user lock + set 代替 list）| Performance | 小（改 store.py）|

### Tier 2 — 架構改善（解決 durability + scalability 根本問題）

| 編號 | 改善項目 | 影響 NFR | 工作量 |
|------|---------|---------|--------|
| 2A | Redis 作為持久化 store（替換 in-memory dict）| Durability, Scalability | 中（新增 store_redis.py）|
| 2B | Redis 作為工作佇列（異步 deliver）| Performance, Reliability | 中（新增 queue.py + worker）|
| 2C | Per-user rate limiting（Redis sliding window）| Backpressure | 小（有 Redis 後易實現）|

### Tier 3 — 生產強化（高可用 + 可運維）

| 編號 | 改善項目 | 影響 NFR | 工作量 |
|------|---------|---------|--------|
| 3A | Circuit breaker（per-channel）| Reliability | 中 |
| 3B | Dead letter queue + 定時重試 | Reliability, Durability | 中 |
| 3C | Grafana dashboard（交付率、延遲、DLQ 積壓）| Observability | 小（有 metrics 後）|
| 3D | Graceful degradation（channel priority、shed load）| Backpressure | 大 |

---

## 關鍵洞察

1. **不能跳過 Durability 直接做 Performance 優化。** 把 deliver() 改成異步之前，必須先有持久化 store。否則 worker 重啟時，所有 in-flight jobs 直接消失，換來的是更快的資料丟失。

2. **Observability 不是錦上添花，是除錯的基礎設施。** 在沒有 /metrics 的情況下，Tier 2 的改善只能靠 k6 的外部視角驗證，根本看不到 idempotency hit rate 或 channel 失敗率的變化。先加 metrics，後續每個改善才有量化依據。

3. **In-memory store 是所有問題的樞紐。** 它同時限制了 scalability（無法跨 worker）、durability（重啟即失）、backpressure（無全局計數器做 rate limiting）。Tier 2 最重要的一步就是把 store 換成 Redis。

4. **水平擴展和 in-memory store 是互相排斥的。** 在換掉 in-memory store 之前，任何「加 worker」的操作都是在擴大問題的規模，而不是解決問題。
