import os

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.20"))
RETRY_BASE_DELAY_S = float(os.getenv("RETRY_BASE_DELAY_S", "0.1"))
ATTEMPT_TIMEOUT_S = float(os.getenv("ATTEMPT_TIMEOUT_S", "5.0"))
REDIS_URL = os.getenv("REDIS_URL", "")

# Circuit breaker
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_RECOVERY_SECONDS = float(os.getenv("CB_RECOVERY_SECONDS", "30.0"))

# Per-user rate limiting (requests per window)
RATE_LIMIT_PER_USER = int(os.getenv("RATE_LIMIT_PER_USER", "100"))
RATE_LIMIT_WINDOW_S = int(os.getenv("RATE_LIMIT_WINDOW_S", "60"))

# Delivery worker batch tuning
# Rule: num_workers × BATCH_SIZE = target_concurrent_deliveries
# Default 20 for 1 worker; set to 5 when running 4 workers to keep same Redis pressure.
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "20"))
