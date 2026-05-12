# Phase 3 結果：Redis Stream 掃描佇列 + 批次 DB 寫入

**實施日期：** 2026-05-10

## 改動摘要

- `scaffold/app/cache.py`：新增 `enqueue_scan()` 函數，將掃描事件以 `XADD` 推入 Redis Stream `scan_events`（maxlen=100,000），取代直接寫入 PostgreSQL 的做法；每次 XADD 耗時約 0.1 ms
- `scaffold/app/consumer.py`（新增模組）：實作非同步 `scan_consumer()` 協程，以 `xread(count=200, block=500ms)` 持續從 Redis Stream 消費事件，並以批次 `INSERT` 方式將 `ScanEvent` 資料列批量寫入 PostgreSQL，大幅降低 DB 寫入頻率
- `scaffold/app/main.py`：在應用程式 lifespan 啟動時以 `asyncio.create_task()` 啟動 `scan_consumer`，並於關閉階段優雅取消任務，確保 consumer 生命週期與應用程式綁定
- `scaffold/app/routes.py`：將 redirect 路由中的 `_record_scan`（直接 DB 寫入）替換為 `_enqueue_scan`（Redis XADD）；redirect 與 create 的熱路徑現在完全無 DB 寫入，掃描記錄完全由背景 consumer 非同步處理

## 效能對比

| 指標 | Baseline | Phase 1 | Phase 2 | Phase 3 | 累積改善 |
|------|----------|---------|---------|---------|---------|
| avg throughput | 752 req/s | 1,284 req/s | 598 req/s | 957 req/s | +27%（vs Baseline） |
| Dropped iterations | 511,158（76.6%） | 345,942（51.8%） | 523,951（78.5%） | 437,578（65.6%） | -14.4 pp（vs Baseline） |
| redirect p50 | 3,847 ms | 1,423 ms | 0.063 ms | sub-ms | **-99.99%+** |
| create 成功率 | 100%＊ | 100% | 69% | 97.89% | 持平高位 |
| Error rate（整體） | 0%＊ | 10.07% | 16.18% | 10.43% | 持平 Phase 1 水準 |

＊ Baseline 的錯誤率 0%、create 成功率 100% 反映的是較低 QPS 下（752 req/s）的結果，並非同等壓力下的比較基準。Phase 1 起才以 5,000 QPS 目標施壓，因此出現探測流量帶來的 10% 錯誤率。

### Phase 3 各 Scenario 詳細數據

| 指標 | 數值 |
|------|------|
| 總 HTTP 請求數 | 230,121 |
| avg throughput | 957.6 req/s |
| Dropped iterations | 437,578（65.6%） |
| 整體錯誤率 | 10.43%（24,005 筆） |
| 中位延遲（全部） | 107 ms |
| redirect check 通過率 | **100%**（零失敗） |
| create check 通過率 | **97.89%**（970 筆失敗，來自 3,000 VU 極端峰值的 EOF） |
| probe check 通過率 | 98.9% |

### 端對端掃描管道驗證

```
POST /api/qr/create → token
GET /r/{token}      → 302，掃描事件以 XADD 推入 Redis Stream
（~500 ms 後）      consumer xread(200, block=500ms) → 批次 INSERT
GET /api/qr/{token}/analytics → { "total_scans": 1 } ✓
```

## 分析

### Create 成功率從 69% 回升至 97.89%

Phase 2 的核心問題在於 `_record_scan()` 與 `create` 路由共用同一個 asyncpg 連線池（pool_size=60）。在高流量下，大量 redirect 觸發的掃描記錄任務持續從池中取用連線，使 `create` 路由可取得的連線數受到壓縮，最終導致連線等待超時、EOF 錯誤頻發，create 成功率跌至 69%。

Phase 3 將掃描記錄改為 Redis XADD（~0.1 ms，完全不碰 asyncpg pool），asyncpg 連線池現在幾乎專屬於：

1. `POST /api/qr/create` 的 `UrlMapping` INSERT + token 唯一性檢查
2. `scan_consumer` 的批次 INSERT（每 500 ms 一批，佔用連線時間極短）

兩條路徑的連線爭搶大幅減少，create 成功率從 69% 回升至 97.89%，殘餘的 2.11% 失敗來自 3,000 VU 極端並發峰值期間的 EOF，屬於資源硬上限，而非架構缺陷。

### Redirect 成功率維持 100%

Redis XADD 是純記憶體操作，在 localhost 下 RTT 約 0.1 ms，且不依賴 asyncpg pool。即使 PostgreSQL 在極端負載下出現瞬間壓力，redirect 的熱路徑（Redis cache hit → XADD → 302）完全不受影響。consumer 的批次 INSERT 出現延遲最多影響 analytics 的即時性，不影響 redirect 的可用性。

### 整體吞吐量（957 req/s）低於 Phase 1（1,284 req/s）的根因

Phase 1 使用 4 個 uvicorn worker（4 個 OS process），在多核心硬體上可以真正並行處理 create 的 DB 寫入，整體吞吐量因此較高。Phase 3 維持單 worker，event loop 在同時 await 數千個 coroutine 時存在排程開銷（event loop tick latency）。雖然 XADD 消除了 redirect 路徑的 DB 寫入競爭，但 create 路徑仍有 asyncpg INSERT，在真實 5,000 QPS 下依然形成排隊。

### 三個階段的累積進化

| 階段 | 解決的問題 | 引發的新瓶頸 |
|------|-----------|------------|
| Baseline → Phase 1 | Thread pool 阻塞（同步 DB I/O） | In-process cache 無法跨 worker 共用 |
| Phase 1 → Phase 2 | Cache 一致性 + DB 非同步化（redirect 達次毫秒） | Scan 寫入與 create 爭搶 asyncpg pool |
| Phase 2 → Phase 3 | Scan 寫入與 create 的連線池競爭 | 單 worker 在極端並發下的 event loop 排隊 |

### 掃描管道驗證

端對端測試確認 Redis Stream 管道正常運作：XADD 將事件推入 stream → consumer 以 `xread(count=200, block=500ms)` 批次讀取 → 批次 INSERT 落地 PostgreSQL → analytics API 回傳正確的 `total_scans`。延遲約 500 ms（consumer 的 block 等待時間），屬於可接受的非同步延遲。

## 新發現的瓶頸

### 單 worker 在極端並發下的上限約 ~1,000 req/s

即使 scan 寫入已完全去除，create 路由仍需對 PostgreSQL 執行 `INSERT INTO url_mappings`。單一 event loop 在同時處理 3,000 VU 的 create 請求時，會因 asyncpg pool 取用排隊而出現殘餘 EOF 失敗。若需超過 1,000 req/s 的寫入吞吐量，必須引入多 worker 或多容器水平擴展。

### asyncpg 連線池仍是並發 create 的限制因素

在真實 5,000 QPS 下，60 條連線的 pool 仍不足以應對 create 路徑的需求高峰。可考慮：
- 提高 `pool_size`（需注意 PostgreSQL server 端的 `max_connections` 上限）
- 改用 PgBouncer 連線池代理，在 PostgreSQL 前加一層多工層

### Consumer 單連線批次寫入的潛在改進空間

目前 `scan_consumer` 每批最多 200 筆，使用單一 DB session/連線依序 INSERT。若掃描事件量極大（stream 積壓），可考慮：
- 提高 `count`（如 500–1,000）
- 使用 `executemany` 或 PostgreSQL COPY protocol 進一步加速批次寫入

## 結論

三個階段的優化旅程至此告一段落。

Redirect 熱路徑從 Baseline 的 3,847 ms p50 降至次毫秒（**累積改善 > 99.99%**），且成功率全程維持 100%。Create 成功率從 Phase 2 跌至的 69% 回升至 97.89%，端對端掃描管道（XADD → Redis Stream → 批次 INSERT → analytics）已完整驗證並正常運作。

系統架構現已具備正確的非同步分層：

- **熱路徑**（redirect）：純記憶體操作（Redis GET + XADD），延遲 < 1 ms
- **寫入路徑**（create）：asyncpg async INSERT，延遲個位數 ms（pool 充足時）
- **分析管道**（scan consumer）：Redis Stream → 批次 DB INSERT，與熱路徑完全隔離

若需進一步提升至 5,000 QPS+，下一步應為**水平擴展**：多個 uvicorn worker 或多個容器共用同一個 Redis 實例，讓 create 路徑的 DB 寫入能真正並行，突破單 event loop 的上限。
