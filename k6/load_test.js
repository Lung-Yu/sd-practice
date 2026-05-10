import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8100";

export const options = {
  tags: { service: "qr-code" },
  stages: [
    { duration: "10s", target: 10 }, // ramp up
    { duration: "30s", target: 20 }, // sustained load
    { duration: "10s", target: 0 },  // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
    "checks{scenario:redirect}": ["rate>0.99"],
    "checks{scenario:create}": ["rate>0.99"],
  },
};

export function setup() {
  const urls = [
    "https://github.com",
    "https://google.com",
    "https://cloudflare.com",
    "https://fastapi.tiangolo.com",
    "https://docs.python.org",
  ];
  const tokens = [];
  for (let i = 0; i < 20; i++) {
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: urls[i % urls.length] + "?seed=" + i }),
      { headers: { "Content-Type": "application/json" }, tags: { name: "setup_create" } }
    );
    if (res.status === 200) tokens.push(JSON.parse(res.body).token);
  }
  return { tokens };
}

export default function (data) {
  const roll = Math.random();

  if (roll < 0.70 && data.tokens.length > 0) {
    // Hot path: redirect (tagged for Prometheus label filtering)
    const token = data.tokens[Math.floor(Math.random() * data.tokens.length)];
    const res = http.get(`${BASE_URL}/r/${token}`, {
      redirects: 0,
      tags: { name: "redirect", scenario: "redirect" },
    });
    check(res, { "redirect → 302": (r) => r.status === 302 }, { scenario: "redirect" });

  } else if (roll < 0.90) {
    // Create a new QR code
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: "https://example.com/load?ts=" + Date.now() }),
      {
        headers: { "Content-Type": "application/json" },
        tags: { name: "create", scenario: "create" },
      }
    );
    check(res, { "create → 200": (r) => r.status === 200 }, { scenario: "create" });

  } else {
    // 404 probe
    const res = http.get(`${BASE_URL}/r/INVALID`, {
      redirects: 0,
      tags: { name: "not_found", scenario: "probe" },
    });
    check(res, { "probe → 404": (r) => r.status === 404 }, { scenario: "probe" });
  }

  sleep(0.1);
}
