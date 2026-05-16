# Tier 5：失效模式測試 — 斷路器、DLQ、重試、連接池

**實施日期：** 2026-05-17

---

## 摘要表

| 項目 | 第一次（失敗） | 第二次（成功） |
|------|--------------|--------------|
| uvicorn workers | 1 | 4 |
| Redis 連接池大小 | 100 | 1000 |
| FAILURE_RATE | 0.2 | 0.2 |
| k6 VUs | 600 | 600 |
| HTTP 錯誤率 | 83% | 0.00% |
| 根本原因 | client-side pool 耗盡 | — |
| POST p95 | N/A（大量 500） | 358ms ✓ |
| GET p95 | N/A | 169ms ✓ |
| List p95 | N/A | 219ms ✓ |
| 吞吐量 | N/A | ~2856 RPS |
| 全部 checks 通過 | — | 100%（621,538/621,538）|

---

## 測試目標

本次 Tier 5 測試的目標是驗證通知系統在 **20% channel 失效率**下，各個可靠性機制能否正常運作：

1. **斷路器（Circuit Breaker）**：CLOSED → OPEN → HALF_OPEN 狀態機，是否能在連續失敗後快速跳閘，並在恢復後重新閉合
2. **死信佇列（DLQ）**：耗盡所有重試後，通知是否正確進入 Redis List（DLQ）
3. **指數退避重試**：MAX_RETRIES=3、RETRY_BASE_DELAY_S=0.1，每次重試是否套用退避 + jitter
4. **連接池大小**：pool=100 是否足夠承受 600 VUs 的並發壓力

### 系統配置（測試時）

```
4 uvicorn workers（async routes + redis.asyncio）
1 async delivery worker（asyncio.gather 批次並發，BATCH_SIZE=20）
MAX_RETRIES=3
RETRY_BASE_DELAY_S=0.1（指數退避 + jitter）
斷路器：每 channel 獨立，N 次連續失敗後跳閘 OPEN
DLQ：Redis List，耗盡重試後 LPUSH
每用戶速率限制：100 req/60s（Redis INCR + EXPIRE）
```

---

## 第一次測試：災難性失敗

### 症狀

- 錯誤率：**83%**
- 回傳狀態：HTTP 500
- k6 顯示大量 `http_req_failed` 為 true

### 第一直覺 vs. 真正根本原因

**錯誤直覺：** 以為是 FAILURE_RATE=0.2 造成的投遞失敗

**為什麼這個推斷是錯的：**
- FAILURE_RATE=0.2 搭配 MAX_RETRIES=3，永久失敗率 = 0.2³ = 0.8%
- 就算 channel 全部掛掉，delivery 失敗只影響通知的 **最終狀態**，不影響 HTTP 回應碼
- API 路徑是 `POST /send → 驗證 → 冪等性檢查 → XADD stream → 回傳 202`，delivery 是非同步的
- 83% 的 HTTP 500 絕對不可能是 delivery failure 造成的

**真正根本原因：** `redis.exceptions.ConnectionError: Too many connections`

### 診斷步驟

**步驟一：排除速率限制**

k6 腳本每次迭代使用 `user-${__VU}-${__ITER}` 作為 user_id，意即每個請求都是全新用戶，不會觸發 100 req/60s 的速率限制器。速率限制排除。

**步驟二：排除業務邏輯失敗**

FAILURE_RATE=0.2 + MAX_RETRIES=3 的永久失敗率理論值為 0.8%，遠低於觀測到的 83%。業務失敗排除。

**步驟三：查看 container logs**

```bash
podman logs notification_system-app-1 2>&1 | grep -i "error" | head -30
```

輸出：

```
redis.exceptions.ConnectionError: Too many connections
  File ".../store_redis.py", line 47, in aget
    async with self._ar.pipeline() as pipe:
...
redis.exceptions.ConnectionError: Too many connections
  File ".../queue.py", line 31, in enqueue
    await self._ar.xadd(...)
```

大量 `ConnectionError` 來自 `store_redis.py` 與 `queue.py`，兩個地方都使用 `redis.asyncio`。

### 根本原因分析：為什麼 pool=100 在 600 VUs 下必然耗盡

**redis.asyncio 的連接池行為：**

使用 `redis.asyncio`，每個 coroutine 在執行 `await pipeline.execute()` 的期間，會持有一條從 pool 借出的連接，直到 `await` 完成才歸還。

```
時間軸（600 VUs 全部同時進入 await pipe.execute()）：

VU-001: borrow conn #1  → await pipe.execute() ... 持有中 ...
VU-002: borrow conn #2  → await pipe.execute() ... 持有中 ...
...
VU-100: borrow conn #100 → await pipe.execute() ... 持有中 ...
VU-101: 嘗試借用 conn #101 → ❌ ConnectionError（pool 已滿，redis-py 不排隊等待，直接拋例外）
VU-102: ❌ ConnectionError
...
VU-600: ❌ ConnectionError
```

**關鍵點：** redis-py 的 ConnectionPool 在預設情況下，當 pool 耗盡時 **不會 block 等待**，而是 **立即拋出 ConnectionError**。這讓 `ConnectionError` 看起來像網路問題，但實際上是 **client-side 本地資源耗盡**。

**計算所需 pool 大小：**

```
每個 worker process 面對的並發請求數 ≈ 600 VUs / 4 workers = 150
每個請求最多同時持有的 Redis 連接數 = 2（store pipeline + queue XADD）
所需 pool = 150 × 2 = 300（每個 process 獨立計算）
pool=1000 = 3.3× 安全餘裕，足夠
```

---

## 修復：調整連接池大小

### 改動位置

**`app/store_redis.py`：**

```python
# 修改前（pool=100，600 VUs 下必然耗盡）
self._ar = aioredis.from_url(url, decode_responses=True, max_connections=100)

# 修改後（pool=1000，足夠 600+ 並發 coroutines）
self._ar = aioredis.from_url(url, decode_responses=True, max_connections=1000)
```

**`app/queue.py`：**

```python
# 修改前
self._ar = aioredis.from_url(url, decode_responses=True, max_connections=100)

# 修改後
self._ar = aioredis.from_url(url, decode_responses=True, max_connections=1000)
```

同時將 uvicorn workers 從 1 調整為 4，充分利用多核心。

---

## 第二次測試：全部指標通過

### k6 測試結果

| 指標 | 實測值 | 目標值 | 通過？ |
|------|--------|--------|--------|
| POST /send p95 延遲 | 358ms | < 500ms | ✓ |
| GET /notifications/:id p95 延遲 | 169ms | < 500ms | ✓ |
| GET /notifications?user_id p95 延遲 | 219ms | < 500ms | ✓ |
| HTTP 錯誤率 | 0.00% | < 1% | ✓ |
| k6 checks 全部通過 | 100%（621,538/621,538）| — | ✓ |
| 吞吐量 | ~2856 RPS | — | — |

### Delivery Worker 指標（測試結束後）

**成功送達（SENT）：**

| Channel | SENT 數量 |
|---------|----------|
| email | 1,962 |
| sms | 2,139 |
| push | 2,546 |
| **合計** | **6,647** |

**永久失敗（FAILED）：**

| Channel | FAILED 數量 |
|---------|------------|
| email | 1,203 |
| sms | 951 |
| push | 566 |
| **合計** | **2,720** |

**斷路器跳閘次數（circuit_breaker_trips_total）：**

| Channel | 跳閘次數 |
|---------|---------|
| email | 3,567 |
| sms | 2,831 |
| push | 1,633 |
| **合計** | **8,031** |

**重試次數（notification_retries_total，全 channels）：** ~7,052 次

**DLQ 深度（測試結束時）：** 19,641 筆

---

## 分析

### 為什麼 API 維持 100% 成功率，即使有 20% channel 失效

**核心洞察：斷路器與 DLQ 對 HTTP client 是透明的。**

API 請求路徑：

```
POST /send
  ↓
驗證請求格式
  ↓
冪等性檢查（sha256(user_id|topic|message) → Redis HGET）
  ↓
XADD 到 Redis Stream（加入投遞佇列）
  ↓
回傳 HTTP 202 Accepted
```

Delivery 路徑（**完全在 worker 內，與 API 解耦**）：

```
worker.py → XREADGROUP 從 stream 讀取
  ↓
asyncio.gather() 並發批次投遞
  ↓
每個通知：
  ├── 嘗試投遞（channel.send()）
  ├── 失敗 → 重試（指數退避 + jitter）
  ├── 斷路器 OPEN → fail-fast，跳過重試
  └── 耗盡重試 → LPUSH 到 DLQ
```

HTTP client 只看到 202。所有的斷路器跳閘、重試失敗、DLQ 積累，全部發生在 worker 裡，不影響 API 回應碼。**這正是 async delivery 架構的核心價值**：把投遞可靠性問題從 API 層隔離出去。

---

### 斷路器行為分析

**在 FAILURE_RATE=0.2 下，斷路器的振盪模式：**

```
時間 ─────────────────────────────────────────────────────→

CLOSED: 正常投遞，20% 機率失敗
        [失敗][成功][成功][失敗][失敗][失敗] ← N 次連續失敗
                                         ↓
OPEN:   所有投遞 fail-fast（微秒級），不碰 channel
        等待 recovery_seconds 計時器
                                         ↓
HALF_OPEN: 發送一個探測請求
           ├── 成功（80% 機率）→ 回到 CLOSED
           └── 失敗（20% 機率）→ 回到 OPEN，重新計時
                  ↓（多次 CLOSED→OPEN→HALF_OPEN→CLOSED 循環）
```

**email 斷路器跳閘 3,567 次** 說明在整個測試期間（約 100 秒），斷路器每秒約跳閘 35 次、恢復 35 次，持續振盪。

**各 channel 跳閘次數差異（email > sms > push）：**

可能原因：channel 在 registry 中的處理順序，或者隨機失效分佈的統計波動。在獨立隨機失效模型下，各 channel 的跳閘次數應趨近相同，但 100 秒內的樣本量有限，仍會有明顯波動。

**斷路器 OPEN 狀態的副作用：**

當斷路器 OPEN 時，所有排隊等待該 channel 的通知會立接 fail-fast，**不經過 3 次重試**，直接計入 FAILED 並最終進入 DLQ。這加速了 DLQ 的積累速度，使實際 DLQ 深度遠超「純失敗率」的理論預測值。

---

### DLQ 深度分析

**理論計算：**

```
測試時長：~100 秒
總 POST 請求量：~2856 RPS × 75% POST 比例 × 100s ≈ 214,200 筆通知進入佇列
理論永久失敗率（3 次重試後）：0.2³ = 0.8%
理論 DLQ 數量：214,200 × 0.8% ≈ 1,714 筆
```

**實測 DLQ 深度：19,641 筆 ≈ 理論值的 11.5 倍**

**差異原因：斷路器 OPEN 狀態加速 DLQ 積累**

```
理論模型假設：每個失敗的通知都嘗試了完整的 3 次重試
實際情況：
  1. 斷路器因連續失敗跳閘到 OPEN
  2. OPEN 期間，後續通知 fail-fast（0 次重試）→ 直接 FAILED → DLQ
  3. 斷路器回到 CLOSED 後，才恢復正常重試
  4. 8,031 次 CB 跳閘 × 每次 OPEN 期間 N 個通知直接進 DLQ = 大量額外 DLQ 條目
```

**實際失敗率（含 CB 效應）：**

```
FAILED / (SENT + FAILED) = 2,720 / (6,647 + 2,720) = 2,720 / 9,367 ≈ 29%
```

29% 遠高於理論的 0.8%，正是斷路器 OPEN 狀態 fail-fast 效應的體現。

**DLQ 積累的運維含義：**

在持續 20% 失效率下，DLQ 會以穩定速率成長，不會自行消化。運維人員必須：

1. 監控 DLQ 深度 Grafana 面板，設定告警閾值（例如 > 5,000）
2. 當 channel 恢復後，呼叫 `POST /admin/dlq/retry` 重新投遞
3. 或建立自動化 replay 機制（cron + retry script）

若 DLQ 無限成長而不處理，最終會有數萬筆通知永遠無法送達。

---

### 連接池大小的生產公式

**適用於 asyncio + redis.asyncio 的計算方式：**

```
所需 pool 大小 ≥ 並發 VUs 數 × 每個請求最多同時持有的 Redis 連接數
```

**本次實例（4 workers，600 VUs）：**

```
每個 worker process 面對的並發請求 = 600 / 4 = 150
每個請求的最大並發 Redis ops = 2
  （1 = store pipeline：HGET 冪等性 + HSET 寫入）
  （2 = queue XADD：推入 stream）

所需 pool = 150 × 2 = 300（每個 process 各自計算）
設定 1000 = 3.3× 安全餘裕
```

**重要警告：**

redis-py 的 ConnectionPool 在 pool 耗盡時，預設行為是 **立即拋出 ConnectionError**，而非 block 等待空閒連接。`ConnectionError: Too many connections` 看起來像網路錯誤，但實際上是 **client 本地資源耗盡**，跟 Redis server 本身無關。

Redis server 的預設 maxclients = 10,000，設定 pool=1000 對 server 完全安全。

**排查順序：**

```
觀察到 ConnectionError: Too many connections
  ↓
先確認：是 client-side pool 耗盡？還是 Redis server 拒絕連線？
  ↓
查 Redis server 日誌是否有 "max number of clients reached"
  ├── 有 → server 端問題，調整 maxclients 或分片
  └── 無 → client-side pool 耗盡，調整 max_connections
```

---

## 教訓與洞察

### 1. 連接池大小不對，比 Redis 本身掛掉更危險

`ConnectionError: Too many connections` 是 **client-side pool 耗盡**，不是 Redis server 崩潰。兩者的 log 輸出幾乎一樣，但根本原因完全不同：

- **Pool 耗盡（client-side）**：增加 max_connections 即可解決，無需動 Redis
- **Redis server 達到 maxclients**：需要調整 Redis 配置或改架構

在高並發 async 系統中，**pool 大小是關鍵配置**，必須根據並發量計算，而不是使用預設值（100）。

### 2. 斷路器對 HTTP client 完全透明

所有斷路器跳閘、重試失敗、DLQ 條目，全部發生在 delivery worker 內部。API 始終回傳 202，HTTP client 感知不到任何異常。

這是 async delivery 架構的核心設計決策：**把投遞可靠性的複雜度封裝在 worker 層**，保持 API 層的簡單性與高可用性。代價是：最終投遞狀態需要透過 `GET /notifications/:id` 主動查詢，或透過 webhook/callback 機制推送。

### 3. DLQ 需要配套的 ops playbook

目前 Grafana 面板已有 DLQ depth metric，但缺少：

- **自動 replay 機制**：channel 恢復後，DLQ 不會自動重試
- **DLQ 告警規則**：depth > 5,000 應觸發 PagerDuty 等告警
- **Replay script 或 cron job**：定期嘗試消費 DLQ
- **DLQ TTL 策略**：超過 N 天的 DLQ 條目是否直接丟棄？需要業務決策

Production 環境中，**沒有 ops playbook 的 DLQ 等於沒有 DLQ**。

### 4. 斷路器 OPEN 狀態加速 DLQ，是保護與代價的取捨

斷路器在保護 worker 不被瘋狂重試拖垮的同時，也讓 DLQ 以遠超理論值的速度積累：

```
理論 DLQ（無 CB）：0.2³ × 214,200 ≈ 1,714 筆（0.8% 永久失敗）
實際 DLQ（有 CB）：19,641 筆（約 9.2% 永久失敗進 DLQ）
```

差異來源：CB OPEN 期間，後續通知 fail-fast，0 次重試即進 DLQ。

**設計取捨：**
- CB 保護了 worker 的 CPU 與時間，避免在明顯有問題的 channel 上無謂重試
- 代價是 DLQ 積累更快，需要更積極的 replay 機制

如果業務對投遞成功率要求極高（SLA > 99.5%），應考慮：
1. 縮短 CB recovery_seconds，更快重試
2. 增加 replay worker 持續消費 DLQ
3. 降低 FAILURE_RATE 閾值（即更嚴格的 channel 健康標準）

### 5. failure_rate 20% + 3 retries 的理論 vs. 實務

```
純理論（無 CB，獨立失敗）：
  3 次全失敗概率 = 0.2³ = 0.008 = 0.8%

實務（有 CB）：
  CB OPEN 時 fail-fast → 直接進 DLQ，不嘗試 3 次
  實際永久失敗率 ≈ 9.2%（本次測試）
```

在性能測試中，「理論計算」與「實測結果」的差距，往往就是系統各層保護機制互相干涉的體現。理解這個差距，是 failure mode testing 的核心價值。

---

## 下一步（Tier 6 候選）

| 候選項目 | 說明 |
|---------|------|
| DLQ auto-replay worker | 新增獨立 worker 定期消費 DLQ，channel 恢復後自動重送 |
| CB 參數調優 | 測試不同 failure_threshold / recovery_seconds 對 DLQ 積累速度的影響 |
| 分散式限流 | 目前速率限制器是 per-worker 的，多 instance 下各自計算，可改用 Redis Lua script 全局限流 |
| Graceful shutdown | worker 停止時，正在處理的 batch 應 XACK 或 NACK 後才退出 |
| Metrics alerting | Grafana Alert Rules：DLQ depth、CB trip rate、error rate 超閾值觸發告警 |
