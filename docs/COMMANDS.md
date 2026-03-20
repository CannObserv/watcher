# Common Commands

## Setup

```bash
# Install dependencies (creates .venv automatically)
uv sync
```

## Development

```bash
# FastAPI dev server (auto-reload)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Testing

```bash
# Run all tests (excludes integration)
uv run pytest

# Run with coverage
uv run pytest --cov

# Run a specific file
uv run pytest tests/path/to/test_file.py --no-cov

# Run integration tests (hits live external services)
uv run pytest -m integration
```

## Linting

```bash
# Check
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .
```

## Git Submodules

```bash
# Init after cloning
git submodule update --init --recursive

# Force-refresh vendor skills
git submodule update --remote --merge vendor/gregoryfoster-skills vendor/obra-superpowers
```
