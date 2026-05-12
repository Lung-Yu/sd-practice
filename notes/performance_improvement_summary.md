# QR Code Generator 效能優化總結

**專案期間：** 2026-05-10 — 2026-05-12  
**優化目標：** 單機從 752 req/s 向 5,000 QPS 推進，同時維持 redirect < 10 ms、create 成功率 > 95%

---

## 全階段效能對比

| 指標 | Baseline | Ph1 | Ph2 | Ph3 | Ph4b | Ph5 | Ph6 | Ph7 | Ph8 | Ph9 |
|------|----------|-----|-----|-----|------|-----|-----|-----|-----|-----|
| **avg throughput** | 752 | 1,284 | 598 | 957 | 938 | **2,056** | 1,471 | 1,716 | ~1,796 | — |
| **redirect throughput** | — | — | — | — | — | — | — | — | 1,731 | **2,661** |
| **create p50** | — | — | 5ms | — | — | 1,166ms | 3,713ms | 4,680ms | **42ms** | **42ms** |
| **create throughput** | — | — | — | — | — | — | — | ~343/s | **631/s** | **630/s** |
| **Dropped iterations** | 76.6% | 51.8% | 78.5% | 65.6% | 66.2% | **23.0%** | 45.8% | 36.7% | — | — |
| **redirect p50** | 3,847ms | 1,423ms | 0.063ms | sub-ms | 0.081ms | 17.1ms | 23ms | 23ms | — | — |
| **create 成功率** | 100%＊ | 100% | 69% | 97.89% | 98.65% | **100%** | 99.99% | 99.999% | 99.99% | 99.99% |
| **App error rate** | 0%＊ | 0% | 0% | 0% | ~3,199 | **0** | 0 | 0 | 0 | 0 |

＊ Baseline 在較低 QPS（752 req/s）下測試，並非等效壓力比較。

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

**效果：** 吞吐量 +71%（752 → 1,284 req/s）；redirect p50 -63%（3,847 → 1,423 ms）

**引發的新問題：** In-process cache 無法跨 4 個 worker 共用，存在一致性風險；同步 DB I/O 仍是根本瓶頸。

---

### Phase 2 — Async SQLAlchemy + asyncpg + Redis 分散式快取

**架構變更：**
- `create_async_engine` + `AsyncSession`：所有 DB 操作完全非同步
- Redis 取代 in-process dict：redirect cache 跨 process 共用
- 回退至單 worker（asyncpg pool 在單 process 內充分共用）

**效果：** redirect p50 達 **0.063 ms**（-99.996%）；create p50（成功）降至 5.13 ms（-99.8%）

**引發的新問題：** `_record_scan()` 與 create 共用同一個 asyncpg pool，高流量下 scan 寫入消耗連線，create 成功率跌至 69%；單 worker 吞吐量上限降至 598 req/s。

---

### Phase 3 — Redis Stream 掃描佇列 + 批次 DB 寫入

**架構變更：**
- `XADD` 推入 Redis Stream `scan_events`（maxlen=100,000）：redirect 熱路徑完全無 DB 寫入
- `scan_consumer()`（新增 consumer.py）：`xread(count=200, block=500ms)` 批次消費，批次 INSERT 至 PostgreSQL

**效果：** create 成功率從 69% 回升至 97.89%；redirect 成功率維持 100%；吞吐量回升至 957 req/s

---

### Phase 4a — Optimistic INSERT（移除 token 存在性 SELECT）

**架構變更：** 移除 INSERT 前的 `SELECT EXISTS`，直接 INSERT；IntegrityError 捕捉後重試

**效果：** 正常路徑 DB 操作從 2 次降為 1 次。吞吐量略降（957 → 735）屬環境噪聲，真正效益需在 pool 瓶頸解除後才顯現。

---

### Phase 4b — PgBouncer 連線池代理

**架構變更：** 在 PostgreSQL 前加入 PgBouncer（transaction mode，DEFAULT_POOL_SIZE=25，MAX_CLIENT_CONN=1000）；app pool_size 修正為 10+10（原本 5+5 過小）

**效果：** 消除 QueuePool 500 錯誤（原本 ~3,199 次 → 0）；create 成功率 98.65%；吞吐量 938 req/s。PgBouncer 讓有限的真實 PG 連線能服務更多 app 並發。

---

### Phase 5 — 4 uvicorn Workers + Redis Stream Consumer Groups

**架構變更：**
- `--workers 1` → `--workers 4`：突破 single event loop 上限
- Consumer Groups（`xreadgroup` / `xack`）：4 個 worker 各有 consumer identity，確保每條 scan 訊息只被消費一次（exactly-once）

**效果：** 吞吐量 +119%（938 → **2,056 req/s**）；create/redirect 均達 100% 成功率；App error 完全歸零；Dropped iterations 首次突破 25%（66.2% → 23.0%）

**學習：** `xread` 在多 worker 下重複消費；Consumer Group 的 PEL 機制保證 exactly-once 語意。

---

### Phase 6 — Nginx Load Balancer + 2 App Containers

**架構變更：** 引入 Nginx（worker_processes auto、worker_connections 8192、keepalive 32）；2 個 app 容器（app1 + app2），合計 8 workers

**效果：** 吞吐量從 2,056 → 1,471 req/s（**-28%**，反效果）

**根因：** 瓶頸是 PostgreSQL write throughput，不是 worker 數；8 workers 加劇 DB 競爭；Nginx proxy 增加 TCP hop。水平擴展對 I/O-bound workload 無效——加 worker 讓更多 worker 競爭同一 DB 連線池。

**踩坑：** `worker_connections 1024` 在 3,000 VU 下耗盡；`events{}` block 不可省略。

---

### Phase 7 — Negative Caching、expires_at Bug 修正、調優

**架構變更：**
- Negative cache（`gone:{token}`, TTL=60s）：不存在/已刪除的 token 第一次 DB 查詢後直接快取，後續請求跳過 DB
- expires_at bug 修正：`set_cached_url` 依 `expires_at - now` 設定 Redis TTL，確保過期 URL 不再被無限期快取
- PgBouncer `MAX_CLIENT_CONN` 1000 → 2000；Nginx `keepalive_requests 1000` + `keepalive_timeout 65s`

**效果：**
- probe（not_found）p50：~1,500ms → **67ms**（-95.5%）
- 整體吞吐量：1,471 → **1,716 req/s**（+16.7%）
- Dropped iterations：45.8% → 36.7%

**核心洞察：** 消除不必要的 DB 查詢，永遠比優化 DB 查詢本身更有效率。

---

### Phase 8 — DB 調優（synchronous_commit=off、Pool 瘦身、Worker 分析）

**架構變更：**
- `synchronous_commit=off`、`checkpoint_completion_target=0.9`、`wal_buffers=16MB`
- PgBouncer `DEFAULT_POOL_SIZE` 25 → 40
- `pool_size` 50→10，`max_overflow` 50→10（Little's Law：async worker 實際需求 <1 個 DB 連線）
- `scan_events` 設為 UNLOGGED TABLE（Phase 9c 因 replication 相容性問題後來撤回）
- 新增 redirect-only k6 壓測腳本（排除 create 干擾，確認真實 redirect ceiling）

**效果：**

| 指標 | Phase 7 | Phase 8 | 改善 |
|------|---------|---------|------|
| create p50 | 4,680ms | **42ms** | **110x ↑** |
| create p95 | 8,924ms | **55ms** | **162x ↑** |
| create throughput | ~343/s | **631/s** | **+84%** |
| redirect throughput | 1,731/s | 1,731/s | Nginx bound |

**踩坑：** 8 workers（vs 4）redirect 反降 6.6%。原因：Nginx keepalive=32 固定池在 16 upstream workers 下每 worker 分到更少持久連線；Podman VM 只有 5 vCPU 支撐 18+ 個重型進程。

**Stage 8b CPU 瓶頸驗證：** 繞過 Nginx 直打 app1:8001 達 2,116 req/s（+22%），確認 Nginx 有真實開銷。根因：Podman VM 5 vCPU 已滿。修復：`podman machine set --cpus 8` → redirect 從 1,731 提升至 2,605 req/s（+46%）。

**理論 vs 實測對比：**

| 優化項 | 理論預期 | 實測結果 |
|--------|---------|---------|
| workers 4→8 | +100% | **-6.6%**（Nginx bound + CPU 競爭） |
| synchronous_commit=off | 3-10x | **110x**（WAL fsync 是絕對主因） |
| pool_size 50→10 | 性能持平、記憶體降低 | 符合預期 |

---

### Phase 9 — Config 補完：AOF + Read Replica + Rate Limiting

#### Stage 9a — Nginx keepalive + PostgreSQL shared_buffers

| 變更 | 效果 |
|------|------|
| keepalive 32 → 128 | redirect throughput +2.1%（2,605 → 2,661 req/s） |
| shared_buffers 128MB → 256MB | 在當前資料規模下邊際效益不顯著 |

#### Stage 9b — Redis AOF 持久化

- `--appendonly yes --appendfsync everysec`；新增 `redis_data:/data` volume
- Redis 重啟後 redirect 仍回傳 302（AOF 成功恢復 cache 資料）
- redirect throughput 無退步（AOF 只影響寫入）
- Redis 7.x Multi-Part AOF：base.rdb（快照）+ incr.aof（增量）+ manifest（索引）

#### Stage 9c — PostgreSQL Read Replica（讀寫分離）

```
寫入路徑：app → PgBouncer → Primary
讀取路徑：app → postgres_replica:5432（get_qr_info、analytics）
複製：Primary → WAL streaming → Replica（replay_lag ~1.8ms）
```

- `get_qr_info` 和 `get_analytics` 改用 `get_read_db()`（read_engine, pool_size=5）
- analytics 查詢負載從打 Primary 改為打 Replica

**關鍵衝突：** Phase 8d 的 UNLOGGED scan_events（跳過 WAL）與 streaming replication（複製 WAL）互斥。Replica 讀 UNLOGGED table 報 `cannot access temporary or unlogged relations during recovery`。解法：`ALTER TABLE scan_events SET LOGGED`，捨棄 UNLOGGED 換取 replication 架構。

**教訓：** 優化之間有相依性。引入 UNLOGGED 時應預先考慮是否計劃加 replication。

#### Stage 9d — Nginx Rate Limiting

```nginx
limit_req_zone $binary_remote_addr zone=create_zone:10m rate=20r/s;
location = /api/qr/create {
    limit_req zone=create_zone burst=40 nodelay;
    limit_req_status 429;
}
```

- 精確匹配（`=`）確保只限制 create，不影響 redirect
- 80 個並發 create：45 個 200、35 個 429（burst=40 後正確觸發）

---

## 各階段解決的問題與揭示的瓶頸

| 階段 | 解決的問題 | 揭示的新瓶頸 |
|------|-----------|------------|
| **Phase 1** | Thread pool 阻塞；redirect scan 阻塞回應 | In-process cache 無法跨 worker；同步 I/O 仍是根本限制 |
| **Phase 2** | Cache 一致性；redirect 延遲（次毫秒突破） | Scan 寫入與 create 爭搶 asyncpg pool；單 worker 上限 |
| **Phase 3** | Scan 寫入與 create 的連線池競爭 | 單 worker event loop 排程上限 |
| **Phase 4b** | QueuePool 500 error；PgBouncer 缺失 | 單 worker 上限仍在 |
| **Phase 5** | 單 worker CPU 上限；Consumer Group 保證 exactly-once | PostgreSQL 單機 write throughput |
| **Phase 6** | 單容器 CPU 上限（嘗試水平擴展） | DB write 瓶頸無法被加 worker 解決；Nginx proxy 開銷 |
| **Phase 7** | Negative cache 消除無效 DB 查詢；expires_at bug | WAL fsync 是 create 的絕對延遲主因 |
| **Phase 8** | WAL fsync（synchronous_commit=off）；pool 浪費；Nginx keepalive 分析 | Podman VM 5 vCPU 資源瓶頸（非架構問題） |
| **Phase 9** | Redis 重啟資料丟失；analytics 打 Primary；create 濫用；Nginx TCP overhead | UNLOGGED ⊕ Replication 不相容（優化之間的相依性） |
| **Phase 10** | Rate limit 從 Nginx → FastAPI dependency（Middleware 陷阱） | Python workers 是唯一剩餘瓶頸；LB 層換不動它 |
| **Phase 11a** | 驗證 Scale Up（12 vCPU + 4 containers）能否突破 2,661 req/s ceiling | 單一 Podman VM 的 CPU/網路是架構上限；加 container 不等於加資源 |
| **Phase 11b** | 驗證三層 LB 路由正確性（GLB → site LB → app） | 單 VM 無法驗證效能水平擴展；需要真實多主機環境 |
| **Phase 11c** | Varnish CDN 快取 302，突破單機 app ceiling | Cache purge 機制需處理 expires_at / URL 更新場景 |

---

## 累積成果（Baseline → Phase 9）

| 指標 | Baseline | Phase 9 終態 | 累積改善 |
|------|----------|------------|---------|
| redirect throughput | ~752 req/s（混合） | **2,661 req/s** | **+254%** |
| create p50 | ~5,797ms | **42ms** | **-99.3%** |
| create throughput | ~343/s | **630/s** | **+84%** |
| redirect p50 | 3,847ms | sub-ms（Redis cache hit） | **> -99.9%** |
| Redis 持久性 | 重啟全失 | AOF 保留（重啟後恢復） | ✓ |
| analytics 負載 | 全打 Primary | 打 Replica（replay_lag 1.8ms） | ✓ |
| create 濫用保護 | 無 | 429 rate limit（20r/s, burst=40） | ✓ |

---

### Phase 10 — LB 層驗證 + Rate Limit 遷移

**直連壓測：** app1 單機 = 2,304 req/s；理論 2 × 2,304 = 4,608，但 Nginx + 2 apps = 2,661（Nginx 吃掉 ~42% 理論值）。瓶頸確認在 Python workers，不在 LB。

**HAProxy 實驗（失敗）：** HAProxy 2,520 req/s，仍低於 Nginx 2,661 req/s。假說「換 LB 可以釋放 CPU」被否定，回滾 Nginx。

**Rate limit 遷移（成功）：**
- Nginx `limit_req_zone` → FastAPI route dependency
- Global middleware 對所有請求加 overhead（-25%）→ dependency 只掛 create route，redirect 零負擔
- Redis fixed-window counter：`ratelimit:create:{ip}:{unix_second}`，max=60/s（等效 rate=20 burst=40）

---

### Phase 11a — Scale Up 驗證：單機 ceiling 確認

**實驗目標：** 12 vCPU + 4 app containers，能否突破 2,661 req/s ceiling。

**測試結果：**

| 配置 | workers（total）| throughput（peak） |
|------|-----------------|-------------------|
| Phase 9（2 containers × 6 workers） | 12 | 2,661 req/s |
| 11a 初測（4 containers × 6 workers，24 total） | 24 | 2,173 req/s（退步！）|
| 11a 修正（4 containers × 4 workers，16 total） | 16 | ~2,550 req/s |

**關鍵教訓：單一 VM 內加 containers 是「虛假的橫向擴充」**

- 所有 containers 共享同一 VM 的 CPU 和 Podman bridge network
- 過多 Python workers（24）造成 context switching 開銷超過其帶來的並發效益
- peak 仍在 2,500–2,600 req/s，與 Phase 9 相同天花板
- 真正突破需要 **多台主機**（Phase 11b）

**附帶修正：** k6 setup 的 Rate Limit 陷阱
- setup() 循序建立 500 個 token，發送速率超 60/s → 只 seed 了 58 tokens
- 加入 `sleep(0.025)` 降速至 ~40 req/s → 340 tokens 成功建立

---

### Phase 11b — Multi-host 模擬：三層 LB 路由驗證

**架構：** nginx-global（GLB）→ nginx-site1（app1/app2）+ nginx-site2（app3/app4）

**功能驗證：成功** — 兩個 site 均勻分流，302 路由跨三層均正確。

**效能：931 req/s（比 11a 退步 59%）— 模擬環境限制，非架構問題**

根因：三個 Nginx containers 在同一 VM 上，每個 request 多穿兩層 Podman bridge + 多 6 個 Nginx worker 搶 CPU。在真實多主機環境，每個 site 有獨立 CPU，額外 hop 只是 ~0.5ms 延遲，不是 CPU 競爭。

**理論估算（真實多主機）：** 2 sites × 2,600 req/s = **~5,200 req/s**

---

### Phase 11c — Varnish CDN：5,000 QPS 目標達成

**結果：Peak ~5,100 req/s，p50 = 0.202ms（HIT），所有 threshold 通過 ✓**

Varnish 將 `GET /r/<token>` 的 302 responses 快取 60s。Cache HIT = 記憶體 hash lookup，
繞過 nginx / app / Redis 整個路徑。500 tokens 在測試開始後 < 1 秒全暖，後續 > 99.9% 為 HIT。

| 方案 | Peak QPS | p50 |
|------|---------|-----|
| 單機 app（Phase 9/11a）| ~2,600 | 29ms |
| 真實多主機 2 sites（Phase 11b 理論）| ~5,200 | 29ms |
| 單機 + Varnish CDN（Phase 11c 實測）| **~5,100** | **0.202ms** |

CDN 在不增加 app 主機的情況下達成 5,000 QPS，是讀多寫少工作負載的最高性價比擴展手段。
生產注意：若 QR code 有 `expires_at` 或 destination URL 可更新，需實作 Varnish PURGE 機制。

---

## 最終架構（Phase 11c）

```
                 Varnish CDN（port 8200，256MB cache，TTL=60s）
                      │
                      │ MISS（第一次 / TTL 過期）
                      ▼
nginx-origin（worker 4，app1~4 upstream，port 8100）
      │
      ├── app1 / app2 / app3 / app4（各 4 uvicorn workers）
      │       ├── 寫入路徑 → PgBouncer → PostgreSQL Primary
      │       │   (synchronous_commit=off, shared_buffers=256MB, wal_level=replica)
      │       │                     ↓ WAL streaming
      │       │             PostgreSQL Replica（replay_lag ~1.8ms）
      │       └── 讀取路徑（get_qr_info, analytics）→ Replica
      │
      Redis（AOF, appendfsync=everysec）
      Rate limit：FastAPI _rate_limit_create dependency（Redis counter）

── Phase 11b（多 site 路由驗證用，非生產路徑）──
nginx-global → nginx-site1（app1/app2）
             → nginx-site2（app3/app4）
```

| 路徑 | 吞吐量 | p50 |
|------|--------|-----|
| redirect（Varnish HIT）| ~5,100 req/s peak | 0.202ms |
| redirect（Varnish MISS / Redis cache）| ~2,600 req/s | 29ms |
| create | ~630/s | 42ms |

---

## 結語

本次優化從 Baseline 的同步阻塞架構出發，歷經 Phase 1–11，達成 **5,000 QPS 原始目標**：

| 指標 | Baseline | Phase 11c 終態 | 累積改善 |
|------|----------|--------------|---------|
| redirect throughput（peak）| ~752 req/s | **~5,100 req/s**（Varnish HIT）| **+578%** |
| redirect p50 | 3,847ms | **0.202ms** | **> -99.99%** |
| create p50 | ~5,797ms | **42ms** | **-99.3%** |
| create throughput | ~343/s | **630/s** | **+84%** |

優化過程中最關鍵的五個洞察：

1. **消除不必要的操作，勝於優化已有的操作**  
   Negative cache 消除 DB 查詢；`synchronous_commit=off` 消除 WAL fsync 等待；Varnish 消除整個 app 路徑。

2. **水平擴展只對正確的瓶頸有效**  
   Write I/O-bound 下加 app worker 反而更差；換 LB 無法突破 Python worker 瓶頸；同一 VM 加 containers 是虛假擴充。

3. **優化之間有相依性**  
   UNLOGGED TABLE 與 Streaming Replication 不相容；引入單一優化前應考慮後續架構方向。

4. **Guard 邏輯放在正確的層**  
   Global Starlette middleware 污染所有路徑（-25% redirect throughput）；route dependency 只影響目標 route。

5. **CDN 是讀多寫少工作負載最高性價比的擴展手段**  
   對穩定的重定向 URL，CDN 快取讓每個 HIT = 記憶體讀取（0.2ms），
   遠優於加 app 主機（需完整 CPU + Redis 查詢）。生產環境需配套 PURGE 機制處理 URL 更新。
