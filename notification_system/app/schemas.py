from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SendRequest(BaseModel):
    user_id: str
    channel: str
    message: str
    topic: str = "default"


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
