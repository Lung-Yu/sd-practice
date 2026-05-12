import hashlib


def compute_key(user_id: str, topic: str, message: str) -> str:
    raw = f"{user_id}|{topic}|{message}"
    return hashlib.sha256(raw.encode()).hexdigest()
