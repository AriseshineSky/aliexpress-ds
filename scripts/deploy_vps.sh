#!/usr/bin/env bash
# Deploy aliexpress-ds to Admin@34.172.204.102 and install systemd units.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${DEPLOY_HOST:-Admin@34.172.204.102}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/home/Admin/aliexpress-ds}"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
RSYNC=(rsync -az --delete
  --exclude .venv
  --exclude .git
  --exclude __pycache__
  --exclude '*.pyc'
  --exclude data/*.jsonl
  --exclude .ruff_cache
  -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
)

echo "==> rsync → $HOST:$REMOTE_DIR"
"${RSYNC[@]}" "$ROOT/" "$HOST:$REMOTE_DIR/"

echo "==> remote: uv sync + config-check"
"${SSH[@]}" "$HOST" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
export PATH="\$HOME/.local/bin:\$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="\$HOME/.local/bin:\$PATH"
fi
uv sync
uv run aliexpress-ds config-check
uv run aliexpress-ds queue-status || true
EOF

echo "==> install systemd units (needs sudo)"
"${SSH[@]}" "$HOST" bash -s <<EOF
set -euo pipefail
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-queue-worker.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-token-refresh.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-token-refresh.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aliexpress-ds-token-refresh.timer
sudo systemctl enable --now aliexpress-ds-queue-worker.service
sudo systemctl status aliexpress-ds-queue-worker.service --no-pager -l | head -40
sudo systemctl list-timers 'aliexpress-ds*' --no-pager
EOF

echo "==> done"
echo "Logs: ssh $HOST 'sudo journalctl -u aliexpress-ds-queue-worker -f'"
