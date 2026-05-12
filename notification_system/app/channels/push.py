import random
from datetime import datetime

from .. import config
from .base import BaseChannel, ChannelDeliveryError


class PushChannel(BaseChannel):
    def send(self, user_id: str, message: str) -> None:
        if random.random() < config.FAILURE_RATE:
            raise ChannelDeliveryError("push: FCM token expired (simulated)")
        print(f"[PUSH]  user={user_id} msg={message!r} at={datetime.utcnow().isoformat()}")
