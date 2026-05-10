# 壓力測試報告：5,000 QPS 系統極限測試

**測試日期：** 2026-05-10  
**測試工具：** k6 + Prometheus Remote Write + Grafana  
**測試環境：** 本機 Podman（Apple Silicon Mac Mini）  
**目標系統：** FastAPI + PostgreSQL（單一 uvicorn worker）  

---

## 測試目標

找出系統在 5,000 QPS 目標下的實際吞吐量上限，並定位主要瓶頸。

---

## 測試配置

### k6 場景設計（`k6/load_test.js`）

```
executor: ramping-arrival-rate
preAllocatedVUs: 200  /  maxVUs: 3000
```

| 階段 | 時長 | 目標 QPS |
|------|------|---------|
| Warm-up | 30s | 500 |
| Ramp | 60s | 2,000 |
| Push | 60s | 5,000 |
| Hold | 60s | 5,000 |
| Ramp-down | 30s | 0 |

**為何用 `ramping-arrival-rate` 而非 VU stages：**  
VU stages 控制的是併發數，當伺服器延遲上升時 QPS 會自動下降，壓不出真正極限。  
`ramping-arrival-rate` 持續嘗試達到目標 RPS，系統跟不上時會顯示 dropped iterations，  
能精確反映系統的真實吞吐量上限。

### 流量混合比例

| Scenario | 佔比 | 說明 |
|----------|------|------|
| redirect（`/r/{token}`） | 70% | 快取命中的 hot path |
| create（`POST /api/qr/create`） | 20% | 需寫入 DB |
| not_found（`/r/INVALID`） | 10% | 快取 miss + DB miss |

事前 seed 200 個 token，確保 redirect 測試以快取命中為主。

---

## 測試結果

### 整體數據

| 指標 | 數值 |
|------|------|
| 測試目標 QPS | 5,000 |
| **實際峰值 QPS** | **752** |
| 平均 QPS（有效期間） | 423 |
| 總完成請求 | 156,341 |
| 被丟棄的 iterations | 511,158（占 76.6%） |
| VU 峰值 | 3,000（已觸及上限） |
| 所有 Checks 通過率 | 100%（回應正確性無問題） |
| 傳送流量 | 15.1 MB |
| 接收流量 | 24.2 MB |

### 各 Scenario 請求數

| Scenario | 請求數 | 佔比 |
|----------|--------|------|
| redirect | 109,384 | 70% |
| create | 31,316 | 20% |
| not_found | 15,641 | 10% |

### 延遲（毫秒）

| Scenario | p50 | p95 | p99 |
|----------|-----|-----|-----|
| redirect | 3,847 ms | 4,436 ms | 4,731 ms |
| not_found | 3,855 ms | 4,438 ms | 4,732 ms |
| create | 5,797 ms | 6,611 ms | 6,976 ms |

> **對比正常狀態：** redirect p50 約 12ms、create p50 約 15ms。  
> 壓測時延遲爆升 300~400 倍，表示伺服器已嚴重排隊。

### App 端實際處理（Prometheus 側量）

| HTTP 狀態 | 請求數 |
|-----------|--------|
| 302（成功 redirect） | 109,384 |
| 404（not found） | 15,641 |

---

## 關鍵發現

### 1. 真實上限：約 750 QPS

系統能穩定處理的請求上限約為 **750 QPS**，僅達目標的 15%。  
k6 maxVUs 已耗盡（3,000 VU 全卡在等待），仍無法推高 QPS，確認瓶頸在伺服器端。

### 2. 主要瓶頸：同步 DB I/O 阻塞 event loop

FastAPI 的路由是 `def`（同步函數），由 uvicorn 的 thread pool executor 執行。  
每次 DB 查詢都會佔用一個 thread，thread pool 耗盡後請求排隊等待，延遲急劇上升。

**特別嚴重的地方：**
- `create`：每次都做 INSERT（延遲最高，p50 = 5.8s）
- `redirect`：就算快取命中，仍會呼叫 `_record_scan()` 同步寫入 DB，抵消了快取的優勢
- `not_found`：快取 miss 後查 DB，延遲與 redirect 接近

### 3. 快取設計的侷限

`redirect_cache` 是 in-process dict，只在單 worker 下有效。  
一旦擴展到多 worker（見後文建議），快取無法共用，快取命中率會大幅下降。

### 4. Dropped Iterations = 系統的誠實自白

```
dropped iterations: 511,158 (76.6%)
```

k6 在 `ramping-arrival-rate` 模式下，當所有 VU 都忙碌時會直接丟棄該次 iteration。  
這個數字代表系統「看到了但完全沒有機會處理」的請求量，是最直接的容量告警指標。

---

## 瓶頸路徑圖

```
客戶端請求
    │
    ▼
uvicorn event loop（單 worker）
    │
    ├── redirect（快取命中）
    │       └── _record_scan() ──► PostgreSQL INSERT ← 同步阻塞
    │
    ├── redirect（快取 miss）
    │       ├── DB SELECT ← 同步阻塞
    │       └── _record_scan() ──► DB INSERT ← 同步阻塞
    │
    └── create
            ├── validate_url()
            ├── generate_token() + token_exists_in_db() ← DB SELECT
            └── DB INSERT ← 同步阻塞
```

---

## 改善建議（依優先順序）

### 短期（預期提升到 2,000~3,000 QPS）

**1. 多 worker 部署**
```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```
直接將吞吐量乘以 CPU 核心數，是最快見效的改法。  
注意：多 worker 後 `redirect_cache`（in-process dict）需換成 Redis。

**2. `_record_scan()` 改為 fire-and-forget**  
```python
import asyncio
from fastapi import BackgroundTasks

@router.get("/r/{token}")
async def redirect(token, request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    # ... 查快取、查 DB ...
    background_tasks.add_task(_record_scan, token, request, db)  # 非同步寫入
    return RedirectResponse(url=..., status_code=302)
```
redirect 不需要等 scan 記錄完成，這個改動可讓 redirect p50 回到個位數 ms。

### 中期（預期提升到 5,000+ QPS）

**3. 改用 async SQLAlchemy + asyncpg**
```
pip install sqlalchemy[asyncio] asyncpg
```
將所有 DB 操作改為 `async def`，解除 I/O 阻塞，讓 event loop 在 DB 等待時處理其他請求。

**4. Redis 作為分散式快取**
```yaml
# docker-compose.yml
redis:
  image: docker.io/redis:7-alpine
  ports:
    - "6479:6379"
```
替換 `redirect_cache` dict，支援多 worker 共用、設定 TTL 自動過期。

### 長期（預期提升到 10,000+ QPS）

**5. 將 scan 記錄移到 message queue（Kafka / Redis Streams）**  
redirect 完全不碰 DB，只推一個 event 到 queue，由獨立的 consumer 批次寫入。

**6. Connection pooling 調優**  
```python
engine = create_async_engine(DATABASE_URL, pool_size=20, max_overflow=40)
```

---

## 監控觀察技巧（Grafana）

壓測時，在 Grafana dashboard 觀察以下指標可快速判斷狀況：

| 指標 | 代表什麼 |
|------|---------|
| `k6_vus` 持續爬升到 maxVUs | 伺服器跟不上，VU 不夠用 |
| `k6_dropped_iterations_total` 上升 | 已達到系統真實上限 |
| `k6_http_req_duration_p99` > 1s | 嚴重排隊，需立刻行動 |
| `k6_checks_rate` 維持 100% | 回應正確性沒問題（只是慢） |
| App 端 `http_request_duration_seconds` p99 | 與 k6 側對比，差距代表網路/k6 本身開銷 |

---

## 結語

本次壓測成功定位到系統在**單 worker 同步 DB I/O** 下的真實上限約為 750 QPS。  
系統的正確性（check pass rate 100%）沒有問題，純粹是吞吐量的架構問題。  
最高優先的改善項目是：**多 worker + `_record_scan()` 非同步化**，  
預計可在不重寫架構的前提下，將上限推到 2,000~3,000 QPS。
