# QR Code Generator 效能優化完整記錄

**專案期間：** 2026-05-10 — 2026-05-12  
**優化目標：** 從 752 req/s 達到 5,000 QPS redirect，同時維持 redirect p50 < 10ms、create 成功率 > 95%

---

## 一、專案概覽

### 背景與目標

本專案以一個標準 QR Code 短網址服務為場景，模擬從初始同步阻塞架構出發，逐步透過非同步化、快取分層、DB 調優、CDN 等手段，將系統推進至 5,000 QPS 的效能目標。

服務有兩條核心路徑：
- **redirect（讀取）**：`GET /r/{token}` → Redis 快取查詢 → 302
- **create（寫入）**：`POST /api/qr/create` → PostgreSQL INSERT → 回傳 token

### 最終成果摘要表

| 指標 | Baseline | Phase 11c 終態 | 累積改善 |
|------|----------|--------------|---------|
| redirect 峰值吞吐量 | ~752 req/s | **~5,100 req/s**（Varnish HIT）| **+578%** |
| redirect p50 | 3,847ms | **0.202ms** | **> -99.99%** |
| create p50 | ~5,797ms | **42ms** | **-99.3%** |
| create 吞吐量 | ~343/s | **630/s** | **+84%** |
| create 成功率 | 100%* | 99.99% | 持平 |

*Baseline 在較低負載（752 req/s）下測試，非等效壓力。

### 架構演進示意

```
Baseline：
  Client → uvicorn(1 worker, 同步) → PostgreSQL
                    ↓ 每次 redirect 都等待 DB INSERT

Phase 5：
  Client → uvicorn(4 workers, 非同步) → Redis(快取)
                                      → Redis Stream → consumer → PostgreSQL

Phase 9：
  Client → Nginx(LB) → app1/app2(各6 workers) → Redis(AOF)
                          ↓ 寫入         ↓ 讀取
                      PgBouncer     PostgreSQL Replica
                          ↓
                   PostgreSQL Primary

Phase 11c（終態）：
  Client → Varnish CDN(記憶體快取，HIT=0.2ms) → Nginx → app1~4 → Redis → PostgreSQL
                    ↑
         99%+ HIT 直接從記憶體回傳 302
```

---

## 二、起點：Baseline 架構分析

### 初始架構

- 單一 uvicorn worker（單 OS process、單 event loop）
- 同步 SQLAlchemy（psycopg2）：每個請求佔用一條 thread 等待 DB 回應
- In-process dict 作為 redirect cache（只在單 process 有效）
- 每次 redirect 在回傳 302 前必須完成 `INSERT INTO scan_events`（同步阻塞）

### 為什麼慢

Baseline 的核心問題是「同步阻塞 + 單 worker」的雙重限制：

1. **Thread pool 是硬上限**：同步路由每次 DB 操作都佔住一條 thread，預設 thread pool（通常 40 條）耗盡後請求直接排隊。
2. **Redirect 等掃描寫入**：即使已從 in-process cache 拿到 URL，redirect 仍要等 `INSERT INTO scan_events` 完成才能回傳 302，這是不必要的阻塞。
3. **In-process cache 無法共用**：即便之後擴展為多 worker，各 process 的 cache 無法同步。

### Baseline 壓測數字

| 指標 | 數值 |
|------|------|
| 測試 QPS 目標 | 5,000 req/s |
| 實際 avg throughput | 752 req/s |
| Dropped iterations | 76.6%（大量請求根本送不進去） |
| redirect p50 | 3,847ms |
| create p50 | 5,797ms |

---

## 三、Phase 1–9 優化歷程

### Phase 1 — 多 Worker + Fire-and-Forget Scan

**問題**：單 uvicorn worker 的 thread pool 耗盡；redirect 必須同步等待掃描 INSERT 完成。

**解法**：
- `--workers 4`：啟動 4 個獨立 OS process，thread pool 容量 ×4
- `BackgroundTasks.add_task(_record_scan)`：redirect 先回傳 302，scan INSERT 推後執行（fire-and-forget）

**結果**：

| 指標 | Baseline | Phase 1 | 改善 |
|------|----------|---------|------|
| avg throughput | 752 req/s | 1,284 req/s | +71% |
| redirect p50 | 3,847ms | 1,423ms | -63% |
| create p50 | 5,797ms | 2,221ms | -62% |
| Dropped iterations | 76.6% | 51.8% | -24.8pp |

**教訓**：多 worker 與 fire-and-forget 是正確方向，但 in-process cache 在 4 個 worker 間無法共用，存在一致性風險；真正的瓶頸是同步 DB I/O，fire-and-forget 只是讓 redirect 不用等 scan，create 仍是同步的。

---

### Phase 2 — Async SQLAlchemy + asyncpg + Redis 分散式快取

**問題**：同步 DB I/O 佔用 thread；in-process cache 無法跨 worker 共用。

**解法**：
- `create_async_engine` + `AsyncSession`：所有 DB 操作完全非同步，DB 等待期間 event loop 繼續處理其他請求
- Redis 取代 in-process dict：redirect cache 跨 process 共用，命中則完全不碰 DB
- 回退至單 worker（asyncpg pool 在單 process 內即可充分共用）

**結果**：

| 指標 | Phase 1 | Phase 2 | 說明 |
|------|---------|---------|------|
| avg throughput | 1,284 req/s | 598 req/s | 整體退步（見教訓）|
| redirect p50 | 1,423ms | **0.063ms** | -99.996%，突破次毫秒 |
| create p50（成功） | 2,221ms | **5.13ms** | -99.8% |
| create 成功率 | 100% | 69% | 退步 |

**教訓**：整體數字退步因為：(1) 回退至單 worker；(2) asyncpg pool 被 scan 寫入消耗，create 連線不足。但分開看：redirect 熱路徑已完全解決（0.063ms），create pool 正常時也只要 5ms。整體退步是資源問題，不是 async 的問題。「拆解數字而非看整體」是這個 phase 最重要的診斷習慣。

---

### Phase 3 — Redis Stream 掃描佇列 + 批次 DB 寫入

**問題**：`_record_scan()` 與 create 路由共用同一個 asyncpg pool，高流量下 scan 寫入消耗連線，create 成功率跌至 69%。

**解法**：
- `XADD scan_events`（maxlen=100,000）：redirect 熱路徑的 scan 記錄改為推入 Redis Stream，耗時約 0.1ms，完全不碰 asyncpg pool
- 新增 `consumer.py`：`xread(count=200, block=500ms)` 批次消費 Redis Stream，批次 INSERT 至 PostgreSQL

redirect 的完整路徑現在是：Redis GET（cache hit）→ XADD（0.1ms）→ 302，無任何 DB 操作。

**結果**：

| 指標 | Phase 2 | Phase 3 | 改善 |
|------|---------|---------|------|
| avg throughput | 598 req/s | 957 req/s | +60% |
| create 成功率 | 69% | 97.89% | +28.89pp |
| redirect 成功率 | 100% | 100% | 持平 |
| redirect p50 | 0.063ms | sub-ms | 持平 |

**教訓**：scan 寫入與 create 共用 pool 是零和競爭，解法是完全隔離（不是調大 pool）。Redis Stream 把非同步寫入的延遲從「佔用 DB 連線」變成「佔用 Redis 記憶體」，成本差了 1–2 個數量級。

---

### Phase 4 — PgBouncer 連線池代理（含 4a 與 4b）

#### Phase 4a — Optimistic INSERT

**問題**：create 路由在 INSERT 前先執行 `SELECT EXISTS` 確認 token 不存在，正常路徑有 2 次 DB 操作。

**解法**：移除 SELECT，直接 INSERT；捕捉 `IntegrityError`（token 碰撞）後自動重試，最多 10 次。正常路徑（無碰撞）減為 1 次 DB 操作。

**結果**：avg throughput 735 req/s（略退步），但架構方向正確。退步原因是單 worker event loop 瓶頸掩蓋了改動效益，Optimistic INSERT 的效益需在 pool 瓶頸解除後才顯現。

#### Phase 4b — PgBouncer + Pool Sizing 修正

**問題**：直連 PostgreSQL，大量並發請求讓真實 PG 連線耗盡；app 端 pool_size 初設過小（5+5=10）。

**解法**：
- 引入 PgBouncer（transaction mode，`DEFAULT_POOL_SIZE=25`，`MAX_CLIENT_CONN=1000`）
- 修正 app 端 pool_size 至 50+50=100

關鍵設定：
```
PgBouncer：
  POOL_MODE=transaction
  DEFAULT_POOL_SIZE=25   ← 真實 PG 連線數（要小）
  MAX_CLIENT_CONN=1000

App（SQLAlchemy）：
  pool_size=50 + max_overflow=50   ← app→PgBouncer 連線（要大）
  statement_cache_size=0           ← transaction pooling 不支援 server-side prepared statements
```

**結果**：

| 指標 | Phase 4a | Phase 4b-fix | 改善 |
|------|---------|-------------|------|
| avg throughput | 735 req/s | 938 req/s | +28% |
| QueuePool 500 error | 0 | **0**（初版有 3,199 次，修正後歸零）| 完全消除 |
| create 成功率 | 94.56% | 98.65% | +4.09pp |

**教訓**：PgBouncer 的設計原則是「app 端 pool 要大，PgBouncer 端 pool 要小」。app 端 pool 太小，請求在抵達 PgBouncer 之前就已排隊，PgBouncer 發揮不了 multiplex 的效益。同時，PgBouncer transaction pooling 模式與 asyncpg 的 server-side prepared statements 不相容，必須設定 `statement_cache_size=0`。

---

### Phase 5 — 4 uvicorn Workers + Redis Stream Consumer Groups

**問題**：單 worker event loop 在 ~1,000 req/s 上限；多 worker 後 `xread` 會重複消費 scan 事件（4 個 consumer 各讀到相同訊息 → 寫入 4 倍資料）。

**解法**：
- `--workers 4`：突破單 event loop 上限
- Consumer Groups（`xreadgroup` / `xack`）：每條訊息只分配給 group 內的一個 consumer，`xack` 後才從 PEL（Pending Entry List）移除

```python
CONSUMER_GROUP = "scan_group"
consumer_name = f"worker-{os.getpid()}"   # 每個 worker 唯一身份

await redis.xgroup_create("scan_events", CONSUMER_GROUP, id="0", mkstream=True)
messages = await redis.xreadgroup(
    CONSUMER_GROUP, consumer_name,
    {"scan_events": ">"}, count=200, block=500
)
await redis.xack("scan_events", CONSUMER_GROUP, *msg_ids)
```

Smoke test 驗證：3 次 redirect → `total_scans=3`（非 12），exactly-once 確認。

**結果**：

| 指標 | Phase 4b-fix | Phase 5 | 改善 |
|------|-------------|---------|------|
| avg throughput | 938 req/s | **2,056 req/s** | **+119%** |
| create 成功率 | 98.65% | **100%** | 完整恢復 |
| redirect 成功率 | 100% | 100% | 持平 |
| App error rate | ~3,199 次 | **0** | 完全消除 |
| Dropped iterations | 66.2% | **23.0%** | 首次突破 25% 大關 |

**教訓**：`xread` 在多 worker 下必然重複消費；Consumer Group 的 PEL 機制保證 exactly-once，並提供 at-least-once 容錯（worker 崩潰後訊息可被認領重試）。吞吐量從 938 提升至 2,056（+119%），是整個優化系列中單次改動幅度最大的一次。

---

### Phase 6 — Nginx Load Balancer + 2 App Containers

**問題**：單容器的 CPU 與連線資源仍有上限，嘗試水平擴展。

**解法**：引入 Nginx（`worker_processes auto`，`worker_connections 8192`，`keepalive 32`）；2 個 app 容器（app1 + app2），合計 8 workers。

關鍵踩坑：初始 `worker_connections 1024` 在 3,000 VU 下迅速耗盡（每個 active request 消耗 2 個 connections：client + upstream）；`events{}` block 不可省略。

**結果**：

| 指標 | Phase 5 | Phase 6 | 變化 |
|------|---------|---------|------|
| avg throughput | 2,056 req/s | 1,471 req/s | **-28%（反效果）** |
| create p50 | 1,166ms | 3,713ms | +219% |
| redirect p50 | 17.1ms | 23ms | +34% |

**教訓**：水平擴展對 I/O-bound workload（DB write）無效——加 worker 只是讓更多 worker 競爭同一個 DB 連線池，加劇鎖競爭；Nginx proxy 增加了一次 TCP hop。水平擴展只對 CPU-bound workload 有效，或只對已被 cache 隔離的讀取路徑有效。此 phase 揭示了根本瓶頸是 PostgreSQL write throughput，不是 worker 數。

---

### Phase 7 — Negative Cache + expires_at Bug 修正 + 調優

**問題**：(1) 每次請求不存在的 token 都打一次 DB（probe 路徑佔 10% 流量，p50 ~1,500ms）；(2) `set_cached_url` 未依 `expires_at` 設定 Redis TTL，過期 URL 可被無限期快取。

**解法**：

Negative cache 設計：
```python
# cache.py
async def is_cached_gone(token: str) -> bool:
    return await redis.exists(f"gone:{token}")

async def set_cached_gone(token: str, ttl: int = 60):
    await redis.set(f"gone:{token}", 1, ex=ttl)

# routes.py - redirect()
if await cache.is_cached_gone(token):
    return JSONResponse(status_code=404)  # 跳過 DB
# ... DB 查詢後
if not found:
    await cache.set_cached_gone(token)
```

expires_at TTL 修正：`set_cached_url` 新增 `ttl` 參數，redirect 寫快取時傳入 `expires_at - now`，確保 Redis key 與 URL 同步過期。

**結果**：

| 指標 | Phase 6 | Phase 7 | 改善 |
|------|---------|---------|------|
| avg throughput | 1,471 req/s | 1,716 req/s | +16.7% |
| probe(not_found) p50 | ~1,500ms | **67ms** | **-95.5%** |
| Dropped iterations | 45.8% | 36.74% | -9pp |
| create 成功率 | 99.99% | 99.999% | 持平 |

Smoke test：同一個不存在 token 連續打 3 次 → 第 1 次 79.5ms（DB lookup），第 2 次 3.7ms，第 3 次 3.4ms。加速比 21x。

**教訓**：消除不必要的 DB 查詢，永遠比優化 DB 查詢本身更有效率。Probe 路徑佔 10% 流量，每次都打 DB 白白消耗連線；negative cache 上線後這部分負載幾乎消失，相當於把 10% 的 DB 連線還給 create 路徑，throughput 提升 16.7% 就是直接的體現。

---

### Phase 8 — DB 調優（synchronous_commit=off + Pool 瘦身 + Worker 分析）

**問題**：create p50=4,680ms 極高；pool_size=50+50 是否最優；redirect ceiling 在哪裡。

**解法**：

| 設定 | 變更 | 理由 |
|------|------|------|
| `synchronous_commit=off` | PostgreSQL 預設 → off | 移除 WAL fsync 等待（每次 COMMIT 不再等磁碟確認） |
| `checkpoint_completion_target=0.9` | 預設 → 0.9 | 平滑 checkpoint I/O，減少 WAL 寫入峰值 |
| `wal_buffers=16MB` | 預設 → 16MB | 增加 WAL 緩衝，減少頻繁刷盤 |
| PgBouncer `DEFAULT_POOL_SIZE` | 25 → 40 | 支援更多並發 DB 連線 |
| app `pool_size` | 50 → 10，`max_overflow` 50 → 10 | Little's Law 計算實際需求 < 1 |

Little's Law 計算 pool 需求：
```
λ = 630/s ÷ 8 workers = 79 req/s per worker（保守估算）
W = 5ms（DB 平均耗時，sync_commit=off 後）
N = 79 × 0.005 = 0.39 → pool_size=10 足夠
```

新增 redirect-only k6 腳本（排除 create 干擾，確認真實 redirect ceiling）。

**分段測試結果**：

Stage 8a（4 workers/container，via Nginx）：redirect throughput = 1,731 req/s（Nginx 上限）

Stage 8b（8 workers/container）：1,617 req/s（退步 6.6%）。原因：Nginx keepalive=32 固定池在 16 upstream workers 下每 worker 分到更少持久連線；Podman VM 5 vCPU 支撐 18+ 重型進程過載。

Stage 8c（sync_commit=off + PgBouncer pool=40）：

| 指標 | Phase 7 基線 | Stage 8c | 改善 |
|------|------------|---------|------|
| create p50 | 4,680ms | **42ms** | **110x** |
| create p95 | 8,924ms | **55ms** | **162x** |
| create throughput | ~343/s | **631/s** | +84% |
| redirect throughput | 1,731 req/s | 1,731 req/s | Nginx bound |

**教訓**：`synchronous_commit=off` 的改善幅度（110x）遠超預期（預估 3-10x），因為 WAL fsync 佔了 create 延遲的 99.1%（4,638ms / 4,680ms），是絕對主因。適用場景：可接受最多 200ms 提交丟失（非金融交易）；不寫 WAL 的 `fsync=off` 才會損壞資料，`synchronous_commit=off` 只是延遲刷盤，資料不會損壞。

---

### Phase 8b — Nginx / CPU 瓶頸驗證

**問題**：redirect ceiling 停在 1,731 req/s，懷疑是 Nginx 或 Podman VM CPU 瓶頸。

**驗證方法**：暫時讓 app1 暴露 8001 port，繞過 Nginx 直接壓測，對比有無 Nginx 的差距。

**實驗結果**：

| 測試條件 | vCPU | 途徑 | Throughput | p50 |
|---------|------|------|-----------|-----|
| 5 vCPU，4w/container | 5 | via Nginx | 1,731 req/s | 687ms |
| 5 vCPU，4w/container | 5 | 直打 app1 | **2,116 req/s** | 1.26s |
| 8 vCPU，4w/container | 8 | via Nginx | 2,530 req/s | 47ms |
| 8 vCPU，**6w**/container | 8 | via Nginx | **2,605 req/s** | **36ms** |

**結論**：
- Nginx 有真實開銷（~18%），但不是主要瓶頸
- 根本原因是 Podman VM 5 vCPU：8 Nginx + 8 Python + PG + Redis + 其他 = 18+ 重型 process 搶 5 核，context switch 嚴重
- 修復：`podman machine set --cpus 8`，redirect 從 1,731 提升至 2,605（+50%）
- 8 vCPU 下最優為 6 workers/container（甜蜜點：p50=36ms 最低）

**教訓**：CPU over-subscription 的代價是非線性的。5 vCPU 跑 18+ 重型 process（3.6x 超訂）→ p50=687ms；8 vCPU（3.0x 超訂）→ p50=36ms。少量超訂幾乎無感，嚴重超訂系統看起來「卡住」。

---

### Phase 9 — 架構補完：AOF + Read Replica + Rate Limiting

#### Stage 9a — Nginx keepalive + shared_buffers

| 變更 | 效果 |
|------|------|
| `keepalive 32 → 128` | redirect throughput +2.1%（2,605 → 2,661 req/s）|
| `shared_buffers 128MB → 256MB` | 在當前資料規模下邊際效益不顯著 |

keepalive 增大讓每個 upstream worker 分到更多持久連線，減少 TCP handshake overhead。

#### Stage 9b — Redis AOF 持久化

設定：`--appendonly yes --appendfsync everysec`，新增 `redis_data:/data` volume。

| appendfsync 選項 | 資料安全 | 效能影響 |
|----------------|---------|---------|
| always | 最高（0 資料丟失）| 下降 30-50% |
| **everysec（選擇）** | 高（最多丟 1 秒）| 幾乎無影響（< 1%）|
| no | 最低 | 最佳 |

驗證：Redis 重啟後 redirect 仍回傳 302（AOF 成功恢復 cache 資料）；redirect throughput 無退步（AOF 只影響寫入）。

Redis 7.x Multi-Part AOF：`base.rdb`（快照）+ `incr.aof`（增量）+ `manifest`（索引），重啟時先載入快照再 replay 增量，比純 AOF 快。

#### Stage 9c — PostgreSQL Read Replica（讀寫分離）

```
寫入路徑：app → PgBouncer → PostgreSQL Primary
讀取路徑：app → postgres_replica:5432（get_qr_info、analytics）
複製：Primary → WAL streaming → Replica（replay_lag ~1.8ms）
```

路由規則：GET /api/qr/{token} 和 analytics 改用 `Depends(get_read_db)`（read_engine, pool_size=5）。

**關鍵衝突**：Phase 8 把 `scan_events` 設為 UNLOGGED 加速 consumer 寫入（跳過 WAL）。但 streaming replication 的原理是複製 WAL，UNLOGGED table 不寫 WAL，Replica 讀取時報錯：
```
cannot access temporary or unlogged relations during recovery
```

解法：`ALTER TABLE scan_events SET LOGGED`，捨棄 UNLOGGED 換取 replication 架構。因為 `synchronous_commit=off` 已移除 WAL fsync 等待，WAL 本身不再是效能瓶頸，UNLOGGED 的效益可以放棄。

**教訓**：優化之間有相依性。UNLOGGED 與 Streaming Replication 不相容；引入單一優化前應預先考慮後續架構方向，否則後來需要回退。

#### Stage 9d — Nginx Rate Limiting

```nginx
limit_req_zone $binary_remote_addr zone=create_zone:10m rate=20r/s;

location = /api/qr/create {
    limit_req zone=create_zone burst=40 nodelay;
    limit_req_status 429;
}
```

精確匹配（`=`）確保只限制 create，不影響 GET /api/qr/{token} 等路由。80 個並發 create → 45 個 200、35 個 429（burst=40 後正確觸發）。

**Phase 9 整體成果**：

| 指標 | Phase 7 起點 | Phase 9 終態 | 改善 |
|------|------------|------------|------|
| redirect throughput | 1,731 req/s | **2,661 req/s** | +53% |
| create p50 | 4,680ms | **42ms** | 110x |
| create throughput | ~343 req/s | **630 req/s** | +84% |
| Redis 重啟後資料 | 全失 | AOF 保留 | ✓ |
| analytics 負載 | 打 Primary | 打 Replica | ✓ |

---

## 四、Phase 10：LB 層驗證

### 直連壓測（Stage 10a）

繞過 Nginx 直打 app1，測量單一 app container 的真實 ceiling：2,304 req/s。

理論上 2 個 app 各 2,304 req/s → 4,608 req/s，但 Nginx + 2 apps 實測只有 2,661（差了 42%）。

根本原因：Podman VM 8 vCPU，Nginx 8 workers + app1 6 workers + app2 6 workers = 20 個進程搶 8 核。瓶頸在 Python workers 的 CPU，換 LB 無法突破。

### HAProxy 實驗（Stage 10b）

**假說**：HAProxy per-request CPU 開銷比 Nginx 低，可以把節省的 CPU 還給 Python workers。

**實驗過程**：

第一次測試（global middleware）：2,661 → **1,999 req/s（-25%）**。

原因：`@app.middleware("http")` 底層是 `BaseHTTPMiddleware`，它把每個 response 包在 iterator，對所有請求加 async dispatch overhead，包括原本零 overhead 的 redirect 路徑。同時 k6 setup 發 500 次 create 全被自己的 rate limit 擋住（X-Forwarded-For 帶同一 IP），只建立了 60 個 token。

修正（route dependency）後：HAProxy = 2,520 req/s，仍低於 Nginx 2,661（-5%）。

**結論**：假說被否定，回滾 Nginx。Nginx 對 HTTP reverse proxy 已高度優化，HAProxy 並無優勢；換 LB 無法突破 Python workers 上限。

### Rate Limit 遷移（Stage 10c）

從 Nginx `limit_req_zone` 遷移至 FastAPI route dependency，是本次唯一的正向改動：

```python
async def _rate_limit_create(request: Request) -> None:
    ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    if not await cache.check_rate_limit(ip):
        raise HTTPException(status_code=429)

@router.post("/api/qr/create")
async def create_qr(
    ...,
    _: None = Depends(_rate_limit_create),  # 只掛在 create，redirect 零負擔
):
```

Redis fixed-window counter：
```python
key = f"ratelimit:create:{ip}:{int(time.time()) // window}"
count = await redis_client.incr(key)  # 原子操作，多 worker 計數正確
if count == 1:
    await redis_client.expire(key, window + 1)
return count <= max_requests  # max=60 等效 rate=20 burst=40
```

**教訓**：`BaseHTTPMiddleware` 對所有請求（包括完全無關的 redirect）加 overhead，是 FastAPI/Starlette 的著名效能陷阱。Guard 邏輯應放在 route dependency，只影響目標 route。

---

## 五、Phase 11：Scale Out 驗證

### Phase 11a — Scale Up 驗證（單機 ceiling 確認）

**目標**：12 vCPU + 4 app containers，能否突破 2,661 req/s ceiling。

**測試結果**：

| 配置 | Workers（total）| Throughput（peak）|
|------|----------------|------------------|
| Phase 9（2 containers × 6 workers）| 12 | 2,661 req/s |
| 11a 初測（4 containers × 6 workers，24 total）| 24 | **2,173 req/s（退步 -18%）** |
| 11a 修正（4 containers × 4 workers，16 total）| 16 | ~2,550–2,600 req/s |

初測退步原因：24 Python workers 搶 12 vCPU，context switching 開銷超過多 worker 效益；k6 setup 在 < 1 秒內送出 500 次 create，觸發自己的 rate limit，只建立了 58 個 token（加入 `sleep(0.025)` 降速至 40 req/s 後修正為 340 個）。

**關鍵結論**：加容器無法突破 ceiling。從 2 containers 到 4 containers，peak throughput 仍在 2,500–2,700 req/s 範圍。

原因：所有 containers 共享同一 Podman VM 的 CPU 和虛擬網路。每個 redirect request 穿越 4 次 Podman bridge network，2,500 req/s 下每秒 ~10,000 個 container-to-container 封包。

**教訓**：在同一 VM 內加 containers 是「虛假的橫向擴充」——資源池沒有增加，只是切片方式不同。真正的 scale out 需要多台物理主機，讓每台主機有獨立 CPU、記憶體、網路介面。

---

### Phase 11b — Multi-host 模擬（三層 LB 路由驗證）

**架構（模擬）**：

```
nginx-global（port 8100）
    ├── nginx-site1 → app1, app2
    └── nginx-site2 → app3, app4
                              │
              PgBouncer → PostgreSQL Primary + Replica
              Redis（共用，AOF）
```

**功能驗證：成功** — 兩個 site 均有收到流量，302 路由穿越三層均正確。

**效能：931 req/s（比 11a 退步 59%）** — 模擬環境限制，非架構問題。

原因：三個 Nginx containers 在同一 VM，每個 request 多穿兩層 Podman bridge，加上 6 個額外 Nginx worker 搶 CPU。

在真實多主機環境：各 Nginx 在專屬主機上，額外 hop 只是 ~0.5ms 延遲，不是 CPU 競爭。

**理論多主機效益**：

| 配置 | 理論 redirect QPS | 備注 |
|------|-----------------|------|
| 1 site（Phase 9/11a 實測）| 2,600 req/s | 實測驗證 |
| **2 sites（真實多主機）** | **5,200 req/s** | 線性擴展，各 site 獨立 CPU |
| 4 sites | 10,400 req/s | 需 Redis Cluster |

**教訓**：單 VM 模擬多主機只能驗證路由正確性，無法驗證效能水平擴展性。每次加一層 proxy，在單 VM 是「資源重分配」，在真實多主機是「資源新增」。

---

### Phase 11c — CDN 本地模擬（Varnish — 5,000 QPS 達成）

**架構**：

```
k6
 │
Varnish（port 8200，256MB，TTL=60s）
 │ MISS（第一次，< 0.5s 熱身完畢）    │ HIT（後續 99.9%+）
 ↓                                      ↓
nginx-origin → app1~4 → Redis         直接從記憶體回傳 302（0.2ms）
```

VCL 快取策略：
- `GET /r/<token>` → 302：快取，TTL=60s
- `GET /r/<token>` → 404：不快取
- `POST /api/qr/create`：pass（POST 不快取）

**測試結果**：

| 指標 | Phase 11a（無 CDN）| Phase 11c（Varnish）| 改善 |
|------|-------------------|--------------------|------|
| avg throughput | 2,255 req/s | **3,165 req/s** | +40% |
| **peak throughput** | ~2,550 req/s | **~5,100 req/s** | **+100%** |
| **p50 latency** | 29ms | **0.202ms** | **144x** |
| p95 latency | 4.01s（飽和）| **39.7ms** | 大幅改善 |
| Dropped iterations | 184,679 | **24,578（-87%）** | — |
| Thresholds | 未通過 | **全部通過 ✓** | — |

Hold@6000 req/s 的 60 秒窗口：312,500 iterations ÷ 60s = **~5,208 req/s**，達成 5,000 QPS 目標。

**Varnish HIT 路徑 vs 原路徑**：

```
原路徑：
  Podman bridge + Nginx（0.1ms）→ app Python 解析（0.1ms）→ Redis GET（0.5ms）→ 組 response
  總計：~1ms；消耗 Python CPU + Redis

Varnish HIT：
  Podman bridge + Varnish（0.1ms）→ Hash lookup（< 0.01ms）
  總計：~0.2ms；消耗 Varnish（C，極低 CPU）
```

p95=39.7ms 的少數 MISS 請求需走完整 nginx-origin → app 路徑（~40ms），印證 HIT 路徑與 MISS 路徑的差異。

**生產注意**：若 QR code 有 `expires_at` 或 destination URL 可更新，需實作 PURGE 機制：
```python
# redirect handler 加入動態 Cache-Control
remaining = expires_at - datetime.now() if expires_at else None
ttl = min(int(remaining.total_seconds()), 3600) if remaining else 3600
return RedirectResponse(url=url, headers={"Cache-Control": f"public, max-age={ttl}"})
```

**教訓**：CDN 的本質是把「計算」換成「記憶體查找」。每個 app-path redirect 需要 Python 解析 + Redis 查詢 + asyncio 調度 ≈ 1ms；CDN HIT 只需 hash table lookup ≈ 10μs。當相同 token 被大量重複請求（真實 QR code 場景），CDN 的效益最大。1M 個 302 responses 快取只需 256MB RAM，而 1 台 app VM 需要完整 CPU + OS + Python runtime + Redis 連線。

---

## 六、100M+ DAU 規模估算

摘要自 `phase11d_scaling_estimation.md`，以 Phase 1–11c 實測數據為基礎推算。

### 流量模型假設

```
redirect_QPS_peak = DAU × 5次/天 × 3倍峰值係數 / 86400 ≈ DAU × 0.0001736
create_QPS_peak   = DAU × 0.01次/天 × 3 / 86400   ≈ DAU × 3.47 × 10⁻⁷
```

### 各規模需求對比

| DAU | redirect 峰值 QPS | create 峰值 QPS | 所需 CDN sites | Redis 記憶體 | DB storage/年 |
|-----|-----------------|----------------|--------------|------------|--------------|
| 100K | 17 req/s | 0.03 req/s | 1 | 256MB | 0.18GB |
| 1M | 174 req/s | 0.35 req/s | 1 | 256MB | 1.8GB |
| 10M | 1,736 req/s | 3.5 req/s | 1 | 1GB | 18GB |
| 50M | 8,681 req/s | 17.4 req/s | 2 | 5GB | 91GB |
| 100M | 17,361 req/s | 34.7 req/s | 4 | 10GB | 182GB |
| 500M | 86,806 req/s | 174 req/s | 17 | 50GB | 912GB |
| 1B | 173,611 req/s | 347 req/s | 34 | 100GB | 1.83TB |

（sites 數 = `ceil(峰值 QPS / 5,100)`，每 site 含 Varnish CDN，實測 Phase 11c）

### 規模瓶頸轉移

| DAU 規模 | 主要瓶頸 | 解決手段 |
|---------|---------|---------|
| < 1M | 無瓶頸，單機綽綽有餘 | — |
| 1M–10M | Redis 記憶體 | 升規 Redis（1–4GB）|
| 10M–50M | 單機 redirect ceiling | 加第 2 個 app 主機（真實多主機）|
| 50M–100M | 跨 Region 延遲 | GeoDNS + 各 Region 獨立 Varnish + Redis Cluster |
| 100M–500M | DB storage 增長 | 分片 + cold storage tiering |
| 500M–1B | CDN cache invalidation / hot key | PURGE 全球同步；Redis hot key local replication |

**關鍵洞察**：在任何規模，create 的 DB 負載都不是瓶頸。100M DAU 的 create 峰值只有 34.7 req/s，4 個 site 的 create 容量（4 × 630 = 2,520 req/s）有 72 倍餘裕。真正的長期成本在 DB 儲存與 CDN 一致性，不在 compute。

### 100M DAU 最小配置

```
Global LB（Anycast）
    ├── Region A：Varnish edge（2 sites）→ 10,200 req/s 容量
    └── Region B：Varnish edge（2 sites）→ 10,200 req/s 容量
                              │
              Redis Cluster（6 主 6 從，跨 Region）
              PgBouncer → PostgreSQL Primary + 3 Replicas
```

估算月費（AWS）：~$25,000（不含 CDN 流量費）

---

## 七、架構設計原則總結

從 11 個 Phase 的實驗歸納出 5 大洞察，附具體數字佐證。

### 洞察一：消除不必要的操作，勝於優化已有的操作

- **Negative cache**：消除 probe 路徑的 DB 查詢 → p50 從 1,500ms → 67ms（-95.5%），throughput +16.7%
- **synchronous_commit=off**：消除 WAL fsync 等待 → create p50 從 4,680ms → 42ms（110x）
- **Varnish CDN**：消除整個 app 路徑 → p50 從 29ms → 0.202ms（144x），throughput 從 2,600 → 5,100 req/s

### 洞察二：水平擴展只對正確的瓶頸有效

- Phase 6（DB write 瓶頸 + 加 container）：throughput 從 2,056 → 1,471（**-28%**）
- Phase 10（Python workers 是瓶頸 + 換 LB）：2,661 → 2,520（**-5%**，換 LB 無效）
- Phase 11a（同一 VM 加 containers）：throughput 從 2,661 → 2,173（**-18%**，虛假擴充）
- 反例（Phase 5，CPU-bound workload 加 worker）：938 → 2,056（**+119%**，正確場景）

**規則**：先確認瓶頸層，再決定如何擴展。I/O-bound 加 worker 適得其反；需要真正獨立資源（多主機）才是真正的 scale out。

### 洞察三：優化之間有相依性

- Phase 8（UNLOGGED scan_events）+ Phase 9c（Streaming Replication）= 不相容
  - UNLOGGED 跳過 WAL；Replication 依賴 WAL
  - 後來只能用 `ALTER TABLE scan_events SET LOGGED`，放棄 UNLOGGED 的效益
- Phase 9（Nginx keepalive 增大）必須與 upstream worker 數同步考慮
  - keepalive=32，16 個 upstream workers → 每 worker 平均 2 條持久連線
  - keepalive=128，12 個 upstream workers → 每 worker 10+ 條持久連線

**規則**：引入新優化前，先問「這個改動對哪些後續架構方向有限制」。

### 洞察四：Guard 邏輯放在正確的層

- Phase 10（global middleware 做 rate limit）：redirect throughput -25%（2,661 → 1,999）
- 修正（route dependency 只掛 create）：恢復至 2,520，redirect 零負擔

`BaseHTTPMiddleware` 對所有請求加 async dispatch overhead，是 FastAPI/Starlette 的效能陷阱。Rate limit 等 guard 邏輯應放在 route dependency，只對需要保護的 route 生效。

### 洞察五：CDN 是讀多寫少工作負載最高性價比的擴展手段

| 擴展方式 | 所需資源 | 效益 |
|---------|---------|------|
| 加 1 台 app VM | 完整 VM（CPU + RAM + 費用）| +2,600 req/s |
| Varnish 256MB RAM | 256MB 記憶體 | +2,500 req/s（等效）|

CDN 不解決容量問題，解決的是「相同工作被重複計算」的問題。QR code redirect 是理想的 CDN 場景：URL 穩定、回應小（302 只有 Location header）、access pattern 高度重複（Zipf 分佈）。

生產環境必須配套 PURGE 機制處理 expires_at / URL 更新場景，否則快取中的過期 302 會被繼續提供。

---

## 八、快速查閱

### 全階段效能對比表（Baseline → Phase 11c）

| 指標 | Baseline | Ph1 | Ph2 | Ph3 | Ph4b | Ph5 | Ph6 | Ph7 | Ph8 | Ph9 | Ph10 | Ph11a | Ph11c |
|------|----------|-----|-----|-----|------|-----|-----|-----|-----|-----|------|-------|-------|
| **throughput** | 752 | 1,284 | 598 | 957 | 938 | 2,056 | 1,471 | 1,716 | ~1,796 | 2,661* | 2,661 | ~2,550* | **~5,100*** |
| **redirect p50** | 3,847ms | 1,423ms | 0.063ms | sub-ms | 0.081ms | 17.1ms | 23ms | 23ms | — | — | — | 29ms | **0.202ms** |
| **create p50** | 5,797ms | 2,221ms | 5.13ms | — | — | 1,166ms | 3,713ms | 4,680ms | **42ms** | 42ms | 42ms | 42ms | — |
| **create 成功率** | 100%* | 100% | 69% | 97.89% | 98.65% | 100% | 99.99% | 99.999% | 99.99% | 99.99% | 99.99% | 100% | — |
| **Dropped iter** | 76.6% | 51.8% | 78.5% | 65.6% | 66.2% | 23.0% | 45.8% | 36.7% | — | — | — | — | < 5% |

*Phase 9 的 2,661 是 redirect-only，8 vCPU。Phase 11a peak 是修正後。Phase 11c 是 Varnish HIT 路徑。Baseline 在低 QPS 下測試。

### 各組件容量基準（實測）

| 組件 | 容量上限 | 條件 | Phase |
|------|---------|------|-------|
| 單 site redirect（無 CDN）| ~2,600 req/s | 8 vCPU，4 apps × 4 workers，Nginx | 11a ✓ |
| 單 site redirect（Varnish CDN）| ~5,100 req/s peak | 99%+ hit rate，256MB cache | 11c ✓ |
| create throughput | ~630 req/s | 1 site，PgBouncer pool=40，sync_commit=off | 9 ✓ |
| redirect p50（cache hit）| 0.202ms | Varnish HIT | 11c ✓ |
| redirect p50（Redis hit）| ~0.1ms | Redis GET，app 路徑 | 2 ✓ |
| create p50（正常）| 42ms | sync_commit=off | 8 ✓ |
| Nginx keepalive 最優 | 128 | 12 upstream workers | 9a ✓ |
| Redis 重啟恢復 | ✓ | AOF everysec | 9b ✓ |
| replica replay_lag | ~1.8ms | WAL streaming | 9c ✓ |

### 最終系統架構圖（Phase 11c）

```
                 Varnish CDN（port 8200，256MB，TTL=60s）
                      │
                      │ MISS（第一次 / TTL 過期）
                      ↓
nginx-origin（worker 4，app1~4 upstream，port 8100）
      │
      ├── app1 ─┐
      ├── app2  │ 各 4 uvicorn workers
      ├── app3  │
      └── app4 ─┘
            │
            ├── 寫入路徑 → PgBouncer（pool=40）
            │                  ↓
            │         PostgreSQL Primary
            │         （sync_commit=off, shared_buffers=256MB, wal_level=replica）
            │                  ↓ WAL streaming
            │         PostgreSQL Replica（replay_lag ~1.8ms）
            │
            ├── 讀取路徑（get_qr_info, analytics）→ Replica
            │
            └── Redis（AOF, appendfsync=everysec）
                      ├── redirect cache（{token} → URL）
                      ├── negative cache（gone:{token}，TTL=60s）
                      ├── scan_events（Redis Stream，Consumer Group）
                      └── rate limit counter（ratelimit:create:{ip}:{ts}）

Rate limit：FastAPI _rate_limit_create dependency（Redis fixed-window，60/s）

── Phase 11b（多 site 路由驗證用，非生產路徑）──
nginx-global → nginx-site1（app1/app2）
             → nginx-site2（app3/app4）
```

### 各路徑效能總表

| 路徑 | 吞吐量 | p50 |
|------|--------|-----|
| redirect（Varnish HIT）| ~5,100 req/s peak | 0.202ms |
| redirect（Redis cache hit，無 CDN）| ~2,600 req/s | ~0.1ms |
| redirect（cache miss）| 受 DB 限制 | ~40ms |
| create | ~630 req/s | 42ms |
| probe（not_found，negative cache hit）| 高（Redis 級別）| ~3.5ms |

### 核心公式速查

**所需 site 數估算**：
```
所需 site 數（含 CDN）= ceil(峰值 redirect QPS / 5,100)
所需 site 數（不含 CDN）= ceil(峰值 redirect QPS / 2,600)
```

**peak redirect QPS 估算**：
```
peak_redirect_QPS = DAU × scans_per_day × peak_factor / 86400
                  ≈ DAU × 5 × 3 / 86400  ≈ DAU × 0.000174
```

**pool_size 計算（Little's Law）**：
```
N = λ × W
  λ = create_throughput / total_workers（每 worker 到達率）
  W = DB 平均查詢時間（秒）
pool_size_needed = ceil(N × 1.5)
本專案：N = 52.5 × 0.005 = 0.26 → pool_size=10 足夠
```

**CDN 有效吞吐量**：
```
有效吞吐量 = (hit_rate × CDN_throughput) + ((1-hit_rate) × origin_throughput)
本專案：= (0.999 × 5100) + (0.001 × 2600) ≈ 5,097 req/s
```

---

*本文件彙整自 phase1_result.md 至 phase11d_scaling_estimation.md 及 system_design_handbook.md，涵蓋 2026-05-10 至 2026-05-12 的完整優化記錄。*
