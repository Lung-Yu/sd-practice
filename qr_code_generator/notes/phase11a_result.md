# Phase 11a — Scale Up：vCPU 8→12 + app3/app4 + 壓測驗證（2026-05-12）

## 目標

Phase 10 的 redirect throughput ceiling 為 2,661 req/s（2 app containers × 6 workers on 8 vCPU）。
本 Phase 驗證：將 Podman VM 擴充至 12 vCPU，並新增 app3/app4，是否能突破此 ceiling。

---

## 架構變更

| 項目 | Phase 10 | Phase 11a |
|------|---------|-----------|
| Podman VM vCPU | 8 | 12 |
| App containers | app1, app2 | app1, app2, app3, app4 |
| Workers per container | 6 | 4（初測後調整）|
| Total Python workers | 12 | 16 |
| Nginx worker_processes | auto（8）→ 4 | 4（不變）|
| Nginx upstream keepalive | 128 | 256 |
| Rate limit layer | FastAPI route dependency | 不變 |

---

## 第一次測試：workers 6（24 total）

**結果：2,173 req/s（退步 -18%）**

| 指標 | 值 |
|------|----|
| throughput | **2,173 req/s** |
| http_req_failed | 0.12%（442 failures）|
| seeded tokens | 58（setup 被自己 rate limit） |
| dropped_iterations | 230,720 |

### 問題一：Setup 自我 rate limit

k6 setup() 在單一 goroutine 中快速發送 500 個 create，速率遠超 60/s 的限制。
固定窗口（fixed-window）計數器：第一個 1 秒窗口即達上限，只有 58 個 token 被建立。

**修正**：`sleep(0.025)` 將建立速率限制在約 40 req/s，遠低於 60/s 的閾值。

### 問題二：Workers 過多

24 Python workers 搶 12 vCPU，比 Phase 9 的比例（12 workers / 8 vCPU）更差：

| Phase | Python workers | Nginx workers | 其他服務 | vCPU | Workers/vCPU |
|-------|---------------|--------------|---------|------|--------------|
| 9     | 12            | 8（auto）    | ~8      | 8    | 3.50         |
| 11a初  | 24            | 4            | ~8      | 12   | 3.00         |

看似 Phase 11a 比例更好，但 Python workers 大量增加 context switching，且 event loop 之間的資源競爭（Redis 連線、系統呼叫）也隨之增加。

**修正**：workers 6 → 4（16 total），ratio 降到 2.33 workers/vCPU for Python only。

---

## 第二次測試：workers 4（16 total）+ setup throttle

**結果：2,255 req/s average，peak ≈ 2,550–2,600 req/s**

| 指標 | 值 |
|------|----|
| throughput（avg） | **2,255 req/s** |
| throughput（peak） | **~2,550–2,600 req/s** |
| http_req_failed | 0.04%（160 failures）|
| seeded tokens | 340（大幅改善） |
| dropped_iterations | 184,679 |
| checks（redirect → 302）| **100%** |
| p50 | 29ms |
| p95 | 4.01s（在 6,000 RPS 目標下飽和） |

---

## 核心發現：單一 Podman VM 的 ceiling

### 數據比較

| 實驗 | 配置 | Throughput |
|------|------|-----------|
| Phase 10（直連 app1） | 1 container × 6 workers，bypass Nginx | 2,304 req/s |
| Phase 9 | 2 containers × 6 workers，Nginx | 2,661 req/s |
| Phase 11a（初測）| 4 containers × 6 workers，12 vCPU | 2,173 req/s |
| Phase 11a（修正後）| 4 containers × 4 workers，12 vCPU | ~2,550 req/s peak |

### 結論

**加容器無法突破 ceiling。** 從 2 containers 到 4 containers，peak throughput 仍在 2,500–2,700 req/s 範圍，
與 Phase 9 幾乎相同。

根本原因：**所有 containers 共享同一個 Podman VM 的 CPU 和虛擬網路**。

1. **網路 overhead 是瓶頸之一**：每個 redirect request 至少穿越 4 次 Podman bridge network（client→Nginx→App→Redis→App→Nginx→client）。在 2,500 req/s 下，每秒有 ~10,000 個 container-to-container 封包需要透過 Podman 的 TAP interface 轉發。
2. **Python GIL + asyncio overhead**：每個 uvicorn worker 的 event loop 是單執行緒。即使 await Redis GET 不阻塞 event loop，Python 本身的 task scheduling 和 HTTP parsing 仍有固定 CPU 成本。多 worker 共享 CPU 時，per-worker 效率下降。
3. **Redis 不是瓶頸**：2,500 GET/s 遠低於 Redis 100k+ ops/s 的能力。
4. **Nginx 不是瓶頸**（Phase 10 已驗證）：直連 app1 = 2,304 req/s，與 Nginx 版本相近。

### 重要教訓

> 單一主機的橫向擴充（增加 containers）有明顯的邊際遞減效應。
> 當所有 containers 共享同一台主機的 CPU 和網路時，加 container ≠ 加吞吐量。
> 真正的橫向擴充需要**多台物理主機**（或 VM），讓每台主機有獨立的 CPU 和網路資源。

---

## 最終 Phase 11a 狀態

| 項目 | 值 |
|------|-----|
| redirect throughput ceiling（單 Podman VM） | ~2,600 req/s |
| redirect p50 | 29ms |
| 架構 | Nginx + 4 apps（每 app 4 workers）+ PgBouncer + PG Primary+Replica + Redis |
| 瓶頸 | 單一 VM 的 CPU/網路共享，非 app worker 數 |
| 下一步 | Phase 11b：多主機模擬（雙 docker-compose site + 頂層 LB） |

---

## 學到的系統設計概念

### 1. 橫向擴充（Scale Out）≠ 在同一主機加 containers

Scale out 的前提是每個新節點有**獨立資源**。
在單一 VM 內加 containers 是「虛假的橫向擴充」——資源池沒有增加，只是切片方式不同。

真正的 scale out：
- 加 VM（不同實體機或雲端節點）
- 每個節點有自己的 CPU、記憶體、網路介面

### 2. 最優 worker 數公式（CPU-bound async app）

```
workers_per_container = max(2, min(vCPU_available_to_container × 1.5, 8))
```

在共享 VM 中，`vCPU_available_to_container` 約為 `total_vCPU / (num_containers + 2)`（保留給 LB、DB 等）。

本次實驗：12 vCPU / (4 apps + 2 infra) = 2 vCPU/container → workers = max(2, min(3, 8)) = **3 workers/container**。
實測 4 workers 已接近最優。

### 3. k6 Setup 的 Rate Limit 陷阱

k6 的 `setup()` 是**單一 goroutine 循序執行**，100 個 HTTP requests 可能在 < 1 秒內完成。
若 app 有 per-IP fixed-window rate limit，setup 與正式測試可能觸發相同限制。

**解法**：在 setup 中加入 `sleep()`，確保 create 速率低於 rate limit 閾值。
本例：`sleep(0.025)` → 40 req/s，安全低於 60/s 限制。

---

## Commits

- `2855023` — `feat(phase11a): scale up validation — single-VM ceiling confirmed at ~2600 req/s`
