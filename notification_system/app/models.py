from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class Notification:
    notification_id: str
    user_id: str
    channel: str
    message: str
    topic: str
    idempotency_key: str
    status: NotificationStatus = NotificationStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None
    error: Optional[str] = None
    attempts: int = 0
