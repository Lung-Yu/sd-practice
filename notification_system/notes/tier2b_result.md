# Tier 2B：BackgroundTasks 陷阱 — 為什麼讓事情更糟

**實施日期：** 2026-05-14

## 改動摘要

Tier 2A 的 POST /send p95 卡在 544ms，根本原因是同步路徑做了兩次 Redis round-trip（PENDING → deliver → SENT）。直覺上的修法：把 `deliver()` 移到回應之後執行，讓 HTTP handler 只存 PENDING 就立刻返回 202，縮短 HTTP 路徑中的工作量。

FastAPI 原生提供 `BackgroundTasks` 正是為此設計，實作也極為簡單：

```python
@router.post("/send", status_code=202)
def send_notification(
    payload: SendRequest,
    background_tasks: BackgroundTasks,
):
    notification = store.create_pending(payload)   # 只存 PENDING，一次 pipeline
    background_tasks.add_task(deliver, notification.id)
    return {"id": notification.id, "status": "pending"}
```

理論上：HTTP worker 完成一次 Redis write 即返回 → p95 應大幅降低。

**實測結果與理論完全相反。**

## k6 負載測試結果（Tier 2B）

**測試條件：** 4 uvicorn workers、FAILURE_RATE=0、target 5000 RPS、600 VU 上限

| 指標 | Tier 2A（before） | Tier 2B（after） | Δ |
|------|------------------|-----------------|---|
| POST /send p95 | 544ms | 579ms | ❌ 更差 +35ms |
| GET /{id} p95 | 462ms | 516ms | ❌ 更差 +54ms |
| GET list p95 | 466ms | 519ms | ❌ 更差 +53ms |
| 吞吐量 | ~1750 RPS | ~1767 RPS | 持平 |
| Dropped iterations | 196,653 | 197,086 | 持平 |
| 錯誤率 | 0.00% | 0.00% | 持平 |

POST /send 變慢了，**且連 GET 也一起變慢**。`deliver()` 根本沒有離開 worker，它只是被延後執行，卻佔用了相同的資源。

## 根本原因：BackgroundTasks 共用 anyio thread pool

這是 FastAPI / Starlette 架構中一個高度反直覺的設計細節。

### FastAPI 如何執行 sync 函數

FastAPI 將 **sync** route handler 丟進 `anyio.to_thread.run_sync()`，由 anyio 管理的執行緒池執行，以避免阻塞 async event loop：

```
Event Loop (1 per uvicorn worker)
  │
  ├─ async request 1  ← 直接在 event loop coroutine 執行
  ├─ async request 2
  │
  └─ sync request 3 ─→ anyio thread pool ──→ [Thread-1] route handler
                                         ──→ [Thread-2] route handler
                                         ──→ [Thread-3] route handler
                                              (預設 ~40 threads / worker)
```

**BackgroundTasks 的 sync 函數走同一條路：**

```
POST /send 完成，response 寫出
  │
  └─ background_tasks.run() ─→ anyio thread pool ──→ [Thread-X] deliver()
```

`deliver()` 不是在「某個背景 daemon」執行，而是在**同一個 anyio thread pool** 裡搶一個 thread slot，時間點僅是 response 送出之後。

### 為什麼在過載下反而更糟

```
系統狀態（Tier 2B，1750 RPS，600 VU）：

[anyio thread pool，40 threads per worker]

  Thread 1–30：正在處理 POST /send route handler（等 Redis）
  Thread 31–35：正在處理 GET /{id} route handler（等 Redis）
  Thread 36–40：正在執行 BackgroundTask deliver()（也在等 Redis！）

  → 新的 POST /send 請求到來
  → 沒有空閒 thread
  → 在 event loop queue 等待 thread 釋放
  → 等待時間計入 p95
```

每一個 POST /send 請求「完成」後，都立刻在同一個 thread pool 注入一個 `deliver()` 任務。在系統已飽和（all threads busy）的情況下，這些 background tasks 與新的進入請求爭奪同一批 threads，讓整體排隊時間上升，因此 GET /{id} 這種完全不相關的端點也跟著變慢。

### 文字示意圖

```
【Tier 2A — 同步交付】

  Request ──→ [Thread] ─── HSET(PENDING) ─── deliver() ─── HSET(SENT) ──→ Response
                            ↑___________________一條龍，thread 佔用約 244ms___↑

【Tier 2B — BackgroundTasks（同一 thread pool）】

  Request ──→ [Thread-A] ── HSET(PENDING) ──→ Response (快速返回)
                                    │
                                    └──→ [Thread-B] ── deliver() ── HSET(SENT)
                                         ↑
                                         搶同一個 pool 的 thread，
                                         在飽和時讓 pool 多一個競爭者
```

Thread-B 並沒有「免費」——它從相同的 40-thread 預算中扣除，讓其他請求少一個可用 thread。

## 關鍵洞察：BackgroundTasks 只在有閒置 thread 容量時才有幫助

| 系統狀態 | BackgroundTasks 效果 |
|----------|---------------------|
| 負載低，thread pool 有空閒 | ✓ 有效：background task 填充空閒 thread，HTTP 路徑縮短 |
| 負載高，thread pool 已飽和 | ❌ 有害：background task 與新請求爭搶 thread，p95 上升 |
| 負載極高，持續飽和 | ❌ 雙重傷害：HTTP 變慢 + 背景任務積壓（delivery lag） |

5000 RPS 壓力測試中，系統在目標吞吐量下已飽和，BackgroundTasks 不存在「免費的背景執行」空間。

## 各種「非同步化」方案的比較

| 方案 | 機制 | 是否共用 thread pool | 在飽和下的效果 |
|------|------|---------------------|---------------|
| FastAPI BackgroundTasks（sync） | 同 process，同 anyio pool | ✓ 共用 | ❌ 不幫助，甚至更差 |
| `asyncio.create_task`（async） | 同 process，event loop coroutine | ✗ 不佔 thread | ✓ 有效，前提是 channels 是 async |
| 獨立 delivery worker 容器 | 不同 process，不同 CPU | ✗ 完全隔離 | ✓ 真正的資源隔離 |
| Redis Streams + worker | Queue 解耦 producer / consumer | ✗ 完全隔離 | ✓ Production 標準模式 |

## 正確的 Tier 2B：Redis Streams + 獨立 Worker

正確的解法是將「接收請求」與「執行交付」分成兩個獨立容器，透過 Redis Stream 作為佇列解耦：

```
【HTTP Worker 容器（多個）】          【Delivery Worker 容器（多個）】

  POST /send
    │
    ├─ HSET notification (PENDING)
    ├─ XADD delivery_stream {id}  ──→  XREADGROUP / XACK
    └─ return 202                       │
                                        ├─ channel.send()
                                        └─ HSET notification (SENT)
```

- HTTP worker 只做：存 PENDING + XADD（兩步都是 Redis write，無 channel I/O）
- Delivery worker 只做：從 stream 讀取 → channel.send() → 更新狀態
- 兩組 worker 的 thread pool **完全獨立**，互不干擾
- Redis Stream 提供 at-least-once delivery 語意（consumer group + ACK）
- Delivery worker 可獨立水平擴展，不影響 HTTP SLA

## 為什麼這是 FAANG 面試的經典陷阱

在 System Design 面試中，候選人常說「把耗時操作放到 background task 非同步化」。這在**概念上正確**，但面試官追問的是：

1. **Background task 跑在哪裡？** 同一個 process 的 thread pool，還是獨立 worker？
2. **thread pool 是共用的嗎？** 如果是，在高負載下根本沒有隔離。
3. **如果 background task 失敗了怎麼辦？** BackgroundTasks 沒有重試、沒有 DLQ、沒有 durability。
4. **delivery 的 SLA 是多少？** 如果允許延遲交付，這必須明確告知呼叫方（202 Accepted + async）。

正確答案不是「用 BackgroundTasks」，而是**選擇適當的隔離層級**：同 process async（asyncio）、獨立 process（worker container）、或可靠佇列（Redis Streams / Kafka）。選擇哪一層取決於 durability 需求、delivery latency SLA、以及系統的整體負載模型。

## 結論

Tier 2B 的實驗提供了一個教科書級的反例：**在已飽和的系統中，「把工作移到背景」若沒有真正改變資源的分配方式，只是把競爭從顯式（同步等待）變成隱式（thread pool 爭搶），結果反而更糟。**

真正的非同步化需要跨越「同一個 thread pool」這條邊界——無論是用 `asyncio.create_task`（消滅 thread 需求）還是獨立 worker 容器（隔離 CPU 與 I/O 資源）。下一步應朝向 **Redis Streams + 獨立 delivery worker** 的架構，達到真正的 producer-consumer 解耦。
