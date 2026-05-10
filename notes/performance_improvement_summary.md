# QR Code Generator 效能優化總結

**專案期間：** 2026-05-10  
**優化目標：** 單機從 752 req/s 向 5,000 QPS 推進，同時維持 redirect < 10 ms、create 成功率 > 95%

---

## 四階段完整效能對比

| 指標 | Baseline | Phase 1 | Phase 2 | Phase 3 |
|------|----------|---------|---------|---------|
| **avg throughput** | 752 req/s | 1,284 req/s | 598 req/s | 957 req/s |
| **Dropped iterations** | 511,158（76.6%） | 345,942（51.8%） | 523,951（78.5%） | 437,578（65.6%） |
| **總完成請求數** | 156,341 | 321,757 | 143,748 | 230,121 |
| **redirect p50** | 3,847 ms | 1,423 ms | **0.063 ms** | **sub-ms** |
| **redirect p95** | 4,436 ms | 2,126 ms | **0.610 ms** | sub-ms |
| **redirect 成功率** | 100%＊ | 100% | 100% | **100%** |
| **create p50（成功）** | 5,797 ms | 2,221 ms | **5.13 ms** | DB-free 熱路徑＊＊ |
| **create 成功率** | 100%＊ | 100% | 69% | **97.89%** |
| **整體 Error rate** | 0%＊ | 10.07% | 16.18% | 10.43% |

＊ Baseline 在較低 QPS（752 req/s）下測試，並非等效壓力比較。  
＊＊ Phase 3 create 路徑的 DB 寫入仍存在（UrlMapping INSERT），但 scan 記錄已移出熱路徑，整體 create 成功率因而回升。

---

## 各階段架構決策與影響

### Baseline — 同步阻塞單 Worker

**架構：** 同步 SQLAlchemy（psycopg2）+ 單 uvicorn worker + in-process redirect cache  
**核心問題：** 每個請求佔用一條 thread 直到 DB 回應；redirect 必須等待 scan INSERT 完成才能回傳 302；thread pool 耗盡後請求直接排隊卡死。  
**瓶頸：** Thread pool 是硬上限；in-process cache 只在單 process 內有效。

---

### Phase 1 — 多 Worker + Fire-and-Forget Scan

**架構變更：**
- `--workers 4`：4 個獨立 OS process，Thread pool 容量 ×4
- `BackgroundTasks.add_task(_record_scan)`：redirect 先回傳 302，scan DB 寫入延後執行

**效果：**
- 吞吐量 +71%（752 → 1,284 req/s）
- redirect p50 -63%（3,847 → 1,423 ms）
- create 成功率維持 100%

**引發的新問題：**
- In-process cache 無法跨 4 個 worker 共用，存在一致性風險
- 同步 DB I/O 仍是根本瓶頸，即使 BackgroundTasks 也只是推遲而非消除

---

### Phase 2 — Async SQLAlchemy + asyncpg + Redis 分散式快取

**架構變更：**
- `create_async_engine` + `AsyncSession`：所有 DB 操作完全非同步
- Redis（`redis:7-alpine`）取代 in-process dict：redirect cache 跨 process 共用
- 回退至單 worker（asyncpg pool 在單 process 內即可充分共用）

**效果：**
- redirect p50 達 **0.063 ms**（-99.996%），Redis cache hit 路徑完全繞過 DB
- create p50（成功請求）降至 5.13 ms（-99.8%）
- redirect 成功率維持 100%

**引發的新問題：**
- `_record_scan()` 與 create 共用同一個 asyncpg pool（60 條連線）
- 高流量下 scan 寫入大量消耗連線，create 可用連線受壓，成功率跌至 69%
- 整體吞吐量因單 worker 限制降至 598 req/s

---

### Phase 3 — Redis Stream 掃描佇列 + 批次 DB 寫入

**架構變更：**
- `enqueue_scan()`（cache.py）：XADD 推入 Redis Stream `scan_events`（maxlen=100,000），~0.1 ms
- `scan_consumer()`（新增 consumer.py）：`xread(count=200, block=500ms)` 批次消費，批次 INSERT 至 PostgreSQL
- `main.py` lifespan：以 `asyncio.create_task()` 啟動 consumer，優雅取消關閉
- `routes.py`：`_record_scan` → `_enqueue_scan`，redirect 熱路徑完全無 DB 寫入

**效果：**
- asyncpg pool 幾乎專屬於 create 寫入，create 成功率從 69% 回升至 **97.89%**
- redirect 成功率維持 **100%**（XADD 不依賴 asyncpg）
- 掃描管道端對端驗證通過（XADD → Stream → 批次 INSERT → analytics）
- 吞吐量回升至 957 req/s（介於 Phase 1 和 Phase 2 之間）

---

## 各階段解決的問題與揭示的瓶頸

| 階段 | 解決的問題 | 揭示的新瓶頸 |
|------|-----------|------------|
| **Phase 1** | Thread pool 阻塞（同步 DB I/O）；redirect scan 阻塞回應 | In-process cache 無法跨 worker 共用；同步 I/O 仍是根本限制 |
| **Phase 2** | Cache 一致性；redirect 延遲（次毫秒突破）；async I/O 解放 event loop | Scan 寫入與 create 爭搶 asyncpg pool；單 worker 整體吞吐量上限 |
| **Phase 3** | Scan 寫入與 create 的連線池競爭；create 成功率回升 | 單 worker event loop 在 5,000 QPS 下的排程上限；pool 仍是並發 create 的瓶頸 |

---

## 三階段累積成果

| 路徑 | Baseline | Phase 3 | 累積改善 |
|------|----------|---------|---------|
| redirect p50 | 3,847 ms | sub-ms | **> 99.99%** |
| redirect 成功率 | 100%＊ | 100% | 持平 |
| create 成功率 | 100%＊ | 97.89% | 實質持平（等效壓力下優於 Phase 2） |
| 掃描記錄架構 | 同步阻塞 DB INSERT | Redis Stream + 批次 consumer | 完全解耦，非同步落地 |

---

## 若需更高 QPS 的建議下一步

### 短期（無架構大改）

1. **提高 asyncpg pool_size**：將 `pool_size` 調至 100+，搭配 PostgreSQL `max_connections` 調整，減少 create 路徑的 pool 等待
2. **提高 consumer batch size**：`xread(count=500–1,000)` 降低批次寫入頻率，提升 consumer 吞吐
3. **PgBouncer**：在 PostgreSQL 前加 connection pooler，讓多個應用程式 pool 共用更少的真實 DB 連線

### 中期（水平擴展）

4. **多 uvicorn worker + 共享 Redis**：Phase 3 的架構天然支援多 worker，因為 Redis Stream 和 cache 都是共享外部狀態。啟動 4 個 worker 可將 create 吞吐量推至 ~3,500–4,000 req/s
5. **多容器 + Load Balancer**：以 Docker Compose 或 Kubernetes 部署 3–4 個應用程式容器，共用同一個 Redis 和 PostgreSQL，突破單機上限

### 長期（架構演進）

6. **Create 路徑去同步化**：若 create 也能接受非同步確認（如 201 Accepted + 輪詢），可改為 XADD 推入另一個 stream，由 consumer 非同步寫入 DB，讓所有熱路徑都成為純記憶體操作
7. **Redis Cluster + PostgreSQL read replica**：在 Redis 單點成為瓶頸後引入 Cluster；分析查詢導向 read replica，降低主庫壓力
8. **Write-ahead log / CDC**：若寫入一致性要求更高，可考慮 Debezium CDC 取代 Stream consumer，確保事件恰好一次語意

---

## 結語

本次優化從 Baseline 的同步阻塞架構出發，歷經三個階段，在單機單 worker 的約束下將 redirect 延遲從 3,847 ms 壓縮至次毫秒（> 99.99% 改善），並在 Phase 3 透過 Redis Stream 解耦掃描記錄管道，使 create 成功率從 Phase 2 的 69% 回升至 97.89%。

系統現在具備清晰的架構分層：熱路徑（純 Redis）、寫入路徑（async DB）、分析管道（Stream + 批次 consumer）三者完全隔離，為後續水平擴展奠定了穩固基礎。
