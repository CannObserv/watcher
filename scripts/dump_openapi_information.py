"""Dump the Information service's OpenAPI schema to stdout as JSON.

Used by the SDK regen workflow. Imports the FastAPI ``app`` and serializes
``app.openapi()`` — no server, no DB connection required.
"""

import json

from src.information.api.main import app


def main() -> None:
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
