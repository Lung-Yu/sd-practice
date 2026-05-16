# Tier 5b：DLQ 重試驗證 — 運維「重播按鈕」

**實施日期：** 2026-05-17

---

## 摘要表

| 項目 | 數值 |
|------|------|
| 測試前提 | Tier 5 失效模式測試後（FAILURE_RATE=0.2，100 秒壓測） |
| DLQ 積壓量（初始） | 22,104 筆 |
| 恢復方式 | 重啟服務，FAILURE_RATE=0、MAX_RETRIES=1 |
| 重播分批策略 | 100 → 5000 × 4 批 → 1904 |
| DLQ 清空時間 | 約 30 秒 |
| 預計 drain rate | ~733 筆/秒 |
| 最終 DLQ 深度 | 0 |
| 最終 FAILED 數量 | 0 |

---

## 系統背景

### DLQ 資料結構

- **Redis List**：`notifications:dlq`
- 每個元素為失敗通知的 `notification_id`（字串）
- 當通知耗盡所有重試次數後，worker 將 ID `RPUSH` 進 DLQ

### Admin 端點

| 端點 | 說明 |
|------|------|
| `GET /admin/dlq` | 回傳目前 DLQ 深度與前幾筆 sample ID |
| `POST /admin/dlq/retry?count=N` | 從 DLQ `LPOP` N 筆，`XADD` 回 delivery stream |

### Worker 架構

Worker 從 delivery stream（`notifications:delivery`）以 `XREADGROUP` 讀取訊息，並以 `asyncio.gather()` 並行處理每批。DLQ 重播後的訊息走**完全相同的 delivery 路徑**，worker 無法區分新通知或重播通知。

---

## 測試流程

### 前置狀態（Tier 5 結束後）

```
GET /admin/dlq
→ {"depth": 22104, "sample": ["notif-00af3b...", "notif-12c9e4...", ...]}
```

- 22,104 筆通知 `status="failed"`，儲存在 Redis Hash
- 斷路器在壓測期間反覆 `CLOSED → OPEN` 震盪，大量通知走快失敗路徑進 DLQ
- 服務仍在執行，但 FAILURE_RATE=0.2 尚未更改

---

### 步驟一：模擬 Channel 恢復

重啟服務，以新環境變數啟動：

```bash
# 停止舊服務
podman-compose down

# 以 FAILURE_RATE=0 重新啟動（模擬 downstream channel 已恢復）
FAILURE_RATE=0 MAX_RETRIES=1 podman-compose up -d
```

**意義：** 這模擬真實運維場景 —— Email/SMS/Push 的外部 provider 原本降級，現在已恢復正常。`FAILURE_RATE=0` 代表所有 `channel.send()` 呼叫都會成功，不需要重試。

驗證 DLQ 深度不變（Worker 啟動後沒有主動消費 DLQ）：

```
GET /admin/dlq
→ {"depth": 22104, "sample": [...]}
```

---

### 步驟二：小批次試驗（100 筆）

在全量重播前，先用小批次確認 retry 路徑正常：

```bash
curl -X POST "http://localhost:8000/admin/dlq/retry?count=100"
```

**回應：**

```json
{
  "requeued": 100,
  "notification_ids": [
    "notif-00af3b2c...",
    "notif-12c9e491...",
    "..."
  ]
}
```

**重播前（單筆通知狀態）：**

```json
{
  "notification_id": "notif-00af3b2c-...",
  "status": "failed",
  "error": "circuit 'push' is OPEN — retry in 2s",
  "sent_at": null,
  "created_at": "2026-05-16T23:01:44.382100"
}
```

等待約 4 秒後，查詢同一筆通知：

**重播後（FAILURE_RATE=0）：**

```json
{
  "notification_id": "notif-00af3b2c-...",
  "status": "sent",
  "error": "circuit 'push' is OPEN — retry in 2s",
  "sent_at": "2026-05-16T23:29:07.162820",
  "created_at": "2026-05-16T23:01:44.382100"
}
```

> **設計怪象（Design Quirk）：** `status` 已更新為 `"sent"`，但 `error` 欄位仍保留舊的失敗訊息。見「分析」章節。

確認 DLQ 深度減少：

```
GET /admin/dlq
→ {"depth": 22004, "sample": [...]}
```

---

### 步驟三：全量清空（21,904 筆剩餘）

分批執行，每批後確認 DLQ 深度：

```bash
# 批次 1
curl -X POST "http://localhost:8000/admin/dlq/retry?count=5000"
# → {"requeued": 5000, ...}
# DLQ depth: 17004

# 批次 2
curl -X POST "http://localhost:8000/admin/dlq/retry?count=5000"
# → {"requeued": 5000, ...}
# DLQ depth: 12004

# 批次 3
curl -X POST "http://localhost:8000/admin/dlq/retry?count=5000"
# → {"requeued": 5000, ...}
# DLQ depth: 7004

# 批次 4
curl -X POST "http://localhost:8000/admin/dlq/retry?count=5000"
# → {"requeued": 5000, ...}
# DLQ depth: 2004

# 批次 5（清尾）
curl -X POST "http://localhost:8000/admin/dlq/retry?count=2004"
# → {"requeued": 2004, ...}
# DLQ depth: 0
```

**全部清空耗時：約 30 秒**（每批約 6 秒處理完畢）

---

### 步驟四：最終狀態確認

```
GET /admin/dlq
→ {"depth": 0, "sample": []}
```

**Prometheus 指標（FAILURE_RATE=0 worker 實例）：**

| 指標 | 數值 |
|------|------|
| `notifications_sent_total{channel="push",status="SENT"}` | 75,630 |
| `notifications_sent_total{channel="sms",status="SENT"}` | 77,037 |
| `notifications_sent_total{channel="email",status="SENT"}` | 75,367 |
| `notifications_sent_total{*,status="FAILED"}` | **0** |

所有重播通知均成功送達，無任何失敗。

---

## 分析

### 重播路徑與正常路徑完全相同

DLQ retry 的實作非常簡潔：

```
POST /admin/dlq/retry
    ↓
LPOP N 筆 notification_id from notifications:dlq
    ↓
for each id:
    XADD notifications:delivery * notification_id <id>
    ↓
Worker XREADGROUP reads message
    ↓
asyncio.gather() → deliver() → channel.send()
    ↓
store.save(notification, status=SENT) → XACK
```

Worker 端完全不知道訊息來源是新的 `POST /send` 還是 DLQ 重播。這個設計的優點是：
- **無重複代碼**：retry 邏輯就是 delivery 邏輯
- **可測試性強**：新增通知與重播通知走相同路徑，行為一致
- **簡單可靠**：沒有特殊狀態要管理

### error 欄位保留問題（設計怪象）

`deliver()` 成功後，呼叫的是：

```python
notification.status = NotificationStatus.SENT
notification.sent_at = datetime.utcnow()
store.save(notification)
```

它**沒有**清除 `notification.error`。因此，成功重播後，通知的 `error` 欄位仍顯示上一次失敗的原因（如 `"circuit 'push' is OPEN — retry in 2s"`）。

**在 Production 中，這個問題應擇一解決：**

| 方案 | 做法 | 優點 |
|------|------|------|
| 清除 error | `notification.error = None` on success | 狀態乾淨，易讀 |
| 歷史記錄 | 改名 `last_error`，新增 `error_history: list` | 保留完整除錯資訊 |

目前行為對 on-call 工程師可能造成誤判：看到 `status="sent"` 但 `error` 非空，需要額外理解。

### DLQ Drain Rate 分析

```
22,000 筆 ÷ 30 秒 ≈ 733 筆/秒
```

**影響因素：**

- **BLOCK_MS=1000ms**：`XREADGROUP` 每次最多 blocking 1 秒等待新訊息。即使 `asyncio.gather()` 並行處理，也要等下一個 polling 週期
- **BATCH_SIZE=20**：每次 XREADGROUP 讀取最多 20 筆訊息並行處理
- **FAILURE_RATE=0**：`channel.send()` 是純 stdout 模擬，幾乎無延遲，不需重試

**實際 drain rate 公式（近似）：**

```
drain_rate = BATCH_SIZE / BLOCK_MS × 1000
           = 20 / 1000 × 1000
           = 20 筆/秒（per polling cycle）
```

實際量測 733 筆/秒，代表 XREADGROUP 並非每次都 blocking 到 1 秒上限，而是在 stream 有訊息時立即返回並繼續處理。大量 `XADD` 後，worker 可快速連續消費。

> **注意：** 若 DLQ 積壓持續增加（新失敗速度 > drain 速度），需考慮增加 worker 數量或減少 BLOCK_MS。

### 無自動重播的限制

目前 DLQ 只能**人工觸發**重播。對比業界實踐：

| 機制 | 現狀 | Production 建議 |
|------|------|----------------|
| 人工重播 | ✅ 已實作 | 保留（緊急情況用） |
| 斷路器恢復後自動觸發 | ❌ 無 | Circuit OPEN→HALF_OPEN 成功後自動 XADD |
| 背景 DLQ drainer | ❌ 無 | 可設定 rate limit（如 100 筆/秒），避免壓垮剛恢復的 channel |
| Alertmanager 整合 | ❌ 無 | DLQ depth > 1000 → webhook → 自動觸發 retry script |

---

## 運維 Playbook

本次測試驗證以下 DLQ 操作劇本的可行性：

```
1. 告警觸發
   └─ Grafana alert: DLQ depth > 1000

2. 診斷
   ├─ GET /admin/dlq           ← 確認積壓量與 sample
   ├─ GET /health              ← 確認服務健康
   └─ 檢查 circuit breaker 狀態（log 或 metrics）

3. 等待 channel 恢復
   └─ 確認 Email/SMS/Push provider 已恢復正常

4. 分批重播
   ├─ 先小批次（100 筆）確認 retry 路徑正常
   ├─ POST /admin/dlq/retry?count=5000 × N 批
   └─ 最後一批清尾

5. 監控
   ├─ 觀察 notifications_sent_total{status="SENT"} 上升
   ├─ 確認 notifications_sent_total{status="FAILED"} 為 0
   └─ GET /admin/dlq → depth: 0
```

---

## 學到的教訓

1. **DLQ retry 路徑與正常路徑完全相同** — 不需要特殊的 retry code path，`XADD` 回 stream 就夠了。這個設計讓代碼保持簡單，同時確保行為一致性。

2. **`error` 欄位應在成功時清除** — 現在成功 retry 後 `error` 欄位還保留舊的失敗訊息，造成混淆。建議在 `deliver()` 成功路徑加上 `notification.error = None`，或改用 `last_error` + `error_history` 架構。

3. **DLQ 是 ops playbook 的一部分，不是最終狀態** — 沒有自動 replay 機制，需要 operator 手動介入。Production 應考慮在斷路器 HALF_OPEN 成功後自動觸發小批次重播，以縮短 MTTD（Mean Time To Detect）和 MTTR（Mean Time To Recover）。

4. **Drain rate 受 BLOCK_MS 限制** — 即使 `asyncio.gather()` 並行，也要等 `XREADGROUP` 的 blocking 週期。DLQ 積壓嚴重時，drain 速度可能跟不上新增速度。實際壓力測試中測得約 733 筆/秒，在 FAILURE_RATE=0 條件下已足夠；若 channel 仍有部分失敗，drain rate 會因重試 backoff 大幅下降。

5. **`FAILURE_RATE` 環境變數讓測試極為方便** — 可以精準模擬「channel 恢復」場景（從 0.2 → 0），不需要真的修改任何 channel 代碼或 mock 外部服務。這是設計通知系統時值得保留的可觀測性接口。
