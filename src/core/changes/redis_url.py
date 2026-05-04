"""Read REDIS_URL with a sensible default for prototype/dev."""

import os


def get_redis_url() -> str:
    """Return the Redis URL.

    Reads REDIS_URL env var. Defaults to redis://localhost:6379/0 for
    prototype convenience (Redis runs on the same VM as Watcher in Phase 2).
    """
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")
