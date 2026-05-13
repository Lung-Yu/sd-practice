import os

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.20"))
RETRY_BASE_DELAY_S = float(os.getenv("RETRY_BASE_DELAY_S", "0.1"))
ATTEMPT_TIMEOUT_S = float(os.getenv("ATTEMPT_TIMEOUT_S", "5.0"))
REDIS_URL = os.getenv("REDIS_URL", "")
