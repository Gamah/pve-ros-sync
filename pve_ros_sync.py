#!/usr/bin/env python3
"""pve-ros-sync: Sync Proxmox VMs/LXCs → MikroTik RouterOS static DNS + Caddy reverse proxy."""

import configparser
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import librouteros
from proxmoxer import ProxmoxAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("pve-ros-sync")

CADDY_START = "# BEGIN pve-ros-sync"
CADDY_END = "# END pve-ros-sync"


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        log.error("Config not found: %s", path)
        sys.exit(1)
    return cfg


# ── Proxmox ───────────────────────────────────────────────────────────────────

def get_pve_vms(cfg: configparser.ConfigParser) -> dict[int, dict]:
    """Return {vmid: {name, tags}} for all VMs/LXCs with VMID 100–255."""
    pve_cfg = cfg["proxmox"]

    kwargs: dict = {
        "host": pve_cfg["host"],
        "user": pve_cfg["user"],
        "verify_ssl": pve_cfg.getboolean("verify_ssl", fallback=False),
        "timeout": int(pve_cfg.get("timeout", "10")),
    }
    if "token_name" in pve_cfg:
        kwargs["token_name"] = pve_cfg["token_name"]
        kwargs["token_value"] = pve_cfg["token_value"]
    else:
        kwargs["password"] = pve_cfg["password"]

    pve = ProxmoxAPI(**kwargs)

    vms: dict[int, dict] = {}
    seen_names: dict[str, int] = {}

    for node in pve.nodes.get():
        node_name = node["node"]
        for kind in ("qemu", "lxc"):
            for vm in getattr(pve.nodes(node_name), kind).get():
                vmid = int(vm["vmid"])
                if not (100 <= vmid <= 255):
                    continue

                raw_name = vm.get("name", f"vm{vmid}").lower()
                # Sanitize: only lowercase alphanumeric and hyphens
                name = re.sub(r"[^a-z0-9-]", "-", raw_name).strip("-")

                if name in seen_names:
                    log.warning(
                        "Name collision: '%s' used by VMID %d and %d — skipping %d",
                        name, seen_names[name], vmid, vmid,
                    )
                    continue
                seen_names[name] = vmid

                tags_raw = vm.get("tags", "")
                tags = {t for t in re.split(r"[;,\s]+", tags_raw) if t}
                revprox = dedup_revprox(parse_revprox(tags), vmid, name)

                vms[vmid] = {"name": name, "revprox": revprox}

    log.info("PVE: found %d hosts in VMID range 100–255", len(vms))
    return vms


def parse_revprox(tags: set[str]) -> list[tuple[str, str | None]]:
    """Return list of (port, public_name_or_None) for all revprox-* tags.

    Tag formats:
      revprox-8080           → port 8080, public subdomain matches VM name
      revprox-8080-watch     → port 8080, public subdomain "watch"
    """
    results = []
    for tag in tags:
        m = re.match(r"^revprox-(\d+)-([a-z0-9-]+)$", tag)
        if m:
            results.append((m.group(1), m.group(2)))
            continue
        m = re.match(r"^revprox-(\d+)$", tag)
        if m:
            results.append((m.group(1), None))
    return results


def dedup_revprox(entries: list[tuple[str, str | None]], vmid: int, name: str) -> list[tuple[str, str | None]]:
    """Deduplicate revprox entries, logging an error for each conflict."""
    seen: set[str | None] = set()
    result = []
    for port, pubname in entries:
        if pubname in seen:
            label = f"'{pubname}'" if pubname is not None else "no pubname"
            log.error("VMID %d (%s): duplicate revprox public name %s on port %s — ignoring", vmid, name, label, port)
            continue
        seen.add(pubname)
        result.append((port, pubname))
    return result


# ── RouterOS DNS ──────────────────────────────────────────────────────────────

def ros_connect(cfg: configparser.ConfigParser):
    ros = cfg["routeros"]
    host = ros["host"]
    log.info("ROS: connecting to %s:%s", host, ros.get("port", "8728"))
    api = librouteros.connect(
        host=host,
        username=ros["user"],
        password=ros["password"],
        port=int(ros.get("port", "8728")),
    )
    log.info("ROS: connected")
    return api


class DryRunDNS:
    """Wraps an ``ip/dns/static`` path: reads pass through, writes are logged only."""

    def __init__(self, real):
        self._real = real

    def __iter__(self):
        return iter(self._real)

    def add(self, **kw):
        log.info("DRY-RUN  would add    %s", kw)

    def update(self, **kw):
        log.info("DRY-RUN  would update %s", kw)

    def remove(self, *ids):
        log.info("DRY-RUN  would remove %s", ", ".join(ids))


def get_current_dns(api, prefix: str, domain: str) -> dict[str, dict]:
    """Return all DNS static entries whose IP falls in prefix.100–255 and name ends in domain."""
    result = {}
    suffix = "." + domain
    dns_path = api.path("ip", "dns", "static")
    for entry in dns_path:
        addr = entry.get("address", "")
        name = entry.get("name", "")
        if not addr or not name:
            continue
        if not name.endswith(suffix):
            continue
        if not addr.startswith(prefix + "."):
            continue
        try:
            last = int(addr.rsplit(".", 1)[1])
        except (ValueError, IndexError):
            continue
        if 100 <= last <= 255:
            result[name] = entry
    log.info("ROS: found %d existing DNS entries in range", len(result))
    return result


def sync_dns(cfg: configparser.ConfigParser, vms: dict[int, dict], dry_run: bool = False) -> None:
    domain = cfg["dns"]["domain"]
    prefix = cfg["dns"]["network_prefix"]

    desired: dict[str, str] = {
        f"{info['name']}.{domain}": f"{prefix}.{vmid}"
        for vmid, info in vms.items()
    }

    api = ros_connect(cfg)
    dns_path = api.path("ip", "dns", "static")
    if dry_run:
        dns_path = DryRunDNS(dns_path)
    current = get_current_dns(api, prefix, domain)

    # Add / update
    for fqdn, ip in desired.items():
        if fqdn not in current:
            log.info("DNS add    %-35s → %s", fqdn, ip)
            dns_path.add(name=fqdn, address=ip)
        else:
            entry = current[fqdn]
            needs_update = entry.get("address") != ip or entry.get("comment", "")
            if needs_update:
                if entry.get("address") != ip:
                    log.info("DNS update %-35s → %s (was %s)", fqdn, ip, entry["address"])
                else:
                    log.info("DNS clear comment %-28s", fqdn)
                dns_path.update(**{".id": entry[".id"], "address": ip, "comment": ""})
            else:
                log.debug("DNS ok     %s → %s", fqdn, ip)

    # Remove stale entries (in range but no longer in PVE)
    for fqdn, entry in current.items():
        if fqdn not in desired:
            log.info("DNS remove %-35s (was %s)", fqdn, entry.get("address"))
            dns_path.remove(entry[".id"])

    sync_wildcard_dns(cfg, dns_path)


def sync_wildcard_dns(cfg: configparser.ConfigParser, dns_path) -> None:
    """Maintain a single split-horizon wildcard so LAN clients resolve the external
    domain (and all subdomains) to Caddy's LAN IP, bypassing NAT hairpin.

    Two matching modes are supported via ``caddy.wildcard_mode``:
      regexp          (default) — works on RouterOS v6 and v7. Writes a ``regexp``
                                   entry matching the apex and all subdomains.
      match-subdomain — RouterOS v7 only. Writes name=<domain>, match-subdomain=yes.

    No-op unless Caddy is enabled and caddy_host is set.
    """
    if not cfg.getboolean("caddy", "enabled", fallback=False):
        return
    ext_domain = cfg["caddy"].get("domain", "").strip()
    caddy_host = cfg["caddy"].get("caddy_host", "").strip()
    if not ext_domain or not caddy_host:
        return

    mode = cfg["caddy"].get("wildcard_mode", "regexp").strip().lower()
    if mode not in ("regexp", "match-subdomain"):
        log.error("caddy.wildcard_mode must be 'regexp' or 'match-subdomain', got %r "
                  "— skipping wildcard", mode)
        return

    # Matches the apex (4tw.pw) and any subdomain (irc.4tw.pw, a.b.4tw.pw).
    regexp = r"(.*\.)?" + ext_domain.replace(".", r"\.") + "$"

    if mode == "regexp":
        desired = {"regexp": regexp, "address": caddy_host}
        label = f"/{regexp}/"
    else:
        desired = {"name": ext_domain, "address": caddy_host, "match-subdomain": "yes"}
        label = f"*.{ext_domain}"

    # Find an existing wildcard entry for this domain in either shape, so we can
    # update in place or migrate between modes.
    existing = next(
        (e for e in dns_path
         if e.get("type", "A") == "A"
         and (e.get("regexp") == regexp or e.get("name") == ext_domain)),
        None,
    )

    if existing is None:
        log.info("DNS add    %-35s → %s (wildcard, %s)", label, caddy_host, mode)
        dns_path.add(**desired)
        return

    same_shape = (
        (mode == "regexp" and existing.get("regexp") == regexp and not existing.get("name"))
        or (mode == "match-subdomain" and existing.get("name") == ext_domain
            and not existing.get("regexp"))
    )

    if same_shape:
        correct = existing.get("address") == caddy_host and (
            mode == "regexp" or existing.get("match-subdomain") in ("true", "yes")
        )
        if correct:
            log.debug("DNS ok     %s → %s (wildcard, %s)", label, caddy_host, mode)
            return
        log.info("DNS update %-35s → %s (was %s) (wildcard, %s)",
                 label, caddy_host, existing.get("address"), mode)
        dns_path.update(**{".id": existing[".id"], **desired})
    else:
        # Shape differs (e.g. switching modes): replace it.
        log.info("DNS migrate %-34s → %s (wildcard → %s)", label, caddy_host, mode)
        dns_path.remove(existing[".id"])
        dns_path.add(**desired)


# ── Caddy ─────────────────────────────────────────────────────────────────────

def build_caddy_block(name: str, ext_domain: str, ip: str, port: str | None) -> str:
    upstream = ip if not port else f"{ip}:{port}"
    return (
        f"{name}.{ext_domain} {{\n"
        f"    reverse_proxy {upstream}\n"
        f"}}\n"
    )


def build_managed_section(vms: dict[int, dict], ext_domain: str, prefix: str) -> str:
    blocks = []
    for vmid in sorted(vms):
        info = vms[vmid]
        ip = f"{prefix}.{vmid}"
        for port, pubname in info["revprox"]:
            name = pubname or info["name"]
            blocks.append(build_caddy_block(name, ext_domain, ip, port))
    return CADDY_START + "\n" + "".join(blocks) + CADDY_END + "\n"


def sync_caddy(cfg: configparser.ConfigParser, vms: dict[int, dict], dry_run: bool = False) -> bool:
    """Rewrite managed section of Caddyfile. Returns True if the file changed."""
    caddy_cfg = cfg["caddy"]
    caddyfile = Path(caddy_cfg["caddyfile"])
    ext_domain = caddy_cfg["domain"]
    prefix = cfg["dns"]["network_prefix"]

    old_content = caddyfile.read_text()
    new_block = build_managed_section(vms, ext_domain, prefix)

    pattern = re.compile(
        rf"^{re.escape(CADDY_START)}\n.*?^{re.escape(CADDY_END)}\n?",
        re.DOTALL | re.MULTILINE,
    )

    if pattern.search(old_content):
        new_content = pattern.sub(new_block, old_content)
    else:
        sep = "\n" if old_content.endswith("\n") else "\n\n"
        new_content = old_content + sep + new_block

    if new_content == old_content:
        log.debug("Caddy: no changes")
        return False

    count = sum(len(info["revprox"]) for info in vms.values())
    if dry_run:
        log.info("DRY-RUN  Caddy would write %d block(s) to %s:\n%s",
                 count, caddyfile, new_block)
        return True

    caddyfile.write_text(new_content)
    log.info("Caddy: wrote %d reverse_proxy block(s)", count)
    return True


def reload_caddy(caddyfile: str) -> None:
    result = subprocess.run(
        ["caddy", "reload", "--config", caddyfile],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("Caddy reloaded OK")
    else:
        log.error("Caddy reload failed: %s", result.stderr.strip())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:] or "-n" in sys.argv[1:]

    config_path = os.environ.get("PVE_ROS_SYNC_CONFIG", "/etc/pve-ros-sync/config.ini")
    cfg = load_config(config_path)

    log.info("=== pve-ros-sync starting%s ===", " (DRY RUN — no changes applied)" if dry_run else "")

    try:
        vms = get_pve_vms(cfg)
    except Exception as exc:
        log.error("PVE fetch failed: %s", exc)
        sys.exit(1)

    try:
        sync_dns(cfg, vms, dry_run=dry_run)
    except Exception as exc:
        log.error("DNS sync failed: %s", exc)

    if cfg.getboolean("caddy", "enabled", fallback=False):
        try:
            changed = sync_caddy(cfg, vms, dry_run=dry_run)
            if changed and not dry_run:
                reload_caddy(cfg["caddy"]["caddyfile"])
        except Exception as exc:
            log.error("Caddy sync failed: %s", exc)

    log.info("=== pve-ros-sync done ===")


if __name__ == "__main__":
    main()
