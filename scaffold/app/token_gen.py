import hashlib
import string
import time

from sqlalchemy.orm import Session

from .models import UrlMapping

BASE62_CHARS = string.ascii_letters + string.digits  # a-zA-Z0-9
TOKEN_LENGTH = 7
MAX_RETRIES = 10


def base62_encode(data: bytes) -> str:
    """Convert bytes to Base62 string."""
    num = int.from_bytes(data, "big")
    if num == 0:
        return BASE62_CHARS[0]
    result = []
    while num > 0:
        num, remainder = divmod(num, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def token_exists_in_db(db: Session, token: str) -> bool:
    return db.query(UrlMapping).filter(UrlMapping.token == token).first() is not None


def generate_token(url: str, db: Session) -> str:
    """SHA-256 + nonce + Base62 token generation with collision retry."""
    for attempt in range(MAX_RETRIES):
        nonce = str(attempt) + str(time.time_ns())
        digest = hashlib.sha256((url + nonce).encode()).digest()
        token = base62_encode(digest)[:TOKEN_LENGTH]
        if not token_exists_in_db(db, token):
            return token
    raise RuntimeError("Token generation failed after max retries")
