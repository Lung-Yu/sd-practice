# Notification System — Design Exercise

## System Overview

Build a notification service that delivers messages to users across multiple channels
(Email, SMS, Push). The system must handle high throughput, tolerate channel failures,
and respect per-user delivery preferences.

---

## Functional Requirements

Answer: which of these will your system support?

- [ ] Send a notification to a single user via a specified channel
- [ ] Send a notification to multiple users (broadcast / fan-out)
- [ ] Support multiple channels: Email, SMS, Push
- [ ] Support notification types: transactional, marketing, system alert
- [ ] Track delivery status per notification (pending → sent / failed)
- [ ] Retry failed deliveries
- [ ] User can opt-out of specific channels or notification types
- [ ] Notification templates with variable substitution
- [ ] Priority queue (urgent alerts bypass marketing queue)
- [ ] Scheduled / delayed delivery

---

## Non-Functional Requirements

Define your targets before designing:

| Property | Your Target | Notes |
|----------|------------|-------|
| API response latency (p99) | | e.g. < 100ms (fire-and-forget) or < 2s (wait for ack) |
| Notification throughput | | e.g. 100k/min peak |
| Delivery guarantee | | at-least-once / at-most-once / exactly-once |
| Availability | | e.g. 99.9% |
| Max acceptable delay | | how long before a sent notification must be delivered |
| Data retention | | how long to keep delivery records |

---

## Design Questions

Answer these before writing any code. The trade-offs you choose here determine the architecture.

### 1. Sync vs Async Delivery

Should `POST /send` block until the notification is delivered, or return immediately with a pending ID?

- **Sync**: caller knows outcome immediately, but latency is tied to the slowest channel (SMTP can take seconds)
- **Async**: fast API response, but caller must poll or use webhooks to know the result

→ Your choice and reasoning:

### 2. Queue / Worker Architecture

What sits between the API and the channel providers?

- **No queue**: API calls channel provider directly (simple, no retry, fails if provider is down)
- **In-process queue** (e.g. asyncio tasks): easy, but lost on restart
- **Redis queue** (e.g. RQ / list + BLPOP): durable, single-machine
- **Message broker** (Kafka, RabbitMQ): durable, distributed, ordered partitions, replay

→ Your choice and reasoning:

### 3. Fan-out Strategy

When sending to 1 million users (e.g. marketing blast):

- **Write-time fan-out**: at send time, enqueue one job per user immediately → high write throughput needed, fast per-user delivery
- **Read-time fan-out**: store one broadcast record, each worker reads and expands → lower write load, but more read complexity

→ Your choice and reasoning:

### 4. Delivery Guarantee

If the worker crashes after the channel provider accepted the message but before you mark it `sent`:

- **At-least-once**: re-enqueue on crash → user may receive duplicates; needs idempotency key at provider level
- **At-most-once**: mark sent before delivering → no duplicates, but may lose messages on crash
- **Exactly-once**: two-phase commit or idempotency + deduplication → complex, slow

→ Your choice and reasoning:

### 5. Rate Limiting

Prevent spamming a single user too many notifications per minute:

- **Token bucket**: smooth out bursts, allows short spikes
- **Fixed window counter** (INCR/EXPIRE in Redis): simple, but allows 2× burst at window boundary
- **Sliding window** (sorted set in Redis): accurate, slightly more Redis ops

→ Where do you enforce limits (API layer vs worker)? Which algorithm?

### 6. Template Rendering: When?

- **At API time**: render before enqueueing → payload is self-contained, no template lookup in worker
- **At worker time**: store template ID + params → smaller queue payload, but worker needs access to template store

→ Your choice and reasoning:

### 7. User Preferences Check: Where?

- **At API layer**: reject immediately if user opted out → saves queue space, but preferences must be fast to query
- **At worker layer**: check before delivering → decoupled, but wastes queue capacity on skipped jobs

→ Your choice and reasoning:

### 8. Channel Fallback

If the primary channel fails (e.g. SMS provider down), should the system:

- **Retry same channel** with backoff: simpler, respects user's channel preference
- **Fallback to secondary channel**: higher delivery rate, but may surprise the user

→ Your choice and reasoning:

### 9. Delivery Status: How Does the Caller Know?

- **Polling**: `GET /notifications/{id}` — simple, adds load
- **Webhook**: caller provides a callback URL — low latency, but caller must expose an endpoint
- **Both**: flexible, standard approach

→ Your choice and reasoning:

---

## API Design

Sketch your endpoints before building:

```
POST   /api/notifications/send          # enqueue a notification
GET    /api/notifications/{id}          # get delivery status
GET    /api/notifications/?user_id=X   # list for a user

GET    /api/users/{id}/preferences      # get opt-in/out settings
PUT    /api/users/{id}/preferences      # update settings
```

Do you need any other endpoints? Change the above if your requirements differ.

---

## Data Model

Sketch your tables / collections. At minimum consider:

- `notifications` — one row per delivery attempt (what fields?)
- `user_preferences` — opt-in/out (keyed by what?)
- Do you need a separate `templates` table or keep templates in code?

---

## Verification

Your implementation should pass all of these:

```bash
# Send a transactional notification
curl -X POST http://localhost:8000/api/notifications/send \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "channel": "email", "type": "transactional",
       "template_id": "welcome", "template_params": {"name": "Alice", "code": "9999"}}'
# → 200, {"notification_id": "...", "status": "pending"}

# Poll status until sent
curl http://localhost:8000/api/notifications/{id}
# → {"status": "sent", "sent_at": "..."}

# Opt out of marketing emails
curl -X PUT http://localhost:8000/api/users/u1/preferences \
  -H "Content-Type: application/json" \
  -d '[{"channel": "email", "type": "marketing", "enabled": false}]'
# → 200

# Send marketing email — should be skipped
curl -X POST http://localhost:8000/api/notifications/send \
  -d '{"user_id": "u1", "channel": "email", "type": "marketing", ...}'
# → notification_id returned, status eventually becomes "skipped"

# Rate limit — send 6 emails in 1 minute (limit is 5)
for i in $(seq 1 6); do curl -X POST .../send ...; done
# → first 5 succeed, 6th returns 429 (if enforced at API) or status=rate_limited (if at worker)

# Unknown template
curl -X POST .../send -d '{"template_id": "nonexistent", ...}'
# → 404
```

---

## Suggested Tech Stack

Python + FastAPI recommended, but any language/framework is fine.

External services to simulate (no real credentials needed):
- Email → print to stdout with simulated failure rate
- SMS → same
- Push → same
