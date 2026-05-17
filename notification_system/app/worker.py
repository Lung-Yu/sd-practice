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


async def _process_message(
    r: aioredis.Redis,
    msg_id: str,
    data: dict,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Fetch, deliver, and ACK one stream message. Runs concurrently with peers."""
    nid = data.get("notification_id")
    if nid:
        notification = await store.aget(nid)
        if notification is not None:
            # deliver() uses ThreadPoolExecutor internally for timeouts, so run
            # it in the executor to avoid blocking the event loop during retries.
            await loop.run_in_executor(None, deliver, notification)
    # ACK regardless — phantom/already-deleted notifications must not requeue.
    await r.xack(STREAM_KEY, GROUP_NAME, msg_id)


async def run() -> None:
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True, max_connections=20)
    await _wait_for_redis(r)
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
        tasks = []
        for _stream, msgs in (messages or []):
            for msg_id, data in msgs:
                tasks.append(_process_message(r, msg_id, data, loop))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for exc in results:
                if isinstance(exc, Exception):
                    print(f"[worker] unhandled error in batch: {exc}", flush=True)

    print("[worker] shutting down gracefully", flush=True)


if __name__ == "__main__":
    if not config.REDIS_URL:
        print("REDIS_URL is not set — worker requires Redis", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run())
