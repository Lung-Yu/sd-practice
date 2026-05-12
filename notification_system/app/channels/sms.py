import random
from datetime import datetime

from .. import config
from .base import BaseChannel, ChannelDeliveryError


class SMSChannel(BaseChannel):
    def send(self, user_id: str, message: str) -> None:
        if random.random() < config.FAILURE_RATE:
            raise ChannelDeliveryError("sms: carrier timeout (simulated)")
        print(f"[SMS]   user={user_id} msg={message!r} at={datetime.utcnow().isoformat()}")
