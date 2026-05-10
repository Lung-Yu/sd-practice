import hashlib
import string
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UrlMapping

BASE62_CHARS = string.ascii_letters + string.digits
TOKEN_LENGTH = 7
MAX_RETRIES = 10


def base62_encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    if num == 0:
        return BASE62_CHARS[0]
    result = []
    while num > 0:
        num, remainder = divmod(num, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


async def token_exists_in_db(db: AsyncSession, token: str) -> bool:
    result = await db.execute(select(UrlMapping).filter(UrlMapping.token == token))
    return result.scalar_one_or_none() is not None


async def generate_token(url: str, db: AsyncSession) -> str:
    for attempt in range(MAX_RETRIES):
        nonce = str(attempt) + str(time.time_ns())
        digest = hashlib.sha256((url + nonce).encode()).digest()
        token = base62_encode(digest)[:TOKEN_LENGTH]
        if not await token_exists_in_db(db, token):
            return token
    raise RuntimeError("Token generation failed after max retries")
