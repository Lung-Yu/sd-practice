# Web 服務效能優化經驗手冊

> 基於 QR Code Generator 十個 Phase 的實戰經驗彙整（2026-05-10 ~ 2026-05-12）  
> 所有公式均以本專案實測數據驗算

---

## 目錄

1. [系統診斷流程](#1-系統診斷流程)
2. [核心計算公式](#2-核心計算公式)
3. [各層優化判斷與效益估算](#3-各層優化判斷與效益估算)
4. [常見陷阱與反模式](#4-常見陷阱與反模式)
5. [快速診斷卡](#5-快速診斷卡)

---

## 本專案硬體與架構基準

```
硬體：Podman VM 8 vCPU（宿主機 Apple Silicon）
架構：Nginx → app1/app2（各 6 uvicorn workers）
      → PgBouncer（pool=40）→ PostgreSQL 16
      → PostgreSQL Replica（WAL streaming）
      Redis 7（AOF）
```

| 指標 | 數值 |
|------|------|
| 總 worker 數 | 12（6 × 2 容器） |
| PgBouncer pool | 40 |
| App pool_size | 10 + max_overflow 10 |
| Redis RTT | ~0.5ms |
| DB query time（sync_commit=off） | ~5ms |
| redirect throughput | 2,661 req/s |
| create p50 | 42ms |
| create throughput | 630 req/s |

---

## 1. 系統診斷流程

### 1.1 瓶頸識別決策樹

```
觀察：p50 高 or 吞吐量低
           │
     p50 > 500ms?
    ┌────────────────────────────┐
   Yes                          No（吞吐量低但延遲 OK）
    │                            │
   DB I/O 主導                  CPU / 並發 主導
    │                            │
   ├─ synchronous_commit=off?   ├─ worker 數夠嗎？(→ §2.3)
   ├─ 有多餘的 SELECT/INSERT?   ├─ 連線池夠嗎？(→ §2.2)
   └─ 有 N+1 查詢？             └─ event loop 飽和？
```

### 1.2 分層診斷順序

優先從最外層往內排查，每層用「隔離測試」確認：

| 層次 | 隔離方法 | 指標 |
|------|---------|------|
| **LB 層** | 直連 app（繞過 LB）壓測 | 直連 vs 透過 LB 的差距 |
| **App 層** | 單 worker 壓測 | per-worker 吞吐量上限 |
| **Cache 層** | 檢查 Redis cache hit rate | `INFO stats` 的 keyspace_hits/misses |
| **連線池層** | 監控 PgBouncer cl_waiting | 等待中的 client 連線數 |
| **DB 層** | `EXPLAIN ANALYZE` + `pg_stat_statements` | slow query / index miss |

### 1.3 本專案各 Phase 瓶頸位置

| Phase | 真正瓶頸 | 診斷方法 |
|-------|---------|---------|
| Baseline | Thread pool 耗盡（同步 DB I/O） | p50=3,847ms，明顯 DB wait |
| Phase 2 | asyncpg pool 被 scan 寫入占滿 | create 成功率 69% |
| Phase 4b | QueuePool 500 錯誤 | 3,199 次 application 500 |
| Phase 5 | 單 worker event loop 積壓 | 4 workers → +119% 線性提升 |
| Phase 6 | PostgreSQL write throughput | 加 container 反而更慢 |
| Phase 8 | WAL fsync（每 COMMIT 等磁碟） | sync_commit=off → 110x |
| Phase 8b | Podman VM 5 vCPU 不足 | 繞過 Nginx 直測，+22% |
| Phase 10 | Python workers CPU 上限 | 換 LB 無效，直連測試確認 |

---

## 2. 核心計算公式

### 2.1 Little's Law（並發量估算）

**公式：**
```
N = λ × W
```
- `N`：系統內同時佔用的並發槽數（例如 DB 連線數）
- `λ`：每個 worker 的請求到達率（req/s）
- `W`：每個請求在系統內的平均停留時間（s）

**用途：計算 connection pool 所需大小**

```
pool_size_needed = ceil(N × safety_margin)
safety_margin = 1.5（建議值）
```

**本專案驗算（create 路徑）：**

```
λ = create_throughput / total_workers = 630 / 12 = 52.5 req/s per worker
W = DB 平均耗時 = 5ms = 0.005s
N = 52.5 × 0.005 = 0.26 個並發 DB 連線 per worker

pool_size_needed = ceil(0.26 × 1.5) = 1
→ 實際設定 pool_size=10，遠超實際需求但安全 ✓
→ Phase 8 從 pool_size=50 降至 10，效能持平，記憶體下降 ✓
```

**實際所需 PgBouncer 後端連線（全局）：**

```
max_concurrent_DB_conns = N_per_worker × total_workers
                        = 0.26 × 12 = 3.1 個

→ PgBouncer pool=40 >> 3.1，有充足餘裕
→ Phase 8 前 pool=25 就已足夠，pool=40 是為了應對 spike
```

**redirect 路徑（cache-hit）：**

```
λ = 2,661 / 12 = 221.8 req/s per worker
W_redis = 0.5ms = 0.0005s（Redis RTT，async 不占 DB 連線）
N_db = 221.8 × 0 = 0（cache hit 不打 DB）
→ redirect 路徑完全不需要 DB 連線 ✓
```

---

### 2.2 Connection Pool 上限計算

**PgBouncer pool 上限（避免超過 PostgreSQL max_connections）：**

```
PgBouncer_POOL_SIZE ≤ (PG_max_connections - reserved) / num_pgbouncer_instances
```

**本專案驗算：**

```
PG_max_connections = 100（PostgreSQL 預設）
reserved = 10（admin / superuser / monitoring 保留）
num_pgbouncer_instances = 1

PgBouncer_POOL_SIZE ≤ (100 - 10) / 1 = 90
→ 實際設定 40，安全且有餘裕 ✓
```

**App pool_size 上限（避免超過 PgBouncer MAX_CLIENT_CONN）：**

```
total_app_pool ≤ PgBouncer_MAX_CLIENT_CONN
total_app_pool = pool_size × total_workers × num_containers
```

**本專案驗算：**

```
total_app_pool = 10 × 6 × 2 = 120
MAX_CLIENT_CONN = 2,000
→ 120 << 2,000，安全 ✓

Phase 6 的問題：pool_size=100 × 4 workers × 2 containers = 800
接近舊的 MAX_CLIENT_CONN=1,000 → Phase 7 調高至 2,000
```

---

### 2.3 Worker 數最佳化

#### 理論最大 worker 數（CPU 預算）

```
workers_max_per_container = floor(
    (total_vCPU - infra_CPU_budget) / num_containers
)
```

**基礎設施 CPU 預算估算：**

| 元件 | 預估 CPU 佔用（核） |
|------|-------------------|
| Nginx（worker_processes auto） | ~1.5（8 vCPU 環境） |
| PostgreSQL Primary | ~0.5 |
| PostgreSQL Replica | ~0.3 |
| PgBouncer | ~0.1 |
| Redis | ~0.2 |
| Prometheus + Grafana | ~0.4 |
| **小計** | **~3.0** |

**本專案驗算：**

```
available_for_python = 8 - 3.0 = 5.0 vCPU
workers_max_per_container = floor(5.0 / 2) = 2.5 → 但 async I/O 使效率提升

實際 6 workers/container 是甜蜜點（Phase 8b 實測：6 > 4 > 8）
→ Async workers 在等待 IO 時讓出 CPU，可適度超訂閱（over-subscribe）
→ 建議：workers_sweet_spot ≈ available_cores_per_container × 1.5~2
→ 本專案：2.5 × 2 = 5，實測 6 最佳 ✓
```

#### 達到目標 QPS 所需 worker 數

**redirect 路徑（CPU-bound）：**

```
workers_needed = ceil(target_QPS / per_worker_throughput / safety_discount)
safety_discount = 0.8（預留 20% 餘裕）
```

**本專案驗算（目標 5,000 QPS redirect）：**

```
per_worker_throughput = 2,304 / 6 = 384 req/s（直連測試）
workers_needed = ceil(5,000 / 384 / 0.8) = ceil(16.3) = 17

but: LB 層 CPU 開銷使實際效率下降
effective_QPS = per_worker_throughput × total_workers × (1 - LB_overhead)
LB_overhead = (理論 - 實測) / 理論 = (12×384 - 2,661) / (12×384)
             = (4,608 - 2,661) / 4,608 = 0.42（42%）

→ 在目前 8 vCPU + Nginx 架構下，無法靠加 worker 達到 5,000 QPS
→ 需要更多 vCPU 或第三個 app 容器
```

**若要達到 5,000 QPS（估算所需 vCPU）：**

```
target = 5,000 req/s
per_worker = 384 req/s（實測）
LB_overhead = 0.42（實測）
effective_per_worker = 384 × (1 - 0.42) = 223 req/s（透過 Nginx 後）

workers_needed = ceil(5,000 / 223) = 23 workers
containers_needed = ceil(23 / 6) = 4 containers

infra_CPU = 3.0
python_CPU = 4 containers × (6 workers / 2 workers_per_core) = 12 cores
→ total_vCPU_needed ≈ 3.0 + 12 = 15 vCPU（含 Nginx 的 CPU 需同步擴充）
```

---

### 2.4 synchronous_commit=off 效益估算

**公式：**

```
create_p50_with_sync    = DB_compute + queue_wait + WAL_fsync
create_p50_without_sync = DB_compute + queue_wait
WAL_fsync ≈ 0 ~ 200ms（依磁碟 I/O 速度而定，批次刷盤間隔）
```

**本專案實測：**

```
create_p50_before = 4,680ms
create_p50_after  = 42ms
WAL_fsync_overhead = 4,680 - 42 = 4,638ms

→ WAL fsync 佔了 4,680ms 中的 99.1%，是絕對主因
→ 改善幅度 = 4,638 / 4,680 = 99.1%（理論）
→ 實測吞吐量提升 = (631 - 343) / 343 = +84%
```

**預測新環境改善幅度：**

```
如果 p50_before > 500ms → WAL fsync 是主因，sync_commit=off 效益大
如果 p50_before < 100ms → 其他因素主導（index miss、N+1 等）
改善倍數估算 = p50_before / (p50_before × 0.01 + other_overhead)
```

---

### 2.5 Cache 效益計算

**有效 DB 負載（考慮 cache hit rate）：**

```
DB_load_effective = total_QPS × (1 - cache_hit_rate)
```

**本專案驗算（redirect 路徑）：**

```
total_redirect_QPS = 2,661
cache_hit_rate = 99%+（所有 active token 均在 Redis 中）
DB_load_effective = 2,661 × 0.01 = 26.6 req/s

→ 若無 Redis cache：2,661 req/s 全部打 DB，以 5ms/query 計算
  concurrent_DB_conns_needed = 2,661 × 0.005 = 13.3 個
  PgBouncer pool=40 可以支撐，但會擠壓 create 路徑的連線餘裕
```

**Negative Cache 效益（probe/404 路徑）：**

```
probe_QPS = total_QPS × probe_traffic_ratio
DB_queries_saved_per_sec = probe_QPS × negative_cache_hit_rate
latency_saved = DB_queries_saved × W_db
```

**本專案驗算：**

```
probe_QPS = 1,716 × 10% = 171.6 req/s（Phase 7 基準）
W_db（SELECT for 404）= ~15ms
DB_time_consumed = 171.6 × 0.015 = 2.57 核秒/秒（相當於 DB 佔用 2.57 個連線）

negative cache 後：
DB_load_probe ≈ probe_QPS × (1/60) = 171.6 / 60 = 2.86 req/s（TTL=60s）
DB_time_saved = (171.6 - 2.86) × 0.015 = 2.53 核秒/秒（節省 98.3%）

→ 釋放約 2.5 個 PgBouncer 連線給 create 路徑
→ throughput 提升 16.7%（Phase 7 實測）✓
```

---

### 2.6 LB 層開銷計算

**LB overhead factor（實測法）：**

```
LB_overhead = (theoretical_total - actual_total) / theoretical_total

theoretical_total = per_worker_ceiling × total_workers（直連測試取得）
actual_total = 透過 LB 的實測吞吐量
```

**本專案驗算：**

```
per_worker_ceiling（直連 app1）= 2,304 / 6 = 384 req/s
theoretical_total = 384 × 12 = 4,608 req/s
actual_total（透過 Nginx）= 2,661 req/s
LB_overhead = (4,608 - 2,661) / 4,608 = 42.2%

原因分析：
1. Nginx 8 workers 消耗 ~1.5 vCPU（來自同一個 VM CPU 池）
2. 20+ 進程競爭 8 vCPU，OS context switch overhead
3. TCP hop 額外延遲（keepalive 降低但無法消除）
```

**換 LB 是否有效的判斷：**

```
if LB_overhead < 10%:
    換 LB 效益低（Python workers 是主因）
if LB_overhead > 30%:
    換 LB 或增加 LB 專屬 vCPU 可能有效

→ 本專案 42.2% overhead 中：
  - 約 20% 是 Nginx CPU 佔用（可透過 LB 專屬 vCPU 解決）
  - 約 22% 是 context switch（增加 vCPU 解決）
  - 換更輕量的 LB 僅影響 Nginx CPU 佔用部分，效益有限
```

---

### 2.7 Rate Limit 參數計算

**Fixed-window Redis 計數器（本專案實作）：**

```
max_requests_per_window = base_rate × window_seconds + burst_allowance

Key: ratelimit:{path}:{ip}:{unix_timestamp // window_seconds}
TTL: window_seconds + 1（避免 key 永久殘留）
```

**等效 Nginx limit_req 參數：**

```
Nginx: rate=R r/s, burst=B, nodelay
Redis: max_requests = R × 1 + B（1 秒 window）

本專案：rate=20, burst=40 → max_requests = 20 + 40 = 60 ✓
```

**Redis rate limit 對 create 路徑的額外延遲：**

```
rate_limit_overhead = Redis_RTT = ~0.5ms
create_p50_with_rate_limit = 42 + 0.5 = 42.5ms（< 1.2% 增加）
→ 可忽略 ✓
```

---

### 2.8 Redis AOF 效能影響估算

```
appendfsync=always:    每次寫入 fsync，吞吐量下降 30~50%
appendfsync=everysec:  每秒 fsync 一次，幾乎無影響（< 1%）
appendfsync=no:        OS 自行決定，最快但可靠性最低
```

**本專案驗算：**

```
redirect（純讀 Redis）：AOF 不影響讀取 → throughput 持平 ✓
create（寫入 Redis Stream）：XADD 每秒約 630 次
fsync overhead per XADD = 0（everysec 批次處理）
→ 實測 create throughput 無退步 ✓
```

---

## 3. 各層優化判斷與效益估算

### 3.1 優化選擇矩陣

| 優化項目 | 判斷條件 | 預期效益 | 代價/風險 |
|---------|---------|---------|---------|
| `synchronous_commit=off` | create p50 > 500ms | 3~100x 延遲改善 | crash 時丟失最多 200ms 提交 |
| Redis cache（redirect） | redirect p50 > 5ms | redirect p50 → 次毫秒 | 需處理 cache invalidation |
| Negative cache | 404/410 流量 > 5% | 降低同等比例 DB 負載 | TTL 期間內無法立即感知刪除 |
| Redis Stream（scan） | scan 寫入與主業務搶 pool | 解耦，幾乎零 overhead | 引入 consumer group 複雜度 |
| PgBouncer | QueuePool 500 error | 消除連線排隊 | 需設定 AUTH_TYPE，DISCARD ALL |
| 增加 workers | CPU 使用率低但 QPS 不足 | 接近線性（async） | 超過 vCPU 後 context switch 惡化 |
| 增加 vCPU | 所有 workers CPU 使用率高 | 線性 | 硬體成本 |
| PG Read Replica | analytics 查詢打 Primary | 讀負載移至 Replica | UNLOGGED table 不相容 |
| `shared_buffers` 增加 | 熱資料集 > 128MB | 小幅提升（熱資料命中） | 需重啟 PG |
| UNLOGGED TABLE | 純寫入、可重建的表 | INSERT ~3x | 與 Replication 不相容 |

### 3.2 各優化的前置條件檢查

**執行 `synchronous_commit=off` 前確認：**

```
□ 業務可接受最多 200ms 的提交丟失
□ 資料不涉及金融/合規場景
□ 未來若加 Read Replica，此設定不影響 WAL 傳輸（wal_level 另行設定）
```

**執行 Read Replica 前確認：**

```
□ 無 UNLOGGED TABLE（或決定放棄 UNLOGGED 換取 Replication）
□ 應用可接受 replay_lag（~1~5ms）的讀取延遲
□ 讀取路徑已確認（哪些 route 走 Replica）
□ 寫入後立即讀取的路徑（如 create 後 redirect）需走 Primary
```

**增加 workers 前確認：**

```
□ total_workers ≤ available_vCPU × 2（async over-subscribe 上限）
□ Nginx keepalive 值 ≥ total_upstream_workers（否則每 worker 的 keepalive 槽減少）
□ PgBouncer MAX_CLIENT_CONN ≥ pool_size × total_workers
□ 先測 per-worker throughput（單 container 隔離測試），確認是 worker 數不足而非其他瓶頸
```

---

## 4. 常見陷阱與反模式

### 4.1 優化之間的相依性衝突

| 組合 | 衝突原因 | 解法 |
|------|---------|------|
| UNLOGGED TABLE + Streaming Replication | UNLOGGED 不寫 WAL，Replica 無法複製 | 選一：放棄 UNLOGGED 或不用 Replica |
| 加 worker + 固定 keepalive | keepalive pool 固定，每 worker 分到更少持久連線 | 同步調高 keepalive 值 |
| global middleware + 高 QPS | middleware 對所有路由加 overhead，含與邏輯無關的熱路徑 | 改用 route dependency 或 route-specific middleware |
| app pool_size 大 + PgBouncer transaction mode | transaction mode 下每個 request 釋放連線，pool_size 設大無意義 | 用 Little's Law 計算實際需求 |

### 4.2 水平擴展的邊界

```
水平擴展（加 container）對以下情況有效：
  ✓ CPU-bound workload（redirect 路徑）
  ✓ 已有共享 cache/state（Redis）

水平擴展對以下情況無效：
  ✗ DB write throughput 瓶頸（加 container 加劇競爭）
  ✗ 單點共享資源已 saturated（PgBouncer 連線、Redis 單節點）
  ✗ LB 層已是 CPU 瓶頸（加 app container 但 LB 無法分流）

本專案 Phase 6 教訓：
  2 containers → throughput 降低（DB write throughput 瓶頸，非 CPU）
```

### 4.3 Starlette/FastAPI Middleware 效能陷阱

```
@app.middleware("http")          ← 對所有請求加 ~20-25% overhead
  → 原因：BaseHTTPMiddleware 將 response 包在 iterator，
           每個 response 多一層 async dispatch

正確做法：
  → 把 guard 邏輯放在 Depends()，只掛在需要的 route
  → 或使用純 ASGI middleware（不用 BaseHTTPMiddleware）

本專案 Phase 10 實測：
  global middleware：1,999 req/s（-25% vs Nginx 基準 2,661）
  route dependency：2,520 req/s（恢復正常）
```

### 4.4 測試環境污染

```
問題：k6 setup 階段與被測試的 rate limit 共用同一 IP
→ setup 發 500 個 create，被自己的 rate limit 攔截，只 seed 60 個 token

根本原因：rate limit 不區分「測試 setup」與「真實流量」

預防方式：
  □ 測試用 token 預先建立（不在 setup 時打受限 endpoint）
  □ 或 setup 使用不同 IP（X-Forwarded-For mock）
  □ 或 rate limit 加 allowlist（bypass for known test IPs）
```

---

## 5. 快速診斷卡

### 5.1 症狀 → 診斷 → 解法

```
症狀：create p50 > 1,000ms
  診斷：SELECT pg_stat_bgwriter; -- 看 checkpoint_write_time
        SHOW synchronous_commit;
  解法：SET synchronous_commit = off;

症狀：create 成功率 < 95%，錯誤為 QueuePool timeout
  診斷：SHOW max_connections; -- PG
        SELECT count(*) FROM pg_stat_activity; -- 當前連線數
  解法：加 PgBouncer；用 Little's Law 重算 pool_size

症狀：redirect p50 > 10ms（已有 Redis）
  診斷：redis-cli INFO stats | grep keyspace
        redis-cli TTL r:{token} -- 確認 TTL 正確
  解法：確認 expires_at TTL 正確傳入 set_cached_url

症狀：404/410 回應慢（p50 > 100ms）
  診斷：確認 is_cached_gone() 邏輯是否存在
  解法：加 negative cache（gone:{token}, TTL=60s）

症狀：加 worker 後 throughput 反而下降
  診斷：確認 Nginx keepalive 值 ≥ total upstream workers
        確認 vCPU 是否已滿（htop / podman stats）
  解法：同步調高 keepalive；或增加 vCPU

症狀：加 container 後 throughput 反而下降
  診斷：確認瓶頸是否在 DB write（create p50 惡化？）
  解法：不加 container；解決 DB write 瓶頸（sync_commit、batching）
```

### 5.2 優化執行順序（ROI 由高到低）

```
1. synchronous_commit=off          → create latency 110x（若 WAL 是主因）
2. Redis cache（redirect）         → redirect p50 次毫秒突破
3. Negative cache（404/410）       → probe 路徑 21x
4. 解耦寫入路徑（Redis Stream）    → 解除連線池競爭
5. 增加 workers（到 vCPU 上限）   → 近線性（+119%）
6. PgBouncer 調優（pool size）     → 消除 QueuePool 500
7. Nginx keepalive 調優            → 小幅（+2~5%）
8. shared_buffers 增加             → 資料集小時幾乎無效
9. Read Replica                    → 讀負載分流（analytics）
10. 換 LB                         → 幾乎無效（Python workers 是瓶頸）
```

### 5.3 試算表（帶入數值驗算）

**A. 需要多少 workers 才能達到目標 QPS？**

```
INPUT:
  target_QPS            = _____ req/s
  per_worker_throughput = _____ req/s（直連單 container / worker 數）
  LB_overhead           = _____ %（直連 vs LB 的差距比例）
  safety_margin         = 0.8

FORMULA:
  effective_per_worker = per_worker_throughput × (1 - LB_overhead)
  workers_needed = ceil(target_QPS / effective_per_worker / safety_margin)

本專案帶入（target = 5,000）：
  per_worker_throughput = 384 req/s
  LB_overhead = 0.42
  effective_per_worker = 384 × 0.58 = 222.7
  workers_needed = ceil(5,000 / 222.7 / 0.8) = ceil(28.1) = 29 workers
  containers_needed = ceil(29 / 6) = 5 containers
```

**B. pool_size 應該設多少？**

```
INPUT:
  create_throughput  = _____ req/s
  total_workers      = _____
  db_query_time_ms   = _____ ms
  safety_margin      = 1.5

FORMULA:
  λ_per_worker = create_throughput / total_workers
  N = λ_per_worker × (db_query_time_ms / 1000)
  pool_size_needed = ceil(N × safety_margin)

本專案帶入：
  λ_per_worker = 630 / 12 = 52.5
  N = 52.5 × 0.005 = 0.26
  pool_size_needed = ceil(0.26 × 1.5) = 1
  → pool_size=10 足夠，pool_size=50 是浪費 ✓
```

**C. Rate limit 參數換算：**

```
INPUT:
  base_rate_per_sec = _____ req/s（穩態允許量）
  burst_allowance   = _____ 個（瞬間額外允許量）

FORMULA（Redis fixed-window）：
  max_requests_per_window = base_rate_per_sec + burst_allowance
  key = "ratelimit:{ip}:{unix_ts // 1}"
  TTL = 2（window + 1）

本專案帶入（base=20, burst=40）：
  max_requests_per_window = 20 + 40 = 60 ✓
```

**D. 加 Read Replica 後的負載分配：**

```
INPUT:
  total_QPS             = _____ req/s
  read_ratio            = _____ %（可走 Replica 的讀取請求比例）
  analytics_ratio       = _____ %

FORMULA:
  primary_load  = total_QPS × (1 - read_ratio - analytics_ratio)
  replica_load  = total_QPS × (read_ratio + analytics_ratio)

本專案帶入（redirect=70%, create=20%, analytics=10%）：
  只有 analytics 走 Replica（redirect 走 Redis，不打 DB）
  primary_load  ≈ total_QPS × 20%（create） + cache_miss（< 1%）
  replica_load  ≈ total_QPS × 10%（analytics）
  → Primary 幾乎只承受 create 負載 ✓
```

---

## 6. CDN 層分析

### 6.1 CDN 效益公式

**有效 throughput（考慮 cache hit rate）：**

```
有效吞吐量 = (hit_rate × CDN_throughput) + ((1 - hit_rate) × origin_throughput)

本專案帶入（Phase 11c）：
  hit_rate        ≈ 0.999（500 tokens 快速全暖）
  CDN_throughput  = 5,100 req/s（Varnish HIT）
  origin_throughput = 2,600 req/s（app path）
  有效吞吐量 = (0.999 × 5100) + (0.001 × 2600) = 5094.9 + 2.6 ≈ 5,097 req/s ✓
```

**Cache 熱身時間估算：**

```
warm_up_requests = unique_token_count
warm_up_time_sec = unique_token_count / origin_throughput

本專案帶入：500 tokens / 2,600 req/s ≈ 0.19 秒
→ 測試開始後 < 0.2 秒快取全暖，後續全為 HIT ✓
```

**CDN 所需記憶體估算：**

```
memory_per_entry = headers(~200B) + body(≈0B for 302) + metadata(~56B) ≈ 256B
cache_memory = active_tokens × memory_per_entry

本專案帶入：500 × 256B = 128KB（遠小於 256MB 配置）
生產估算（1M active tokens）：1,000,000 × 256B = 256MB ← 正好是我們的配置上限
```

### 6.2 何時 CDN 效益最大

| 條件 | CDN 效益 |
|------|---------|
| 相同 URL 被大量重複請求 | 最大（hit_rate → 1）|
| URL 穩定不變（無 expires_at 或很長 TTL）| 最大 |
| 回應體很小（如 302 只有 Location header）| 最大（快取成本最低） |
| 每個請求 URL 都唯一 | 最小（hit_rate → 0）|
| URL 頻繁更新（1 秒內就失效）| 反效果（MISS 開銷 > 省去的 origin 查詢）|

**結論：QR code redirect 是 CDN 的理想場景** — URL 穩定、回應小、access pattern 高度重複（熱門 QR code 被掃描成千上萬次）。

### 6.3 CDN 的必要配套：PURGE 機制

在本系統中，`UrlMapping` model 有 `expires_at` 欄位。若 TTL=60s 期間 QR code 到期，
Varnish 仍會返回舊快取的 302（正確性問題）。

**生產解法選項：**

| 方案 | 實作 | 取捨 |
|------|------|------|
| 主動 PURGE | 在 `expires_at` 更新時發 `PURGE /r/<token>` | 即時準確，需要 PURGE endpoint 配置 |
| 短 TTL | TTL = min(expires_at - now, 60s) | 不需 PURGE，但 MISS 率更高 |
| `Cache-Control: max-age` | App 在 302 response 加 header | CDN 自動尊重，最標準做法 |

**最佳實踐（CloudFront / CDN 通用）：**
```python
# redirect handler 加入動態 Cache-Control
remaining = expires_at - datetime.now() if expires_at else None
ttl = min(int(remaining.total_seconds()), 3600) if remaining else 3600
return RedirectResponse(
    url=url,
    status_code=302,
    headers={"Cache-Control": f"public, max-age={ttl}"}
)
```

### 6.4 CDN vs. 多主機 的選擇

| 擴展方式 | 成本 | 適用 | 限制 |
|---------|------|------|------|
| CDN（Varnish / CloudFront）| 記憶體 ≈ 256MB | read-heavy，URL 穩定 | 需 PURGE；create 不受益 |
| 多主機（加 site）| 1 台 VM（CPU + memory）| write-heavy，或 URL 多樣 | 需 DB/Redis 擴展 |
| 兩者組合 | 最高 | 任何流量類型 | 維運複雜度最高 |

---

## 附錄：本專案實測數據彙整

| Phase | 核心改動 | redirect req/s | create p50 | create req/s |
|-------|---------|---------------|------------|-------------|
| Baseline | 同步單 worker | ~210（混合） | ~5,797ms | ~150 |
| Phase 1 | 4 workers + fire-and-forget | ~360 | — | ~240 |
| Phase 2 | Async + Redis cache | ~168 | 5ms | ~167 |
| Phase 3 | Redis Stream consumer | ~268 | — | ~268 |
| Phase 4b | PgBouncer + 正確 pool | ~263 | — | ~263 |
| Phase 5 | 4 workers + Consumer Group | ~575 | 1,166ms | ~575 |
| Phase 6 | Nginx + 2 containers | ~412 | 3,713ms | ~412 |
| Phase 7 | Negative cache + 調優 | ~480 | 4,680ms | ~343 |
| Phase 8 | sync_commit=off, pool 調優 | 1,731（redirect-only） | **42ms** | **631** |
| Phase 9 | 8 vCPU, keepalive, AOF, Replica | **2,661** | 42ms | 630 |
| Phase 10 | Rate limit → dependency（LB 不變） | 2,661 | 42ms | 630 |
| Phase 11a | 12 vCPU + 4 containers × 4 workers | ~2,550（peak）| 42ms | 630 |
| Phase 11b | 三層 LB（單 VM 模擬，資源共享）| 931（模擬限制）| — | — |
| Phase 11c | Varnish CDN（HIT path）| **~5,100（peak）**，p50=0.202ms | — | — |

> Phase 1~7 的 redirect 數字為混合流量下的 redirect 部分估算值  
> Phase 11b 的數字是單 VM 模擬限制；真實多主機理論值 ~5,200 req/s  
> Phase 11c 的 peak 是在 hold@6000 stage 60 秒窗口的實測值

---

*最後更新：2026-05-12（含 Phase 11a/11b/11c CDN 章節）*
