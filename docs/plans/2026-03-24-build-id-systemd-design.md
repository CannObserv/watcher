# Design: Set BUILD_ID from git SHA at deploy time

**Date:** 2026-03-24
**Issue:** #37

## Goal

Wire `BUILD_ID` env var to the current git commit SHA automatically at service start via systemd, so deployed instances show the real build version instead of `"dev"`.

## Approved Approach

### Systemd unit file (`deploy/watcher.service`)

- `ExecStartPre` runs a bash one-liner: `git -C /home/exedev/watcher rev-parse --short HEAD` → writes `BUILD_ID=<sha>` to `/run/watcher-build-id`
- `EnvironmentFile=/run/watcher-build-id` loads the SHA into the process environment
- Second `EnvironmentFile` loads secrets from `/home/exedev/watcher/env`
- Main `ExecStart` launches uvicorn via `uv run`

### Deployment documentation (`DEPLOYMENT.md`)

- Systemd unit installation and enablement
- How BUILD_ID gets set automatically
- Manual override option
- Fallback behavior (`"dev"` when unset)

## Key Decisions

- **Repo path:** `/home/exedev/watcher` (matches current VM layout)
- **No code changes:** `src/core/config.py` already reads `BUILD_ID` from env with `"dev"` fallback
- **Short SHA:** `--short` flag for human-readable 7-char hash

## Out of Scope

- CI/CD pipeline integration
- Docker/container deployment
- Multi-instance or load-balanced setups
