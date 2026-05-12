# Notification System Prototype

## System Requirements

Build a notification service where:
- Callers send a notification to a user via a specified channel (email, SMS, push)
- Supported channels are simulated — no real credentials needed, just log to stdout
- Each notification has a status that transitions as delivery progresses
- If the channel fails to deliver, the failure is recorded
- Callers can query the status of any notification by ID

## Design Questions

Answer these before you start coding:

1. **Sync vs Async:** Should `POST /send` block until delivery completes, or return a pending ID immediately and deliver in the background? What are the trade-offs for latency, reliability, and API simplicity?

-> Sync (reliability is more important), latency low as possible

2. **Status Lifecycle:** What states does a notification go through from creation to final outcome? Draw the state machine (e.g. pending → ? → ?). What triggers each transition?

->  must has aggreate service for deduplicate notification data

3. **Channel Abstraction:** Email, SMS, and push have different APIs and failure modes. How do you model them so adding a fourth channel later requires minimal code changes?

-> support muti-channel , but current project is demo sample can simluate on dashboard or api to see result 

4. **Simulated Failure:** Real channels fail intermittently. How will you simulate this? What failure rate is realistic per channel, and how does a caller distinguish "not yet delivered" from "permanently failed"?

-> add retry process

5. **Idempotency:** If the caller sends the exact same notification twice (network retry), should the system create two records or deduplicate? What key would you use to detect a duplicate?

-> user message has topic and content (like FCM service )，i use hash function for deduplicate. hash(user_id, message_topic, message_content)

## Verification

Your prototype should pass all of these:

```bash
# Send a notification
curl -X POST http://localhost:8000/api/notifications/send \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u1", "channel": "email", "message": "Your order has shipped."}'
# → 200, {"notification_id": "...", "status": "..."}

# Check status
curl http://localhost:8000/api/notifications/{notification_id}
# → 200, {"notification_id": "...", "user_id": "u1", "channel": "email",
#          "status": "sent", "created_at": "...", "sent_at": "..."}

# Unknown ID
curl http://localhost:8000/api/notifications/nonexistent
# → 404

# Failed delivery (trigger by simulating a failure)
# status should be "failed", error field should be set
curl http://localhost:8000/api/notifications/{failed_id}
# → 200, {"status": "failed", "error": "..."}

# List notifications for a user
curl "http://localhost:8000/api/notifications/?user_id=u1"
# → 200, [{"notification_id": "...", "status": "sent"}, ...]
```

## Suggested Tech Stack

Python + FastAPI recommended, but any language/framework is fine.

---

## Later Phases (do not implement yet)

These will be added progressively once the base works:
- User opt-out preferences
- Retry on failure with backoff
- Per-user rate limiting
- Notification templates
- Fan-out to multiple recipients
- Async worker queue (Redis / Kafka)
- Monitoring and metrics
