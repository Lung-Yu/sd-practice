import os

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0.20"))
