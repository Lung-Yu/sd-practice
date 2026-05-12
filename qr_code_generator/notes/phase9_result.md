# Phase 9 — 架構補完：Config 調優 + AOF + Read Replica + Rate Limiting（2026-05-11）

## 目標

在 Phase 8 完成 DB 調優（synchronous_commit=off, pool_size 瘦身）的基礎上，補完生產環境缺少的四個面向：
- **9a**：微調 Nginx keepalive 和 PostgreSQL shared_buffers
- **9b**：Redis AOF 持久化（可靠性）
- **9c**：PostgreSQL Read Replica 讀寫分離（架構擴展性）
- **9d**：Nginx Rate Limiting（DB 保護）

---

## Stage 9a — Nginx keepalive + PostgreSQL shared_buffers

### 變更

| 檔案 | 變更 |
|------|------|
| `nginx/nginx.conf` | `keepalive 32` → `keepalive 128` |
| `docker-compose.yml` | postgres 加 `-c shared_buffers=256MB` |

### 理由

- keepalive 32 在 12 Python workers + 8 Nginx workers 的環境下平均每個 worker 分到 2.6 條，容易產生 TCP handshake overhead；增加到 128 讓每個 worker 有充裕的持久連線
- shared_buffers 預設 128MB；256MB 讓更多熱資料（url_mappings index）常駐記憶體

### 實測結果

| 指標 | Phase 8 基準 | Stage 9a | 差異 |
|------|------------|---------|------|
| redirect throughput | 2,605 req/s | 2,661 req/s | **+2.1%** |
| create throughput | 631 req/s | 631 req/s | 持平 |
| create p50 | 42ms | 42ms | 持平 |

**結論：** keepalive 增加有小幅效益（+2.1%）；shared_buffers 在當前資料規模下邊際效益不顯著，主要效益在資料集更大時才顯現。

### Commit: `21c3322`

---

## Stage 9b — Redis AOF 持久化

### 變更

| 檔案 | 變更 |
|------|------|
| `docker-compose.yml` | redis 加 `command: redis-server --appendonly yes --appendfsync everysec`，新增 `redis_data:/data` volume |

### 理由

原本 Redis 無任何持久化設定：重啟後所有 cache 全部清空，大量 cache miss 直接打 DB，造成瞬間 DB 壓力。AOF 讓資料在重啟後自動恢復。

**appendfsync 選項比較：**

| 選項 | 行為 | 資料安全 | 效能影響 |
|------|------|---------|---------|
| `always` | 每次寫入都 fsync | 最高（0 資料丟失） | 顯著下降 |
| `everysec`（選擇） | 每秒 fsync 一次 | 高（最多丟失 1 秒） | 幾乎無影響 |
| `no` | 由 OS 決定 | 最低 | 最佳 |

### 驗證結果

- Redis 重啟後 redirect 仍回傳 **302**（AOF 成功恢復 cache 資料）
- `/data/appendonlydir/` 包含三件套：`appendonly.aof.1.base.rdb`、`appendonly.aof.1.incr.aof`、`appendonly.aof.manifest`（Redis 7.x Multi-Part AOF 格式）
- redirect throughput **無退步**（AOF 只影響寫入，redirect 純讀 Redis）

### 重要概念：Redis 7.x Multi-Part AOF

Redis 7 將 AOF 分為兩部分：
- `base.rdb`：某個時間點的 RDB 快照（base snapshot）
- `incr.aof`：快照之後的增量操作日誌
- `manifest`：描述哪些 AOF 文件組成完整的資料集

重啟時 Redis 先載入 base.rdb，再 replay incr.aof，速度比純 AOF 快。

### Commit: `07687a6`

---

## Stage 9c — PostgreSQL Read Replica（讀寫分離）

### 架構設計

```
寫入路徑：app → PgBouncer → PostgreSQL Primary（url_mappings INSERT/UPDATE/DELETE）
讀取路徑：app → postgres_replica:5432（GET /api/qr/{token}、GET /api/qr/{token}/analytics）
複製：Primary → streaming replication → Replica（replay_lag ~1.8ms）
```

**路由規則：**

| Route | 方法 | DB | 原因 |
|-------|------|-----|------|
| `POST /api/qr/create` | 寫入 | Primary（PgBouncer） | 寫入操作 |
| `PATCH /api/qr/{token}` | 寫入 | Primary | 寫入操作 |
| `DELETE /api/qr/{token}` | 寫入 | Primary | 寫入操作 |
| `GET /r/{token}` | 讀取（DB fallback） | Primary | 快取 miss 後需即時一致性 |
| `GET /api/qr/{token}` | 讀取 | **Replica** | 讀取操作，可接受 ~1ms lag |
| `GET /api/qr/{token}/analytics` | 讀取 | **Replica** | 聚合查詢，讀取操作 |

### 變更

| 檔案 | 變更 |
|------|------|
| `docker-compose.yml` | postgres 加 `-c wal_level=replica -c max_wal_senders=3`；新增 `postgres_replica` service；app1/app2 加 `READ_DATABASE_URL` env；新增 `pg_replica_data` volume |
| `scaffold/app/database.py` | 新增 `read_engine`（pool_size=5）、`ReadAsyncSessionLocal`、`get_read_db()` |
| `scaffold/app/routes.py` | `get_qr_info` 和 `get_analytics` 改用 `Depends(get_read_db)` |
| `scaffold/app/models.py` | 移除 `scan_events` 的 UNLOGGED prefix（見下方關鍵衝突） |
| `postgres/replica_entrypoint.sh` | 新建：`pg_basebackup` 初始化 + `chmod 700 $PGDATA`（Podman rootless 權限修正） |

### 驗證結果

- replication state: **streaming**，replay_lag **~1.8ms**
- `GET /api/qr/{token}` 和 analytics 均正常回傳 200
- create-only 壓測：630 req/s、p50=43ms（primary 效能幾乎無退步）

### 關鍵衝突：UNLOGGED TABLE 與 Streaming Replication 不相容

**問題：** Phase 8 把 `scan_events` 設為 UNLOGGED 以加速 consumer 寫入（跳過 WAL）。但 streaming replication 的運作原理是複製 WAL（Write-Ahead Log）到 replica。UNLOGGED table 不寫 WAL，因此 replica 不含這張表的任何資料。

**後果：** replica 試圖讀取 UNLOGGED table 時報錯：
```
cannot access temporary or unlogged relations during recovery
```

**根本衝突：**

| 設計 | UNLOGGED 優先 | Replication 優先 |
|------|--------------|----------------|
| scan_events | 無 WAL，寫入 3x 更快 | 有 WAL，可複製到 replica |
| replica 可讀 | ❌ | ✓ |
| crash 後資料 | 清空 | 保留 |

**決策：** 選擇 Replication 優先（`ALTER TABLE scan_events SET LOGGED`），原因：
1. 讀寫分離的架構價值高於 scan_events 寫入效能
2. `scan_events` 是 analytics 資料，不應不可讀
3. `synchronous_commit=off` 已移除 WAL fsync 等待，WAL 本身不是效能瓶頸

**教訓：** 優化之間有相依性。Phase 8 的 UNLOGGED 優化看似獨立，實際上限制了 Phase 9c 的架構選擇。在引入 UNLOGGED 時應預先考慮是否計劃加 replication。

### Commit: `8bcbd17`

---

## Stage 9d — Nginx Rate Limiting

### 變更

| 檔案 | 變更 |
|------|------|
| `nginx/nginx.conf` | 新增 `limit_req_zone`，為 `location = /api/qr/create` 加 rate limit |

### 設定說明

```nginx
limit_req_zone $binary_remote_addr zone=create_zone:10m rate=20r/s;

location = /api/qr/create {
    limit_req zone=create_zone burst=40 nodelay;
    limit_req_status 429;
    ...
}
```

| 參數 | 值 | 說明 |
|------|-----|------|
| `rate=20r/s` | 每秒 20 個 | 每個 IP 每秒最多 20 個 create 請求 |
| `burst=40` | 40 個 | 瞬間 burst 上限（token bucket） |
| `nodelay` | — | burst 內的請求立刻處理，不人為排隊延遲 |
| `limit_req_status 429` | 429 | 超出回 429（而非預設 503） |
| `zone:10m` | 10 MB | 可記錄約 16 萬個 IP 的計數器 |

**`location = /api/qr/create`（精確匹配）vs `location /api/qr/create`（前綴匹配）：** 精確匹配確保只有這個路徑受限，不影響 `GET /api/qr/{token}` 等其他路由。

### 驗證結果

| 測試 | 請求數 | 200 | 429 | 302 |
|------|--------|-----|-----|-----|
| 正常流量（循序 5 req） | 5 | 5 | 0 | — |
| Burst 測試（80 個並發 create） | 80 | 45 | **35** | — |
| Redirect（50 個並發） | 50 | — | 0 | **50** |

- Rate limit 正確觸發（超過 burst=40 後返回 429）
- Redirect 路徑完全不受影響

### Rate Limiting 的系統設計考量

**為什麼在 Nginx 層而非 App 層做 rate limit？**
- Nginx 在請求進入 Python 之前就攔截，節省 App 的 CPU
- Nginx 的 `limit_req_zone` 使用共享記憶體，跨多個 Nginx worker process 共用計數器
- App 層做 rate limit 需要依賴 Redis 或其他共享狀態，增加複雜度

**為什麼只限制 create 而不限制 redirect？**
- create 是 DB 寫入，成本高，容易被濫用打爆 PostgreSQL
- redirect 是 Redis 讀取，成本極低，限速反而會傷害正常用戶體驗
- 不同路由應有不同的保護策略

### Commit: `3acdecb`

---

## Phase 9 整體總結

### 效能數字對比（累積到 Phase 9 終態）

| 指標 | Phase 7 起點 | Phase 8 | Phase 9 終態 |
|------|------------|---------|------------|
| redirect throughput | 1,731 req/s | 2,605 req/s | **2,661 req/s** |
| create p50 | 4,680ms | 42ms | 42ms |
| create throughput | ~343 req/s | ~631 req/s | ~630 req/s |
| Redis 重啟後資料 | 全失 | 全失 | **AOF 保留** |
| analytics 查詢負載 | 打 Primary | 打 Primary | **打 Replica** |
| create 濫用保護 | 無 | 無 | **429 rate limit** |

### 各 Stage 的學習重點

| Stage | 技術 | 學習重點 |
|-------|------|---------|
| 9a | Nginx keepalive, shared_buffers | 小改動的邊際效益；shared_buffers 在小資料集不顯著 |
| 9b | Redis AOF, Multi-Part AOF | 持久化的 trade-off；everysec 是效能與可靠性的甜蜜點 |
| 9c | Streaming Replication, 讀寫分離 | **UNLOGGED ⊕ Replication = 不相容**；優化之間有相依性 |
| 9d | Nginx limit_req, Token Bucket | 在正確的層做正確的事；精確匹配 vs 前綴匹配 |

### Phase 9 引入的基礎設施元件

```
原本：Nginx → app1/app2 → PgBouncer → PostgreSQL Primary → Redis

現在：Nginx（+rate limit）→ app1/app2
                              ├── 寫入 → PgBouncer → PostgreSQL Primary
                              │                       ↓ WAL streaming
                              └── 讀取 → PostgreSQL Replica（replay_lag 1.8ms）
                              Redis（+AOF，重啟後資料保留）
```

---

## 補充：Phase 8b CPU 瓶頸驗證摘要

（詳見 `phase8b_bottleneck_verification.md`）

- **假說**：redirect ceiling ~1,731 req/s 是 Nginx 瓶頸
- **驗證方法**：繞過 Nginx 直打 app1:8001
- **結果**：直打 app1 達 2,116 req/s（+22%），確認 Nginx 有真實開銷
- **根因**：Podman VM 只有 5 vCPU，18+ 個重型 process 競爭
- **修復**：Podman machine set --cpus 8，redirect 提升到 2,605 req/s（+46%）
- **Worker 最優解**：8 vCPU 環境下，6 workers/container 是甜蜜點（p50=36ms 最低）
