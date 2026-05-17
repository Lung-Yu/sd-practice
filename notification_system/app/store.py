import threading
from typing import Optional

from .models import Notification


class NotificationStore:
    def __init__(self) -> None:
        # Short-held lock for the two global indices (fast dict ops only).
        self._id_lock = threading.Lock()
        self._by_id: dict[str, Notification] = {}
        self._by_key: dict[str, str] = {}

        # Per-user locks so concurrent writes for different users don't contend.
        self._user_locks_lock = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        # set[str] gives O(1) membership vs the prior O(n) list scan.
        self._by_user: dict[str, set[str]] = {}

    def _user_lock(self, user_id: str) -> threading.Lock:
        with self._user_locks_lock:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = threading.Lock()
            return self._user_locks[user_id]

    def get(self, notification_id: str) -> Optional[Notification]:
        return self._by_id.get(notification_id)

    def get_by_key(self, idempotency_key: str) -> Optional[Notification]:
        nid = self._by_key.get(idempotency_key)
        return None if nid is None else self._by_id.get(nid)

    def save(self, notification: Notification) -> None:
        with self._id_lock:
            self._by_id[notification.notification_id] = notification
            self._by_key[notification.idempotency_key] = notification.notification_id
        with self._user_lock(notification.user_id):
            self._by_user.setdefault(notification.user_id, set()).add(notification.notification_id)

    def list_for_user(self, user_id: str) -> list[Notification]:
        with self._user_lock(user_id):
            ids = set(self._by_user.get(user_id, set()))  # snapshot while holding lock
        return [self._by_id[nid] for nid in ids if nid in self._by_id]

    def save_status(self, notification: Notification) -> None:
        # In-memory: status update is the same as a full save (no extra overhead).
        self.save(notification)

    # -- async shims: in-memory ops are instant, no awaitable needed -----------
    async def aget(self, notification_id: str) -> Optional[Notification]:
        return self.get(notification_id)

    async def aget_by_key(self, idempotency_key: str) -> Optional[Notification]:
        return self.get_by_key(idempotency_key)

    async def asave(self, notification: Notification) -> None:
        self.save(notification)

    async def asave_status(self, notification: Notification) -> None:
        self.save(notification)

    async def aget_existing_keys(self, idempotency_keys: list[str]) -> set[str]:
        return {k for k in idempotency_keys if self._by_key.get(k) is not None}

    async def asave_batch(self, notifications: list[Notification]) -> None:
        for n in notifications:
            self.save(n)

    async def alist_for_user(self, user_id: str) -> list[Notification]:
        return self.list_for_user(user_id)


def _make_store() -> "NotificationStore":
    import os
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        from .store_redis import RedisNotificationStore
        return RedisNotificationStore(redis_url)  # type: ignore[return-value]
    return NotificationStore()


store = _make_store()
