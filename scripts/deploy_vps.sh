#!/usr/bin/env bash
# Deploy aliexpress-ds to a VPS via git pull (preferred) or rsync fallback.
# Preserves remote .env / .venv / data/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${DEPLOY_HOST:-Admin@34.172.204.102}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/home/Admin/aliexpress-ds}"
GIT_URL="${GIT_URL:-https://github.com/AriseshineSky/aliexpress-ds.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
# DEPLOY_MODE=git (default) | rsync
DEPLOY_MODE="${DEPLOY_MODE:-git}"
SKIP_START="${SKIP_START:-0}"

SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
RSYNC=(rsync -az --delete
  --exclude .venv
  --exclude .git
  --exclude .env
  --exclude __pycache__
  --exclude '*.pyc'
  --exclude data/
  --exclude .ruff_cache
  --exclude .playwright-mcp
  -e "ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
)

echo "==> deploy mode=$DEPLOY_MODE → $HOST:$REMOTE_DIR (branch=$GIT_BRANCH)"

if [[ "$DEPLOY_MODE" == "git" ]]; then
  "${SSH[@]}" "$HOST" bash -s <<EOF
set -euo pipefail
REMOTE_DIR="$REMOTE_DIR"
GIT_URL="$GIT_URL"
GIT_BRANCH="$GIT_BRANCH"
export PATH="\$HOME/.local/bin:\$PATH"

if [[ ! -d "\$REMOTE_DIR/.git" ]]; then
  echo "==> initializing git checkout in \$REMOTE_DIR"
  mkdir -p "\$REMOTE_DIR"
  # Preserve secrets / runtime state outside of git
  TMP="\$(mktemp -d)"
  for keep in .env .venv data logs; do
    if [[ -e "\$REMOTE_DIR/\$keep" ]]; then
      mv "\$REMOTE_DIR/\$keep" "\$TMP/\$keep"
    fi
  done
  # Clear non-kept contents (old rsync tree) then clone
  find "\$REMOTE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  git clone --branch "\$GIT_BRANCH" --single-branch "\$GIT_URL" "\$REMOTE_DIR"
  for keep in .env .venv data logs; do
    if [[ -e "\$TMP/\$keep" ]]; then
      rm -rf "\$REMOTE_DIR/\$keep"
      mv "\$TMP/\$keep" "\$REMOTE_DIR/\$keep"
    fi
  done
  rm -rf "\$TMP"
else
  echo "==> git pull origin \$GIT_BRANCH"
  cd "\$REMOTE_DIR"
  git remote set-url origin "\$GIT_URL"
  git fetch origin "\$GIT_BRANCH"
  git checkout "\$GIT_BRANCH"
  git reset --hard "origin/\$GIT_BRANCH"
  git clean -fd -e .env -e .venv -e data -e logs
fi

cd "\$REMOTE_DIR"
echo "==> HEAD=\$(git rev-parse --short HEAD) \$(git log -1 --oneline)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="\$HOME/.local/bin:\$PATH"
fi
uv sync
uv run aliexpress-ds config-check || true
uv run aliexpress-ds queue-status || true
EOF
else
  echo "==> rsync → $HOST:$REMOTE_DIR"
  "${RSYNC[@]}" "$ROOT/" "$HOST:$REMOTE_DIR/"
  "${SSH[@]}" "$HOST" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
export PATH="\$HOME/.local/bin:\$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="\$HOME/.local/bin:\$PATH"
fi
uv sync
uv run aliexpress-ds config-check || true
uv run aliexpress-ds queue-status || true
EOF
fi

if [[ "$SKIP_START" == "1" ]]; then
  echo "==> SKIP_START=1 — not touching systemd"
  echo "==> done"
  exit 0
fi

# Install enqueue / bestsellers timers only on primary host (one feeder is enough).
INSTALL_ENQUEUE_TIMER="${INSTALL_ENQUEUE_TIMER:-}"
if [[ -z "$INSTALL_ENQUEUE_TIMER" ]]; then
  if [[ "$HOST" == "Admin@34.172.204.102" || "$HOST" == *34.172.204.102* ]]; then
    INSTALL_ENQUEUE_TIMER=1
  else
    INSTALL_ENQUEUE_TIMER=0
  fi
fi
INSTALL_BESTSELLERS_TIMER="${INSTALL_BESTSELLERS_TIMER:-$INSTALL_ENQUEUE_TIMER}"

echo "==> install systemd units (needs sudo; enqueue_timer=$INSTALL_ENQUEUE_TIMER bestsellers_timer=$INSTALL_BESTSELLERS_TIMER)"
"${SSH[@]}" "$HOST" bash -s <<EOF
set -euo pipefail
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-queue-worker.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-token-refresh.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-token-refresh.timer" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-enqueue.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-enqueue.timer" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-bestsellers.service" /etc/systemd/system/
sudo cp "$REMOTE_DIR/deploy/aliexpress-ds-bestsellers.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aliexpress-ds-token-refresh.timer
sudo systemctl enable --now aliexpress-ds-queue-worker.service
sudo systemctl restart aliexpress-ds-queue-worker.service
if [[ "$INSTALL_ENQUEUE_TIMER" == "1" ]]; then
  sudo systemctl enable --now aliexpress-ds-enqueue.timer
else
  sudo systemctl disable --now aliexpress-ds-enqueue.timer 2>/dev/null || true
fi
if [[ "$INSTALL_BESTSELLERS_TIMER" == "1" ]]; then
  sudo systemctl enable --now aliexpress-ds-bestsellers.timer
else
  sudo systemctl disable --now aliexpress-ds-bestsellers.timer 2>/dev/null || true
fi
sudo systemctl status aliexpress-ds-queue-worker.service --no-pager -l | head -40
sudo systemctl list-timers 'aliexpress-ds*' --no-pager
EOF

echo "==> done"
echo "Logs: ssh $HOST 'sudo journalctl -u aliexpress-ds-queue-worker -f'"
echo "Bestsellers: ssh $HOST 'sudo journalctl -u aliexpress-ds-bestsellers -n 100'"
echo "Manual run: ssh $HOST 'cd $REMOTE_DIR && uv run aliexpress-ds bestsellers-daily --dry-run'"
