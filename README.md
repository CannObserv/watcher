# watcher

Monitors cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Setup

```bash
git submodule update --init --recursive
uv sync
```

## Development

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Testing

```bash
uv run pytest
```

See [docs/COMMANDS.md](docs/COMMANDS.md) for full command reference.
