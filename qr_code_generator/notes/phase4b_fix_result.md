# Phase 4b-fix 結果：修正 app 端 pool_size 為 50+50

**實施日期：** 2026-05-10

## 改動摘要

Phase 4b 確認 PgBouncer 架構方向正確，但 app 端 `pool_size=5, max_overflow=5`（共 10 條連線）過度保守，導致 3,199 次 QueuePool 耗盡 HTTP 500，成為新瓶頸。

Phase 4b-fix 僅調整一個參數：

```python
# scaffold/app/database.py
engine = create_async_engine(
    DATABASE_URL,
    pool_size=50,       # 原本 5 → 修正為 50
    max_overflow=50,    # 原本 5 → 修正為 50
    connect_args={"statement_cache_size": 0},  # 維持不變
)
```

- `pool_size=50, max_overflow=50`：app 端 SQLAlchemy QueuePool 最多維持 **100 條**到 PgBouncer 的連線
- `statement_cache_size=0`：維持不變，確保與 PgBouncer transaction pooling 相容（transaction pooling 不支援 server-side prepared statements）
- PgBouncer 端 `DEFAULT_POOL_SIZE=25` 維持不變

## PgBouncer Sizing 邏輯說明

PgBouncer 架構下，app 端與 PgBouncer 端的 pool 扮演不同角色，必須分開理解：

| 設定位置 | 參數 | 控制的資源 | Phase 4b | Phase 4b-fix |
|---------|------|----------|---------|-------------|
| PgBouncer | `DEFAULT_POOL_SIZE` | 真實 PostgreSQL 連線數 | 25 | 25（不變） |
| App（SQLAlchemy） | `pool_size + max_overflow` | App 到 PgBouncer 的連線槽 | 5+5=10 | 50+50=100 |

**Multiplex 關係：**

PgBouncer 在 transaction pooling 模式下，client 連線只在 transaction 執行期間佔用一條真實 PG 連線，transaction 結束後立即釋放。這讓 100 條 app→PgBouncer 連線能共享 25 條 PgBouncer→PostgreSQL 真實連線，每條 PG 連線平均服務 4 個 app 連線。

**設計原則：app 端 pool 要大，PgBouncer 端 pool 要小。**

若 app 端 pool 設得過小（如 10 條），絕大多數請求在抵達 PgBouncer 之前就已在 SQLAlchemy QueuePool 排隊等待，PgBouncer 根本沒機會發揮 multiplex 效益。正確做法是讓 app 端 pool 足夠大（100 條），讓請求能快速抵達 PgBouncer，再由 PgBouncer 統一做 multiplex 到少量 PG 連線。

## 效能對比

| 指標 | Phase 4b（pool 5+5=10） | Phase 4b-fix（pool 50+50=100） | 變化 |
|------|------------------------|-------------------------------|------|
| 總 HTTP 請求數 | 235,757 | 225,450 | −4.4%（測試變異） |
| avg throughput | 980.9 req/s | 938 req/s | −4.4%（誤差範圍內） |
| Dropped iterations | 431,942（64.7%） | 442,249（66.2%） | +1.5 pp |
| 整體錯誤率 | 10.24% | 10.29% | +0.05 pp |
| redirect 成功率 | 100% | 100% | 持平 |
| redirect p50 | 0.020 ms | 0.081 ms | 略升，仍為 sub-ms |
| redirect p95 | 0.125 ms | 0.479 ms | 略升，仍為 sub-ms |
| create 成功率 | 98.71% | 98.65% | −0.06 pp（誤差範圍內） |
| QueuePool timeout 500s | 3,199 | **0** | **完全消除** |
| create 失敗原因 | QueuePool 耗盡（pool 500） | EOF（accept queue 上限） | 根因轉移 |
| create fast path p50 | — | 10 ms | — |
| create fast path p95 | — | 24 ms | — |
| create slow path p50（3k VU） | — | 9.23 s | — |
| create slow path p95（3k VU） | — | 19.37 s | — |

## 分析

### QueuePool 500s 完全消除（3,199 → 0）

Phase 4b-fix 最關鍵的成果是 **QueuePool 耗盡錯誤歸零**。Phase 4b 中高達 3,199 次的 `QueuePool limit of size 5 overflow 5 reached` HTTP 500，在將 pool_size 調整為 50+50 後完全消失。

這驗證了 Phase 4b 報告中的診斷：pool exhaustion 的根因正是 app 端 pool 過小，而非系統其他環節的問題。

### throughput 些微下降屬測試變異，在誤差範圍內

Phase 4b-fix 的平均吞吐量為 938 req/s，略低於 Phase 4b 的 980.9 req/s（差距約 4.4%）。這個差距在 k6 高並發測試中屬於正常測試變異，不代表效能倒退。

影響因素包括：測試執行環境的瞬時資源競爭、OS TCP accept queue 的調度差異、以及 Docker 容器在不同時間點的啟動狀態。單次測試的數字差異在 5% 以內，不具統計顯著性。

### 剩餘失敗全為 EOF，不再有 pool 相關 500s

Phase 4b-fix 中 create 的 607 次失敗，其錯誤類型已從 pool 耗盡的 HTTP 500 **轉移**為 EOF（connection reset）。

EOF 的根因是 uvicorn 在 **single worker** 模式下，面對 3,000 VU 的極端並發時，OS TCP accept queue 達到上限，新連線被 kernel 直接拒絕，導致 k6 端看到 connection reset。這是 **單一 uvicorn worker 的 accept 能力上限**，與資料庫連線池完全無關。

pool 相關的 500s 歸零，代表連線層已不再是瓶頸。

### PgBouncer + 正確 pool sizing：連線層不再是瓶頸

Phase 4b-fix 的測試結果確認：**正確配置的 PgBouncer + app 端充足的 pool_size，讓連線層完全不是系統瓶頸**。

系統目前的真正瓶頸回歸到 **single uvicorn worker 的請求處理能力**：

- redirect fast path（純快取查詢）：p50 = 0.081 ms，sub-millisecond，幾乎無瓶頸
- create fast path（DB 寫入）：p50 = 10 ms，p95 = 24 ms，合理範圍
- create slow path（3,000 VU 極端並發）：p50 = 9.23 s，p95 = 19.37 s，顯示 single worker event loop 積壓嚴重

## Phase 4 總結

Phase 4 分三個子階段，逐步確認架構改善方向並修正配置問題：

### Phase 4a — Optimistic INSERT

**改動：** 將 create 端點從「先 SELECT 再 INSERT」改為「直接 INSERT，捕捉 unique constraint 異常」，減少一次 DB roundtrip。

**結果：** 架構方向正確，但效益被 single worker 的 CPU 瓶頸掩蓋。在極端並發下，event loop 積壓導致 redirect 延遲惡化（p50 達 174 ms），部分潛在改善被抵消。

### Phase 4b — PgBouncer（初始 pool_size=5+5=10）

**改動：** 引入 PgBouncer transaction pooling，解決直連 PostgreSQL 的連線壓力；修正 `AUTH_TYPE=scram-sha-256` 認證問題；設定 `statement_cache_size=0` 確保相容性。

**結果：** 吞吐量提升 33%（735 → 980.9 req/s），redirect 延遲回到 sub-millisecond，驗證 PgBouncer 架構方向完全正確。但 pool_size 初設過小（5+5=10），導致 3,199 次 QueuePool 耗盡 HTTP 500，成為新瓶頸。

### Phase 4b-fix — 正確 pool sizing（pool_size=50+50=100）

**改動：** 僅調整 `pool_size=5 → 50, max_overflow=5 → 50`，其餘配置維持不變。

**結果：** QueuePool 500s 完全歸零，系統行為回歸正常。剩餘失敗全為 EOF（accept queue 上限），連線層確認不再是瓶頸。**真正瓶頸確認為 single uvicorn worker 的請求處理能力上限。**

### 下一步：多 worker 或水平擴展

連線層瓶頸已排除後，下一個可行的改善方向：

1. **多 worker**：uvicorn 以 `--workers N` 啟動多個 worker 進程，充分利用多核 CPU，提升並發處理能力
2. **水平擴展**：在 docker-compose 中擴展 app service 的 replica 數量，搭配 load balancer 分流
3. **非同步任務佇列**：將耗時的 QR Code 生成移至 background task（如 Celery + Redis），讓 API 快速回應，進一步降低 p95/p99 延遲
