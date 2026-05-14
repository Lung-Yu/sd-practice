# Tech Note — Notification System 技術指令參考

> 本文件整理 Notification System 專案從 Tier 1 到 Tier 3 實作過程中用到的所有工具、指令與技術細節。  
> 所有標注「# 本專案實測」的指令均在本專案環境中實際執行過。  
> 最後更新：2026-05-14

---

## 目錄

1. [環境管理（Podman）](#一環境管理podman)
2. [負載測試（k6）](#二負載測試k6)
3. [Redis 操作指令](#三redis-操作指令)
4. [FastAPI / uvicorn 啟動設定](#四fastapi--uvicorn-啟動設定)
5. [Prometheus / Grafana 指令](#五prometheus--grafana-指令)
6. [Admin API 端點](#六admin-api-端點)
7. [環境變數說明](#七環境變數說明)
8. [附錄：常見 Debug 指令集](#附錄常見-debug-指令集)

---

## 一、環境管理（Podman）

### 重要原則

**`down` 必須在 `up --build` 之前執行。** podman-compose 不會自動替換正在執行的容器。若跳過 `down` 直接 `up --build`，舊容器仍在跑，新 image 不會被套用。

### 基本模式（單容器，無 nginx）

```bash
# 啟動（4 uvicorn workers，直接 port 8000）
podman-compose -f docker-compose.yml up -d --build      # 本專案實測

# 停止
podman-compose -f docker-compose.yml down

# 重建（先 down 再 up）
podman-compose -f docker-compose.yml down && \
podman-compose -f docker-compose.yml up -d --build
```

### 高負載測試模式（4 uvicorn workers + delivery worker）

```bash
# 啟動（FAILURE_RATE=0，適合 k6 吞吐量測試）
podman-compose -f docker-compose.yml -f k6s/docker-compose.loadtest.yml up -d --build  # 本專案實測

# 停止
podman-compose -f docker-compose.yml -f k6s/docker-compose.loadtest.yml down
```

### nginx 水平擴展模式（4 app containers + nginx + delivery worker）

```bash
# 啟動（4 notification-api 容器，nginx 在 port 8080 做 LB）
podman-compose \
  -f docker-compose.yml \
  -f k6s/docker-compose.loadtest.yml \
  -f docker-compose.nginx-scale.yml \
  up -d --build                                           # 本專案實測

# 停止
podman-compose \
  -f docker-compose.yml \
  -f k6s/docker-compose.loadtest.yml \
  -f docker-compose.nginx-scale.yml \
  down
```

### 查看容器狀態

```bash
# 列出所有 notification_system 相關容器
podman ps --filter name=notification_system

# 查看 app 容器 log（即時串流）
podman logs -f notification_system_notification-api_1

# 查看 delivery worker log
podman logs -f notification_system_delivery-worker_1

# 查看 nginx log
podman logs -f notification_system_nginx_1
```

---

## 二、負載測試（k6）

### 執行模式

```bash
# 單容器（直接打 port 8000，不過 nginx）
BASE_URL=http://localhost:8000 k6 run k6s/k6.js           # 本專案實測

# 透過 nginx（port 8080，nginx 做 round-robin 或 least_conn）
BASE_URL=http://localhost:8080 k6 run k6s/k6.js           # 本專案實測

# 帶 Prometheus remote write（即時寫入 Grafana 觀察）
K6_PROMETHEUS_RW_SERVER_URL=http://localhost:9090/api/v1/write \
K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM=false \
k6 run -o experimental-prometheus-rw k6s/k6.js            # 本專案實測
```

### k6 腳本架構

**Executor：ramping-arrival-rate**

| 階段 | 持續時間 | RPS |
|------|----------|-----|
| ramp-up | 30s | 0 → 5000 |
| sustain | 60s | 5000 |

```javascript
// 關鍵參數
preAllocatedVUs: 250,
maxVUs: 600,
```

**setup() 函數：**  
在正式測試前，setup() 用 POST /send 預先建立 200 個 notification，取得 ID 清單，供 GET /{id} 測試用。

> 注意：若 Redis 正在 AOF replay，setup() 在此期間執行將得到 `seedIds = []`，導致整個測試階段的 GET 檢查全部用 fallback UUID → 500 / 0% 通過率。解法：確保 API worker 啟動後已通過 Redis readiness check 再執行 k6。

**Threshold 設定：**

```javascript
// 每個端點獨立 Trend
'notification_send_duration{...}': ['p(95)<500', 'p(99)<1000'],
'notification_get_duration{...}':  ['p(95)<500', 'p(99)<1000'],
'notification_list_duration{...}': ['p(95)<500', 'p(99)<1000'],
// 全局錯誤率
'errors': ['rate<0.01'],
```

---

## 三、Redis 操作指令

### 連線

```bash
# 進入 redis-cli（互動模式）
podman exec -it notification_system_redis_1 redis-cli     # 本專案實測

# 單次執行指令
podman exec notification_system_redis_1 redis-cli <command>
```

### Delivery Stream 操作

```bash
# 查看 stream 累積深度（k6 高負載後可能積壓）
redis-cli xlen notifications:delivery

# 修剪 stream（清除 k6 遺留的大量積壓訊息）
redis-cli xtrim notifications:delivery MAXLEN 100          # 本專案實測

# 查看 consumer group 狀態（PEL 大小、最後交付時間）
redis-cli xinfo groups notifications:delivery

# 查看某個 consumer 的 pending messages
redis-cli xpending notifications:delivery delivery-workers - + 10
```

### DLQ（Dead-Letter Queue）操作

```bash
# 查看 DLQ 深度
redis-cli llen notifications:dlq

# 非破壞性查看最前面 5 筆
redis-cli lrange notifications:dlq 0 4
```

### Notification Hash 操作

```bash
# 查看完整 notification 資料
redis-cli hgetall "notification:{id}"

# 查看 idempotency key
redis-cli get "idempotency:{sha256_key}"

# 查看某用戶的 notification ID 清單（ZSET，score = timestamp）
redis-cli zrange "user:{user_id}:notifications" 0 -1 withscores
```

### Rate Limit 操作

```bash
# 查看某用戶當前 bucket 的計數
redis-cli get "ratelimit:{user_id}:{epoch_bucket}"

# 查看 key TTL
redis-cli ttl "ratelimit:{user_id}:{epoch_bucket}"
```

### 一般診斷

```bash
# 查看連線數（clients）
redis-cli info clients

# 查看記憶體使用
redis-cli info memory

# 查看所有 notification key 數量
redis-cli keys "notification:*" | wc -l

# PING（確認 Redis 已就緒，AOF replay 完成）
redis-cli ping   # → PONG 代表就緒
```

---

## 四、FastAPI / uvicorn 啟動設定

### API server

```bash
# 4 workers（本專案 Dockerfile 預設）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

**為什麼選 4 workers？**  
在 600 VU 負載、IO-bound async 路由下，4 workers ≈ 3072 RPS（實測）。超過 4 workers 後，Redis 連線池競爭帶來的額外開銷大於並行效益——diminishing returns 在本硬體上約在 4 workers 觸底。

### Delivery worker

```bash
# 獨立 delivery worker（Tier 2C 起引入）
python -m app.worker
```

### Async vs Sync 路由

| 路由類型 | 執行位置 | IO-bound 效益 |
|----------|----------|---------------|
| `def route()` (sync) | anyio thread pool（最多 ~40 threads/worker） | 差：thread slot 成為瓶頸 |
| `async def route()` + `redis.asyncio` | event loop coroutine | 佳：await 時 event loop 可服務其他請求 |

**實測差異（單容器，4 workers）：**

| 指標 | Sync（Tier 2C） | Async（Tier 3B） | 改善 |
|------|----------------|----------------|------|
| POST p95 | 466ms | 283ms | −39% |
| GET p95 | 450ms | 137ms | −69% |
| 吞吐量 | ~2070 RPS | ~3072 RPS | +48% |

**連線池大小：** async 模式下，pool 大小應等於「同時在等 IO 的 coroutine 峰值數量」，而非 worker 數量。本專案設為 `max_connections=100`（每個 uvicorn worker 獨立的 asyncio Redis 連線池）。

---

## 五、Prometheus / Grafana 指令

### 啟動監控（從 repo root）

```bash
# 啟動 Prometheus（:9090）與 Grafana（:3000）
cd .. && ./scripts/monitoring.sh start                     # 本專案實測

# 停止
cd .. && ./scripts/monitoring.sh stop
```

### 驗證 metrics 端點

```bash
# 查看 app 暴露的所有 Prometheus metrics
curl http://localhost:8000/metrics

# 篩選關鍵 counter
curl -s http://localhost:8000/metrics | grep notifications_sent_total
curl -s http://localhost:8000/metrics | grep circuit_breaker_trips_total
curl -s http://localhost:8000/metrics | grep rate_limit_hits_total
```

### 關鍵 Metrics 清單

| Metric 名稱 | 類型 | 說明 |
|------------|------|------|
| `notifications_sent_total` | Counter | 成功交付次數（by channel） |
| `notification_delivery_duration_seconds` | Histogram | 完整交付耗時（含 retry） |
| `circuit_breaker_trips_total` | Counter | 熔斷器觸發次數（by channel） |
| `rate_limit_hits_total` | Counter | 429 觸發次數（by user） |
| `dlq_depth` | Gauge | DLQ 目前深度 |

### Grafana

- URL：http://localhost:3000（帳號 admin / 密碼 admin）
- Dashboard 路徑：`monitoring/grafana/dashboards/app-notification.json`（7 個 panel）
- Dashboard 名稱：**Notification System**

---

## 六、Admin API 端點

所有 admin 端點無需認證（本專案為學習用途，生產環境應加 auth middleware）。

```bash
# 查看熔斷器狀態（CLOSED / OPEN / HALF_OPEN）
curl -s http://localhost:8000/admin/health/channels | python3 -m json.tool

# 查看 DLQ 深度 + 前幾筆非破壞性 peek
curl -s http://localhost:8000/admin/dlq | python3 -m json.tool

# 從 DLQ 重新排入 delivery stream（N 筆）
curl -s -X POST "http://localhost:8000/admin/dlq/retry?count=5" | python3 -m json.tool
```

---

## 七、環境變數說明

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `""` | Redis 連線 URL；空字串 = in-memory store（Tier 1） |
| `FAILURE_RATE` | `0.20` | Channel 失敗率模擬（0.0 = 完全不失敗，適合吞吐量測試） |
| `MAX_RETRIES` | `3` | 每次交付最多嘗試次數 |
| `RETRY_BASE_DELAY_S` | `0.1` | 指數退避基礎延遲（秒） |
| `ATTEMPT_TIMEOUT_S` | `5.0` | 單次嘗試 timeout（秒） |
| `CB_FAILURE_THRESHOLD` | `5` | 連續失敗幾次觸發熔斷器 |
| `CB_RECOVERY_SECONDS` | `30.0` | OPEN → HALF_OPEN 的等待時間（秒） |
| `RATE_LIMIT_PER_USER` | `100` | 每用戶每時間窗口最大請求數 |
| `RATE_LIMIT_WINDOW_S` | `60` | Rate limit 時間窗口（秒） |

**k6s/docker-compose.loadtest.yml 覆蓋設定：**

```yaml
environment:
  - FAILURE_RATE=0       # 移除失敗模擬，測量純吞吐量上限
  - MAX_RETRIES=3
  - REDIS_URL=redis://redis:6379
```

---

## 附錄：常見 Debug 指令集

### Smoke Test

```bash
# POST /send — 發送一則通知
curl -s -X POST http://localhost:8000/api/notifications/send \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","message":"hello","topic":"test","channel":"email"}' \
  | python3 -m json.tool

# GET /{id} — 查看通知狀態（將 {id} 換成上方回傳的 id）
curl -s http://localhost:8000/api/notifications/{id} | python3 -m json.tool

# GET list — 列出某用戶所有通知
curl -s "http://localhost:8000/api/notifications/?user_id=u1" | python3 -m json.tool
```

### Rate Limit 測試

```bash
# 快速送出 110 次請求，預期第 101~110 次收到 429
for i in $(seq 1 110); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST http://localhost:8000/api/notifications/send \
    -H "Content-Type: application/json" \
    -d '{"user_id":"rate_test","message":"msg","topic":"t","channel":"email"}'
done | sort | uniq -c
```

### Nginx 健康確認

```bash
# nginx health check endpoint（Tier 3A 起可用）
curl -s http://localhost:8080/nginx-health

# 確認 nginx 能路由到後端
curl -s http://localhost:8080/api/notifications/ | python3 -m json.tool
```

### 確認 Redis 已就緒（AOF replay 完成）

```bash
# 應回傳 PONG；若 Redis 仍在 replay 則回傳錯誤
podman exec notification_system_redis_1 redis-cli ping
```
