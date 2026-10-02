"""The retry strategy the fact-apply tasks share (#241 CR-2, #325)."""

import procrastinate
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError

# Transient-infra retry for the apply tasks (CR-2): DB restarts
# (OperationalError), broker/notifier blips. Mirrors check_watched_item's
# shape; permanent errors (bugs) still fail the job — each reaper's re-defer
# is the last-resort resurrection for those. Its own module because both apply
# families use it and the fetch apply imports the process leg (#325).
APPLY_RETRY = procrastinate.RetryStrategy(
    max_attempts=3,
    exponential_wait=5,
    retry_exceptions={
        ConnectionError,
        TimeoutError,
        RedisConnectionError,
        RedisTimeoutError,
        OperationalError,
    },
)
