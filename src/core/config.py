"""Application-wide configuration constants read from environment."""

import os

BUILD_ID = os.environ.get("BUILD_ID", "dev")
