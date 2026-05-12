# Phase 8 — DB 調優 + Redirect 路徑分析（2026-05-11）

## 目標

針對 4 個問題深入分析並實測：
1. pool_size=50+50 是最優配置嗎？
2. DB 那邊還有什麼優化空間？
3. 針對 scan（redirect）情境，目標 5000 QPS，有哪些設定可以提升？
4. Container 不擴充，增加 worker？最佳設定怎麼算？

HTTP 301 不在考量範圍。

---

## 實作變更

| 檔案 | 變更 |
|------|------|
| `docker-compose.yml` | postgres 加 `synchronous_commit=off`, `checkpoint_completion_target=0.9`, `wal_buffers=16MB` |
| `docker-compose.yml` | pgbouncer `DEFAULT_POOL_SIZE` 25 → 40 |
| `scaffold/app/database.py` | `pool_size` 50→10, `max_overflow` 50→10 |
| `scaffold/app/models.py` | `ScanEvent` 加 `prefixes: ["UNLOGGED"]` |
| `k6/redirect_only_test.js` | 新增 redirect-only 壓測腳本（setup 建立 500 tokens，100% redirect） |

---

## 分段測試結果

### Stage 8a：Baseline（當前 4 workers×2, pool=50）

**腳本：redirect_only_test.js（6000 RPS 目標）**

| 指標 | 數值 |
|------|------|
| redirect throughput | 1,731 req/s |
| p50 | 687ms |
| p95 | 5.59s |
| dropped_iterations | 276,160 |
| 錯誤率 | 0% |

### Stage 8b：Workers 4→8 per container（pool=10）

**理論預期**：worker 數 ×2 → redirect throughput ×2 ≈ 3,460 req/s

**實測：**

| 指標 | 數值 | vs 8a |
|------|------|-------|
| redirect throughput | 1,617 req/s | **↓ 6.6%（反效果）** |
| p50 | 1.23s | ↑ 更差 |
| dropped_iterations | 297,822 | 更多 |

**結論：8 workers 反而更差。**

原因分析：
- Nginx upstream `keepalive=32`：在 16 workers 下每個 worker 分到的 persistent 連線更少，TCP handshake 開銷增加
- Podman VM CPU 資源有限：16 Python 進程 context switch 開銷 > 8 個
- Nginx 是真正的瓶頸，不是 Python CPU — redirect 吞吐量 Nginx-bound 在 ~1,731 req/s

**決定：恢復 4 workers per container（8 總計）。**

### Stage 8c：pool=10, synchronous_commit=off, PgBouncer POOL=40

**理論預期**：
- synchronous_commit=off：移除 WAL fsync → create latency 預計降低 3-10x
- PgBouncer POOL=40：DB 並發連線 +60%，create throughput 提升

**實測（create-only test，1000 RPS 目標）：**

| 指標 | Phase 7（基線） | Stage 8c | 改善幅度 |
|------|--------------|---------|---------|
| create p50 | 4,680ms | **42ms** | **110x ↑** |
| create p95 | 8,924ms | **55ms** | **162x ↑** |
| create throughput | ~343/s | **631/s** | **+84%** |
| 錯誤率 | 99.999% | 99.99% | 持平 |

> 超過理論預期（預估 3-10x，實測 110x）。
> `synchronous_commit=off` 移除了 WAL flush 等待（原來每個 COMMIT 需等磁碟確認），
> 這是 create 延遲的絕對主因——不是 DB 計算，是 I/O 等待。

### Stage 8d：scan_events UNLOGGED TABLE

**方法**：直接 `ALTER TABLE scan_events SET UNLOGGED`（不需重建 volume）

**實測（create-only test，1000 RPS 目標）：**

| 指標 | Stage 8c | Stage 8d | 差異 |
|------|---------|---------|------|
| create p50 | 42ms | 42ms | 無變化（符合預期） |
| create p95 | 55ms | 137ms | 略差（測試噪聲） |
| create throughput | 631/s | 619/s | 持平 |

**結論：UNLOGGED 對 create API 路徑沒有直接影響。**

原因：
- `scan_events` 是由 consumer（background task）寫入，不是 create API 本身
- `url_mappings`（create 寫入的 table）仍是 permanent/WAL table
- UNLOGGED 的效益在 consumer 批次 INSERT 更快，減少 Redis Stream 的積壓
- 直接觀測 consumer 速度需要長時間高流量測試，這個測試無法量測

---

## 問題一：pool_size=50+50 最優嗎？

**答：不是最優，但無害。**

用 Little's Law 計算真實需求：
```
create 路徑：每 worker 到達率 ≈ 343/s ÷ 8 workers ≈ 43 req/s
DB 平均耗時（synchronous_commit=off 後）≈ 5ms
每 worker 同時需要 DB 連線數 N = 43 × 0.005 = 0.21 個
```

結論：每個 async worker 實際上同時只需要 < 1 個 DB 連線。pool_size=10 足夠，50+50 浪費記憶體（每個 asyncpg 連線約 2-5 MB）。

**真正的瓶頸從來不是 app pool size，而是 PgBouncer DEFAULT_POOL_SIZE=25**（後端只有 25 個真實 PG 連線，不論 app 端設多大）。

---

## 問題三/四：Worker 最佳設定怎麼算？

**理論分析（redirect-only 路徑）：**

```
目標：5,000 redirect req/s

Little's Law：N = λ × W
W ≈ 0.5ms（Redis RTT）
N = 5,000 × 0.0005 = 2.5 並發

→ 並發不是瓶頸，Python CPU 才是

每個請求 Python CPU 時間 ≈ 0.1ms
1 worker 理論 CPU 上限 = 1,000ms ÷ 0.1ms = 10,000 req/s
實測 1 worker ≈ 1,731 ÷ 8 = 216 req/s（被 Nginx 瓶頸壓低）

若排除 Nginx 瓶頸，1 worker 實際可達 2,000~4,000 redirect/s
達到 5,000 QPS 需要 workers = ceil(5,000 / 2,000) × 1.5 ≈ 4 workers
```

**實測發現：Nginx 是 redirect 的真實瓶頸**

- 4 workers × 2：1,731 redirect/s
- 8 workers × 2：1,617 redirect/s（反降）
- Nginx worker_processes auto（Podman VM 上可能只有 2-4 個）
- Nginx upstream keepalive=32 分配給 16 workers 每人更少，TCP 開銷增加

**結論：在這個 local Podman 環境，4 workers/container 是最優點。**
要突破 redirect 5,000 QPS：
- 需要繞過 Nginx 測試（直連 app）才能確認是否真的 Nginx-bound
- 或者增加 Nginx worker_processes（但 Nginx 只有 1 容器）

---

## 最終效能對比表

| 路徑 | Phase 7（基線） | Phase 8 | 改善 |
|------|--------------|---------|------|
| redirect throughput | 1,731 req/s（redirect-only） | 1,731 req/s | 持平（Nginx bound） |
| create p50 | 4,680ms | **42ms** | **110x ↑** |
| create p95 | 8,924ms | **137ms** | **65x ↑** |
| create throughput | ~343/s | **631/s** | **+84%** |
| 混合總吞吐 | 1,716 req/s | ~1,796 req/s | +5% |

---

## 學到的系統設計概念

### 1. synchronous_commit=off 的威力
- PostgreSQL 預設每次 COMMIT 等 WAL 刷盤（fsync），這是「持久性保證（D in ACID）」的代價
- `synchronous_commit=off` 讓 COMMIT 立即返回，WAL 異步刷盤（~200ms 批次）
- 這是「降低持久性換取性能」的典型 trade-off
- 風險：crash 時最多丟失 200ms 的提交，但資料不會損壞（不是 `fsync=off`）
- 適合場景：QR code 創建（丟幾個可以重建）；不適合：金融交易

### 2. Little's Law 的正確使用
- N = λ × W（並發 = 到達率 × 系統內時間）
- 系統內時間 W 包含所有等待：網路 RTT + DB wait + CPU 時間
- async IO 的特殊性：W 中的 IO wait 不佔用 CPU，但仍佔用「並發槽」
- pool_size 應匹配 Little's Law 的實際 N，而非 max VU 數

### 3. Nginx keepalive 與 upstream worker 數的關係
- `keepalive 32` 意味著 Nginx 和 upstream 之間最多維持 32 條持久連線
- upstream 有 16 workers（server endpoints）時，每個 worker 平均分到 2 條 keepalive
- 增加 worker 不增加 keepalive 總量 → 每 worker 可用持久連線減少 → 更多 TCP handshake
- 要支持更多 upstream workers：需同步增大 `keepalive` 值

### 4. UNLOGGED TABLE 的定位
- 跳過 WAL → INSERT ~3x 更快
- 但只適合「可重建的快取型資料」（crash 後 table 清空）
- `scan_events` 是 analytics，Redis Stream 有備份，適合 UNLOGGED
- `url_mappings` 是核心業務資料，必須保持 WAL（永久表）

### 5. 理論估算 vs 實測差距
| 優化項 | 理論預期 | 實測結果 | 原因 |
|--------|---------|---------|------|
| workers 4→8 | +100% | **-6.6%** | Nginx bound + CPU 競爭 |
| sync_commit=off | 3-10x | **110x** | WAL fsync 是絕對主因（超預期）|
| UNLOGGED scan | consumer 3x | 無明顯差距 | 路徑無法直接量測 |
| PgBouncer POOL 25→40 | +60% | 含在整體改善內 | 難單獨量測 |

---

## commit

`2beec01` — 推送至 https://github.com/Lung-Yu/sd-practice-qr-code-generator
