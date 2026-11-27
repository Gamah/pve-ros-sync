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
if ! python3 -c "import ensurepip" &>/dev/null; then
    apt-get install -y python3-venv
fi

# ── 2. Install directory + virtualenv ─────────────────────────────────────────
echo "--> Setting up $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
if [ ! -x "$VENV/bin/pip" ]; then
    rm -rf "$VENV"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# ── 3. Copy scripts ───────────────────────────────────────────────────────────
echo "--> Copying pve_ros_sync.py, ban_page.py ..."
cp "$REPO_DIR/pve_ros_sync.py" "$INSTALL_DIR/pve_ros_sync.py"
cp "$REPO_DIR/ban_page.py" "$REPO_DIR/awesome_face.svg" "$INSTALL_DIR/"

# ── 4. Config (never overwrite existing) ─────────────────────────────────────
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    echo "--> Creating $CONFIG_DIR/config.ini from example — EDIT THIS BEFORE STARTING."
    cp "$REPO_DIR/config.ini.example" "$CONFIG_DIR/config.ini"
    chmod 600 "$CONFIG_DIR/config.ini"
else
    echo "--> Config already exists at $CONFIG_DIR/config.ini — skipping."
fi

# ── 5. CrowdSec agent + LAPI ──────────────────────────────────────────────────
if ! command -v cscli &>/dev/null; then
    echo "--> Installing CrowdSec from packagecloud ..."
    apt-get install -y curl gnupg debian-archive-keyring apt-transport-https
    install -d /etc/apt/keyrings
    curl -fsSL https://packagecloud.io/crowdsec/crowdsec/gpgkey \
        | gpg --dearmor --yes -o /etc/apt/keyrings/crowdsec_crowdsec-archive-keyring.gpg
    echo "deb [signed-by=/etc/apt/keyrings/crowdsec_crowdsec-archive-keyring.gpg] https://packagecloud.io/crowdsec/crowdsec/any any main" \
        > /etc/apt/sources.list.d/crowdsec_crowdsec.list
    apt-get update
    apt-get install -y crowdsec
fi
echo "--> Configuring CrowdSec (Caddy collection, journal source, escalating bans) ..."
cscli hub update >/dev/null
cscli collections install crowdsecurity/caddy
install -d /etc/crowdsec/acquis.d
cp "$REPO_DIR/crowdsec/acquis-caddy.yaml"   /etc/crowdsec/acquis.d/caddy.yaml
cp "$REPO_DIR/crowdsec/profiles.yaml.local" /etc/crowdsec/profiles.yaml.local
cp "$REPO_DIR/crowdsec/config.yaml.local"   /etc/crowdsec/config.yaml.local
systemctl restart crowdsec
systemctl is-active --quiet crowdsec || { echo "!! crowdsec failed to start: journalctl -u crowdsec"; exit 1; }

# ── 6. Secrets in /etc/pve-ros-sync/.env (generated once, never overwritten) ──
ENV_FILE="$CONFIG_DIR/.env"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
NEW_SECRETS=""
ensure_secret() {
    if ! grep -q "^$1=" "$ENV_FILE"; then
        echo "$1=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> "$ENV_FILE"
        NEW_SECRETS="$NEW_SECRETS $1"
    fi
}
secret() { grep "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2-; }
for v in CROWDSEC_API_KEY BANPAGE_LAPI_KEY BANPAGE_SECRET WEBUI_LAPI_PASSWORD; do ensure_secret "$v"; done

# A bouncer is (re)registered when its key is new or CrowdSec lost it (reinstall).
ensure_bouncer() {
    local name="$1" var="$2"
    if [[ " $NEW_SECRETS " == *" $var "* ]] \
        || ! grep -q "\"name\": *\"$name\"" <<<"$(cscli bouncers list -o json)"; then
        cscli bouncers delete "$name" &>/dev/null || true
        cscli bouncers add "$name" -k "$(secret "$var")" >/dev/null
        echo "--> Registered bouncer $name"
    fi
}
ensure_bouncer caddy "CROWDSEC_API_KEY"
ensure_bouncer ban-page "BANPAGE_LAPI_KEY"
cscli machines add crowdsec-web-ui --password "$(secret WEBUI_LAPI_PASSWORD)" -f /dev/null --force >/dev/null

# ── 7. Caddy with the CrowdSec bouncer (keeps the Debian unit and user) ───────
# The package's binary is diverted to caddy.default; apt upgrades land there
# and never replace the custom build. CADDY_REBUILD=1 fetches a fresh one.
BOUNCER="github.com/hslatman/caddy-crowdsec-bouncer@v0.15.1"
if [ "${CADDY_REBUILD:-}" = 1 ] || ! grep -qx 'http.handlers.crowdsec' <<<"$(caddy list-modules 2>/dev/null)"; then
    echo "--> Downloading Caddy with $BOUNCER ..."
    arch="$(dpkg --print-architecture)"
    curl -fsSL -o /usr/bin/caddy.custom.new \
        "https://caddyserver.com/api/download?os=linux&arch=$arch&p=$BOUNCER"
    chmod 755 /usr/bin/caddy.custom.new
    grep -qx 'http.handlers.crowdsec' <<<"$(/usr/bin/caddy.custom.new list-modules)" \
        || { echo "!! downloaded Caddy lacks the bouncer"; exit 1; }
    mv /usr/bin/caddy.custom.new /usr/bin/caddy.custom
    if ! grep -q caddy.default <<<"$(dpkg-divert --list /usr/bin/caddy)"; then
        dpkg-divert --divert /usr/bin/caddy.default --rename /usr/bin/caddy
    fi
    update-alternatives --install /usr/bin/caddy caddy /usr/bin/caddy.default 10
    update-alternatives --install /usr/bin/caddy caddy /usr/bin/caddy.custom 50
fi
install -d /etc/systemd/system/caddy.service.d
cp "$REPO_DIR/systemd/caddy-crowdsec.conf" /etc/systemd/system/caddy.service.d/crowdsec.conf

# ── 8. Web UI on :6969 (Docker, host network, LAPI stays on localhost) ────────
if ! command -v docker &>/dev/null; then
    apt-get install -y docker.io
fi
echo "--> (Re)creating crowdsec-web-ui container ..."
WEBUI_IMAGE="ghcr.io/theduffman85/crowdsec-web-ui:latest"
docker pull -q "$WEBUI_IMAGE" >/dev/null
docker rm -f crowdsec-web-ui &>/dev/null || true
install -d /var/lib/crowdsec-web-ui
CONFIG_INSTANCE_LAPI_AUTH_PASSWORD="$(secret WEBUI_LAPI_PASSWORD)" docker run -d \
    --name crowdsec-web-ui --restart unless-stopped --network host \
    -v /var/lib/crowdsec-web-ui:/app/data \
    -e CONFIG_SERVER_PORT=6969 \
    -e CONFIG_INSTANCE_LAPI_URL=http://127.0.0.1:8080 \
    -e CONFIG_INSTANCE_LAPI_AUTH_USERNAME=crowdsec-web-ui \
    -e CONFIG_INSTANCE_LAPI_AUTH_PASSWORD \
    "$WEBUI_IMAGE" >/dev/null

# ── 9. Systemd units ──────────────────────────────────────────────────────────
echo "--> Installing systemd units ..."
cp "$REPO_DIR/pve-ros-sync.service" /etc/systemd/system/pve-ros-sync.service
cp "$REPO_DIR/pve-ros-sync.timer"   /etc/systemd/system/pve-ros-sync.timer
cp "$REPO_DIR/systemd/pve-ros-sync-banpage.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now pve-ros-sync-banpage.service
systemctl restart pve-ros-sync-banpage.service
systemctl restart caddy
systemctl is-active --quiet caddy || { echo "!! caddy failed to start: journalctl -u caddy"; exit 1; }

# ── 10. Enable timer, sync now (writes the Caddyfile and LAN allowlist) ───────
systemctl enable --now pve-ros-sync.timer
if ! grep -q "10.0.0.X\|changeme\|xxxxxxxx" "$CONFIG_DIR/config.ini"; then
    systemctl start pve-ros-sync.service
fi

echo ""
echo "==> Deploy complete."
echo ""
echo "    Config:   $CONFIG_DIR/config.ini"
echo "    Logs:     journalctl -u pve-ros-sync -f"
echo "    Run now:  systemctl start pve-ros-sync.service"
echo "    Timer:    systemctl list-timers pve-ros-sync.timer"
echo "    Secrets:  $ENV_FILE (generated; nothing to fill in)"
echo "    Bans:     cscli decisions list   |   ban page log: journalctl -u pve-ros-sync-banpage"
echo "    Web UI:   http://<this host>:6969 — open it now: the first visitor creates the admin"
echo ""

# Warn if config still has placeholder
if grep -q "10.0.0.X\|changeme\|xxxxxxxx" "$CONFIG_DIR/config.ini" 2>/dev/null; then
    echo "  !! Config still contains placeholder values — edit $CONFIG_DIR/config.ini before starting."
fi
if ! grep -q "^contact_domain *= *[^ ]" "$CONFIG_DIR/config.ini" 2>/dev/null; then
    echo "  !! Set [banpage] contact_domain in $CONFIG_DIR/config.ini (ban page contact), then:"
    echo "     systemctl restart pve-ros-sync-banpage"
fi
