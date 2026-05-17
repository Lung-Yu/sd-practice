"""
Async delivery worker process.

Run with:
    python -m app.worker

Reads pending notification IDs from the Redis Stream via consumer group
'delivery-workers'. Multiple worker containers share the group — Redis ensures
each message is delivered to exactly one consumer, so delivery is never duplicated
even with N workers competing.

Tier 4 change: async main loop + asyncio.gather() for concurrent batch delivery.
Previously each batch of BATCH_SIZE messages was processed sequentially (total
time = sum of delivery times). Now they run concurrently (total time = max of
delivery times). channel.send() is still sync (simulates network I/O) so it
runs in the default thread executor — no event-loop blocking.

Tier 7+ batch fetch: abatch_get() pipelines N HGETALLs → 1 round-trip to primary
Redis (vs N individual round-trips). Batch XACK collapses N XACKs → 1 command to
delivery Redis per batch.

Consumer name = hostname (unique per container in docker-compose).
"""
import asyncio
import signal
import socket
import sys

import redis
import redis.asyncio as aioredis
from prometheus_client import start_http_server

from . import config
from .delivery import deliver
from .queue import GROUP_NAME, STREAM_KEY
from .store import store

METRICS_PORT = 8001   # delivery-worker exposes /metrics on this port

BATCH_SIZE = config.WORKER_BATCH_SIZE   # set via WORKER_BATCH_SIZE env var
BLOCK_MS = 1000   # how long to block waiting for new messages

CONSUMER_NAME = socket.gethostname()


async def _wait_for_redis(r: aioredis.Redis, retries: int = 60, delay: float = 2.0) -> None:
    """Block until Redis is ready (handles BusyLoadingError on AOF replay)."""
    for attempt in range(1, retries + 1):
        try:
            await r.ping()
            return
        except (redis.exceptions.BusyLoadingError, redis.exceptions.ConnectionError) as e:
            print(f"[worker] Redis not ready ({e}), retry {attempt}/{retries}…", flush=True)
            await asyncio.sleep(delay)
    raise RuntimeError("Redis did not become ready in time")


async def _ensure_group(r: aioredis.Redis) -> None:
    try:
        await r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except redis.exceptions.ResponseError:
        pass  # group already exists


async def _process_batch(
    r: aioredis.Redis,
    msgs: list[tuple[str, dict]],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Fetch all notifications in one pipeline round-trip, deliver concurrently, ACK in batch."""
    msg_ids = [msg_id for msg_id, _ in msgs]
    nids = [data.get("notification_id") for _, data in msgs]

    # One pipeline round-trip to primary Redis (vs N individual HGETALLs).
    notifications = await store.abatch_get([nid for nid in nids if nid])

    # Map nid → notification for lookup (abatch_get preserves order of non-None nids).
    nid_list = [nid for nid in nids if nid]
    nid_to_notif = {nid: n for nid, n in zip(nid_list, notifications) if n is not None}

    # Concurrent delivery — one executor task per notification.
    deliver_tasks = []
    for nid in nids:
        notif = nid_to_notif.get(nid) if nid else None
        if notif is not None:
            deliver_tasks.append(loop.run_in_executor(None, deliver, notif))
        else:
            deliver_tasks.append(asyncio.sleep(0))

    results = await asyncio.gather(*deliver_tasks, return_exceptions=True)
    for exc in results:
        if isinstance(exc, Exception):
            print(f"[worker] delivery error: {exc}", flush=True)

    # Batch ACK: 1 XACK command for the whole batch (vs N individual XACKs).
    # ACK regardless — phantom/already-deleted notifications must not requeue.
    if msg_ids:
        await r.xack(STREAM_KEY, GROUP_NAME, *msg_ids)


async def run() -> None:
    # Tier 7: XREADGROUP/XACK on delivery Redis; store reads/writes on primary Redis.
    r = aioredis.from_url(config.DELIVERY_REDIS_URL, decode_responses=True, max_connections=20)
    await _wait_for_redis(r)

    # Also wait for primary Redis — abatch_get() hits store._ar which connects
    # to REDIS_URL. Primary Redis may still be loading AOF when delivery Redis is
    # already available (they are independent processes with separate AOF files).
    # Without this wait, the worker enters a tight error loop at startup.
    primary_r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await _wait_for_redis(primary_r)
    await primary_r.aclose()

    await _ensure_group(r)

    start_http_server(METRICS_PORT)
    print(f"[worker] metrics server listening on :{METRICS_PORT}", flush=True)

    loop = asyncio.get_event_loop()
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[worker] {CONSUMER_NAME} ready — consuming {STREAM_KEY}/{GROUP_NAME}", flush=True)

    while running:
        messages = await r.xreadgroup(
            GROUP_NAME,
            CONSUMER_NAME,
            {STREAM_KEY: ">"},   # ">" = only unclaimed messages
            count=BATCH_SIZE,
            block=BLOCK_MS,
        )
        all_msgs: list[tuple[str, dict]] = []
        for _stream, msgs in (messages or []):
            all_msgs.extend(msgs)

        if all_msgs:
            try:
                await _process_batch(r, all_msgs, loop)
            except Exception as exc:
                print(f"[worker] unhandled batch error: {exc}", flush=True)

    print("[worker] shutting down gracefully", flush=True)


if __name__ == "__main__":
    if not config.REDIS_URL:
        print("REDIS_URL is not set — worker requires Redis", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run())
