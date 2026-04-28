#!/usr/bin/env bash
# Weekly disk cleanup — see deploy/watcher-cleanup.service and .timer
set -euo pipefail

LOG_DIR="/var/log/watcher"
LOG_FILE="$LOG_DIR/cleanup-$(date -u +%Y%m%d-%H%M%S).log"
PLAYWRIGHT_THRESHOLD_BYTES=$((2 * 1024 * 1024 * 1024))

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=== Watcher disk cleanup $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo ""

echo "--- Disk before ---"
df -h /
echo ""

echo "--- Top-10 disk consumers ---"
du -sh /* /home/exedev/* 2>/dev/null | sort -rh | head -10 || true
echo ""

# VS Code server LRU: remove server installs not accessed in 30+ days
if [ -d "$HOME/.vscode-server/cli/servers" ]; then
    echo "--- VS Code server LRU (>30 days) ---"
    find "$HOME/.vscode-server/cli/servers" -mindepth 1 -maxdepth 1 -mtime +30 -exec rm -rf {} +
    echo "done"
    echo ""
fi

# npm caches
echo "--- npm caches ---"
rm -rf "$HOME/.npm/_npx" "$HOME/.npm/_cacache"
echo "done"
echo ""

# uv cache prune
echo "--- uv cache prune ---"
/usr/local/bin/uv cache prune --force
echo ""

# APT package cache (clean only — no autoremove)
echo "--- APT package cache ---"
sudo /bin/apt-get clean
echo "done"
echo ""

# Docker dangling images (conservative — no volumes, no stopped containers)
if command -v docker &>/dev/null; then
    echo "--- Docker dangling images ---"
    docker image prune -f
    echo ""
fi

# Journal logs older than 14 days
echo "--- Journal vacuum (14 days) ---"
sudo /bin/journalctl --vacuum-time=14d
echo ""

# Playwright cache: audit only — do not delete (browsers may be in use)
PLAYWRIGHT_CACHE="$HOME/.cache/ms-playwright"
if [ -d "$PLAYWRIGHT_CACHE" ]; then
    echo "--- Playwright cache audit ---"
    PLAYWRIGHT_BYTES=$(du -sb "$PLAYWRIGHT_CACHE" | cut -f1)
    PLAYWRIGHT_HUMAN=$(du -sh "$PLAYWRIGHT_CACHE" | cut -f1)
    echo "size: $PLAYWRIGHT_HUMAN"
    if [ "$PLAYWRIGHT_BYTES" -gt "$PLAYWRIGHT_THRESHOLD_BYTES" ]; then
        echo "WARNING: Playwright cache exceeds 2 GB threshold ($PLAYWRIGHT_HUMAN) — manual review needed"
    fi
    echo ""
fi

echo "--- Disk after ---"
df -h /
echo ""

echo "=== Cleanup complete ==="

# Retain the 10 most recent logs
find "$LOG_DIR" -name 'cleanup-*.log' -type f | sort -r | tail -n +11 | xargs -r rm -f
