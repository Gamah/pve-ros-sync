# pve-ros-sync

Syncs Proxmox VE VM/LXC names to MikroTik RouterOS static DNS entries and
optionally manages Caddy reverse proxy blocks.

- VMID determines IP: VM `104 (plex)` → `plex.lan` = `10.0.0.104`
- IPs below `.100` are never touched
- VMs/LXCs tagged `revprox` or `revprox-PORT` get a block in the Caddyfile
- Runs every 5 minutes via systemd timer, logs to journald

## Deploy

```bash
git clone https://github.com/gamah/pve-ros-sync.git
cd pve-ros-sync
bash deploy.sh
```

`deploy.sh` is idempotent — safe to re-run after pulling updates.

## Configuration

`deploy.sh` copies `config.ini.example` to `/etc/pve-ros-sync/config.ini` on
first run. **Edit that file before starting the service** — it contains your
credentials and is excluded from git via `.gitignore`.

```bash
$EDITOR /etc/pve-ros-sync/config.ini
systemctl start pve-ros-sync.service   # test run
journalctl -u pve-ros-sync -f          # watch output
```

## Proxmox setup

```bash
pveum user add pve-ros-sync@pve
pveum role add RosSync -privs "VM.Audit"
pveum aclmod / -user pve-ros-sync@pve -role RosSync
pveum user token add pve-ros-sync@pve sync --privsep 0
```

Copy the token value into `config.ini` under `[proxmox]`.

## RouterOS setup

Add a user in **System → Users** with group `write`. The API on port 8728 is
available automatically — no extra toggle needed.

## Caddy reverse proxy

Tag a VM with `revprox` or `revprox-PORT` in Proxmox. On the next sync the
Caddyfile gains a managed block:

```
# BEGIN pve-ros-sync
plex.domain.tld {
    reverse_proxy plex.lan:1337
}
# END pve-ros-sync
```

Everything outside the managed block is left untouched. Caddy is reloaded
automatically when the block changes.
