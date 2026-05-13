"""
Delivery worker process.

Run with:
    python -m app.worker

Reads pending notification IDs from the Redis Stream via consumer group
'delivery-workers'. Multiple worker containers share the group — Redis ensures
each message is delivered to exactly one consumer, so delivery is never duplicated
even with N workers competing.

Consumer name = hostname (unique per container in docker-compose).
"""
import signal
import socket
import sys

import redis

from . import config
from .delivery import deliver
from .queue import GROUP_NAME, STREAM_KEY, ensure_group
from .store import store

BATCH_SIZE = 10   # messages to claim per XREADGROUP call
BLOCK_MS = 1000   # how long to block waiting for new messages

CONSUMER_NAME = socket.gethostname()


def _wait_for_redis(r: redis.Redis, retries: int = 15, delay: float = 1.0) -> None:
    """Block until Redis is ready to accept commands (handles BusyLoadingError on AOF replay)."""
    for attempt in range(1, retries + 1):
        try:
            r.ping()
            return
        except (redis.exceptions.BusyLoadingError, redis.exceptions.ConnectionError) as e:
            print(f"[worker] Redis not ready ({e}), retry {attempt}/{retries}…", flush=True)
            import time
            time.sleep(delay)
    raise RuntimeError("Redis did not become ready in time")


def run() -> None:
    r = redis.from_url(config.REDIS_URL, decode_responses=True)
    _wait_for_redis(r)
    ensure_group()

    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[worker] {CONSUMER_NAME} ready — consuming {STREAM_KEY}/{GROUP_NAME}", flush=True)

    while running:
        messages = r.xreadgroup(
            GROUP_NAME,
            CONSUMER_NAME,
            {STREAM_KEY: ">"},   # ">" = only unclaimed messages
            count=BATCH_SIZE,
            block=BLOCK_MS,
        )
        for _stream, msgs in (messages or []):
            for msg_id, data in msgs:
                nid = data.get("notification_id")
                if nid:
                    notification = store.get(nid)
                    if notification is not None:
                        deliver(notification)
                # ACK regardless — if store.get() returns None the notification
                # was already deleted or this is a phantom; don't requeue.
                r.xack(STREAM_KEY, GROUP_NAME, msg_id)

    print("[worker] shutting down gracefully", flush=True)


if __name__ == "__main__":
    if not config.REDIS_URL:
        print("REDIS_URL is not set — worker requires Redis", file=sys.stderr)
        sys.exit(1)
    run()
