import hashlib
import string
import time

BASE62_CHARS = string.ascii_letters + string.digits
TOKEN_LENGTH = 7


def base62_encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    if num == 0:
        return BASE62_CHARS[0]
    result = []
    while num > 0:
        num, remainder = divmod(num, 62)
        result.append(BASE62_CHARS[remainder])
    return "".join(reversed(result))


def generate_token(url: str) -> str:
    nonce = str(time.time_ns())
    digest = hashlib.sha256((url + nonce).encode()).digest()
    return base62_encode(digest)[:TOKEN_LENGTH]
