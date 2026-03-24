# Deployment

## Systemd Service

A systemd unit file is provided at `deploy/watcher.service`.

### Installation

```bash
# Copy (or symlink) the unit file
sudo cp deploy/watcher.service /etc/systemd/system/watcher.service

# Reload systemd, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable watcher
sudo systemctl start watcher
```

### Logs

```bash
sudo journalctl -u watcher -f
```

## BUILD_ID

The systemd unit automatically sets `BUILD_ID` to the current git short SHA before each start via `ExecStartPre`. This value is used for:

- Static asset cache-busting (`?v=<sha>` query params)
- Page footer version display
- `/health` endpoint `build` field

### Manual Override

To pin a specific build ID, set it in the `env` file:

```bash
echo 'BUILD_ID=abc1234' >> env
```

The `env` file is loaded after `/run/watcher/build-id`, so values in `env` take precedence.

### Fallback

When `BUILD_ID` is not set (e.g., running locally without the systemd unit), the application defaults to `"dev"`.
