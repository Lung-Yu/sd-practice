from abc import ABC, abstractmethod


class ChannelDeliveryError(Exception):
    pass


class BaseChannel(ABC):
    @abstractmethod
    def send(self, user_id: str, message: str) -> None:
        """Attempt delivery. Raises ChannelDeliveryError on failure."""
        ...
