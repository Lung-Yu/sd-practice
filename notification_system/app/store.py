import threading
from typing import Optional

from .models import Notification


class NotificationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, Notification] = {}
        self._by_key: dict[str, str] = {}
        self._by_user: dict[str, list[str]] = {}

    def get(self, notification_id: str) -> Optional[Notification]:
        return self._by_id.get(notification_id)

    def get_by_key(self, idempotency_key: str) -> Optional[Notification]:
        nid = self._by_key.get(idempotency_key)
        if nid is None:
            return None
        return self._by_id.get(nid)

    def save(self, notification: Notification) -> None:
        with self._lock:
            self._by_id[notification.notification_id] = notification
            self._by_key[notification.idempotency_key] = notification.notification_id
            self._by_user.setdefault(notification.user_id, [])
            if notification.notification_id not in self._by_user[notification.user_id]:
                self._by_user[notification.user_id].append(notification.notification_id)

    def list_for_user(self, user_id: str) -> list[Notification]:
        ids = self._by_user.get(user_id, [])
        return [self._by_id[nid] for nid in ids if nid in self._by_id]


store = NotificationStore()
