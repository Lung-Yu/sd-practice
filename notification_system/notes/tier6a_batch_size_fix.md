# Tier 6a：BATCH_SIZE 反比例縮放修復

## 問題回顧：Tier 6 的效能倒退

在 Tier 6 中，我們將 delivery-worker 從 1 個容器擴展到 4 個容器，預期能獲得 4 倍的交付吞吐量。但結果完全相反：

| 配置 | POST p95 | GET p95 | Throughput | All pass? |
|------|----------|---------|------------|-----------|
| Tier 4：1w × BS=20 | 361ms ✓ | 172ms ✓ | 2,736 RPS | ✓ |
| Tier 6：4w × BS=20 | 1,450ms ❌ | 532ms ❌ | 800 RPS | ❌ |

POST p95 從 361ms 暴增到 1,450ms，足足惡化了 4 倍。吞吐量也從 2,736 RPS 暴跌到 800 RPS。

這不是隨機的波動，而是一個可預測的系統性問題。

---

## 根本原因：Redis 單執行緒命令佇列飽和

### Redis 的架構限制

Redis 是**單執行緒**的。它一次只能處理一個命令（或 pipeline）。這個設計讓 Redis 避免了鎖的競爭，在低並發情況下極為高效，但在高並發寫入時會成為嚴重瓶頸。

### Tier 6 的並發計算

每個 delivery-worker 使用 `asyncio.gather()` 以 BATCH_SIZE=20 並發處理：

- `store.aget(nid)` → 每條訊息一次 Redis HGETALL
- `loop.run_in_executor(None, deliver, notification)` → 每條訊息一次 pipeline（HSET + SET + ZADD）
- `r.xack()` → 每條訊息一次 XACK

**4 個 worker × BATCH_SIZE=20 = 80 個同時進行的 Redis pipeline**

加上 API 端：600 VUs × 每次請求 4 個非同步 Redis 操作 = 數百個同時進行的 API 命令。

Redis 的命令佇列瞬間被撐爆。每個命令都要在佇列中等待，等待時間隨佇列長度線性增長。

### 連鎖效應

```
Worker 佔用 80 個 pipeline 槽位
    ↓
API 的每個 Redis 命令都要排隊等待
    ↓
POST /send 需要 4 個 Redis 操作，每個都延遲 10× 以上
    ↓
POST p95 從 361ms → 1,450ms（約 4 倍惡化）
    ↓
吞吐量從 2,736 → 800 RPS（Little's Law：延遲增加 → 相同 VU 數產生更少 RPS）
```

這是**IO 瓶頸的進階版本**：增加 worker 不會增加計算能力，反而增加了序列化點（Redis）的競爭。

---

## 修復方案：BATCH_SIZE 反比例縮放

### 核心規則

```
num_workers × BATCH_SIZE = 目標並發交付數量（常數）
```

如果想保持與 Tier 4 相同的 Redis 壓力：

```
Tier 4：1 worker × BATCH_SIZE=20 = 20 並發
Tier 6a：4 workers × BATCH_SIZE=5  = 20 並發（相同的 Redis 壓力）
```

增加 worker 數量時，必須等比例減少每個 worker 的 BATCH_SIZE，才能維持相同的 Redis 命令速率。

### 實作細節

**`config.py` 的變更：**

```python
# 新增 WORKER_BATCH_SIZE 環境變數，允許每次部署獨立調整
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "20"))
```

**`docker-compose.yml` 的變更：**

```yaml
delivery-worker:
  environment:
    - WORKER_BATCH_SIZE=5   # 4 workers × 5 = 20 並發（與 Tier 4 持平）
```

將 BATCH_SIZE 設計為環境變數是正確的做法。硬編碼的常數會阻止針對不同部署場景的優化。

---

## Tier 6a 實驗結果

### 整體指標對比

| 配置 | POST p95 | GET p95 | Throughput | All pass? |
|------|----------|---------|------------|-----------|
| Tier 4: 1w × BS=20 | 361ms ✓ | 172ms ✓ | 2,736 RPS | ✓ |
| Tier 6: 4w × BS=20 | 1,450ms ❌ | 532ms ❌ | 800 RPS | ❌ |
| Tier 6a: 4w × BS=5 | **351ms ✓** | **162ms ✓** | **2,838 RPS** | **✓** |

Tier 6a 不僅恢復了正常效能，甚至比 Tier 4 略好一些（POST p95：361ms → 351ms，GET p95：172ms → 162ms，吞吐量：2,736 → 2,838 RPS）。

### Consumer 分佈

4 個 worker 各自獨立消費 Redis Stream，分佈幾乎完美均勻：

| Worker | 發送成功數 |
|--------|-----------|
| Worker 1 | 57,739 SENT |
| Worker 2 | 58,150 SENT |
| Worker 3 | 57,988 SENT |
| Worker 4 | 57,971 SENT |
| **合計** | **231,848 SENT** |

每個 worker 各佔約 25%，這是 Redis Consumer Group 自然分配的結果——無需額外的協調機制，只要每個 worker 使用唯一的 consumer name（`socket.gethostname()`），Redis 就會自動負載均衡。

---

## 這個修復「做到了什麼」vs「沒做到什麼」

### 做到了：恢復 API 延遲

BATCH_SIZE 調整讓 Redis 並發壓力回到安全範圍，API 延遲因此恢復正常。這是一個**配置層面的修復**，不需要改動任何業務邏輯。

### 做到了：提升容錯能力

4 個 worker 在相同總並發量下，帶來了實質的容錯收益：

- **失去 1 個 worker**：損失 25% 的交付容量，剩餘 75% 繼續工作
- **失去 1 個 worker（Tier 4）**：損失 100% 的交付容量，服務完全停止

這對生產環境非常重要。單一 worker 是單點故障（SPOF）；4 個 worker 則是部分降級。

### 沒做到：增加交付吞吐量

這是最容易被誤解的地方。

**4 workers × BS=5 = 20 並發，與 1 worker × BS=20 完全相同。**

你沒有獲得更多的交付速度，你只是把相同的工作分散到 4 個容器中。如果目標是提升交付吞吐量，這個方案做不到。

```
錯誤的直覺：
「4 個 worker = 4× 交付速度」

正確的認識：
「4 個 worker × BS=5 = 1 個 worker × BS=20 = 相同的交付速度」
```

---

## 什麼才能真正解決多 Worker 的擴展問題

BATCH_SIZE 調整是一個治標不治本的方案。根本問題是：**所有 worker 共用同一個 Redis 實例**，而 Redis 是單執行緒的，這個序列化點限制了整個系統的上限。

要獲得真正的 4× 交付吞吐量，同時保持 API 延遲穩定，需要 **Tier 7：分離 Redis 實例**：

```
API Redis（Redis-A）：
  - 負責：idempotency 鍵值、rate limiting、通知狀態讀取
  - 使用者：HTTP API workers（600 VUs 的請求）

交付 Redis（Redis-B）：
  - 負責：Redis Stream（XADD/XREADGROUP/XACK）、交付狀態寫入
  - 使用者：delivery workers
```

兩個獨立的工作負載各自有獨立的 Redis，互不干擾：

- API 的讀寫不會因為 worker 的 pipeline 而排隊
- 4 個 worker × BS=20 = 80 並發的 pipeline 只打在 Redis-B，不影響 Redis-A
- API 延遲回到 Tier 3B 的水準（POST p95 ~ 283ms），同時獲得真正的 4× 交付吞吐量

---

## 學到的四個教訓

### 教訓一：擴展 worker 而不減少 BATCH_SIZE 是常見錯誤

「更多 worker = 更高吞吐量」這個直覺在 CPU 密集型場景是正確的，但在 IO 密集型場景完全不適用。

如果瓶頸是共用資源（Redis、資料庫、外部 API），增加 worker 只會增加對該資源的競爭，而不是增加計算能力。

正確的思考方式是：**增加 worker 時，必須問「共用資源能承受多少並發？」**，而不是「我有幾個 CPU 核心？」

### 教訓二：`num_workers × BATCH_SIZE = 常數` 只能保持 Redis 壓力不變，不能提升吞吐量

這個公式的目的是**防止退化**，而不是**促進提升**。

如果你想要更高的吞吐量，你需要更多的 Redis 容量（分片、多實例、Redis Cluster），而不是調整 worker 數量和 BATCH_SIZE 的比例。

### 教訓三：BATCH_SIZE 作為環境變數是正確的設計

硬編碼的常數阻止了針對不同部署場景的調整：

- 單 worker 部署：`WORKER_BATCH_SIZE=20`
- 4 worker 部署：`WORKER_BATCH_SIZE=5`
- 8 worker 部署：`WORKER_BATCH_SIZE=3`

透過環境變數，同一份程式碼可以在不同的部署規模下都保持最佳配置，無需修改程式碼本身。

### 教訓四：多 Worker 在相同總並發下的真實價值是容錯性

當你在相同的 Redis 壓力預算下分配 worker，你並沒有獲得更多的速度，但你獲得了更好的彈性：

- 容器重啟時，其他 worker 繼續工作
- 部署新版本時，可以滾動更新（rolling update）
- 某個 worker 崩潰時，Redis 的 `XPENDING` 保留未確認的訊息，其他 worker 可以認領

這些好處在效能數字上看不到，但在生產環境中非常重要。

---

## 總結

Tier 6a 的核心發現可以用一句話概括：

> **在單一 Redis 的限制下，控制並發量才是關鍵——worker 數量和 BATCH_SIZE 需要反比例調整，以保持 Redis 命令速率在安全範圍內。**

這不是一個令人興奮的結論，因為它說明多 worker 並沒有給我們帶來更快的交付速度。但它確實給了我們一個清晰的下一步方向：Tier 7，分離 API Redis 和交付 Redis，才能讓兩個工作負載都能獨立擴展。
