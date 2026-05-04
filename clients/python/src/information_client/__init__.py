"""information-client — async Python SDK for the Information service.

Pinned 1:1 with Information service version. See README for usage.
"""

from information_client.client import InformationClient
from information_client.defaults import (
    DEFAULT_FETCH_RENDER,
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    fetch_render,
    fetch_timeout_seconds,
)
from information_client.errors import (
    AuthError,
    InformationError,
    NotFound,
    ServerError,
    ValidationError,
)
from information_client.generated.models.info_item_out import InfoItemOut
from information_client.generated.models.info_spec_out import InfoSpecOut

__version__ = "0.1.0"

__all__ = [
    "AuthError",
    "DEFAULT_FETCH_RENDER",
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "InfoItemOut",
    "InfoSpecOut",
    "InformationClient",
    "InformationError",
    "NotFound",
    "ServerError",
    "ValidationError",
    "fetch_render",
    "fetch_timeout_seconds",
]
