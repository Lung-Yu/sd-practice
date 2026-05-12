# Notification System — Exercise

## How to Use

1. Read `PROMPT.md`
2. Answer the Design Questions — fill in your choices directly in `PROMPT.md`
3. Sketch your API and data model in `PROMPT.md`
4. Build the prototype:
   - **Challenge Track:** Build from scratch based on your design decisions
   - **Guided Track:** `scaffold/` will be provided once design is finalised
5. Verify with the curl tests at the bottom of `PROMPT.md`

## Key Design Decisions to Make First

Before writing any code, answer these in `PROMPT.md`:

| Decision | Why it matters |
|----------|---------------|
| Sync vs Async delivery | Determines API contract and architecture |
| Queue technology | Durability, retry, throughput |
| Fan-out strategy | Shapes the worker and DB write patterns |
| Delivery guarantee | At-least-once vs exactly-once complexity |
| Rate limiting algorithm | Where to enforce and which counter approach |
| Template rendering timing | Queue payload size vs worker coupling |
| Preferences check location | API vs worker layer trade-off |
