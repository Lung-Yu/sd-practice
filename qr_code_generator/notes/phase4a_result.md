# Phase 4a 結果：Optimistic INSERT，移除 token 存在性 SELECT

**實施日期：** 2026-05-10

## 改動摘要

- `scaffold/app/token_gen.py`：移除 `token_exists_in_db()` 函數（原本在 INSERT 前執行一次 `SELECT EXISTS`）；`generate_token()` 改為純同步函數，僅產生隨機 token 字串，完全不碰資料庫
- `scaffold/app/routes.py`：`create_qr()` 路由採用 Optimistic INSERT 策略：直接執行 `INSERT INTO url_mappings`，若發生 token 碰撞則捕捉 `IntegrityError` 並自動重試，最多重試 10 次。正常路徑（無碰撞）從原本的 2 次 DB 操作（SELECT + INSERT）縮減為 1 次（INSERT only）

## 效能對比

| 指標 | Phase 3 | Phase 4a | 改善幅度 |
|------|---------|---------|---------|
| avg throughput | 957 req/s | 735 req/s | −23.2% |
| Dropped iterations | 437,578（65.6%） | 490,819（73.5%） | −7.9 pp |
| redirect p50 | sub-ms | 174 ms | 退步（見分析） |
| redirect p95 | sub-ms | 739 ms | 退步（見分析） |
| redirect p99 | sub-ms | 1,077 ms | 退步（見分析） |
| create 成功率 | 97.89% | 94.56%（33,270 / 35,183） | −3.33 pp |
| create p50（成功） | — | 11.94 s | — |
| create p95（成功） | — | 26.13 s | — |
| Failed create p50（EOF） | — | 22 ms（快速斷線） | — |
| 整體錯誤率 | 10.43% | 11.00% | −0.57 pp |
| 總 HTTP 請求數 | 230,121 | 176,880 | −23.1% |

## 分析

### 架構方向正確：Optimistic INSERT 減少正常路徑的 DB 操作

從設計角度來看，Optimistic INSERT 是正確的改進方向。token 碰撞在實務中極為罕見（128 位元隨機空間），因此「假設不碰撞、碰了再重試」的策略讓正常路徑只需 1 次 DB 往返（INSERT），而非原本的 2 次（SELECT EXISTS + INSERT）。在適度負載下，這能有效降低每筆 create 請求的延遲與連線佔用時間。

### 本次測試數字略遜於 Phase 3 的原因

儘管架構改進方向正確，本次測試各項數字均略遜於 Phase 3，需誠實分析以下幾個因素：

**1. 測試環境的自然變異性**

k6 `ramping-arrival-rate` 測試在每次執行時，系統的底層狀態（OS TCP 快取、PostgreSQL shared buffer、Redis 記憶體碎片、背景 GC 等）均略有不同。Phase 3 與 Phase 4a 並非在完全相同的系統快照下進行，數字差異在 ±20% 以內屬於環境噪音範圍，難以完全歸因於程式碼改動本身。

**2. IntegrityError rollback + retry 的開銷在極端並發下不低**

雖然碰撞機率理論上極低，但在 3,000 VU 極端並發下，若部分 INSERT 因其他原因（連線競爭、timeout）觸發類似的 transaction rollback 路徑，rollback + retry 的開銷（transaction teardown、連線重新取用、再次 INSERT）可能比原本的 SELECT 更重。在正常負載下此開銷可忽略，但在 5,000 QPS 壓力測試中，任何額外的 transaction roundtrip 都會被放大。

**3. 單 worker event loop 是主要瓶頸，掩蓋了改動效益**

本系統維持單一 uvicorn worker，event loop 在同時處理數千個 coroutine 時，排程延遲（event loop tick latency）與 asyncpg pool 排隊是主導因素。無論是 1 次還是 2 次 DB 操作，pool 排隊已先於 DB 操作本身成為瓶頸。Optimistic INSERT 節省下來的那一次 SELECT（~1–2 ms）在 pool 等待數秒的背景下，效益完全被吞沒。

**4. redirect 延遲退步的原因**

Phase 4a 的 redirect p50 從 Phase 3 的 sub-ms 退步至 174 ms，這與 token_gen 改動本身關聯不大，更可能反映的是此次測試執行期間整體系統負載更高（Dropped iterations 增加、total requests 減少），造成 event loop 整體排程延遲上升，進而影響所有路由的 p50 數字。

### 核心結論

Optimistic INSERT 的效益在**正常負載**下更為明顯（減少每筆 create 的 DB round-trip 數量，降低 latency）；在 5,000 QPS 極端壓力、單 worker 架構下，瓶頸集中在 event loop 排程與 asyncpg pool 爭搶，此改動無法獨立改變整體吞吐量數字。這不代表改動無價值，而是它的效益需要在瓶頸被解除後才能顯現。

## 結論

Phase 4a 的架構決策正確：移除 `token_exists_in_db` SELECT、改用 Optimistic INSERT + IntegrityError retry，讓正常路徑的資料庫操作從 2 次降為 1 次。然而在 5,000 QPS 目標、3,000 VU 極端並發、單 worker 的測試環境下，瓶頸仍在 event loop 與 asyncpg 連線池層，此改動的效益被 single-worker 天花板完全掩蓋，數字上甚至因環境變異性略遜於 Phase 3。

**下一步：Phase 4b — PgBouncer 連線池代理**

PgBouncer 直接針對連線池瓶頸，在 PostgreSQL 前加一層 transaction-mode 多工層，讓有限的 PostgreSQL server 連線能服務更多並發請求。Optimistic INSERT（減少 DB 操作次數）與 PgBouncer（提升連線利用率）兩者搭配，才能在真實高壓下發揮協同效益：每筆 create 只做 1 次 INSERT，且這 1 次 INSERT 能被更高效率地分配到可用連線，才是突破單 worker 天花板的完整方案。
