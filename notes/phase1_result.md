# Phase 1 結果：多 Worker + fire-and-forget scan

**實施日期：** 2026-05-10

## 改動摘要

- `scaffold/Dockerfile`：uvicorn 從單 worker 改為 `--workers 4`
- `scaffold/app/routes.py`：`_record_scan()` 改為 `BackgroundTasks.add_task()`（fire-and-forget，不阻塞 redirect 回應路徑）

## 效能對比

| 指標 | Baseline | Phase 1 | 改善幅度 |
|------|----------|---------|---------|
| 峰值 QPS | 752 | 1,284 avg | +71% |
| Dropped iterations | 511,158（76.6%） | 345,942（51.8%） | -32.5% |
| 總完成請求 | 156,341 | 321,757 | +106% |
| redirect p50 | 3,847 ms | 1,423 ms | -63% |
| redirect p95 | 4,436 ms | 2,126 ms | -52% |
| create p50 | 5,797 ms | 2,221 ms | -62% |
| create p95 | 6,611 ms | 3,221 ms | -51% |

### Phase 1 各 Scenario 詳細延遲

| Scenario | p50 | p95 | p99 |
|----------|-----|-----|-----|
| redirect | 1,423 ms | 2,126 ms | 2,439 ms |
| create | 2,221 ms | 3,221 ms | 3,650 ms |
| not_found/probe | 1,492 ms | 2,203 ms | 2,527 ms |

### Phase 1 請求分佈

| HTTP 狀態 | Scenario | 請求數 |
|-----------|----------|--------|
| 302 | redirect | 224,822 |
| 200 | create | 64,309 |
| 404 | not_found/probe（刻意探測流量） | 32,426 |

錯誤率 10.07% 全部來自 not_found 探測流量，redirect 與 create 通過率均為 100%。

## 分析

### 多 Worker 帶來的線性吞吐量提升

Baseline 的單 uvicorn worker 意味著整個應用程式只有一個 OS process，所有請求共用同一個 thread pool。FastAPI 的同步路由函數（`def`）每次執行都佔用一條 thread，預設 thread pool 大小有限，一旦耗盡，後續請求只能在 event loop 排隊等待，造成延遲急劇膨脹。

改為 `--workers 4` 後，uvicorn 以 `multiprocessing` 方式啟動 4 個獨立 OS process，每個 process 各自擁有完整的 thread pool。在 Apple Silicon Mac Mini 的多核心環境下，4 個 worker 可以真正並行執行，有效吞吐量從約 750 QPS 提升到 1,284 req/s（+71%），總完成請求數翻倍（+106%）。

### BackgroundTasks 解除 redirect 的 DB 寫入阻塞

Baseline 中，`redirect` 路由在回應 302 之前必須同步呼叫 `_record_scan()`，等待一次 `INSERT INTO scan_events` 完成才能釋放 thread。也就是說，即使 redirect 已從 in-memory cache 拿到 URL，它的延遲仍包含一次完整的 DB 寫入往返時間。

改為 `BackgroundTasks.add_task(_record_scan, ...)` 後，uvicorn 在回應 302 給客戶端之後才執行 scan 記錄，DB 寫入完全脫離 redirect 的關鍵路徑。這是 redirect p50 從 3,847 ms 降至 1,423 ms（-63%）的主因，即使在高負載下也能快速釋放 thread 去處理下一個請求。

`create` 路由本身仍須同步完成 DB INSERT 才能回傳 token，因此改善幅度略低於 redirect，但多 worker 帶來的並行能力也讓 create p50 從 5,797 ms 降至 2,221 ms（-62%）。

## 新發現的瓶頸

### 仍有約 50% Dropped Iterations

在 5,000 QPS 的目標負載下，Dropped iterations 從 76.6% 降至 51.8%，代表系統仍然只能應對約一半的預定到達流量。即使 redirect 已解除 DB 阻塞，`create` 路由的同步 DB INSERT 以及 redirect cache miss 後的同步 SELECT，仍然會消耗 thread pool 資源，在高並發下形成排隊。

### 延遲仍偏高

redirect p95 為 2,126 ms、create p95 為 3,221 ms，距離理想的個位數到兩位數 ms 還有很大差距。根本原因是所有 DB 操作仍為同步阻塞式：每次查詢或寫入都讓 thread 在等待 PostgreSQL 回應期間無法處理其他請求。`BackgroundTasks` 雖然把 scan 寫入推遲，但 `_record_scan()` 本身仍是同步的，執行時依然佔用一條 thread。

### In-process Cache 無法跨 Worker 共用

`redirect_cache` 是 module-level dict，存活在各個 worker process 的獨立記憶體空間中。4 個 worker 各自維護一份互不同步的快取，導致：

1. 某個 worker 建立的 QR code 不會出現在其他 worker 的快取，造成不必要的 cache miss 與額外 DB SELECT。
2. `create` 寫入快取後，`update`/`delete` 呼叫到不同 worker 時無法清除該 worker 的快取條目，存在資料一致性風險。
3. 快取效益隨 worker 數量增加而稀釋，水平擴展的收益會被部分抵銷。

### BackgroundTasks 與同步 DB Session 的隱患

`_record_scan()` 接收的 `db: Session` 是從請求依賴注入取得的，理論上在 response 送出後 SQLAlchemy 可能已開始關閉該 session。在高並發環境下，background task 執行時 session 狀態可能不穩定，需要改為在 background task 內部自行建立獨立 session 才能確保正確性。

## 結論

Phase 1 以最小改動達到了顯著的效能提升：吞吐量翻倍（+106%），redirect 與 create 延遲均腰斬（-52% 至 -63%），驗證了多 worker 並行與 fire-and-forget 非同步化是正確的優化方向。

然而，系統距離 5,000 QPS 目標仍有約 2.5 倍差距。下一個瓶頸已清晰可見：**同步阻塞的 DB I/O**。只要路由函數與 DB 的互動還是同步的，thread pool 就是硬上限，無論加多少 worker 都只是線性擴展。

Phase 2 的優先方向：

1. **async SQLAlchemy + asyncpg**：將所有 DB 操作改為 `async def`，讓 event loop 在等待 DB 回應時繼續處理其他請求，預期可將 QPS 上限推至 5,000+。
2. **Redis 分散式快取**：取代 in-process dict，解決多 worker 快取無法共用的問題，同時支援 TTL 自動過期。
3. **`_record_scan()` 改為獨立 async session**：確保 background task 的 DB session 生命週期獨立，避免潛在的 session 狀態問題。
