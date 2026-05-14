# Tier 3A：Nginx 負載均衡 + 水平擴展

**實施日期：** 2026-05-14

## 改動摘要

在 Tier 2A（Redis 共享狀態）和 Tier 2B（異步交付）的基礎上，加入 nginx 反向代理，並測試 4 容器水平擴展的實際效果。目標是驗證：當單容器已達到 CPU/thread 上限時，橫向複製是否能線性提升吞吐量。

---

## 架構

```
k6 (→ :8080) → nginx → notification-api-1 (4 uvicorn workers)
                      → notification-api-2 (4 uvicorn workers)
                      → notification-api-3 (4 uvicorn workers)
                      → notification-api-4 (4 uvicorn workers)
                              ↓
                          Redis（shared state + delivery queue）
                              ↑
               delivery-worker（1 容器，Redis Stream consumer）
```

所有 API 容器與 delivery-worker 共享同一個 Redis 實例。nginx 做 TCP 長連線複用（keepalive），直接轉發 HTTP/1.1 至後端。

---

## nginx 設定重點

### `notification.conf`（單後端，基準測試用）

```nginx
upstream notification_backend {
    server notification-api:8000;
    keepalive 64;
    keepalive_requests 1000;
}

server {
    location / {
        proxy_http_version 1.1;
        proxy_set_header Connection "";   # 維持 keepalive，不傳 "close"
        proxy_pass http://notification_backend;
    }
}
```

`proxy_set_header Connection ""` 是關鍵：預設 nginx 會將 upstream 的 Connection header 設為 `close`，強制每個請求建立新 TCP 連線。清空這個 header 才能讓 upstream keepalive 生效，減少 TCP handshake 開銷 60–80%。

### `notification-scale.conf`（4 後端，水平擴展用）

```nginx
upstream notification_backends {
    server notification-api-1:8000;
    server notification-api-2:8000;
    server notification-api-3:8000;
    server notification-api-4:8000;
    keepalive 64;
    keepalive_requests 1000;
}
```

其餘設定與單後端版本相同。負載均衡演算法：預設 round-robin。

---

## 基準測試對比

**測試條件：** FAILURE_RATE=0、target 5000 RPS、600 VU 上限、k6 ramping-arrival-rate

| 配置 | 總 workers | 吞吐量 | POST /send p95 | GET /{id} p95 | 所有閾值通過？ |
|------|-----------|--------|--------------|-------------|-------------|
| 1 容器，1 worker | 1 | ~1473 RPS | 1.48s ❌ | 1.43s ❌ | No |
| 1 容器，4 workers（Tier 2A 基準） | 4 | ~2070 RPS | 466ms ✓ | 450ms ✓ | **Yes** |
| 4 容器 × 4 workers + nginx | 16 | ~2362 RPS | 590ms ❌ | 332ms ✓ | No |

---

## 核心洞察：READ 與 WRITE 的擴展非對稱性

### GET /{id} 線性擴展（-26%：450ms → 332ms）

GET 請求是純讀取操作：從 Redis 取一個 HASH。每個 API replica 各自持有獨立的 Redis 連線池，請求由 nginx 分發後，被選中的 replica 直接對 Redis 發出 HGETALL，不需要任何跨 replica 協調。

```
replica-1 → Redis HGETALL
replica-2 → Redis HGETALL   ← 4 個 replica 同時並行，無競爭
replica-3 → Redis HGETALL
replica-4 → Redis HGETALL
```

更多 replica = 更多並行讀取能力。在 IO-bound 的 Redis 讀取場景中，擴展效果接近線性。

### POST /send 在 nginx 下變慢（+27%：466ms → 590ms）

POST 請求的路徑明顯更複雜：

```
POST /send
  ├─ Redis: GET idempotency key（讀）
  ├─ Redis pipeline: HSET(PENDING) + SET(idempotency) + ZADD（寫）
  ├─ Redis Stream XADD（enqueue 給 delivery-worker）
  └─ Redis: HSET(SENT/FAILED)（等 worker 回報結果）
```

加入 nginx 後，寫入路徑有以下問題：

**1. 額外的連線跳躍**

每個請求多一個 nginx→backend 的 TCP 跳躍（約 1–2ms），在 5000 RPS 峰值時，nginx 的 accept queue 和 upstream 的 keepalive 連線池都處於高競爭狀態，排隊延遲從 1–2ms 放大到數十 ms。

**2. Round-robin 無法感知後端負載**

```
VU 1 → nginx → api-1 (已有 50 個 in-flight 請求)
VU 2 → nginx → api-2 (空閒)
VU 3 → nginx → api-3 (空閒)
VU 4 → nginx → api-1 (又分到忙碌的 api-1)  ← round-robin 不管這個
```

某個 replica 因為 Redis 連線池競爭而暫時變慢時，其 keepalive 連線池中的請求全部排隊，但 round-robin 仍然持續分發新請求過去。

**3. 600 VU × 連線池 overhead 的乘積效應**

在 600 VU 同時並發時，nginx 維護 64 個對每個 backend 的 keepalive 連線（4 × 64 = 256 條長連線），同時管理從 k6 進來的 600 個 client 連線。在這個規模下，nginx 的連線管理本身就消耗了可觀的 CPU 時間，加重了整體排隊延遲。

---

## 踩坑記錄：`podman-compose --scale` 的陷阱

**問題描述：**

```bash
podman-compose up --scale notification-api=4
```

預期啟動 4 個 `notification-api` 容器，實際只啟動了 1 個。

**根本原因：**

`podman-compose` 的 `--scale` 支援不完整，無法正確處理多個副本的 container name 衝突與 network alias。與 Docker Compose v2 的行為不同。

**修復方式：**

改為在 `docker-compose.nginx-scale.yml` 中明確定義 4 個獨立服務：

```yaml
services:
  notification-api-1:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

  notification-api-2:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

  notification-api-3:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

  notification-api-4:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

**學到的教訓：** `podman-compose` 和 `docker-compose` 的行為差異在 `--scale` 這個功能上最為明顯。任何依賴 `--scale` 的腳本在 Podman 環境都需要改為明確定義多個服務。

---

## 踩坑記錄：workers 數量是關鍵

**第一次測試（4 容器 × 1 worker）：1473 RPS，p95 = 1.48s**

忘記在每個 replica 的 command 加上 `--workers 4`，每個容器只有 1 個 uvicorn worker：

```yaml
# 錯誤：只有 1 個 worker，容器的 CPU 核心都閒著
command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

4 容器 × 1 worker = 4 個 worker，與「1 容器 × 1 worker」的基準相同，但多了 nginx 的額外開銷，所以比單容器更慢。

**修正後（4 容器 × 4 workers）：2362 RPS，p95 = 590ms**

```yaml
# 正確：明確指定 4 個 worker
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

4 容器 × 4 workers = 16 個 worker，吞吐量從 1473 RPS 提升至 2362 RPS（+60%）。

---

## nginx 的價值（即使不能解決 POST p95）

雖然 nginx 讓 POST /send p95 略微惡化，但在真實生產環境中，nginx 仍然是必要的：

| 功能 | 說明 |
|------|------|
| 穩定的外部 endpoint | 不管後端啟動幾個 replica，對外只暴露 :8080 一個端口 |
| Zero-downtime rolling restart | 可以逐一重啟後端容器，nginx 自動跳過正在重啟的節點 |
| keepalive 減少 TCP handshake | 對後端的長連線，節省 60–80% 的連線建立開銷 |
| /nginx-health endpoint | 供 load balancer probe 用，不需要暴露後端 /health |
| TLS termination | HTTPS 在 nginx 層卸載，後端用 plain HTTP，簡化後端設定 |
| Request logging & access log | 集中的入口點，所有請求都有統一的 log 格式 |

---

## 修復路徑

Tier 3A 揭示了兩個問題，各有對應的修復方向：

### 問題 1：round-robin 無法感知負載

**修復：** nginx upstream 改用 `least_conn`

```nginx
upstream notification_backends {
    least_conn;          # 分發給當前 active connections 最少的後端
    server notification-api-1:8000;
    ...
}
```

`least_conn` 會感知哪個 backend 的 in-flight 請求最少，避免 round-robin 盲目分發到已過載的節點。

### 問題 2：同步 Python routes 下，IO-bound 工作無法充分利用多 worker

**修復：** 路由改為 `async def` + `redis.asyncio`（Tier 3B）

```python
# 同步版本（Tier 3A）：uvicorn worker 的 thread 在 await Redis 期間被佔用
def get_notification(id: str):
    return store.get(id)     # 同步 Redis 呼叫，blocking

# 異步版本（Tier 3B）：coroutine 在 await 期間釋放 thread，event loop 可服務其他請求
async def get_notification(id: str):
    return await store.aget(id)  # 非阻塞，await 期間 worker 可處理其他 coroutine
```

在 IO-bound 場景（絕大多數時間都在等 Redis），`async def` + `asyncio` client 讓每個 uvicorn worker 的 event loop 可以同時服務數百個 coroutine，而不是一個 thread 只能服務一個同步 Redis 呼叫。

---

## 結論

Tier 3A 的核心發現是：**水平擴展對讀寫的效果是非對稱的。**

對於 GET（純讀）：nginx + 4 replica 線性提升讀取並行度，p95 從 450ms 降至 332ms（-26%）。

對於 POST（寫 + 排隊 + 等結果）：nginx 增加一個連線跳躍，在 600 VU 高並發下 round-robin 無法平衡寫入壓力，p95 從 466ms 升至 590ms（+27%），超出閾值。

**真正的問題不是 nginx，而是同步路由在 IO-bound 場景下的浪費**：每個 uvicorn thread 都在「等 Redis」，多開容器只是多了更多在等 Redis 的 thread，而不是更有效率地利用 Redis 的 IO 容量。**Tier 3B 的 async routes 才是治本的方向。**
