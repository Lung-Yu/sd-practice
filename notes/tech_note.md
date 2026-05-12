# Tech Note — QR Code Generator 技術指令參考

> 本文件整理 QR Code Generator 專案從 Baseline 到 Phase 11 實作過程中用到的所有工具、指令與技術細節。  
> 所有標注「# 本專案實測」的指令均在本專案環境中實際執行過。  
> 最後更新：2026-05-12

---

## 目錄

1. [環境管理（Podman）](#一環境管理podman)
2. [負載測試（k6）](#二負載測試k6)
3. [資料庫（PostgreSQL）](#三資料庫postgresql)
4. [連線池（PgBouncer）](#四連線池pgbouncer)
5. [快取（Redis）](#五快取redis)
6. [反向代理（Nginx）](#六反向代理nginx)
7. [CDN（Varnish）](#七cdnvarnish)
8. [應用層（FastAPI + uvicorn）](#八應用層fastapi--uvicorn)
9. [監控（Prometheus + Grafana）](#九監控prometheus--grafana)
10. [Git 工作流程](#十git-工作流程)
11. [附錄：快速 Debug 指令集](#附錄快速-debug-指令集)

---

## 一、環境管理（Podman）

### 為什麼用 Podman

本專案使用 Podman 而非 Docker，原因是 macOS 上 Podman 可以在不需要 root daemon 的情況下執行容器（rootless 模式），安全性較高。Podman Machine 提供一個輕量 Linux VM 讓容器在其中運作。

### podman machine 指令

```bash
# 初始化 Podman Machine（僅第一次執行）
podman machine init

# 啟動 Podman Machine
podman machine start                        # 本專案實測

# 查看目前 Machine 狀態
podman machine list

# 調整 vCPU 數量（需先停止 Machine）
podman machine stop
podman machine set --cpus 8                 # 本專案實測：Phase 9 調至 8 vCPU
podman machine set --cpus 12               # Phase 11a 調至 12 vCPU
podman machine start

# 調整記憶體（單位 MB）
podman machine set --memory 8192

# 刪除 Machine（清空所有容器與 volume）
podman machine rm
```

> 技術限制：Podman rootless 模式下，無法 bind port < 1024（例如 port 80 或 443）。  
> 本專案因此將對外 port 設為 8100（nginx-global）與 8200（varnish），繞過此限制。

### podman compose 指令

本專案使用 `podman compose`（等同於 `docker compose`，呼叫 Compose V2 API）。

```bash
# 啟動所有服務（背景執行）
podman compose up -d                        # 本專案實測

# 重新 build 後啟動（程式碼有變更時使用）
podman compose up -d --build                # 本專案實測

# 停止所有服務（保留 volume）
podman compose down                         # 本專案實測

# 停止所有服務並刪除 volume（完全清除資料）
podman compose down -v

# 只重新 build 特定 image
podman compose build app1

# 查看所有容器狀態
podman compose ps                           # 本專案實測

# 查看某個服務的 log（即時串流）
podman compose logs -f app1                 # 本專案實測
podman compose logs -f nginx-origin

# 重啟特定服務
podman compose restart app1                 # 本專案實測

# 重新建立特定服務（不重建 image）
podman compose up -d --no-deps app1
```

### 常用 container 操作

```bash
# 查看所有正在運行的容器
podman ps

# 查看所有容器（含已停止）
podman ps -a

# 進入容器執行 shell
podman exec -it qr_code_generator_app1_1 sh      # 本專案實測
podman exec -it qr_code_generator_postgres_1 sh

# 查看容器資源使用（類似 htop）
podman stats

# 停止單一容器
podman stop qr_code_generator_app1_1

# 刪除單一容器
podman rm qr_code_generator_app1_1

# 查看容器 log
podman logs qr_code_generator_app1_1

# 查看 volume 清單
podman volume ls

# 刪除特定 volume
podman volume rm qr_code_generator_pg_data
```

---

## 二、負載測試（k6）

### 為什麼用 k6

k6 是用 Go 撰寫的開源負載測試工具，支援 JavaScript 撰寫測試腳本，並提供 `ramping-arrival-rate` executor 能精確控制每秒請求數（RPS），適合找到系統吞吐量上限。

### k6 run 指令與環境變數

```bash
# 基本執行（使用預設 BASE_URL）
k6 run k6/redirect_only_test.js              # 本專案實測

# 傳入環境變數（指定測試目標）
k6 run -e BASE_URL=http://localhost:8100 k6/redirect_only_test.js   # 本專案實測
k6 run -e BASE_URL=http://localhost:8200 k6/redirect_only_test.js   # 測試 Varnish 路徑

# 直連 app1 繞過 LB（隔離測試用）
k6 run -e BASE_URL=http://localhost:8001 k6/redirect_only_test.js   # 本專案實測

# 輸出結果到 CSV
k6 run --out csv=result.csv k6/redirect_only_test.js

# 輸出到 Prometheus（remote write）
k6 run --out experimental-prometheus-rw k6/redirect_only_test.js
```

### ramping-arrival-rate executor 設定說明

本專案使用 `ramping-arrival-rate` 而非 `ramping-vus`，原因是它直接控制每秒到達的請求數，不受 VU 響應速度影響，能更準確反映真實流量壓力。

```javascript
// 取自本專案 k6/redirect_only_test.js
scenarios: {
  redirect_stress: {
    executor: "ramping-arrival-rate",
    startRate: 0,          // 初始 RPS
    timeUnit: "1s",        // RPS 的時間單位
    preAllocatedVUs: 300,  // 預先分配的 VU 數（影響測試啟動速度）
    maxVUs: 5000,          // 最大 VU 數（當 preAllocated 不夠時才動態增加）
    stages: [
      { duration: "20s", target: 1000 }, // 暖機：20 秒內爬坡到 1000 RPS
      { duration: "30s", target: 3000 }, // 加壓：升至 3000 RPS
      { duration: "30s", target: 5000 }, // 衝刺：升至 5000 RPS
      { duration: "60s", target: 6000 }, // 找上限：維持 6000 RPS 60 秒
      { duration: "20s", target: 0    }, // 降溫
    ],
  },
},
```

> 關鍵參數：
> - `preAllocatedVUs`：至少要設到預期最大並發數的一半，太小會在測試開始時出現 `dropped_iterations`
> - `maxVUs`：設太小也會限制最大並發

### thresholds 設定方式

Thresholds 是測試的「合格門檻」，k6 結束時會依此判斷 pass/fail。

```javascript
thresholds: {
  // 95th percentile 回應時間 < 500ms
  http_req_duration: ["p(95)<500"],

  // 失敗率 < 1%
  http_req_failed: ["rate<0.01"],

  // 特定 check 的通過率 > 99%
  "checks{scenario:redirect}": ["rate>0.99"],
},
```

### setup() / default() / teardown() 模式

k6 測試的生命週期分為三個函式，本專案使用 `setup()` 預先建立測試資料：

```javascript
// setup()：在測試開始前執行一次，return 值傳給 default()
export function setup() {
  // 建立 500 個 QR code，把 token 收集起來
  const tokens = [];
  for (let i = 0; i < 500; i++) {
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: urls[i % urls.length] + "?seed=" + i }),
      { headers: { "Content-Type": "application/json" } }
    );
    if (res.status === 200) tokens.push(JSON.parse(res.body).token);
    sleep(0.025); // 節流：維持在 rate limit 40 req/s 以下  # 本專案實測
  }
  return { tokens };
}

// default()：每個 VU 反覆執行的主測試邏輯
export default function (data) {
  const token = data.tokens[Math.floor(Math.random() * data.tokens.length)];
  const res = http.get(`${BASE_URL}/r/${token}`, {
    redirects: 0,  // 不自動跟隨 redirect，只測 302 本身的速度
  });
  check(res, { "redirect → 302": (r) => r.status === 302 });
}

// teardown()：測試結束後執行一次（本專案未使用）
export function teardown(data) {
  // 清理資源
}
```

### http.get / http.post 選項

```javascript
// GET 請求，關閉自動 redirect（本專案實測：測試 redirect 吞吐量時必用）
const res = http.get(url, {
  redirects: 0,                    // 不跟隨 redirect，直接取得 302 回應
  tags: { name: "redirect" },      // 標記此請求，用於 threshold 篩選
  timeout: "5s",                   // 請求超時
});

// POST 請求（JSON body）
const res = http.post(
  `${BASE_URL}/api/qr/create`,
  JSON.stringify({ url: "https://example.com" }),
  {
    headers: { "Content-Type": "application/json" },
    tags: { name: "create" },
  }
);
```

### 結果解讀：dropped_iterations 的意義

`dropped_iterations` 是 `ramping-arrival-rate` 特有的指標：

```
dropped_iterations: 當系統來不及在預定時間處理請求時，k6 放棄發送的請求數量
```

- 若 `dropped_iterations` > 0，代表系統已達到吞吐量上限，無法處理設定的 RPS
- 可以用它反推系統實際上限：`actual_RPS = target_RPS - (dropped_iterations / total_duration)`
- 本專案在 Phase 8 測試時，hold@6000 RPS 的 stage 出現大量 `dropped_iterations`，確認 Nginx 瓶頸在 1,731 req/s

---

## 三、資料庫（PostgreSQL）

### psql 連線指令

```bash
# 連線到本機 PostgreSQL（Port 5532 映射至容器 5432）
psql -h localhost -p 5532 -U qruser -d qrcode          # 本專案實測

# 連線到容器內的 PostgreSQL
podman exec -it qr_code_generator_postgres_1 psql -U qruser -d qrcode  # 本專案實測

# 執行單一 SQL 指令後離開（-c 選項）
psql -h localhost -p 5532 -U qruser -d qrcode -c "SHOW synchronous_commit;"
```

### 重要效能參數說明

本專案在 `docker-compose.yml` 的 postgres `command` 中直接帶入以下參數：

```yaml
command: >
  postgres
  -c synchronous_commit=off
  -c checkpoint_completion_target=0.9
  -c wal_buffers=16MB
  -c shared_buffers=256MB
  -c wal_level=replica
  -c max_wal_senders=3
```

| 參數 | 本專案值 | 說明 |
|------|---------|------|
| `synchronous_commit=off` | off | **最重要的效能開關**。關閉後 COMMIT 不等 WAL 刷磁碟，延遲從 4,680ms 降至 42ms（110x）。代價是 crash 時最多丟失 200ms 的已提交資料。 |
| `checkpoint_completion_target=0.9` | 0.9 | checkpoint 寫入分散在 90% 的 checkpoint 間隔時間內，減少 I/O 高峰。預設 0.5 可能造成 I/O 短暫飆高。 |
| `wal_buffers=16MB` | 16MB | WAL 寫入 buffer 大小，預設 auto（通常 4MB）。加大後 WAL 先批次寫入記憶體，再一次刷磁碟。 |
| `shared_buffers=256MB` | 256MB | PostgreSQL 的共享記憶體 buffer，相當於 DB 的 L1 cache。建議設為總記憶體的 25%。 |
| `wal_level=replica` | replica | WAL 記錄詳細度，設為 replica 才能啟用 Streaming Replication。 |
| `max_wal_senders=3` | 3 | 允許同時連線的 Replica 數量（WAL sender 程序數）。 |

### WAL Replication 設定

WAL（Write-Ahead Log）Streaming Replication 讓 Replica 即時接收 Primary 的 WAL 日誌並重放，實現熱備份與讀寫分離。

**Primary 需要設定：**

```sql
-- 建立 replication 用的使用者（在 Primary 執行）
CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'replicatorpass';

-- 確認 wal_level（本專案透過 docker-compose.yml 啟動參數設定）
SHOW wal_level;  -- 應顯示 replica

-- 查看目前 WAL sender 連線狀態
SELECT * FROM pg_stat_replication;
```

**Replica 初始化（本專案 postgres/replica_entrypoint.sh）：**

```bash
# pg_basebackup：從 Primary 複製完整資料目錄到 Replica
pg_basebackup \
  -h postgres \           # Primary 主機名（容器網路內）
  -U replicator \         # 使用 replicator 帳號
  -D "$PGDATA" \          # 目標資料目錄
  -Xs \                   # 使用 streaming 模式傳輸 WAL
  -R \                    # 自動生成 standby.signal 和 recovery 設定
  --checkpoint=fast \     # 強制立即 checkpoint，加快初始化
  -P                      # 顯示進度
```

### Replica 延遲查詢 SQL

```sql
-- 在 Primary 查詢 Replica 落後的 WAL bytes 數
SELECT
  client_addr,
  sent_lsn,
  write_lsn,
  flush_lsn,
  replay_lsn,
  pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes
FROM pg_stat_replication;

-- 在 Replica 查詢自身落後 Primary 的時間
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;

-- 在 Replica 確認是否為備用模式
SELECT pg_is_in_recovery();  -- 應回傳 t (true)
```

### UNLOGGED TABLE 與 Replication 的衝突

```sql
-- UNLOGGED TABLE：不寫 WAL，INSERT 速度約快 3 倍
-- 但：與 Streaming Replication 不相容！
-- Replica 無法複製 UNLOGGED TABLE 的資料
CREATE UNLOGGED TABLE scan_events_fast (...);  -- 不建議與 Replica 同時使用

-- 本專案選擇使用一般 TABLE + Redis Stream 解耦，兼顧效能與可複製性
```

> 本專案教訓（Phase 9）：啟用 Read Replica 後，若 `scan_events` 是 UNLOGGED TABLE，Replica 上該表會是空的，Analytics 查詢回傳全部為 0。需選擇：放棄 UNLOGGED 或不使用 Replica。本專案選擇一般 TABLE + Redis Stream consumer 異步寫入解耦。

### 常用診斷 SQL

```sql
-- 查看當前所有連線
SELECT pid, usename, application_name, client_addr, state, query
FROM pg_stat_activity
WHERE datname = 'qrcode';

-- 查看當前連線數
SELECT count(*) FROM pg_stat_activity WHERE datname = 'qrcode';

-- 查詢慢查詢（需啟用 pg_stat_statements）
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;

-- 查看 checkpoint 統計（判斷是否有 I/O 壓力）
SELECT * FROM pg_stat_bgwriter;

-- 查看 index 使用情況
SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read
FROM pg_stat_user_indexes
ORDER BY idx_scan DESC;

-- 確認 synchronous_commit 設定
SHOW synchronous_commit;

-- 查看資料表大小
SELECT
  tablename,
  pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) AS total_size
FROM pg_tables
WHERE schemaname = 'public';
```

---

## 四、連線池（PgBouncer）

### 為什麼使用 PgBouncer

PostgreSQL 每個連線需要一個獨立的 OS 進程，連線本身有 5~10MB 記憶體開銷。當 FastAPI 有多個 worker、每個 worker 有 connection pool 時，直連 PostgreSQL 容易造成連線數爆炸。PgBouncer 作為連線代理，將眾多 app 連線複用到少量 DB 連線，大幅降低 PostgreSQL 的連線壓力。

### 模式說明

| 模式 | 說明 | 適用場景 |
|------|------|---------|
| `session` | 整個 client session 使用同一個 DB 連線 | 有用到 `SET`、`PREPARE`、`LISTEN` 等 session 狀態時 |
| `transaction` | 每個 transaction 結束後釋放 DB 連線 | **本專案使用**：適合短暫的無狀態 transaction |
| `statement` | 每個 SQL statement 後釋放 DB 連線 | 不允許 multi-statement transaction，限制多 |

### 本專案 PgBouncer 設定

```yaml
# 取自 docker-compose.yml
environment:
  - DB_USER=qruser
  - DB_PASSWORD=qrpass
  - DB_HOST=postgres
  - DB_NAME=qrcode
  - POOL_MODE=transaction           # 使用 transaction mode
  - MAX_CLIENT_CONN=2000            # 最大 client（app）連線數
  - DEFAULT_POOL_SIZE=40            # 每個 user+database 組合的後端 DB 連線池大小
  - SERVER_RESET_QUERY=DISCARD ALL  # 連線還給 pool 前清除 session state
  - AUTH_TYPE=scram-sha-256         # 認證方式（與 PostgreSQL 16 一致）
```

### 重要參數說明

**`DEFAULT_POOL_SIZE=40`**  
PgBouncer 最多同時維持 40 條到 PostgreSQL 的真實連線。根據 Little's Law 計算，本專案實際需求約 3 條，40 是安全餘裕值。

**`MAX_CLIENT_CONN=2000`**  
PgBouncer 最多接受 2000 條來自 app 的連線。本專案有 2 個 app container、每個 6 workers、每個 worker pool_size=10：  
`2 × 6 × 10 = 120 << 2000`，安全。

**`SERVER_RESET_QUERY=DISCARD ALL`**  
transaction mode 下，連線歸還 pool 前執行 `DISCARD ALL`，清除可能殘留的 `SET LOCAL`、`TEMP TABLE` 等 session state，避免不同 transaction 之間互相干擾。

**為什麼用 transaction mode：**  
FastAPI + asyncpg 的每個 request 只在 `async with session:` 區塊內持有 DB 連線，transaction 結束立即釋放。transaction mode 正好匹配這種模式，讓連線利用率最高。但要注意：asyncpg + PgBouncer transaction mode 需設定 `statement_cache_size=0`，否則 asyncpg 的 prepared statement 快取與 PgBouncer 連線切換不相容，會出現錯誤。

---

## 五、快取（Redis）

### redis-cli 連線與常用指令

```bash
# 連線到本機 Redis（Port 6479 映射至容器 6379）
redis-cli -h localhost -p 6479                         # 本專案實測

# 連線到容器內的 Redis
podman exec -it qr_code_generator_redis_1 redis-cli   # 本專案實測

# 查看所有統計資訊（判斷 cache hit/miss）
redis-cli -h localhost -p 6479 INFO stats              # 本專案實測

# 查看記憶體使用量
redis-cli -h localhost -p 6479 INFO memory

# 查看某個 key 的值
redis-cli -h localhost -p 6479 GET "r:abc12345"

# 查看某個 key 的剩餘 TTL（秒）
redis-cli -h localhost -p 6479 TTL "r:abc12345"

# 查看符合 pattern 的所有 key（生產環境不要用 KEYS，改用 SCAN）
redis-cli -h localhost -p 6479 SCAN 0 MATCH "r:*" COUNT 100

# 刪除某個 key
redis-cli -h localhost -p 6479 DEL "r:abc12345"

# 查看 Redis 基本資訊（版本、連線數等）
redis-cli -h localhost -p 6479 INFO server
```

### Stream 指令

Redis Stream 是本專案用來解耦「redirect 事件記錄」與「DB 寫入」的核心機制。

```bash
# XLEN：查看 Stream 的積壓訊息數
redis-cli -h localhost -p 6479 XLEN scan_events        # 本專案實測

# XINFO STREAM：查看 Stream 詳細資訊
redis-cli -h localhost -p 6479 XINFO STREAM scan_events

# XINFO GROUPS：查看 Consumer Group 狀態
redis-cli -h localhost -p 6479 XINFO GROUPS scan_events  # 本專案實測

# XINFO CONSUMERS：查看 Consumer Group 中的消費者
redis-cli -h localhost -p 6479 XINFO CONSUMERS scan_events scan_workers

# XADD：手動新增一條訊息（測試用）
redis-cli -h localhost -p 6479 XADD scan_events MAXLEN 100000 '*' token abc12345 user_agent "test" ip "127.0.0.1" ts "2026-01-01T00:00:00"

# XREADGROUP：從 Consumer Group 讀取訊息（應用層使用，這裡是指令範例）
redis-cli -h localhost -p 6479 XREADGROUP GROUP scan_workers worker-1 COUNT 10 STREAMS scan_events '>'

# XACK：確認訊息已處理（防止重複消費）
redis-cli -h localhost -p 6479 XACK scan_events scan_workers <message-id>

# XPENDING：查看待確認（unacknowledged）訊息數
redis-cli -h localhost -p 6479 XPENDING scan_events scan_workers - + 10
```

### AOF 持久化設定

```bash
# 本專案 Redis 啟動設定（取自 docker-compose.yml）
command: redis-server --appendonly yes --appendfsync everysec
```

| `appendfsync` 值 | 說明 | 效能影響 |
|-----------------|------|---------|
| `always` | 每次寫入立即 fsync，最安全 | 吞吐量下降 30~50% |
| `everysec` | 每秒 fsync 一次，**本專案使用** | 幾乎無影響（< 1%），最多丟失 1 秒資料 |
| `no` | 由 OS 決定，最快 | 可能丟失大量資料 |

> 本專案選擇 `everysec`：redirect 路徑讀取 Redis 不受 AOF 影響，create 路徑每秒 630 次 XADD，`everysec` 批次 fsync 幾乎無額外開銷，實測 throughput 未退步。

### Key 命名慣例（本專案使用的 key 格式）

| Key 格式 | 用途 | TTL |
|---------|------|-----|
| `r:{token}` | 快取 token 對應的原始 URL | 有 expires_at 則用剩餘秒數；否則 86400 秒（24h） |
| `gone:{token}` | Negative cache：此 token 已刪除或過期 | 60 秒 |
| `ratelimit:create:{ip}:{window}` | Rate limit 計數器（fixed-window 每秒） | 2 秒（window + 1） |
| `scan_events` | Redis Stream：redirect 掃描事件佇列 | 無（透過 MAXLEN 控制長度上限 100,000） |

### Python redis.asyncio 常用模式（含 Rate Limit 實作）

```python
import redis.asyncio as aioredis
import time

# 初始化 Redis client（取自 app/main.py）
redis_client = aioredis.from_url(
    "redis://localhost:6479/0",
    decode_responses=False,  # 回傳 bytes，手動 decode，避免型別混亂
)

# GET / SET 快取（取自 app/cache.py）
async def get_cached_url(token: str) -> str | None:
    value = await redis_client.get(f"r:{token}")
    return value.decode() if value else None

async def set_cached_url(token: str, url: str, ttl: int | None = None) -> None:
    ex = ttl if ttl and ttl > 0 else 86400  # 預設 24h
    await redis_client.set(f"r:{token}", url, ex=ex)

# Negative cache（標記已刪除的 token）
async def set_cached_gone(token: str) -> None:
    await redis_client.set(f"gone:{token}", b"1", ex=60)

async def is_cached_gone(token: str) -> bool:
    return await redis_client.exists(f"gone:{token}") > 0

# Fixed-window Rate Limit（取自 app/cache.py）
# 每個 IP 在每 1 秒 window 內最多 60 個請求
async def check_rate_limit(ip: str, max_requests: int = 60, window: int = 1) -> bool:
    key = f"ratelimit:create:{ip}:{int(time.time()) // window}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, window + 1)  # window + 1 讓 key 自然過期
    return count <= max_requests

# XADD：寫入 Redis Stream（取自 app/cache.py）
async def enqueue_scan(token: str, user_agent: str, ip: str) -> None:
    await redis_client.xadd(
        "scan_events",
        {
            b"token": token.encode(),
            b"user_agent": user_agent.encode(),
            b"ip": ip.encode(),
            b"ts": datetime.utcnow().isoformat().encode(),
        },
        maxlen=100000,  # 限制 Stream 最大長度，超過時自動截斷舊訊息
    )

# XREADGROUP Consumer Group 消費模式（取自 app/consumer.py）
# 建立 Consumer Group（幂等，多次呼叫安全）
try:
    await redis_client.xgroup_create("scan_events", "scan_workers", id="0", mkstream=True)
except Exception:
    pass  # BUSYGROUP 代表已存在，忽略

# 從 Group 讀取訊息（id=">" 代表只讀新訊息）
events = await redis_client.xreadgroup(
    "scan_workers",        # group name
    f"worker-{os.getpid()}",  # consumer name（本專案用 PID 區分）
    {"scan_events": ">"},  # stream: ">" 代表只讀尚未分配的新訊息
    count=200,             # 每次最多讀 200 條
    block=500,             # 最多等待 500ms，避免 CPU 空轉
)

# 確認訊息已處理（取自 app/consumer.py）
msg_ids = [msg_id for msg_id, _ in messages]
await redis_client.xack("scan_events", "scan_workers", *msg_ids)
```

---

## 六、反向代理（Nginx）

### 關鍵設定說明

```nginx
# 取自 nginx/nginx.conf（nginx-origin，直接對接 app1~4）
worker_processes 4;       # worker 數：本專案設 4，對應 nginx-origin 的需求

events {
    worker_connections 8192;  # 每個 worker 最多 8192 個並發連線
    use epoll;                # Linux 高效 I/O 多路復用（容器 Linux 必用）
    multi_accept on;          # 一次接受所有等待的連線（而非一次一個）
}
```

| 設定 | 說明 |
|------|------|
| `worker_processes` | 通常設為 CPU 核數（nginx-origin 設 4，nginx-global/site 設 2） |
| `worker_connections` | 每個 worker 的最大連線數，總並發 = worker_processes × worker_connections |
| `use epoll` | Linux 的高效 event notification，比 select/poll 快 |
| `multi_accept on` | worker 被喚醒時接受所有等待的連線，減少 syscall 次數 |

### upstream keepalive 設定與為什麼重要

```nginx
# 取自 nginx/nginx.conf
upstream qr_app {
    server app1:8000;
    server app2:8000;
    server app3:8000;
    server app4:8000;
    keepalive 256;          # 本專案實測：對 redirect 吞吐量影響顯著
    keepalive_requests 1000;
    keepalive_timeout  65s;
}

# 使用 keepalive 時，必須搭配以下設定
location / {
    proxy_pass         http://qr_app;
    proxy_http_version 1.1;        # HTTP/1.1 才支援 persistent connection
    proxy_set_header   Connection "";  # 清除 Connection header，避免發送 close
}
```

> 為什麼 keepalive 重要：  
> 沒有 keepalive 時，每個請求都要 TCP 3-way handshake（新建連線），高 QPS 下佔用大量 CPU 和時間。  
> keepalive 讓 Nginx 與後端 app 保持持久連線，複用已建立的 TCP 連線，大幅降低延遲和 CPU 開銷。  
> 本專案實測：keepalive 從 64 調整到 256 後，redirect 吞吐量從 1,731 提升至 2,661 req/s。

### Rate limiting

```nginx
# 在 http 區塊定義 limit zone（取自 nginx/nginx.conf）
# $binary_remote_addr：使用二進位 IP 格式，比字串更省空間
# zone=create_zone:10m：名為 create_zone，分配 10MB 記憶體（可記錄 ~160,000 個 IP）
# rate=20r/s：每個 IP 每秒最多 20 個請求
limit_req_zone $binary_remote_addr zone=create_zone:10m rate=20r/s;

# 在 location 套用 rate limit
location = /api/qr/create {
    limit_req zone=create_zone burst=40 nodelay;
    # burst=40：允許瞬間額外 40 個請求（burst queue）
    # nodelay：burst 內的請求立即處理，不排隊等待
    limit_req_status 429;    # 超過限制回傳 429，不用預設的 503
    ...
}
```

> 本專案同時在 Nginx 和 FastAPI 應用層都實作了 Rate Limit：
> - Nginx 層：`rate=20r/s burst=40`（按 IP 限速）
> - 應用層（`cache.py`）：Redis fixed-window 60 req/s per IP
> 兩層互相補充，Nginx 層先擋，應用層是第二道防線。

### 三層 LB 結構的設定模式

本專案 Phase 11b 實作了三層 Nginx LB 架構，模擬多機房部署：

```
nginx-global (Port 8100)    # 第一層：全域負載均衡，接受外部流量
    ├── nginx-site1          # 第二層：Site 1 的負載均衡（對接 app1, app2）
    │       ├── app1:8000
    │       └── app2:8000
    └── nginx-site2          # 第二層：Site 2 的負載均衡（對接 app3, app4）
            ├── app3:8000
            └── app4:8000
```

```nginx
# nginx-global.conf：對接兩個 site nginx
upstream global_sites {
    server nginx-site1:80;
    server nginx-site2:80;
    keepalive 64;
    keepalive_requests 1000;
    keepalive_timeout 65s;
}

# nginx-site1.conf：對接兩個 app
upstream site1_apps {
    server app1:8000;
    server app2:8000;
    keepalive 128;
    keepalive_requests 1000;
    keepalive_timeout 65s;
}
```

> 注意：在單一 VM 上模擬三層 LB，所有 Nginx worker 共享同一個 CPU pool，LB 開銷累加（本專案 Phase 11b 實測吞吐量反而下降至 931 req/s）。三層 LB 的效益在實際多機房環境才能體現。

---

## 七、CDN（Varnish）

### 為什麼加 Varnish

Varnish 是 HTTP 加速器（HTTP cache），放在 Nginx 前面，快取 302 redirect 回應。對於熱門 QR code（被大量掃描），Varnish HIT 的情況下完全不需要打到 app 層，本專案實測 Varnish HIT 吞吐量達 ~5,100 req/s，p50 降至 0.202ms（vs. 直連 app 的 2,661 req/s）。

### VCL 基本結構

VCL（Varnish Configuration Language）是 Varnish 的設定語言：

```vcl
vcl 4.0;

# 定義 backend（原始伺服器）
backend default {
    .host = "nginx-origin";   # 本專案後端是 nginx-origin（取自 default.vcl）
    .port = "80";
    .connect_timeout   = 5s;
    .first_byte_timeout = 30s;
    .between_bytes_timeout = 30s;
}

# vcl_recv：接收到請求時的邏輯
sub vcl_recv {
    unset req.http.Cookie;    # 清除 Cookie，避免妨礙快取（QR redirect 無需 Cookie）

    if (req.method == "GET" && req.url ~ "^/r/") {
        return (hash);        # 符合條件的請求：查快取
    }
    return (pass);            # 其他請求（create、analytics）：直接到後端
}

# vcl_backend_response：後端回應到達時的邏輯
sub vcl_backend_response {
    if (bereq.url ~ "^/r/" && beresp.status == 302) {
        set beresp.ttl = 60s;          # 快取 302 redirect，保留 60 秒
        unset beresp.http.Set-Cookie;  # 移除 Set-Cookie，讓快取生效
        return (deliver);
    }

    if (beresp.status >= 400) {
        set beresp.ttl = 0s;           # 錯誤回應不快取
        set beresp.uncacheable = true;
        return (deliver);
    }
}

# vcl_deliver：回應送出前的邏輯（加 debug header）
sub vcl_deliver {
    if (obj.hits > 0) {
        set resp.http.X-Cache = "HIT";
        set resp.http.X-Cache-Hits = obj.hits;   # 此物件被命中幾次
    } else {
        set resp.http.X-Cache = "MISS";
    }
}
```

### X-Cache header 觀察方式

```bash
# 確認 Varnish 是否命中快取（本專案實測）
curl -s -o /dev/null -D - http://localhost:8200/r/{token} | grep X-Cache
# 第一次：X-Cache: MISS
# 第二次起：X-Cache: HIT

# 完整 header 輸出（包含 X-Cache-Hits）
curl -v http://localhost:8200/r/{token} 2>&1 | grep -E "(X-Cache|Location|HTTP)"
```

### PURGE 機制說明

本專案的 Varnish 設定沒有 PURGE 機制（開發/練習環境用 TTL=60s 自然過期）。生產環境需要：

```vcl
# 在 vcl_recv 加入 PURGE 處理（生產環境範例）
sub vcl_recv {
    if (req.method == "PURGE") {
        # 只允許來自內部網路的 PURGE 請求
        if (!client.ip ~ trusted_ips) {
            return (synth(405, "Not allowed"));
        }
        return (purge);
    }
}
```

```bash
# 主動 PURGE 某個 token 的快取（在 update/delete 後呼叫）
curl -X PURGE http://varnish-host/r/{token}
```

> 三種 PURGE 策略的取捨（參見 system_design_handbook.md §6.3）：
> 1. **主動 PURGE**：即時準確，需要在 app 的 update/delete 路由中發 PURGE 請求
> 2. **短 TTL**：簡單，但 MISS 率提高
> 3. **Cache-Control: max-age**：最標準，讓 CDN 自動遵守 app 設定的過期時間

---

## 八、應用層（FastAPI + uvicorn）

### uvicorn 啟動參數

```dockerfile
# 取自 scaffold/Dockerfile
CMD ["uvicorn", "app.main:app",
     "--host", "0.0.0.0",
     "--port", "8000",
     "--workers", "4",                  # worker 進程數（本專案實測最佳值）
     "--timeout-graceful-shutdown", "30" # 優雅關閉等待秒數
]
```

| 參數 | 本專案值 | 說明 |
|------|---------|------|
| `--workers` | 4 | uvicorn 的多進程模式（每個進程是獨立的 event loop）。本專案 Phase 5 實測：4 workers 比 1 worker 提升 119%，接近線性。 |
| `--timeout-graceful-shutdown` | 30 | 收到 SIGTERM 後，等待現有請求處理完成的秒數。避免 rolling deploy 時請求被強制中斷。 |

> workers 數量選擇原則（本專案實測）：
> - Async workers 在等待 I/O 時讓出 CPU，可適度超訂閱（over-subscribe）
> - 建議：`workers = available_cores_per_container × 1.5~2`
> - 本專案 8 vCPU VM，2 個 app container，每容器約 2.5 核可用
> - 實測 6 workers/container 是甜蜜點（Phase 8b 實測：6 > 4 > 8）

### create_async_engine 設定

```python
# 取自 scaffold/app/database.py

# 寫入引擎（連接到 PgBouncer）
engine = create_async_engine(
    DATABASE_URL,                       # postgresql+asyncpg://...@pgbouncer:5432/qrcode
    pool_size=10,                       # 本進程維持的連線數（實際需求約 1，10 有充足餘裕）
    max_overflow=10,                    # 高峰時允許額外借用的連線數（最多 pool_size + max_overflow = 20）
    connect_args={"statement_cache_size": 0},  # 關鍵：PgBouncer transaction mode 不支援 prepared statement
)

# 讀取引擎（直連 Replica，繞過 PgBouncer）
read_engine = create_async_engine(
    READ_DATABASE_URL,                  # postgresql+asyncpg://...@postgres_replica:5432/qrcode
    pool_size=5,                        # 讀取負載較低，pool 較小
    max_overflow=5,
    connect_args={"statement_cache_size": 0},
)
```

> `statement_cache_size=0` 是必要設定：asyncpg 預設會 cache prepared statement，但 PgBouncer 在 transaction mode 下切換 backend 連線時，cached statement 在新連線上不存在，導致報錯。設為 0 關閉 cache 即可解決。

### AsyncSession dependency 模式

```python
# 取自 scaffold/app/database.py
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
# expire_on_commit=False：commit 後不自動 expire ORM 物件，避免 lazy load 問題

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session  # FastAPI Depends() 會在 request 結束後自動關閉 session

async def get_read_db():
    async with ReadAsyncSessionLocal() as session:
        yield session

# 在 route 中使用
@router.get("/api/qr/{token}")
async def get_qr_info(token: str, db: AsyncSession = Depends(get_read_db)):
    # 唯讀路由使用 Replica
    ...

@router.post("/api/qr/create")
async def create_qr(req: CreateRequest, db: AsyncSession = Depends(get_db)):
    # 寫入路由使用 Primary（via PgBouncer）
    ...
```

### BackgroundTasks vs. Redis Stream 的取捨

| 機制 | 優點 | 缺點 | 本專案使用場景 |
|------|------|------|--------------|
| `BackgroundTasks` | 簡單，不需額外基礎設施 | 佔用 app worker 的 event loop，與主業務競爭 connection pool | 不推薦用於高 QPS 場景 |
| Redis Stream | 解耦，consumer 獨立消費，不佔用 request 處理的 DB 連線 | 需要 Redis，需實作 Consumer Group | 本專案 scan_events 寫入（Phase 3 以後） |

```python
# 本專案的折衷：BackgroundTask 只負責 XADD（極輕量），DB 寫入交給 Consumer
@router.get("/r/{token}")
async def redirect(token: str, request: Request, background_tasks: BackgroundTasks, ...):
    # ...
    background_tasks.add_task(_enqueue_scan, token, request)  # 只是 XADD，不打 DB
    return RedirectResponse(url=cached_url, status_code=302)

async def _enqueue_scan(token: str, request: Request) -> None:
    await cache.enqueue_scan(token=token, user_agent=..., ip=...)  # 呼叫 XADD
```

### route dependency（Depends）vs. global middleware 效能差異

```python
# 錯誤做法：global middleware（BaseHTTPMiddleware）
# 對所有請求加約 20-25% overhead，包含不需要 rate limit 的 redirect 路徑
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ...

# 正確做法：route-level dependency（本專案使用）
# 只對特定 route 執行 rate limit 邏輯（取自 routes.py）
async def _rate_limit_create(request: Request) -> None:
    ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
    if not await cache.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too Many Requests")

@router.post("/api/qr/create")
async def create_qr(
    req: CreateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_rate_limit_create),  # 只掛在 create 路由
):
    ...
```

> 本專案 Phase 10 實測：
> - global middleware：1,999 req/s（-25% vs 基準）
> - route dependency：2,520 req/s（恢復正常）
> 原因：`BaseHTTPMiddleware` 將每個 response 包在 iterator，多一層 async dispatch 開銷。

### Redis Stream Consumer Group 實作模式

```python
# 取自 scaffold/app/consumer.py
# 每個 uvicorn worker 進程啟動一個 consumer task
async def scan_consumer() -> None:
    from . import cache

    consumer_name = f"worker-{os.getpid()}"  # 用 PID 區分 consumer，避免名稱衝突

    # 等待 Redis client 初始化完成
    while cache.redis_client is None:
        await asyncio.sleep(1)

    # 建立 Consumer Group（mkstream=True：Stream 不存在時自動建立）
    # id="0"：從頭消費（也可用 "$" 只消費新訊息）
    try:
        await cache.redis_client.xgroup_create("scan_events", "scan_workers", id="0", mkstream=True)
    except Exception:
        pass  # BUSYGROUP：group 已存在，多個 worker 同時初始化是安全的

    while True:
        events = await cache.redis_client.xreadgroup(
            "scan_workers",
            consumer_name,
            {"scan_events": ">"},
            count=200,    # 批次讀取，提高 DB 寫入效率（batch insert）
            block=500,    # 阻塞 500ms 等待新訊息，避免 busy-waiting
        )

        # 批次寫入 DB
        async with AsyncSessionLocal() as db:
            db.add_all([ScanEvent(...) for _, m in messages])
            await db.commit()

        # 批次 ACK（確認已處理）
        await cache.redis_client.xack("scan_events", "scan_workers", *msg_ids)
```

---

## 九、監控（Prometheus + Grafana）

### 服務端點

| 服務 | 外部 Port | 說明 |
|------|---------|------|
| Prometheus | 9190 | 本專案映射，原始 9090。訪問 `http://localhost:9190` |
| Grafana | 3100 | 本專案映射，原始 3000。訪問 `http://localhost:3100`（admin/admin）|
| FastAPI metrics | /metrics | 各 app 容器的 Prometheus 端點（透過 prometheus-fastapi-instrumentator 自動掛載） |

```python
# FastAPI 自動掛載 /metrics 端點（取自 app/main.py）
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)
# 訪問 http://app1:8000/metrics 可查看所有指標
```

### 常用 PromQL 查詢

```promql
# HTTP 請求吞吐量（每秒請求數）
rate(http_requests_total[1m])

# 按 endpoint 和 method 分類的吞吐量
sum by (handler, method) (rate(http_requests_total[1m]))

# 95th percentile 回應時間
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[1m]))

# 按 endpoint 的 p95 回應時間
histogram_quantile(0.95,
  sum by (handler, le) (rate(http_request_duration_seconds_bucket[1m]))
)

# 錯誤率
rate(http_requests_total{status=~"5.."}[1m]) / rate(http_requests_total[1m])

# Redis XLEN（需自行加 custom metric）
redis_stream_length{stream="scan_events"}
```

### Grafana 基本操作

```
1. 登入：http://localhost:3100
   帳號/密碼：admin / admin（取自 docker-compose.yml GF_SECURITY_ADMIN_PASSWORD=admin）

2. 新增 Datasource：
   Configuration → Data Sources → Add data source → Prometheus
   URL：http://prometheus:9090（容器網路內，不用外部 port）

3. 新增 Dashboard：
   Dashboards → New → New Dashboard → Add visualization
   選 Prometheus datasource，輸入 PromQL 查詢

4. Import 現成 Dashboard：
   Dashboards → New → Import → 輸入 Grafana dashboard ID
   （FastAPI 推薦 dashboard ID：14528）
```

---

## 十、Git 工作流程

### 本專案使用的 commit message 格式

參考近期 commit 記錄，本專案遵循 Conventional Commits 規範：

```
<type>(<scope>): <description>

類型（type）：
  feat     新功能
  fix      Bug 修復
  refactor 重構（不影響功能）
  perf     效能優化
  docs     文件變更
  test     測試相關
  chore    建置、CI 等雜項

範例（本專案實際使用）：
  feat(qr): handle validate_url errors and invalidate cache on expires_at update
  feat(qr-code): handle validate_url error and add python prerequisite
```

### 常用指令

```bash
# 查看狀態
git status
git diff

# 暫存與提交
git add scaffold/app/routes.py scaffold/app/cache.py
git commit -m "feat(qr): add negative cache for 404/410 responses"

# 查看 log
git log --oneline -10

# 建立功能分支
git checkout -b feat/rate-limiting

# Push 並建立 PR（使用 GitHub CLI）
git push -u origin feat/rate-limiting
gh pr create --title "feat: add rate limiting" --body "..."

# 查看 PR 狀態
gh pr list
gh pr view <number>
```

---

## 附錄：快速 Debug 指令集

### 一行確認各服務健康

```bash
# 確認所有容器正在運行
podman compose ps                                               # 本專案實測

# 確認 PostgreSQL Primary 可接受連線
psql -h localhost -p 5532 -U qruser -d qrcode -c "SELECT 1;"  # 本專案實測

# 確認 PostgreSQL Replica 是備用模式
psql -h localhost -p 5433 -U qruser -d qrcode -c "SELECT pg_is_in_recovery();"

# 確認 Redis 正常
redis-cli -h localhost -p 6479 PING                            # 本專案實測

# 確認 nginx-global 正常
curl -s -o /dev/null -w "%{http_code}" http://localhost:8100/api/qr/nonexistent
# 應回傳 404

# 確認 Varnish 正常
curl -s -o /dev/null -w "%{http_code}" http://localhost:8200/api/qr/nonexistent
# 應回傳 404
```

### 測試 redirect 路徑的 curl 指令

```bash
# 建立一個 QR code（取得 token）
curl -s -X POST http://localhost:8100/api/qr/create \
  -H "Content-Type: application/json" \
  -d '{"url": "https://github.com"}' | python3 -m json.tool       # 本專案實測

# 測試 redirect（不跟隨，只看 302 和 Location header）
curl -s -o /dev/null -D - http://localhost:8100/r/{token}          # 本專案實測
# 應看到：HTTP/1.1 302 Found
#         Location: https://github.com

# 測試 Varnish 路徑的 redirect
curl -s -o /dev/null -D - http://localhost:8200/r/{token}

# 測試 rate limit（連續發多個請求）
for i in $(seq 1 70); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8100/api/qr/create \
    -H "Content-Type: application/json" \
    -d '{"url": "https://example.com"}'
done
# 前 60 個應回 200，之後應回 429
```

### 確認 Varnish cache HIT/MISS

```bash
# 第一次請求（應為 MISS）
curl -s -D - http://localhost:8200/r/{token} 2>&1 | grep "X-Cache"
# X-Cache: MISS

# 第二次請求（應為 HIT）
curl -s -D - http://localhost:8200/r/{token} 2>&1 | grep "X-Cache"
# X-Cache: HIT

# 查看 HIT 計數
curl -s -D - http://localhost:8200/r/{token} 2>&1 | grep -E "X-Cache"
# X-Cache: HIT
# X-Cache-Hits: 3     ← 已被命中 3 次
```

### 確認 Redis Stream 積壓量

```bash
# 查看 scan_events Stream 積壓訊息數
redis-cli -h localhost -p 6479 XLEN scan_events                    # 本專案實測

# 查看 Consumer Group 狀態（pending 數量代表待處理訊息）
redis-cli -h localhost -p 6479 XINFO GROUPS scan_events            # 本專案實測
# 輸出中 "pending" 欄位：應接近 0，代表 consumer 有正常消費

# 查看所有 consumer 的處理狀態
redis-cli -h localhost -p 6479 XINFO CONSUMERS scan_events scan_workers

# 確認 cache hit/miss 比例
redis-cli -h localhost -p 6479 INFO stats | grep keyspace
# keyspace_hits:1234567    ← cache 命中次數
# keyspace_misses:123      ← cache 未命中次數
# 命中率 = hits / (hits + misses) * 100%

# 確認某 token 是否在 Redis 快取中
redis-cli -h localhost -p 6479 GET "r:{token}"
redis-cli -h localhost -p 6479 TTL "r:{token}"  # -1=無期限, -2=不存在, 正數=剩餘秒數
```

### 確認 PgBouncer 連線池狀態

```bash
# 連線到 PgBouncer 管理介面（透過 psql）
psql -h localhost -p 6432 -U qruser pgbouncer -c "SHOW POOLS;"
# 查看 cl_active（活躍 client）, cl_waiting（等待中）, sv_active（活躍 DB 連線）

psql -h localhost -p 6432 -U qruser pgbouncer -c "SHOW STATS;"
# 查看 total_requests, avg_req, avg_query 等統計
```

---

*文件來源：本專案實際程式碼、設定檔與十個 Phase 的實測記錄*  
*最後更新：2026-05-12*
