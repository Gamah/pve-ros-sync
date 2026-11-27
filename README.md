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

### Dry run

Preview every RouterOS DNS and Caddyfile change without applying anything:

```bash
/usr/local/lib/pve-ros-sync/venv/bin/python \
  /usr/local/lib/pve-ros-sync/pve_ros_sync.py --dry-run
```

It still reads from Proxmox and RouterOS, but logs each entry it *would* add,
update, or remove (and the Caddy block it *would* write) instead of making the
change. Use `PVE_ROS_SYNC_CONFIG=/path/to/config.ini` to point at an alternate
config.

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
    log
    @secrets path_regexp (^|/)\.(env|git)([^A-Za-z0-9]|$)
    route {
        crowdsec
        respond @secrets 404
        reverse_proxy 10.0.0.104:1337
    }
    handle_errors 403 {
        reverse_proxy 127.0.0.1:9180 { … }   # ban page, see below
    }
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

`.env*` and `.git*`-style paths (`/.env`, `/x/.env.bak`, `/.git/config`,
`/.git-credentials`) get a 404 from Caddy on every site and never reach the
service; the probe is still logged for CrowdSec.

Every block carries `log`, so each request is written to Caddy's journal as
JSON, with the connecting address in `request.remote_ip`:
`journalctl -u caddy | grep '"host":"plex.domain.tld"'`.

## Split-horizon wildcard DNS (fix LAN QUIC/connection errors)

When LAN clients access `name.domain.tld`, public DNS resolves to your external
IP. If your router doesn't hairpin NAT reliably — common with QUIC/HTTP3 on
MikroTik — you'll get QUIC errors or `CONNECTION_REFUSED`.

Set `caddy_host` in `config.ini` to this host's LAN IP:

```ini
[caddy]
caddy_host = 10.0.0.X
; wildcard_mode = regexp   ; default; see below
```

The sync then maintains a single wildcard static DNS entry in RouterOS so
`*.domain.tld` (and the apex) resolves directly to Caddy on the LAN, bypassing
NAT entirely. Public DNS is unaffected. The tool only creates/updates this one
entry (it won't delete it if you later unset `caddy_host`).

**RouterOS version matters.** `match-subdomain` only exists on RouterOS **v7**;
on **v6** the static DNS entry has no such field — you must use a `regexp` entry.
Control this with `wildcard_mode`:

```
wildcard_mode = regexp           (default — works on v6 AND v7)
  → regexp=(.*\.)?domain\.tld$   address=10.0.0.X

wildcard_mode = match-subdomain  (v7 only)
  → name=domain.tld  match-subdomain=yes  address=10.0.0.X
```

The default `regexp` works everywhere, so most setups need only set `caddy_host`.
If you switch modes later, the sync migrates the existing entry automatically.
Note: a stale hand-made entry with the literal name `*.domain.tld` is **not** a
real wildcard on RouterOS (the `*` is literal) — delete it.

## CrowdSec bans

`deploy.sh` puts CrowdSec in front of every managed site. Scanners (path
probing, `.git`/`.env` fetches, crawling) are banned at Caddy and get a 403
page instead of the service.

- **Caddy** is replaced by the same build plus the
  `hslatman/caddy-crowdsec-bouncer` module, downloaded from caddyserver.com.
  The Debian package's binary is diverted to `/usr/bin/caddy.default`, so the
  unit, user and `apt upgrade` keep working and never overwrite the custom
  build. To update Caddy or the module (pinned in `deploy.sh`):
  `CADDY_REBUILD=1 bash deploy.sh`. A drop-in in `caddy.service.d/` gives
  Caddy the bouncer key and drops `--environ` so secrets aren't logged.
- New bans reach Caddy within 5s (`ticker_interval`). A ban stops new
  requests only: open websockets stay up until `systemctl reload caddy`.
- The sync writes the bouncer's global options between
  `# BEGIN/END pve-ros-sync global` inside the Caddyfile's global block
  (creating the block if there is none).
- **CrowdSec** reads Caddy's access log from the journal. LAPI stays on
  `127.0.0.1:8080`. Bans escalate per repeat offence: 4h, 24h, 6d, 36d, 216d,
  10 years; alerts are kept a year (`/etc/crowdsec/*.local`).
- **LAN is never banned:** the sync keeps CrowdSec allowlist `lan` at
  `<network_prefix>.0/24`. VPN clients are not allowlisted.
- **If CrowdSec is down** traffic passes: bans already pulled still apply, new
  ones don't arrive until it's back.
- **Ban page** (`pve-ros-sync-banpage`, `127.0.0.1:9180`): tells the visitor
  to email `<subdomain>@<contact_domain>` (and `contact_email`, if set) with a
  10-character code, HMAC of Caddy's request uuid and the CrowdSec decision
  id. Each served page logs `code= uuid= decision= ip= site=`, so a code
  resolves with `journalctl -u pve-ros-sync-banpage | grep CODE`, and the
  uuid finds the request in `journalctl -u caddy`.
- **Web UI** (`TheDuffman85/crowdsec-web-ui`, Docker, host network) at
  `http://<caddy host>:6969`. It listens on all interfaces, so don't forward
  6969 on the router. The first visitor creates the admin account: open it
  right after the first deploy. It logs in to LAPI as machine `crowdsec-web-ui`
  and can ban and unban.

### Secrets: `/etc/pve-ros-sync/.env`

Generated by `deploy.sh` on first run and never overwritten; nothing to fill in.

| Variable | Used by |
|---|---|
| `CROWDSEC_API_KEY` | Caddy bouncer (`cscli bouncers` name `caddy`) |
| `BANPAGE_LAPI_KEY` | ban page's decision lookups (bouncer `ban-page`) |
| `BANPAGE_SECRET` | HMAC key for the ban page code |
| `WEBUI_LAPI_PASSWORD` | web UI's LAPI login (machine `crowdsec-web-ui`) |

To rotate one, delete its line and re-run `deploy.sh`.

### Checking it

```bash
cscli metrics                       # caddy lines read and parsed
cscli decisions add -i <ip> -d 5m   # non-LAN ip (phone on mobile data): ban page
cscli decisions list
cscli allowlists inspect lan
```

The face on the ban page is `awesome_face.svg`, CC0, copied from gamtube.
