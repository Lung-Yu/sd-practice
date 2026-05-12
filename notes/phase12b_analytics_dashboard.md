# Phase 12b：Analytics Dashboard 改進

## 實作內容

**功能：Analytics Dashboard 改進**

改善 Prometheus + Grafana 監控設定，使其涵蓋全部 4 個 app 容器（之前只抓取 app1 和 app2），並在 Grafana dashboard 中新增「Total QR Codes Created」統計面板。

---

## 背景說明

系統中已透過 `prometheus-fastapi-instrumentator==7.0.0` 在 `main.py` 設定好自動儀器化，對所有路由產生 HTTP 延遲直方圖。自訂計數器定義於 `scaffold/app/metrics.py`：

- `qr_codes_created_total`（Counter）
- `qr_redirects_total{status}`（帶標籤的 Counter）
- `qr_cache_hits_total`（Counter）
- `qr_cache_misses_total`（Counter）

---

## 修改的檔案

| 檔案 | 變更內容 |
|------|--------|
| `monitoring/prometheus.yml` | 新增 `app3:8000` 和 `app4:8000` 至 scrape targets（原本缺少這兩個） |
| `monitoring/grafana/provisioning/dashboards/qr_code.json` | 新增「Total QR Codes Created」統計面板（id=201，使用 `sum(qr_codes_created_total)`） |
| `nginx/nginx.conf` | 在 nginx upstream 中新增 `app3:8000` 和 `app4:8000`；設定 `worker_processes=4` |

---

## Prometheus Scrape 設定（修正後）

```yaml
scrape_configs:
  - job_name: 'app'
    static_configs:
      - targets: ['app1:8000', 'app2:8000', 'app3:8000', 'app4:8000']
    metrics_path: /metrics
```

---

## 新增的 Grafana 面板

```json
{
  "title": "Total QR Codes Created",
  "type": "stat",
  "expr": "sum(qr_codes_created_total)"
}
```

---

## Commits

- `4a6d5a6` — `feat(analytics): add app3/4 prometheus targets, stat panel, nginx upstream fix`

---

## 學習重點

### 1. Prometheus scrape target 完整性

水平擴展時，務必更新 `prometheus.yml` 以納入所有新增實例。缺少 targets 等於監控盲點——指標遺失時不會有任何警告，屬於靜默丟失（silently dropped）。

### 2. prometheus-fastapi-instrumentator 自動儀器化

此套件會自動為所有路由建立 `http_request_duration_seconds` 直方圖，新增路由時無需修改程式碼——只需確保所有實例都被 Prometheus 抓取即可。

### 3. 跨實例使用 `sum()` 聚合

在 Prometheus 中，`qr_codes_created_total` 是每個實例各自的 Counter。若要取得所有 4 個 app 容器的總數，需使用 `sum(qr_codes_created_total)` 進行跨實例聚合。
