from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SendRequest(BaseModel):
    user_id: str
    channel: str
    message: str
    topic: str = "default"
    priority: str = "normal"  # "normal" | "critical"


class SendResponse(BaseModel):
    notification_id: str
    status: str


class NotificationDetail(BaseModel):
    notification_id: str
    user_id: str
    channel: str
    status: str
    created_at: datetime
    sent_at: Optional[datetime] = None
    error: Optional[str] = None


class NotificationSummary(BaseModel):
    notification_id: str
    status: str


class FanoutRequest(BaseModel):
    user_ids: list[str]
    channel: str
    message: str
    topic: str = "default"
    priority: str = "normal"  # "normal" | "critical"


class FanoutResponse(BaseModel):
    fanout_id: str          # shared trace ID for this fan-out batch
    user_count: int
    notification_ids: list[str]
    skipped: int            # idempotency dedup hits
