import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8100";

// Redirect-only stress test — isolates the cache-hit redirect path
// from the create/DB path to find the true redirect QPS ceiling.
export const options = {
  tags: { service: "qr-code", scenario: "redirect-only" },

  scenarios: {
    redirect_stress: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: 300,
      maxVUs: 5000,
      stages: [
        { duration: "20s", target: 1000 }, // warm-up
        { duration: "30s", target: 3000 }, // ramp
        { duration: "30s", target: 5000 }, // push
        { duration: "60s", target: 6000 }, // hold at 6000 — find ceiling
        { duration: "20s", target: 0    }, // ramp down
      ],
    },
  },

  thresholds: {
    http_req_duration:              ["p(95)<500"],
    http_req_failed:                ["rate<0.01"],
    "checks{scenario:redirect}":   ["rate>0.99"],
  },
};

export function setup() {
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
  for (let i = 0; i < 500; i++) {
    const res = http.post(
      `${BASE_URL}/api/qr/create`,
      JSON.stringify({ url: urls[i % urls.length] + "?seed=" + i }),
      { headers: { "Content-Type": "application/json" }, tags: { name: "setup_create" } }
    );
    if (res.status === 200) tokens.push(JSON.parse(res.body).token);
    // Throttle to ~40 req/s to stay under the 60/s rate limit per IP.
    // Without this, all 500 creates land in one 1-second window → only 60 seeded.
    sleep(0.025);
  }
  console.log(`Setup: seeded ${tokens.length} tokens into Redis cache`);
  return { tokens };
}

export default function (data) {
  if (data.tokens.length === 0) return;
  const token = data.tokens[Math.floor(Math.random() * data.tokens.length)];
  const res = http.get(`${BASE_URL}/r/${token}`, {
    redirects: 0,
    tags: { name: "redirect", scenario: "redirect" },
  });
  check(res, { "redirect → 302": (r) => r.status === 302 }, { scenario: "redirect" });
}
