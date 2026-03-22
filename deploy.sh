#!/usr/bin/env bash
# deploy.sh — install or re-deploy pve-ros-sync on the target machine
# Usage: bash deploy.sh   (or chmod +x deploy.sh && ./deploy.sh)
# Safe to run multiple times (idempotent). Run from the repo root.
set -euo pipefail

# Make self executable for future runs
chmod +x "$0"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/usr/local/lib/pve-ros-sync"
CONFIG_DIR="/etc/pve-ros-sync"
VENV="$INSTALL_DIR/venv"

echo "==> Installing pve-ros-sync from $REPO_DIR"

# ── 1. System deps ────────────────────────────────────────────────────────────
echo "--> Checking system packages..."
if ! command -v python3 &>/dev/null; then
    apt-get install -y python3
fi
if ! command -v pip3 &>/dev/null; then
    apt-get install -y python3-pip
fi
if ! python3 -c "import venv" &>/dev/null; then
    apt-get install -y python3-venv
fi

# ── 2. Install directory + virtualenv ─────────────────────────────────────────
echo "--> Setting up $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# ── 3. Copy script ────────────────────────────────────────────────────────────
echo "--> Copying pve_ros_sync.py ..."
cp "$REPO_DIR/pve_ros_sync.py" "$INSTALL_DIR/pve_ros_sync.py"

# ── 4. Config (never overwrite existing) ─────────────────────────────────────
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    echo "--> Creating $CONFIG_DIR/config.ini from example — EDIT THIS BEFORE STARTING."
    cp "$REPO_DIR/config.ini.example" "$CONFIG_DIR/config.ini"
    chmod 600 "$CONFIG_DIR/config.ini"
else
    echo "--> Config already exists at $CONFIG_DIR/config.ini — skipping."
fi

# ── 5. Systemd units ──────────────────────────────────────────────────────────
echo "--> Installing systemd units ..."
cp "$REPO_DIR/pve-ros-sync.service" /etc/systemd/system/pve-ros-sync.service
cp "$REPO_DIR/pve-ros-sync.timer"   /etc/systemd/system/pve-ros-sync.timer

systemctl daemon-reload

# ── 6. Enable + start timer ───────────────────────────────────────────────────
systemctl enable --now pve-ros-sync.timer

echo ""
echo "==> Deploy complete."
echo ""
echo "    Config:   $CONFIG_DIR/config.ini"
echo "    Logs:     journalctl -u pve-ros-sync -f"
echo "    Run now:  systemctl start pve-ros-sync.service"
echo "    Timer:    systemctl list-timers pve-ros-sync.timer"
echo ""

# Warn if config still has placeholder
if grep -q "10.0.0.X\|changeme\|xxxxxxxx" "$CONFIG_DIR/config.ini" 2>/dev/null; then
    echo "  !! Config still contains placeholder values — edit $CONFIG_DIR/config.ini before starting."
fi
