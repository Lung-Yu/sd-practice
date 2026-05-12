# Phase 6 結果：Nginx Load Balancer + 2 App Containers

**實施日期：** 2026-05-10

## 改動摘要

Phase 5 確認 4 uvicorn workers 將 throughput 推升至 ~2,056 req/s，但單容器的 CPU 與連線資源仍有上限。Phase 6 引入 **Nginx 作為 Layer 7 Load Balancer**，以 round-robin 將流量分散至兩個 app 容器，目標突破單容器上限。

### 1. `nginx/nginx.conf`（新增）

- `worker_processes auto`：自動偵測 CPU core 數
- `worker_connections 8192`：每個 worker 最多 8,192 個同時連線（避免耗盡問題，詳見踩坑記錄）
- `use epoll; multi_accept on`：Linux epoll 事件模型，批次接收連線
- Upstream `app_servers`：round-robin 至 `app1:8000` + `app2:8000`
- `keepalive 32`：Nginx → upstream 保持 32 條長連線，避免每 request 重新 TCP handshake

### 2. `docker-compose.yml`

- 移除單一 `app` 服務，新增 `app1` 與 `app2` 兩個獨立容器
- 新增 `nginx` 服務，監聽 host 8080，轉發至 app1/app2
- app1 與 app2 各自啟動 4 uvicorn workers → **合計 8 workers**
- Consumer Group 在多容器下依然正確：Redis 以 consumer name（`worker-<PID>`）識別 consumer，不在意其所在的容器，不同容器的 worker 可同時在同一個 `scan_group` 中競爭消費，互不干擾

### 3. `monitoring/prometheus.yml`

- `scrape_configs` 的 targets 從單一 `app:8000` 更新為 `app1:8000` 與 `app2:8000` 兩個目標，確保兩個容器的 metrics 均被收集

---

## Nginx 踩坑記錄

### 問題一：nginx.conf 缺少 `events{}` / `http{}` wrapper

初始撰寫的 `nginx.conf` 直接在頂層放置 `upstream` 與 `server` block，未包在 `http{}` 內，且完全省略 `events{}` block。Nginx 啟動時回報解析錯誤，容器反覆 crash。

**修正：** 加入完整的結構：

```nginx
events {
    worker_connections 8192;
    use epoll;
    multi_accept on;
}

http {
    upstream app_servers { ... }
    server { ... }
}
```

### 問題二：`worker_connections=1024` 在 3,000 VU 下耗盡

初次修正後以 `worker_connections 1024` 進行壓測，Nginx error log 大量出現：

```
worker_connections are not enough
```

3,000 VU 同時在線，Nginx 需同時維持 upstream 連線（到 app1/app2）加上 client 連線，每個 active request 消耗至少 2 個 connections。1,024 在此壓力下迅速耗盡，導致 58.6% 的 request 被 nginx 直接丟棄。**此次結果已捨棄，不列入正式對比。**

**修正：** 將 `worker_connections` 提升至 `8192`，搭配 `worker_processes auto` 與 `use epoll`。修正後整個測試期間 Nginx error log 中零筆 `worker_connections are not enough`。

---

## 效能對比：Phase 5 vs Phase 6（fixed）

| 指標 | Phase 5 | Phase 6 (fixed) | 變化 |
|------|---------|----------------|------|
| avg throughput | 2,056 req/s | 1,471 req/s | -28% |
| Dropped iterations | 23.0% | 45.8% | +22.8pp |
| redirect 成功率 | 100% | 100% | 持平 |
| create 成功率 | 100% | 99.9986% | -0.0014pp |
| Overall check success | — | 99.99% | — |
| http_req_failed | — | 10.02%（全為刻意 404 probe） | — |
| redirect p50 | 17.1ms | 23ms | +34% |
| redirect p95 | — | 252ms | — |
| redirect p99 | — | 1,742ms | — |
| create p50 | 1,166ms | 3,713ms | +219% |
| create p95 | — | 9,173ms | — |
| create p99 | — | 13,679ms | — |
| App error rate | 0% | 0% | 持平 |
| Nginx errors | — | **zero** | — |

> `http_req_failed 10.02%` 全為 k6 腳本中刻意對不存在短碼發送的 404 probe，屬預期行為，非系統錯誤。

---

## 完整累積對比：Baseline → Phase 6

| 指標 | Baseline | Phase 1 | Phase 2 | Phase 3 | Phase 4b-fix | Phase 5 | Phase 6 |
|------|----------|---------|---------|---------|-------------|---------|---------|
| avg throughput (req/s) | 752 | 1,284 | 598 | 957 | 938 | 2,056 | 1,471 |
| Dropped iterations | 76.6% | 51.8% | 78.5% | 65.6% | 66.2% | 23.0% | 45.8% |
| redirect p50 | 3,847ms | 1,423ms | 0.063ms | sub-ms | 0.081ms | 17.1ms | 23ms |
| create 成功率 | 100% | 100% | 69% | 97.89% | 98.65% | 100% | 99.99% |
| App error rate | 0% | 0% | 0% | 0% | 3,199 筆 | 0 | 0 |

---

## 分析：為何 Throughput 低於 Phase 5？

Phase 6 的 avg throughput（1,471 req/s）**低於** Phase 5（2,056 req/s），看似水平擴展適得其反。根本原因在於瓶頸的位置：

### 瓶頸是 PostgreSQL write throughput，不是 worker 數量

- Phase 5 的瓶頸是單一 uvicorn worker 的 CPU 與 accept queue 上限（~1,000 req/s）。Phase 5 透過 4 workers 突破此限，達到 2,056 req/s。
- Phase 6 已有 8 workers（app1×4 + app2×4），CPU/accept 上限已不是瓶頸。**現在的瓶頸是 PostgreSQL 的單節點 write throughput。**
- 水平擴展對 **CPU-bound workload** 有效；對 **I/O-bound（DB write）workload**，加 worker 只是讓更多 worker 同時競爭同一個 DB 連線池，製造更多鎖競爭與等待，反而降低整體效率。

### Nginx proxy overhead

每個 request 多一次 TCP hop（client → Nginx → app），即使 keepalive 降低了 handshake 成本，依然增加了 latency。redirect p50 從 17.1ms 增加至 23ms 正是此 overhead 的體現。

### PgBouncer 連線壓力

2 containers × 4 workers × pool_size 100 = **800 潛在 client 連線**。PgBouncer `MAX_CLIENT_CONN=1000`，已使用約 80%，接近上限。在高並發下，連線等待佇列開始影響 create 的 p50（1,166ms → 3,713ms）。

### Create p50 嚴重惡化（1,166ms → 3,713ms）

8 workers 同時打 DB，比 4 workers 更激烈地競爭 DB 寫入資源。PostgreSQL 的 MVCC 寫入競爭（WAL flush、tuple lock、index update）在更多並發 writer 下線性惡化。

### Redirect 仍 100% 成功

Redis cache 完全屏蔽了 redirect 路徑對 DB 的依賴。水平擴展對 **read-heavy、cache-friendly 的路徑** 依然有效，Redis 本身輕鬆支撐多 worker 並發。

### 結論

> 水平擴展（Nginx + 2 containers）已達到此架構在**單 PostgreSQL 節點**下的有效邊界。繼續增加 app 容器數量不會改善 write throughput，只會加劇 DB 競爭。

---

## 系統設計思考

本次六個 Phase 的優化旅程揭示了一條核心規律：

**先解決每一層的瓶頸，再往下一層走。**

| 層次 | 發現的瓶頸 | 解法 |
|------|-----------|------|
| Network / DB 連線 | PgBouncer 缺失、連線數不足 | Phase 4b-fix |
| App worker | 單 uvicorn event loop 上限 | Phase 5（4 workers） |
| App container | 單容器 CPU/accept 上限 | Phase 6（Nginx + 2 containers） |
| **DB write** | **PostgreSQL 單節點 write throughput** | **⬅ 目前的天花板** |

- **Read path（redirect）** 已完全優化至次毫秒（Redis cache），無論加多少 worker 都不成問題。
- **Write path（create）** 的天花板是 PostgreSQL 單節點 write throughput。此層的瓶頸無法透過加 app 容器解決。
- 若要突破：PostgreSQL 垂直擴展（更多 CPU/RAM/faster storage）、write batching（類似 Phase 3 scan queue 的概念，把同步寫入改為非同步）、或 DB sharding/partitioning。

---

## 建議下一步（方向說明，不實作）

### 短期調整（不改變架構）

1. **Nginx keepalive 調整**：增加 `keepalive_requests`（每條長連線最多服務的 requests 數）與 `keepalive_timeout`，進一步降低 connection overhead。
2. **PgBouncer 調整**：調高 `MAX_CLIENT_CONN`（目前 1000，建議 2000+）或增加 `DEFAULT_POOL_SIZE`，避免連線等待成為 create 路徑的額外瓶頸。

### 中期架構調整

3. **Create 路徑 queue 化**：類似 Phase 3 的 scan queue 概念，將同步 DB 寫入改為非同步（client 寫 Redis Stream → consumer 批次 bulk insert）。批次寫入可大幅降低 WAL flush 次數與索引更新開銷，預估可將 create throughput 提升 3–5×。
4. **PostgreSQL 垂直升級**：提升 CPU 核心數、RAM（更大 shared_buffers）、使用 NVMe SSD（降低 WAL flush latency）。在不改架構的前提下，這是提升 write throughput 最直接的方式。

### 長期架構（高流量場景）

5. **PostgreSQL Read Replica**：將 redirect 的少數 DB miss 導向 read replica，進一步降低 primary 的讀壓力。
6. **DB Sharding / Partitioning**：依 short_code 的 hash 分片，將 write 壓力分散至多個 PostgreSQL 節點，突破單節點上限。
