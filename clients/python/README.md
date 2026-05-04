# information-client

Async Python SDK for the Information service. Generated from the service's
OpenAPI schema, pinned 1:1 with server version.

## Install (path dependency, prototype phase)

In the consuming repo's `pyproject.toml`:

```toml
[tool.uv.sources]
information-client = { path = "../watcher/clients/python", editable = true }
```

## Usage

```python
from information_client import InformationClient

async with InformationClient(base_url="http://localhost:8020", api_key="...") as client:
    spec = await client.get_primary_info_spec("01HZZZ...")
    print(spec.document)
```

## Regenerate after a server schema change

```bash
bash clients/python/scripts/regen.sh
```
