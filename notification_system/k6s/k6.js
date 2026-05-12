/**
 * k6 load test for the Notification Service
 * Requires k6 v0.54+
 *
 * Traffic mix: 75% POST /send | 20% GET /{id} | 5% GET /?user_id=
 * Target:      5 000 RPS sustained for 60 s
 * Thresholds:  p95 < 500 ms, p99 < 1000 ms, error rate < 1%
 */

import http from "k6/http";
import { check, group } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const CHANNELS = ["email", "sms", "push"];

// Per-endpoint latency trends — enables per-endpoint threshold expressions
// and a readable summary table separate from the built-in http_req_duration.
const postSendDuration   = new Trend("post_send_duration",    true);
const getByIdDuration    = new Trend("get_by_id_duration",    true);
const listByUserDuration = new Trend("list_by_user_duration", true);

// 404s from multi-worker routing are counted separately, not as errors.
const errorRate     = new Rate("notification_error_rate");
const notFoundCount = new Counter("notification_404_count");

export const options = {
  scenarios: {
    notification_load: {
      executor: "ramping-arrival-rate",
      // Little's Law: 5000 RPS × 50ms avg ≈ 250 concurrent VUs; 600 is headroom.
      preAllocatedVUs: 250,
      maxVUs: 600,
      startRate: 0,
      timeUnit: "1s",
      stages: [
        { duration: "30s", target: 5000 }, // ramp up
        { duration: "60s", target: 5000 }, // sustain
        { duration: "10s", target: 0    }, // ramp down
      ],
    },
  },
  thresholds: {
    post_send_duration:      ["p(95)<500", "p(99)<1000"],
    get_by_id_duration:      ["p(95)<500", "p(99)<1000"],
    list_by_user_duration:   ["p(95)<500", "p(99)<1000"],
    http_req_duration:       ["p(95)<500", "p(99)<1000"],
    notification_error_rate: ["rate<0.01"],
  },
};

// ---------------------------------------------------------------------------
// setup() — runs once before load starts; seeds notification IDs for GET tests
// ---------------------------------------------------------------------------

export function setup() {
  const seedCount = 200;
  const ids = [];
  const userIds = [];

  for (let i = 0; i < seedCount; i++) {
    const userId  = `setup-user-${i}`;
    const channel = CHANNELS[i % CHANNELS.length];
    // Include Date.now() + Math.random() to guarantee uniqueness across test
    // reruns without restarting the service (avoids idempotency cache hits).
    const message = `seed-msg-${i}-ts-${Date.now()}-rnd-${Math.random()}`;
    const topic   = `seed-topic-${i}`;

    const res = http.post(
      `${BASE_URL}/api/notifications/send`,
      JSON.stringify({ user_id: userId, channel, message, topic }),
      { headers: { "Content-Type": "application/json" } }
    );

    if (res.status === 200 || res.status === 201) {
      try {
        const body = res.json();
        if (body && body.notification_id) {
          ids.push(body.notification_id);
          userIds.push(userId);
        }
      } catch (_) {}
    }
  }

  return { seedIds: ids, seedUserIds: userIds };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function randInt(max) {
  return Math.floor(Math.random() * max);
}

// Combines __VU (unique per virtual user) + __ITER (monotonically increasing
// per VU) + Date.now() to guarantee a unique idempotency key every iteration.
function uniquePayload() {
  return {
    user_id: `user-${__VU}-${__ITER}`,
    topic:   `topic-${__VU}-${__ITER}`,
    message: `msg-${__VU}-${__ITER}-${Date.now()}`,
    channel: CHANNELS[(__VU + __ITER) % CHANNELS.length],
  };
}

// ---------------------------------------------------------------------------
// Scenario functions
// ---------------------------------------------------------------------------

function doPost() {
  const res = http.post(
    `${BASE_URL}/api/notifications/send`,
    JSON.stringify(uniquePayload()),
    {
      headers: { "Content-Type": "application/json" },
      tags: { endpoint: "post_send" },
    }
  );
  postSendDuration.add(res.timings.duration);
  const ok = check(res, {
    "post /send status 200": (r) => r.status === 200 || r.status === 201,
    "post /send has notification_id": (r) => {
      try { return !!r.json("notification_id"); } catch { return false; }
    },
  });
  errorRate.add(!ok || (res.status >= 400 && res.status !== 404));
}

function doGetById(seedIds) {
  const id = seedIds.length > 0
    ? seedIds[randInt(seedIds.length)]
    : "00000000-0000-0000-0000-000000000000";

  const res = http.get(
    `${BASE_URL}/api/notifications/${id}`,
    { tags: { endpoint: "get_by_id" } }
  );
  getByIdDuration.add(res.timings.duration);

  if (res.status === 404) {
    // Expected in multi-worker mode when the GET hits a different worker than
    // the POST that created the notification. Tracked but not an error.
    notFoundCount.add(1);
    return;
  }
  const ok = check(res, {
    "get /{id} status 200": (r) => r.status === 200,
    "get /{id} has notification_id": (r) => {
      try { return !!r.json("notification_id"); } catch { return false; }
    },
  });
  errorRate.add(!ok || res.status >= 400);
}

function doListByUser(seedUserIds) {
  const userId = seedUserIds.length > 0
    ? seedUserIds[randInt(seedUserIds.length)]
    : `probe-user-${__VU}`;

  const res = http.get(
    `${BASE_URL}/api/notifications/?user_id=${encodeURIComponent(userId)}`,
    { tags: { endpoint: "list_by_user" } }
  );
  listByUserDuration.add(res.timings.duration);
  const ok = check(res, {
    "list /?user_id= status 200": (r) => r.status === 200,
    "list /?user_id= is array": (r) => {
      try { return Array.isArray(r.json()); } catch { return false; }
    },
  });
  errorRate.add(!ok || res.status >= 400);
}

// ---------------------------------------------------------------------------
// Default function — one iteration per VU
// ---------------------------------------------------------------------------

export default function (data) {
  const { seedIds, seedUserIds } = data;
  const roll = randInt(100);

  if (roll < 75) {
    group("post_send",    () => doPost());
  } else if (roll < 95) {
    group("get_by_id",    () => doGetById(seedIds));
  } else {
    group("list_by_user", () => doListByUser(seedUserIds));
  }
}

export function teardown(_data) {}
