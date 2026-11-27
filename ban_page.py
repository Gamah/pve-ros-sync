#!/usr/bin/env python3
"""ban-page: the 403 Caddy serves to an IP CrowdSec has banned.

Caddy's error route proxies banned requests here with X-Request-ID (Caddy's
per-request uuid, also in its access log) and X-Ban-Site (the subdomain hit).
The page shows a short code, HMAC(secret, uuid:decision); each served page
logs code → uuid → decision, so an emailed code resolves with one grep.
"""

import base64
import configparser
import hashlib
import hmac
import html
import json
import logging
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("ban-page")

LAPI_URL = "http://127.0.0.1:8080"
CACHE_TTL = 60  # seconds a decision lookup is reused for one IP
FACE_SVG = Path(__file__).with_name("awesome_face.svg")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>403 banned</title>
<link rel="icon" href="{face}">
<style>
:root{{--bg:#0a0d0c;--text:#d9ddd9;--dim:#7c857f;--line:#232a26;--green:#7fb39d;
--mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:1rem/1.6 var(--sans)}}
main{{max-width:44rem;margin:0 auto;padding:4rem 1.25rem 5rem}}
img{{width:6rem;height:6rem;display:block;margin-bottom:2rem}}
h1{{margin:0 0 1.5rem;font-size:clamp(2rem,6vw,3.25rem);font-weight:800;line-height:1;letter-spacing:-.02em}}
h1 span{{display:block;font-size:clamp(5rem,20vw,10rem);letter-spacing:-.05em;color:var(--green)}}
p{{color:var(--dim)}}
a{{color:var(--text);text-underline-offset:.25em}}
pre{{margin:1.5rem 0;padding:1.5rem 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
font:1.5rem/1.7 var(--mono);letter-spacing:.12em;color:var(--green)}}
</style></head><body><main>
<img src="{face}" alt="">
<h1>Banned <span>403</span></h1>
<p>Your address has been banned from this site.</p>
<p>If you think that's a mistake, email {contact} with this code:</p>
<pre>{code}</pre>
</main></body></html>
"""


def make_code(secret: bytes, request_id: str, decision_id: str) -> str:
    """10 base32 characters (50 bits): typeable, no case or 0/O confusion."""
    mac = hmac.new(secret, f"{request_id}:{decision_id}".encode(), hashlib.sha256).digest()
    return base64.b32encode(mac).decode()[:10]


def contact_line(site: str, domain: str, extra: str) -> str:
    addrs = []
    if site and domain:
        addrs.append(f"{site}@{domain}")
    if extra:
        addrs.append(extra)
    if not addrs:
        return "the site's administrator"
    links = [f'<a href="mailto:{html.escape(a)}">{html.escape(a)}</a>' for a in addrs]
    return " or ".join(links)


def pick_decision(decisions) -> str:
    """The id of the newest active ban on this IP, or "none"."""
    ids = [d["id"] for d in decisions or [] if d.get("type") == "ban" and isinstance(d.get("id"), int)]
    return str(max(ids)) if ids else "none"


class DecisionLookup:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.cache: dict[str, tuple[float, str]] = {}

    def __call__(self, ip: str) -> str:
        now = time.monotonic()
        hit = self.cache.get(ip)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        req = urllib.request.Request(
            f"{LAPI_URL}/v1/decisions?ip={ip}", headers={"X-Api-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                decision = pick_decision(json.load(resp))
        except Exception as exc:
            log.warning("LAPI lookup for %s failed: %s", ip, exc)
            decision = "unknown"
        if len(self.cache) > 10000:
            self.cache.clear()
        self.cache[ip] = (now, decision)
        return decision


def face_uri() -> str:
    try:
        return "data:image/svg+xml;base64," + base64.b64encode(FACE_SVG.read_bytes()).decode()
    except OSError:
        return ""


def make_handler(secret: bytes, lookup, contact_domain: str, contact_email: str):
    face = face_uri()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body: bool):
            # Caddy is the only client: X-Forwarded-For's last entry is the peer it saw.
            ip = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            request_id = self.headers.get("X-Request-ID", "")
            site = self.headers.get("X-Ban-Site", "")
            decision = lookup(ip) if ip else "unknown"
            code = make_code(secret, request_id, decision)
            log.info("code=%s uuid=%s decision=%s ip=%s site=%s",
                     code, request_id, decision, ip, site)
            page = PAGE.format(code=code, face=face, contact=contact_line(site, contact_domain, contact_email))
            data = page.encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")  # any request body is left unread
            self.close_connection = True
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_HEAD(self):
            self.respond(body=False)

        def __getattr__(self, name):
            # Every other method (GET, POST, PROPFIND, …) gets the page.
            if name.startswith("do_"):
                return lambda: self.respond(body=True)
            raise AttributeError(name)

        def log_message(self, fmt, *args):
            pass  # one line per request is logged in respond()

    return Handler


def main() -> None:
    secret = os.environ.get("BANPAGE_SECRET", "")
    api_key = os.environ.get("BANPAGE_LAPI_KEY", "")
    if not secret or not api_key:
        log.error("BANPAGE_SECRET and BANPAGE_LAPI_KEY must be set (see /etc/pve-ros-sync/.env)")
        sys.exit(1)

    # Under systemd the config arrives as a credential (the service has no access to /etc).
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred_dir, "config.ini") if cred_dir else Path(
        os.environ.get("PVE_ROS_SYNC_CONFIG", "/etc/pve-ros-sync/config.ini"))
    cfg = configparser.ConfigParser()
    cfg.read(path)
    bp = cfg["banpage"] if cfg.has_section("banpage") else {}
    port = int(bp.get("port", "9180"))

    handler = make_handler(secret.encode(), DecisionLookup(api_key),
                           bp.get("contact_domain", ""), bp.get("contact_email", ""))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    log.info("listening on 127.0.0.1:%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
