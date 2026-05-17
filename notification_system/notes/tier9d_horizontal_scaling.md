# Tier 9D — Horizontal API Scaling Revisit

## 目標

重新評估：現在系統有 async routes + Redis + async workers，  
增加 uvicorn workers 數量能帶來多少吞吐量提升？

---

## 實驗設計

- **控制組**：1 uvicorn worker（asyncio 單事件迴圈）
- **實驗組**：4 uvicorn workers（4 個 Python 程序，各自事件迴圈）
- **入口**：nginx (:8080) → notification-api:8000
- **後端**：2 Redis（primary state + delivery stream）+ 4 delivery workers
- **FAILURE_RATE**：0.20（預設）

---

## 測試結果

### 吞吐量對比（POST /send 單端點）

| Concurrency | 1 Worker (RPS) | 4 Workers (RPS) | 倍數 |
|-------------|---------------|-----------------|------|
| 50 | 1,189 | 1,380 | 1.16× |
| 100 | 1,435 | 1,354 | 0.94× |
| 200 | 1,481 | 1,450 | 0.98× |
| 300 | 1,538 | 1,625 | 1.06× |

**結論：4 workers ≈ 1 worker（差異在噪音範圍內）**

---

## 為什麼沒有提升？

### 根本原因：I/O-bound async workload

FastAPI + aioredis 是純 async I/O-bound 架構：

```
POST /send → await r.incr(key) → await r.get(key) → await pipe.execute() → await r.xadd()
```

每個步驟都是 `await`，Python 事件迴圈在等 Redis 回應時可以處理其他請求。

```
1 uvicorn worker + asyncio:
  Request 1 → await Redis → (switch to Request 2 while waiting)
  Request 2 → await Redis → (switch to Request 3)
  ...
  [concurrent I/O with single thread]
```

**一個事件迴圈就能並發處理數百個 Redis 請求。**

### 多 worker 的問題

```
4 uvicorn workers:
  Worker 1 → handles requests 1-N/4
  Worker 2 → handles requests N/4+1 to N/2
  ...
  [4 independent event loops, each with 1/4 of connections]
```

- 每個 worker 的 Redis connection pool 各自獨立 → pool 資源被分散
- worker 間有 process fork overhead（內存複製、signal handling）
- 每個 worker 只有 25% 的連接 → 利用率反而更低

---

## 何時多 worker 有效？

| 場景 | 多 worker 是否有效 |
|------|------------------|
| **I/O-bound async（本系統）** | ❌ 無效，asyncio 已經 concurrent |
| **CPU-bound**（JSON 解析大量資料、加密）| ✅ 有效，打破 GIL |
| **Sync blocking code 在 request path** | ✅ 有效（但應改成 async） |
| **需要 fault isolation** | ✅ 有效（1 worker 崩潰不影響其他）|
| **5 萬+ RPS 時單個事件迴圈達到極限** | ✅ 有效 |

---

## 實際瓶頸在哪裡？

在 1,000-1,500 RPS 附近，瓶頸是：

1. **Redis 吞吐量**：每個 POST /send 需要 4-6 個 Redis 命令（rate limit + dedup + save + enqueue）
2. **Python thread pool**：`deliver()` 是 sync 函數，用 `run_in_executor` 跑在 worker 的 thread pool 裡，thread pool 容量有限
3. **nginx keepalive 連接數**：`keepalive 64` 限制後端連接重用

**不是**：Python event loop CPU 算力。

---

## 正確的水平擴展策略

```
理論：
  1 instance × 1000 RPS = 1000 RPS
  4 instances × 1000 RPS = 4000 RPS（理論線性）

實際（I/O-bound）：
  擴展瓶頸點，不是 API instance 數量
  → Redis cluster（多 shard）
  → 增加 delivery workers（交付吞吐）
  → 更大的 Redis connection pool
  → 更快的 Redis 機器
```

水平擴展 API 實例的真正目的：
1. **高可用性（HA）**：一個 instance 掛掉，其他繼續服務
2. **地理分散**：多個 region 各自有 API instance
3. **CPU-intensive 工作負載**（本系統不適用）

---

## Podman-compose 水平擴展的限制

podman-compose `--scale notification-api=4` 要求：
- notification-api 不能綁定固定 host port（`ports: "8000:8000"` 防止多實例）
- 需要把 port binding 移除，讓 nginx 成為唯一入口

解法：`docker-compose.scaled.yml` override，設 `ports: []`  
問題：podman-compose 對 pod 網路的 scale 支援不完整，DNS 解析可能失敗

**生產建議**：用 Kubernetes Deployment replicas，不是 docker-compose scale。

---

## 關鍵結論

> 對於 async I/O-bound FastAPI 應用，
> 擴展 uvicorn worker 數量不能線性提升吞吐量。
> 真正的瓶頸是 Redis 吞吐量，而不是 Python event loop。
> 要提升至 5000+ RPS，應優先優化：
> 1. Redis pipeline（減少 RTT）← 本系統已做
> 2. Redis connection pool 大小
> 3. 更快的 Redis 硬體或 cluster
