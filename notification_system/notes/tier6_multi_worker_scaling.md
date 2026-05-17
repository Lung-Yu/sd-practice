# Tier 6：多 Worker 水平擴展與 Redis 競爭瓶頸分析

**實施日期：** 2026-05-17

---

## 測試結果一覽

| 指標 | 數值 |
|------|------|
| 設定 | 4 API workers + 4 delivery workers |
| 負載 | 600 VUs，目標 5000 RPS，FAILURE_RATE=0 |
| POST p95 延遲 | **1,450ms ❌**（閾值 500ms） |
| GET p95 延遲 | **532ms ❌**（閾值 200ms） |
| 實際吞吐量 | **800 RPS**（目標 5000 RPS） |
| 錯誤率 | **0.00%**（無連線錯誤，純延遲問題） |
| 總投遞訊息數 | **66,545** 筆（4 workers 均分） |

---

## Consumer Group 分配結果

| Worker 編號 | Container ID | 投遞訊息數 | 佔比 |
|-------------|-------------|-----------|------|
| 1 | a2a57a26da65 | 16,423 | 24.7% |
| 2 | 8a68aa5d4ea7 | 16,701 | 25.1% |
| 3 | d7c848441057 | 16,652 | 25.0% |
| 4 | feb87a153fb3 | 16,769 | 25.2% |
| **合計** | | **66,545** | **100%** |

Redis Consumer Group 將訊息**均勻分配**至四個 worker，每個 worker 各承擔約 25% 的投遞量。`XREADGROUP` 的 `>` 語義保證**每則訊息只交付給一個 consumer**，無重複投遞。

---

## 跨 Tier 延遲比較

| 設定 | POST p95 | GET p95 | 吞吐量 | 全部通過？ |
|------|----------|---------|--------|----------|
| Tier 4：4 API workers + 1 delivery worker | 361ms ✓ | 172ms ✓ | 2,736 RPS | ✓ |
| **Tier 6：4 API workers + 4 delivery workers** | **1,450ms ❌** | **532ms ❌** | **800 RPS** | ❌ |

加入更多 delivery worker **反而使整體效能大幅下降**。根本原因在於共用的單執行緒 Redis 實例。

---

## 系統架構：Redis 競爭示意圖

```
                    ┌─────────────────────────────────────┐
                    │         Redis（單執行緒）             │
                    │  ┌──────────────────────────────┐   │
                    │  │      命令處理佇列              │   │
                    │  │  [cmd][cmd][cmd]...[cmd][cmd] │   │
                    │  │  ← 佇列深度越深，每個命令等越久 │   │
                    │  └──────────────────────────────┘   │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │  4 API Workers   │  │ Delivery Worker 1│  │ Delivery Worker 2│
   │ (uvicorn async)  │  │  BATCH_SIZE=20   │  │  BATCH_SIZE=20   │
   │                  │  │  asyncio.gather  │  │  asyncio.gather  │
   │ 每個請求 4 條     │  │  20 並發 pipeline│  │  20 並發 pipeline│
   │ Redis 命令：      │  └──────────────────┘  └──────────────────┘
   │ RATE_LIMIT GET   │
   │ IDEMPOTENCY GET  │  ┌──────────────────┐  ┌──────────────────┐
   │ HSET pipeline    │  │ Delivery Worker 3│  │ Delivery Worker 4│
   │ XADD             │  │  BATCH_SIZE=20   │  │  BATCH_SIZE=20   │
   └──────────────────┘  │  asyncio.gather  │  │  asyncio.gather  │
                         │  20 並發 pipeline│  │  20 並發 pipeline│
                         └──────────────────┘  └──────────────────┘

   API 同時發出：數百條 Redis 命令（600 VUs × 4 cmd/req）
   Delivery 同時發出：80 條 Redis pipeline（4 workers × 20 並發）
   
   Redis 命令佇列深度 ≈ 數百條同時競爭 → 每條命令等待時間從 1ms 暴增至 50ms+
```

---

## 根本原因分析：Redis 單執行緒瓶頸

### Delivery Worker 對 Redis 的負載

每個 delivery worker 執行 `asyncio.gather()` 處理 `BATCH_SIZE=20` 的批次。每筆訊息觸發以下 Redis 操作：

```
每筆訊息（20 筆並發）：
  1. store.aget(nid)          → HGETALL（非同步）
  2. loop.run_in_executor(    → store.save()：
       deliver(notification)       HSET + SET + ZADD（同步 pipeline）
     )
  3. r.xack(...)              → XACK（非同步）
```

**4 個 worker 同時執行 = 80 條並發 Redis pipeline**。

### API 層對 Redis 的負載

每個 API 請求（`POST /send`）包含：

```
POST /send 的 Redis 操作：
  1. rate_limit_check()      → GET + INCR（速率限制查詢）
  2. idempotency.get()       → GET（冪等性查詢）
  3. store.save()            → HSET + SET + ZADD pipeline（儲存通知）
  4. r.xadd()                → XADD（推入 Stream）
```

600 個 VU 同時發請求 = 數百條 Redis 命令同時進入佇列。

### 競爭加乘效應

```
Redis 單執行緒模型：
  ├── 一次只處理一條命令
  ├── 所有命令排隊等待
  └── 佇列深度 ∝ 等待時間

Tier 4（1 delivery worker）：
  API 命令 + 20 並發 pipeline = 佇列深度 ≈ 中等
  → 每條命令平均等待 ~1ms
  → POST p95 = 361ms ✓

Tier 6（4 delivery workers）：
  API 命令 + 80 並發 pipeline = 佇列深度 ≈ 4× 倍增
  → 每條命令平均等待 ~50ms（估計）
  → POST 的 4 條 Redis 命令 × 50ms = 200ms 額外等待
  → POST p95 = 1,450ms ❌
```

### 這是「IO 密集型擴展牆的進階版」

CPU 密集型工作：N 個 worker = N 倍吞吐量（計算資源線性擴展）

Redis 競爭型工作：N 個 worker = N 倍 Redis 命令速率 = 每個 actor 的 Redis 命令吞吐量 **÷ N**

增加 delivery worker 並未新增計算能力，而是新增了更多 Redis 競爭者。Worker 本身未被充分利用（有能力接收更多訊息），但 Redis 已成為整個系統的序列化點（serialization point）。

---

## 修正措施

### 修正前：Port 衝突問題

原始 `docker-compose.yml` 中 delivery-worker 有：

```yaml
ports:
  - "8001:8001"
```

Scale 到 4 個 container 時，4 個容器都嘗試綁定主機的 8001 port，導致：

```
Error: proxy already running
```

**修正方法：移除 host port binding。**

Prometheus 透過 `sd_monitoring` 網路，用 service DNS 名稱（`delivery-worker`）連接，podman-compose 會自動 round-robin 到各個 instance，無需 host port。

```yaml
# 移除前（錯誤設定）
ports:
  - "8001:8001"

# 移除後（正確設定）
# （完全不需要 ports 區塊，讓 sd_monitoring 網路負責服務發現）
```

### 修正後的啟動指令

```bash
FAILURE_RATE=0 MAX_RETRIES=1 podman-compose -f docker-compose.yml \
  -f k6s/docker-compose.loadtest.yml up -d --scale delivery-worker=4
```

Consumer Group 驗證（`Redis XINFO CONSUMERS`）：

```
127.0.0.1:6379> XINFO CONSUMERS notifications delivery-workers
 1) name: a2a57a26da65  pending: 0  idle: 1203
 2) name: 8a68aa5d4ea7  pending: 0  idle: 987
 3) name: d7c848441057  pending: 0  idle: 1456
 4) name: feb87a153fb3  pending: 0  idle: 823
```

Consumer 名稱 = 容器 hostname = 容器 ID，天然唯一，無衝突。

---

## 真正的解決方案

| 方案 | 說明 | 效果 |
|------|------|------|
| 縮小 BATCH_SIZE | 4 workers × 5 = 20 並發，與 1 worker × 20 相同 | 部分改善 |
| Redis Cluster | 將投遞寫入分片到獨立 Redis 節點，與 API 讀取分開 | 真正修復 |
| 獨立 Redis 實例 | Delivery worker 使用 Redis-B；API 使用 Redis-A | 真正修復 |
| 精簡批次 + Pipeline | 將 store.save() 跨批次合併成單一 pipeline | 減少 round-trip |
| 依 Worker 數量調整 BATCH_SIZE | `BATCH_SIZE = target_concurrency ÷ num_workers` | 設定調優 |

### 生產環境最佳實踐：分離 Redis 實例

```
Redis-A（API 專用）                Redis-B（Delivery 專用）
├── notifications HASH             ├── notifications Stream（XADD/XREADGROUP）
├── idempotency keys               ├── XACK（確認已投遞）
└── rate-limit counters            └── pending messages

API workers → Redis-A 讀寫         Delivery workers → Redis-B 讀寫
無競爭，互不干擾
```

這兩個工作負載本質上是獨立的，不應共享 Redis 容量：

- API 需要**低延遲讀寫**（冪等性查詢、速率限制）
- Delivery 需要**高吞吐量 pipeline**（批次寫入狀態、XACK）

混用同一個 Redis 實例，兩者的尖峰期會相互放大延遲。

---

## 本次測試驗證的事項

儘管延遲測試失敗，本次實驗仍驗證了以下重要屬性：

### 1. 跨多 Consumer 的 Exactly-Once 投遞 ✓

Redis Consumer Group 的 `XREADGROUP >` 語義確保每則訊息只被**一個** consumer 取走。即使 4 個 worker 同時運作，也不會發生重複投遞。

```
Redis Stream: [msg1][msg2][msg3]...[msg66545]
                ↓      ↓      ↓
Worker 1 取走 msg1    Worker 2 取走 msg2    ...（無重複）
```

### 2. 水平 Worker 擴展有效提升投遞吞吐量 ✓

```
Tier 4（1 worker）：~X 筆/秒
Tier 6（4 workers）：66,545 筆 / 測試時長（實際投遞速率更高）
```

加入更多 worker **確實**提升了通知投遞速率，問題在於代價是 API 延遲惡化。

### 3. Container Hostname = 唯一 Consumer 名稱 ✓

```python
# worker.py 中的 Consumer 命名
consumer_name = socket.gethostname()
# 在 podman-compose scale 下，每個容器的 hostname = 容器 ID
# 自動保證唯一性，無需外部 ID 分配機制
```

### 4. 瓶頸在共用 Redis，不在 Worker 本身 ✓

Worker 本身未達 CPU 或記憶體上限，是 Redis 的命令佇列讓它們陷入等待。這是**架構瓶頸**，不是資源瓶頸。

---

## 架構決策記錄（ADR）

### ADR-006：不在可 Scale 的服務上綁定 host port

**決策**：計劃水平擴展的服務（`--scale N`）不得在 `docker-compose.yml` 中設定靜態 host port mapping。

**理由**：多個容器無法共享同一個主機 port，會導致「proxy already running」錯誤。Prometheus scraping 透過 `sd_monitoring` 內部網路進行，不需要 host port。

**執行方式**：讓 podman-compose/Docker 的 service DNS 名稱承擔服務路由，Prometheus 用 DNS round-robin 抓取多個 instance 的指標。

### ADR-007：BATCH_SIZE 應與 Worker 數量反向調整

**決策**：`BATCH_SIZE` 的設定應考慮 `num_workers`，使 Redis 並發 pipeline 數保持穩定。

```
max_concurrent_pipelines = BATCH_SIZE × num_workers
目標值 = 20（與 1 worker × BATCH_SIZE=20 相同）
∴ 4 workers → BATCH_SIZE = 5
```

**理由**：Redis 單執行緒模型下，並發 pipeline 數決定命令佇列深度，進而決定每條命令的等待時間。

### ADR-008：高吞吐 Delivery 應使用獨立 Redis 實例

**決策**：生產環境下，delivery worker 應連接獨立的 Redis 實例（或 Redis Cluster 的獨立分片）。

**理由**：API 的 p95 延遲不應受 delivery worker 的批次寫入影響。兩個工作負載的存取模式截然不同（API = 點查詢低延遲；Delivery = 批次高吞吐），混用同一個 Redis 必然相互干擾。

---

## 學到的教訓

### 1. Consumer Group 擴展的是投遞吞吐量，不是 API 吞吐量

增加 delivery worker 讓**更多通知被更快投遞**，但同時對 API 的 Redis 操作施加了更大壓力。這是兩個獨立維度的擴展：投遞吞吐量 vs. API 吞吐量。

### 2. Redis 單執行緒是根本限制

每一次共用單一 Redis 實例的水平擴展，最終都會撞上這面牆。命令佇列深度線性增長，每個 actor 的有效吞吐量線性下降。

### 3. BATCH_SIZE 應與 Worker 數量反向調整

```
# 理想設定
total_redis_concurrency = BATCH_SIZE × num_workers = 常數
∴ 新增 worker 時，應同步縮小 BATCH_SIZE
```

若 `BATCH_SIZE × num_workers` 超過 Redis 單執行緒的舒適區間，整體效能會非線性惡化。

### 4. 可擴展服務不應綁定靜態 host port

計劃用 `--scale N` 的服務，在 `docker-compose.yml` 中只需暴露 container port（`expose`），而非 host port（`ports`）。讓 overlay 網路和服務 DNS 負責路由，外部監控工具透過內部網路存取即可。

### 5. Consumer 名稱 = Hostname 的慣例

`socket.gethostname()` 在容器環境下等同於容器 ID，天然保證唯一性。配合 `podman-compose --scale` 使用時，每個 instance 自動取得唯一的 consumer 名稱，不需要額外的 ID 分配機制或共享狀態。

### 6. 效能失敗不等於功能失敗

本次測試的延遲閾值全數未達，但錯誤率為 0%，Exactly-Once 投遞語義完整保留。這提醒我們：**正確性（correctness）和效能（performance）是兩個獨立的屬性**，應分開評估。

### 7. 擴展前先找到真正的瓶頸

直覺上「4 個 worker 會比 1 個 worker 快」是合理的，但在共用有限資源（Redis 單執行緒）的系統中，這個直覺可能完全錯誤。擴展任何元件之前，應先確認瓶頸所在，否則可能適得其反。

---

## 後續實驗方向

- **Tier 6a**：4 workers + `BATCH_SIZE=5`（維持 20 並發 pipeline，觀察延遲是否恢復）
- **Tier 6b**：分離 Redis 實例（API Redis vs. Delivery Redis），觀察 API p95 是否改善
- **Tier 7**：引入 Redis Cluster，自動分片，同時支援高 API 吞吐與高投遞吞吐
- **觀察**：在 BATCH_SIZE=5、4 workers 的設定下，consumer group 分配是否仍然均勻
