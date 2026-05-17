from datetime import datetime
from typing import Optional

import redis as redis_lib
import redis.asyncio as aioredis

from . import config
from .models import Notification, NotificationStatus

_IDEMPOTENCY_TTL = 86_400  # 24 h


def _serialize(n: Notification) -> dict:
    return {
        "notification_id": n.notification_id,
        "user_id": n.user_id,
        "channel": n.channel,
        "message": n.message,
        "topic": n.topic,
        "idempotency_key": n.idempotency_key,
        "status": n.status.value,
        "created_at": n.created_at.isoformat(),
        "sent_at": n.sent_at.isoformat() if n.sent_at else "",
        "error": n.error or "",
        "attempts": str(n.attempts),
    }


def _deserialize(d: dict) -> Notification:
    return Notification(
        notification_id=d["notification_id"],
        user_id=d["user_id"],
        channel=d["channel"],
        message=d["message"],
        topic=d["topic"],
        idempotency_key=d["idempotency_key"],
        status=NotificationStatus(d["status"]),
        created_at=datetime.fromisoformat(d["created_at"]),
        sent_at=datetime.fromisoformat(d["sent_at"]) if d.get("sent_at") else None,
        error=d["error"] or None,
        attempts=int(d["attempts"]),
    )


class RedisNotificationStore:
    """
    Drop-in replacement for NotificationStore backed by Redis.

    Key layout:
      notification:{id}           HASH  — full notification fields
      idempotency:{sha256_key}    STRING (notification_id) TTL 24 h
      user:{user_id}:notifications ZSET  scored by created_at unix timestamp
    """

    def __init__(self, url: str) -> None:
        self._r = redis_lib.from_url(url, decode_responses=True)
        # Pool sized for 600+ VUs: each concurrent coroutine may hold a
        # connection during await pipeline.execute(). max_connections=100
        # was exhausted at ~600 VU concurrency → ConnectionError.
        self._ar = aioredis.from_url(url, decode_responses=True, max_connections=1000)

    # -- read ops (no locking needed; Redis handles concurrency) --------------

    def get(self, notification_id: str) -> Optional[Notification]:
        d = self._r.hgetall(f"notification:{notification_id}")
        return _deserialize(d) if d else None

    def get_by_key(self, idempotency_key: str) -> Optional[Notification]:
        nid = self._r.get(f"idempotency:{idempotency_key}")
        return None if nid is None else self.get(nid)

    # -- write op: single pipeline → atomic from client's perspective ---------

    def save(self, notification: Notification) -> None:
        pipe = self._r.pipeline()
        pipe.hset(f"notification:{notification.notification_id}", mapping=_serialize(notification))
        if config.NOTIFICATION_TTL_S:
            pipe.expire(f"notification:{notification.notification_id}", config.NOTIFICATION_TTL_S)
        pipe.set(
            f"idempotency:{notification.idempotency_key}",
            notification.notification_id,
            ex=_IDEMPOTENCY_TTL,
        )
        pipe.zadd(
            f"user:{notification.user_id}:notifications",
            {notification.notification_id: notification.created_at.timestamp()},
        )
        pipe.execute()

    def save_status(self, notification: Notification) -> None:
        """Update only delivery-outcome fields (status, sent_at, error, attempts).
        Skips idempotency SET and user ZADD — both were written on initial create
        and never change. Reduces delivery-status writes from 3 commands → 1."""
        self._r.hset(
            f"notification:{notification.notification_id}",
            mapping={
                "status": notification.status.value,
                "sent_at": notification.sent_at.isoformat() if notification.sent_at else "",
                "error": notification.error or "",
                "attempts": str(notification.attempts),
            },
        )

    async def asave_status(self, notification: Notification) -> None:
        await self._ar.hset(
            f"notification:{notification.notification_id}",
            mapping={
                "status": notification.status.value,
                "sent_at": notification.sent_at.isoformat() if notification.sent_at else "",
                "error": notification.error or "",
                "attempts": str(notification.attempts),
            },
        )

    # -- list: batch HGETALL via pipeline to avoid N round-trips --------------

    def list_for_user(self, user_id: str) -> list[Notification]:
        ids = self._r.zrange(f"user:{user_id}:notifications", 0, -1)
        if not ids:
            return []
        pipe = self._r.pipeline()
        for nid in ids:
            pipe.hgetall(f"notification:{nid}")
        return [_deserialize(d) for d in pipe.execute() if d]

    # -- async variants: same logic, asyncio client → no thread pool ----------

    async def abatch_get(self, notification_ids: list[str]) -> list[Optional["Notification"]]:
        """Fetch N notifications in one pipeline round-trip instead of N individual HGETALLs."""
        if not notification_ids:
            return []
        pipe = self._ar.pipeline()
        for nid in notification_ids:
            pipe.hgetall(f"notification:{nid}")
        results = await pipe.execute()
        return [_deserialize(d) if d else None for d in results]

    async def aget(self, notification_id: str) -> Optional[Notification]:
        d = await self._ar.hgetall(f"notification:{notification_id}")
        return _deserialize(d) if d else None

    async def aget_by_key(self, idempotency_key: str) -> Optional[Notification]:
        nid = await self._ar.get(f"idempotency:{idempotency_key}")
        return None if nid is None else await self.aget(nid)

    async def aget_existing_keys(self, idempotency_keys: list[str]) -> set[str]:
        """Pipeline GET for N idempotency keys → 1 round-trip.
        Returns the set of keys that already exist (used for fan-out dedup)."""
        if not idempotency_keys:
            return set()
        pipe = self._ar.pipeline()
        for key in idempotency_keys:
            pipe.get(f"idempotency:{key}")
        results = await pipe.execute()
        # Return original keys (not nids) where a value exists
        return {key for key, nid in zip(idempotency_keys, results) if nid is not None}

    async def asave(self, notification: Notification) -> None:
        pipe = self._ar.pipeline()
        pipe.hset(f"notification:{notification.notification_id}", mapping=_serialize(notification))
        if config.NOTIFICATION_TTL_S:
            pipe.expire(f"notification:{notification.notification_id}", config.NOTIFICATION_TTL_S)
        pipe.set(
            f"idempotency:{notification.idempotency_key}",
            notification.notification_id,
            ex=_IDEMPOTENCY_TTL,
        )
        pipe.zadd(
            f"user:{notification.user_id}:notifications",
            {notification.notification_id: notification.created_at.timestamp()},
        )
        await pipe.execute()

    async def asave_batch(self, notifications: list[Notification]) -> None:
        """Persist N notifications in one pipeline round-trip (fan-out write path).
        4N Redis commands (HSET + EXPIRE + SET + ZADD per notification) → 1 round-trip."""
        if not notifications:
            return
        pipe = self._ar.pipeline()
        for n in notifications:
            pipe.hset(f"notification:{n.notification_id}", mapping=_serialize(n))
            if config.NOTIFICATION_TTL_S:
                pipe.expire(f"notification:{n.notification_id}", config.NOTIFICATION_TTL_S)
            pipe.set(
                f"idempotency:{n.idempotency_key}",
                n.notification_id,
                ex=_IDEMPOTENCY_TTL,
            )
            pipe.zadd(
                f"user:{n.user_id}:notifications",
                {n.notification_id: n.created_at.timestamp()},
            )
        await pipe.execute()

    async def alist_for_user(self, user_id: str) -> list[Notification]:
        ids = await self._ar.zrange(f"user:{user_id}:notifications", 0, -1)
        if not ids:
            return []
        pipe = self._ar.pipeline()
        for nid in ids:
            pipe.hgetall(f"notification:{nid}")
        return [_deserialize(d) for d in await pipe.execute() if d]
