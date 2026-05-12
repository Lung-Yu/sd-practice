from .base import BaseChannel
from .email import EmailChannel
from .push import PushChannel
from .sms import SMSChannel

_REGISTRY: dict[str, type[BaseChannel]] = {
    "email": EmailChannel,
    "sms": SMSChannel,
    "push": PushChannel,
}


def get_channel(name: str) -> BaseChannel:
    cls = _REGISTRY.get(name.lower())
    if cls is None:
        raise ValueError(f"Unknown channel: {name!r}. Valid channels: {list(_REGISTRY)}")
    return cls()
