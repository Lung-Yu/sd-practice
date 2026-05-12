# Phase 4b 結果：加入 PgBouncer Transaction Pooling

**實施日期：** 2026-05-10

## 改動摘要

### PgBouncer 架構說明

PgBouncer 是一個輕量級 PostgreSQL 連線池代理（connection pooler proxy）。在 **transaction pooling** 模式下，PgBouncer 只在一筆 transaction 執行期間才將「client 連線」綁定到「真實 PostgreSQL 連線」；transaction 結束後立即釋放，讓其他 client 可以使用同一條真實 PG 連線。這使得大量的 app client 連線能被 multiplex 到少量真實 PG server 連線上，大幅降低 PostgreSQL 的連線壓力。

### docker-compose.yml 新增 pgbouncer service

```yaml
pgbouncer:
  image: docker.io/edoburu/pgbouncer:latest
  environment:
    - DB_USER=qruser
    - DB_PASSWORD=qrpass
    - DB_HOST=postgres
    - DB_NAME=qrcode
    - POOL_MODE=transaction
    - MAX_CLIENT_CONN=1000
    - DEFAULT_POOL_SIZE=25
    - SERVER_RESET_QUERY=DISCARD ALL
    - AUTH_TYPE=scram-sha-256
  depends_on:
    postgres:
      condition: service_healthy
```

- `POOL_MODE=transaction`：transaction pooling 模式，最大化連線複用率
- `MAX_CLIENT_CONN=1000`：PgBouncer 最多接受 1,000 個 client（app 端）連線
- `DEFAULT_POOL_SIZE=25`：PgBouncer 對 PostgreSQL 最多維持 25 條真實連線

### app DATABASE_URL 改指向 pgbouncer

app service 的環境變數由原本直連 postgres 改為經由 pgbouncer 代理：

```
DATABASE_URL=postgresql+asyncpg://qruser:qrpass@pgbouncer:5432/qrcode
```

### database.py pool_size 5+5 與 statement_cache_size=0

```python
engine = create_async_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=5,
    connect_args={"statement_cache_size": 0},
)
```

- `pool_size=5, max_overflow=5`：app 端 SQLAlchemy QueuePool 最多維持 10 條到 PgBouncer 的連線（初始設定，後續發現此數字過小，詳見分析）
- `statement_cache_size=0`：**transaction pooling 不支援 server-side prepared statements**。在 transaction pooling 模式下，每次 transaction 結束後 client 連線會被交還給不同的 backend PG 連線，但 prepared statement 是綁定在特定 backend connection 上的 server-side 物件。若 asyncpg 嘗試在不同連線上重用同一個 prepared statement，會導致 `prepared statement "..." does not exist` 錯誤。設定 `statement_cache_size=0` 停用 asyncpg 的 prepared statement cache，改為每次發送原始 SQL，確保與 transaction pooling 相容。

## AUTH_TYPE 修正說明

**問題根源：** PostgreSQL 16 預設認證方式為 **SCRAM-SHA-256**。edoburu/pgbouncer 映像的預設 `AUTH_TYPE` 為 `trust`（無認證），但實際上它需要代表 client 向 PostgreSQL 進行認證。

**症狀：** PgBouncer 啟動後嘗試連線 PostgreSQL 時，因為 `AUTH_TYPE=trust` 無法完成 SCRAM-SHA-256 握手，導致所有 DB 請求失敗。

**修正：** 在 PgBouncer 的環境變數中明確加入 `AUTH_TYPE=scram-sha-256`，讓 PgBouncer 使用 SCRAM-SHA-256 協議向 PostgreSQL 16 認證，連線才能正常建立。

## 效能對比

| 指標 | Phase 4a | Phase 4b | 改善幅度 |
|------|---------|---------|---------|
| avg throughput | 735 req/s | 980.9 req/s | +33.5% |
| Dropped iterations | 490,819（73.5%） | 431,942（64.7%） | +8.8 pp |
| 整體錯誤率 | 11.00% | 10.24% | −0.76 pp |
| checks_failed | — | 0.34% | — |
| redirect p50 | 174 ms | 0.020 ms | 大幅改善 |
| redirect p95 | 739 ms | 0.125 ms | 大幅改善 |
| redirect p99 | 1,077 ms | 0.981 ms | 大幅改善 |
| redirect 成功率 | 100% | 100% | 持平 |
| create 成功率 | 94.56% | 98.71% | +4.15 pp |
| 總 HTTP 請求數 | 176,880 | 235,757 | +33.3% |
| QueuePool timeout 錯誤 | 0 | 1,055 次 | 新瓶頸出現 |
| HTTP 500 (pool 耗盡) | — | 3,199 次 | 新瓶頸出現 |

## 分析

### PgBouncer 確實帶來顯著改善

與 Phase 4a 相比，Phase 4b 各項指標均有明顯改善：

- **吞吐量 +33%**（735 → 980.9 req/s）：PgBouncer 讓有限的 25 條真實 PG 連線服務更多並發請求，PostgreSQL 連線壓力大幅降低
- **create 成功率 +4.15 pp**（94.56% → 98.71%）：PgBouncer 提升連線可用性，減少因連線不足導致的 create 失敗
- **redirect 延遲大幅改善**：p50 從 174 ms 降至 0.020 ms，p95 從 739 ms 降至 0.125 ms，顯示系統整體負載大幅降低，event loop 排程延遲回到正常水準

### app 端 pool_size=10 成為新瓶頸

儘管 Phase 4b 帶來整體改善，測試中出現了 **1,055 次 QueuePool timeout 錯誤**與 **3,199 次 HTTP 500**，直接根因是：

```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 5 reached,
connection timed out, timeout 30.00
```

**問題核心：** app 端 `pool_size=5, max_overflow=5` 表示 SQLAlchemy 最多同時持有 10 條到 PgBouncer 的連線。在 3,000 VU 的極端並發下，這 10 個槽位迅速飽和，新的 DB 請求必須在 QueuePool 排隊等候，等待超過 30 秒後觸發 timeout，返回 HTTP 500。

### 正確的 PgBouncer sizing 邏輯

PgBouncer 架構下，app 端與 PgBouncer 端的 pool 大小有不同的含義，不能混為一談：

| 設定位置 | 參數 | 控制的資源 | 建議大小 |
|---------|------|----------|---------|
| PgBouncer | `DEFAULT_POOL_SIZE` | 真實 PostgreSQL 連線數 | 小（25～50），受 PG `max_connections` 限制 |
| App（SQLAlchemy） | `pool_size + max_overflow` | App 到 PgBouncer 的連線數 | 大（50～100），PgBouncer 負責 multiplex |

**設計要點：** PgBouncer 的核心價值在於將大量 client 連線 multiplex 到少量 PG 連線。若 app 端 pool 也設得很小（10 條），則大部分請求在抵達 PgBouncer 之前就已在 SQLAlchemy QueuePool 排隊，PgBouncer 根本沒機會發揮 multiplex 效益。

正確做法是「app 端 pool 大，PgBouncer 端 pool 小」：
- App 端 `pool_size=50, max_overflow=50`（100 條 app→PgBouncer 連線）
- PgBouncer `DEFAULT_POOL_SIZE=25`（25 條 PgBouncer→PG 真實連線）
- PgBouncer 將 100 條 client 連線 multiplex 到 25 條 PG 連線，每條 PG 連線平均服務 4 個 client

## 結論

Phase 4b 驗證了 PgBouncer 的架構方向完全正確：transaction pooling 有效提升了連線複用率，吞吐量提升 33%、create 成功率提升至 98.71%、redirect 延遲從數百毫秒降回 sub-millisecond。

然而，`pool_size=5+5=10` 的過度保守設定抵消了 PgBouncer 大部分潛力，造成 1,055 次 QueuePool timeout 與 3,199 次 HTTP 500，成為本階段的新瓶頸。

**下一步：Phase 4b-fix**

將 app 端 pool_size 調整為 `pool_size=50, max_overflow=50`（共 100 條 app→PgBouncer 連線），維持 PgBouncer `DEFAULT_POOL_SIZE=25` 不變，讓 PgBouncer 真正發揮 multiplex 效益，預計可進一步消除 pool exhaustion 錯誤，將 create 成功率推向 100%，吞吐量突破 1,000 req/s。
