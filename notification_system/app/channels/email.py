import random
from datetime import datetime

from .. import config
from .base import BaseChannel, ChannelDeliveryError


class EmailChannel(BaseChannel):
    def send(self, user_id: str, message: str) -> None:
        if random.random() < config.FAILURE_RATE:
            raise ChannelDeliveryError("email: SMTP connection refused (simulated)")
        print(f"[EMAIL] user={user_id} msg={message!r} at={datetime.utcnow().isoformat()}")
