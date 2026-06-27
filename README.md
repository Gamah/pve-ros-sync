# pve-ros-sync

Syncs Proxmox VE VM/LXC names to MikroTik RouterOS static DNS entries and
optionally manages Caddy reverse proxy blocks.

- VMID determines IP: VM `104 (plex)` → `plex.lan` = `10.0.0.104`
- IPs below `.100` are never touched
- VMs/LXCs tagged `revprox-PORT` or `revprox-PORT-pubname` get a block in the Caddyfile
- Multiple `revprox-*` tags on one guest produce multiple blocks (one per port)
- Runs every 5 minutes via systemd timer, logs to journald

## IP address assumption

**This project does not assign or manage IP addresses.** It assumes that each
VM/LXC already has the IP `<network_prefix>.<vmid>` — derived purely from the
VMID. You are responsible for ensuring VMs actually use that address.

Common approaches:
- Assign a static IP inside the guest matching its VMID
- Configure a DHCP reservation in RouterOS (**IP → DHCP Server → Leases**)
  binding the guest's MAC to `<prefix>.<vmid>`

If a VM's real IP doesn't match its VMID-derived address, DNS and reverse proxy
entries will point to the wrong host and there will be no warning.

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

Tag a VM with `revprox-PORT` in Proxmox. On the next sync the Caddyfile gains
a managed block using the VM name as the public subdomain:

```
# BEGIN pve-ros-sync
plex.domain.tld {
    reverse_proxy 10.0.0.104:1337
}
# END pve-ros-sync
```

To use a **different public subdomain** than the VM name, append it to the tag:
`revprox-PORT-pubname`. For example, VM `104 (plex)` tagged `revprox-1337-watch`
produces `watch.domain.tld` instead of `plex.domain.tld`.

### Multiple ports on one guest

Add multiple `revprox-*` tags to expose more than one port. Each tag must
resolve to a distinct public subdomain — use the pubname suffix to disambiguate:

```
revprox-8080          → plex.domain.tld    (uses VM name)
revprox-9090-admin    → admin.domain.tld
```

If two tags would produce the same subdomain (e.g. two bare `revprox-PORT` tags,
or two tags with the same pubname), the duplicate is dropped and logged as an
error in the service journal.

Everything outside the managed block is left untouched. Caddy is reloaded
automatically when the block changes.

This tool does **not** manage DNS for the external `domain.tld`. To avoid NAT
hairpin issues (QUIC errors / `CONNECTION_REFUSED`) when LAN clients access
`name.domain.tld`, add a single wildcard static DNS entry in RouterOS pointing at
the Caddy host:

```
*.domain.tld → 10.0.0.X  (Caddy's LAN IP)
```

LAN clients then resolve the external domain directly to Caddy, bypassing NAT
entirely. Public DNS is unaffected.
