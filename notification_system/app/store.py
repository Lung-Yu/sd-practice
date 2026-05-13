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


store = NotificationStore()
