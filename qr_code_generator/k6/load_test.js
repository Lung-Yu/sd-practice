import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8100";

export const options = {
  tags: { service: "qr-code" },

  // ramping-arrival-rate drives a fixed RPS target regardless of response time.
  // The executor adds VUs automatically when the server slows down, revealing the true limit.
  scenarios: {
    stress: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: 200,
      maxVUs: 3000,
      stages: [
        { duration: "30s", target: 500  }, // warm-up
        { duration: "60s", target: 2000 }, // ramp
        { duration: "60s", target: 5000 }, // push to 5000 QPS
        { duration: "60s", target: 5000 }, // hold — find the ceiling
        { duration: "30s", target: 0    }, // ramp down
      ],
    },
  },

  // Relaxed thresholds for a stress/limit-finding run
  thresholds: {
    http_req_duration:           ["p(95)<3000"],  // warn above 3 s p95
    http_req_failed:             ["rate<0.10"],   // allow up to 10% errors before failing
    "checks{scenario:redirect}": ["rate>0.90"],
    "checks{scenario:create}":   ["rate>0.90"],
  },
};

export function setup() {
  // Pre-seed 200 tokens so the redirect hot path hits the in-memory cache
  const urls = [
    "https://github.com",
    "https://google.com",
    "https://cloudflare.com",
    "https://fastapi.tiangolo.com",
    "https://docs.python.org",
    "https://pypi.org",
    "https://stackoverflow.com",
    "https://developer.mozilla.org",
    "https://aws.amazon.com",
    "https://kubernetes.io",
  ];
  const tokens = [];
  for (let i = 0; i < 200; i++) {
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: urls[i % urls.length] + "?seed=" + i }),
      { headers: { "Content-Type": "application/json" }, tags: { name: "setup_create" } }
    );
    if (res.status === 200) tokens.push(JSON.parse(res.body).token);
  }
  console.log(`Setup complete: seeded ${tokens.length} tokens`);
  return { tokens };
}

export default function (data) {
  const roll = Math.random();

  if (roll < 0.70 && data.tokens.length > 0) {
    // 70% — redirect hot path (cache hits)
    const token = data.tokens[Math.floor(Math.random() * data.tokens.length)];
    const res = http.get(`${BASE_URL}/r/${token}`, {
      redirects: 0,
      tags: { name: "redirect", scenario: "redirect" },
    });
    check(res, { "redirect → 302": (r) => r.status === 302 }, { scenario: "redirect" });

  } else if (roll < 0.90) {
    // 20% — create (write path, hits DB)
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: "https://example.com/stress?ts=" + Date.now() }),
      {
        headers: { "Content-Type": "application/json" },
        tags: { name: "create", scenario: "create" },
      }
    );
    check(res, { "create → 200": (r) => r.status === 200 }, { scenario: "create" });

  } else {
    // 10% — 404 probe (cache miss → DB miss)
    const res = http.get(`${BASE_URL}/r/INVALID`, {
      redirects: 0,
      tags: { name: "not_found", scenario: "probe" },
    });
    check(res, { "probe → 404": (r) => r.status === 404 }, { scenario: "probe" });
  }
  // No sleep — arrival-rate executor controls throughput
}
