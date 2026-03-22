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

                vms[vmid] = {"name": name, "tags": tags}

    log.info("PVE: found %d hosts in VMID range 100–255", len(vms))
    return vms


def parse_revprox(tags: set[str]) -> tuple[bool, str | None]:
    """Return (enabled, port_or_None) from a set of PVE tags."""
    for tag in tags:
        if tag == "revprox":
            return True, None
        m = re.match(r"^revprox-(\d+)$", tag)
        if m:
            return True, m.group(1)
    return False, None


# ── RouterOS DNS ──────────────────────────────────────────────────────────────

def ros_connect(cfg: configparser.ConfigParser):
    ros = cfg["routeros"]
    return librouteros.connect(
        host=ros["host"],
        username=ros["user"],
        password=ros["password"],
        port=int(ros.get("port", "8728")),
    )


def get_current_dns(api, prefix: str) -> dict[str, dict]:
    """Return all DNS static entries whose IP falls in prefix.100–255."""
    result = {}
    for entry in api("/ip/dns/static/print"):
        addr = entry.get("address", "")
        name = entry.get("name", "")
        if not addr or not name:
            continue
        if not addr.startswith(prefix + "."):
            continue
        try:
            last = int(addr.rsplit(".", 1)[1])
        except (ValueError, IndexError):
            continue
        if 100 <= last <= 255:
            result[name] = entry
    return result


def sync_dns(cfg: configparser.ConfigParser, vms: dict[int, dict]) -> None:
    domain = cfg["dns"]["domain"]
    prefix = cfg["dns"]["network_prefix"]

    desired: dict[str, str] = {
        f"{info['name']}.{domain}": f"{prefix}.{vmid}"
        for vmid, info in vms.items()
    }

    api = ros_connect(cfg)
    current = get_current_dns(api, prefix)

    # Add / update
    for fqdn, ip in desired.items():
        if fqdn not in current:
            log.info("DNS add    %-35s → %s", fqdn, ip)
            api("/ip/dns/static/add", **{
                "name": fqdn,
                "address": ip,
                "comment": "pve-ros-sync",
            })
        elif current[fqdn].get("address") != ip:
            log.info("DNS update %-35s → %s (was %s)", fqdn, ip, current[fqdn]["address"])
            api("/ip/dns/static/set", **{
                ".id": current[fqdn][".id"],
                "address": ip,
                "comment": "pve-ros-sync",
            })
        else:
            log.debug("DNS ok     %s → %s", fqdn, ip)

    # Remove stale entries (in range but no longer in PVE)
    for fqdn, entry in current.items():
        if fqdn not in desired:
            log.info("DNS remove %-35s (was %s)", fqdn, entry.get("address"))
            api("/ip/dns/static/remove", **{".id": entry[".id"]})


# ── Caddy ─────────────────────────────────────────────────────────────────────

def build_caddy_block(name: str, ext_domain: str, local_domain: str, port: str | None) -> str:
    upstream = f"{name}.{local_domain}"
    if port:
        upstream = f"{upstream}:{port}"
    return (
        f"{name}.{ext_domain} {{\n"
        f"    reverse_proxy {upstream}\n"
        f"}}\n"
    )


def build_managed_section(vms: dict[int, dict], ext_domain: str, local_domain: str) -> str:
    blocks = []
    for vmid in sorted(vms):
        info = vms[vmid]
        enabled, port = parse_revprox(info["tags"])
        if enabled:
            blocks.append(build_caddy_block(info["name"], ext_domain, local_domain, port))
    return CADDY_START + "\n" + "".join(blocks) + CADDY_END + "\n"


def sync_caddy(cfg: configparser.ConfigParser, vms: dict[int, dict]) -> bool:
    """Rewrite managed section of Caddyfile. Returns True if the file changed."""
    caddy_cfg = cfg["caddy"]
    caddyfile = Path(caddy_cfg["caddyfile"])
    ext_domain = caddy_cfg["domain"]
    local_domain = cfg["dns"]["domain"]

    old_content = caddyfile.read_text()
    new_block = build_managed_section(vms, ext_domain, local_domain)

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

    caddyfile.write_text(new_content)
    count = sum(1 for info in vms.values() if parse_revprox(info["tags"])[0])
    log.info("Caddy: wrote %d reverse_proxy block(s)", count)
    return True


def reload_caddy() -> None:
    result = subprocess.run(
        ["systemctl", "reload", "caddy"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("Caddy reloaded OK")
    else:
        log.error("Caddy reload failed: %s", result.stderr.strip())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    config_path = os.environ.get("PVE_ROS_SYNC_CONFIG", "/etc/pve-ros-sync/config.ini")
    cfg = load_config(config_path)

    log.info("=== pve-ros-sync starting ===")

    try:
        vms = get_pve_vms(cfg)
    except Exception as exc:
        log.error("PVE fetch failed: %s", exc)
        sys.exit(1)

    try:
        sync_dns(cfg, vms)
    except Exception as exc:
        log.error("DNS sync failed: %s", exc)

    if cfg.getboolean("caddy", "enabled", fallback=False):
        try:
            changed = sync_caddy(cfg, vms)
            if changed:
                reload_caddy()
        except Exception as exc:
            log.error("Caddy sync failed: %s", exc)

    log.info("=== pve-ros-sync done ===")


if __name__ == "__main__":
    main()
