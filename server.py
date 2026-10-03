#!/usr/bin/env python3
"""PalmerGuest external portal for UniFi Controller 5.1.x.

UniFi redirects guests to:
  http://<portal-ip>/guest/s/<site>/?id=<client-mac>&ap=<ap-mac>&t=<ts>&url=<orig>&ssid=<ssid>

This server renders the landing page, then authorizes the client with the
classic controller API:

  POST /api/login
  POST /api/s/<site>/cmd/stamgr   {"cmd":"authorize-guest","mac":"...","minutes":N}
"""

from __future__ import annotations

import html
import hmac
import hashlib
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG_PATH = Path(os.environ.get("LOG_PATH", "/data/access.log"))

UNIFI_URL = os.environ.get("UNIFI_URL", "https://127.0.0.1:8443").rstrip("/")
UNIFI_USER = os.environ.get("UNIFI_USER", "portal")
UNIFI_PASS = os.environ.get("UNIFI_PASS", "")
UNIFI_SITE = os.environ.get("UNIFI_SITE", "default")
PASSPHRASE = os.environ.get("GUEST_PASSPHRASE", "")
SECRET = os.environ.get("PORTAL_SECRET", "change-me-palmer-guest")
DEFAULT_MINUTES = int(os.environ.get("AUTH_MINUTES", "1440"))
MAX_MINUTES = int(os.environ.get("AUTH_MINUTES_MAX", "10080"))
UP_KBPS = os.environ.get("UP_KBPS", "").strip()
DOWN_KBPS = os.environ.get("DOWN_KBPS", "").strip()
SSID_NAME = os.environ.get("SSID_NAME", "PalmerGuest")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:8080")

SSL_CTX = ssl._create_unverified_context()
MAC_RE = __import__("re").compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    print(text, end="", flush=True)


def sign(mac: str, site: str, url: str, exp: int) -> str:
    msg = f"{mac}|{site}|{url}|{exp}".encode()
    return hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()


def page(headline: str, body: str) -> bytes:
    html = (ROOT / "portal.html").read_text(encoding="utf-8")
    html = html.replace("{{headline}}", headline).replace("{{body}}", body)
    return html.encode("utf-8")


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def form_body(mac: str, site: str, url: str, error: str = "", preview: bool = False) -> str:
    exp = int(time.time()) + 900
    token = sign(mac, site, url, exp)
    err = '<p class="error">{0}</p>'.format(esc(error)) if error else ""
    note = ""
    if preview:
        note = '<p class="preview-note">Preview only. UniFi has not sent a device address yet, so Connect stays quiet.</p>'
    durations = [
        (1440, "1 day"),
        (4320, "3 days"),
        (10080, "7 days"),
    ]
    chips = []
    for minutes, label in durations:
        if minutes > MAX_MINUTES:
            continue
        checked = " checked" if minutes == DEFAULT_MINUTES else ""
        chips.append(
            '<label><input type="radio" name="minutes" value="{minutes}"{checked}><span>{label}</span></label>'.format(
                minutes=minutes, checked=checked, label=label
            )
        )
    if not any("checked" in c for c in chips) and chips:
        chips[0] = chips[0].replace("<input ", "<input checked ", 1)
    button = '<button type="submit">Connect</button>' if not preview else '<button type="button" disabled style="opacity:.55">Connect</button>'
    form_open = "" if preview else '<form method="post" action="/connect">'
    form_close = "" if preview else "</form>"
    return """
      <p class="lede">You're on the guest network. Make yourself at home.</p>
      <div class="ssid"><span class="dot"></span>{ssid}</div>
      {form_open}
        <input type="hidden" name="mac" value="{mac}">
        <input type="hidden" name="site" value="{site}">
        <input type="hidden" name="url" value="{url}">
        <input type="hidden" name="exp" value="{exp}">
        <input type="hidden" name="token" value="{token}">
        <span class="dur-label">How long</span>
        <div class="durations">{chips}</div>
        {err}
        {button}
      {form_close}
      {note}
      <p class="fine">Access ends when the timer does. Reconnect anytime.</p>
    """.format(
        ssid=esc(SSID_NAME),
        form_open=form_open,
        mac=esc(mac),
        site=esc(site),
        url=esc(url),
        exp=exp,
        token=token,
        chips="".join(chips),
        err=err,
        button=button,
        form_close=form_close,
        note=note,
    )


def pretty_duration(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "1 day" if days == 1 else "{0} days".format(days)
    hours = minutes / 60
    if hours >= 1 and hours == int(hours):
        return "{0} hours".format(int(hours))
    return "{0} minutes".format(minutes)


def usable_url(url: str) -> str:
    lowered = url.lower()
    blocked = (
        "captive.apple.com",
        "connectivitycheck",
        "msftconnecttest",
        "generate_204",
        "neverssl.com",
        "detectportal",
    )
    if not (url.startswith("http://") or url.startswith("https://")):
        return ""
    if any(part in lowered for part in blocked):
        return ""
    return url


def success_body(url: str, minutes: int) -> str:
    pretty = pretty_duration(minutes)
    target = usable_url(url)
    link = ""
    if target:
        link = '<a class="continue" href="{url}"><button class="ghost" type="button">Continue</button></a>'.format(url=esc(target))
    return """
      <div class="ok" aria-hidden="true"><span></span></div>
      <p class="lede">You're connected. The guest network is yours for {pretty}.</p>
      <div class="facts">
        <div class="fact"><b>Network</b><span>{ssid}</span></div>
        <div class="fact"><b>Stay</b><span>{pretty}</span></div>
      </div>
      {link}
      <p class="fine">You can close this window. On an iPhone, close the sign-in sheet too — that last step is Apple's, not ours.</p>
    """.format(pretty=pretty, ssid=esc(SSID_NAME), link=link)


def unifi_call(path: str, payload: dict | None, cookie: str | None, form_json: bool = False, csrf: str | None = None) -> tuple[int, str, list[str], str | None]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        if form_json:
            raw = urllib.parse.urlencode({"json": json.dumps(payload)}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            raw = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        data = raw
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(UNIFI_URL + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
            token = resp.headers.get("X-CSRF-Token")
            return resp.status, body, resp.headers.get_all("Set-Cookie") or [], token
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, exc.headers.get_all("Set-Cookie") or [], exc.headers.get("X-CSRF-Token")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason), [], None


def cookie_from(headers: list[str]) -> str | None:
    parts = []
    for header in headers:
        pair = header.split(";", 1)[0].strip()
        name = pair.split("=", 1)[0].lower()
        if name in ("unifises", "csrf_token", "token"):
            parts.append(pair)
    return "; ".join(parts) if parts else None


def login_reason(status: int, body: str) -> str:
    try:
        parsed = json.loads(body)
        meta = parsed.get("meta", {}) if isinstance(parsed, dict) else {}
        msg = meta.get("msg") or ""
    except json.JSONDecodeError:
        msg = ""
    if status == 0:
        return "The portal could not reach the controller. Check UNIFI_URL from the container."
    if msg == "api.err.Invalid" or status in (400, 401, 403):
        return "The controller rejected the portal username or password. Use a local admin, not a UI.com account."
    if status == 404:
        return "Login path was not found. For this gateway, UNIFI_URL should be https://192.168.1.1 with no port."
    if msg:
        return "Controller login failed: {0}.".format(msg)
    return "Controller login failed (HTTP {0}).".format(status)


def authorize(mac: str, site: str, minutes: int) -> tuple[bool, str]:
    if not UNIFI_PASS:
        return False, "Portal is not configured with a UniFi password yet."
    # 192.168.1.1 with no port is UniFi OS (port 443). 8443 is the old Java controller.
    os_gateway = ":8443" not in UNIFI_URL
    login_path = "/api/auth/login" if os_gateway else "/api/login"
    status, body, set_cookies, csrf = unifi_call(
        login_path,
        {"username": UNIFI_USER, "password": UNIFI_PASS},
        None,
    )
    cookie = cookie_from(set_cookies)
    try:
        meta = json.loads(body).get("meta", {})
    except json.JSONDecodeError:
        meta = {}
    if status >= 400 or status == 0 or meta.get("rc") not in (None, "ok") or not cookie:
        log("login failed status={0} path={1} cookies={2} body={3}".format(
            status, login_path, [c.split("=", 1)[0] for c in set_cookies], body[:300]
        ))
        return False, login_reason(status, body)

    payload = {"cmd": "authorize-guest", "mac": mac, "minutes": minutes}
    if UP_KBPS.isdigit():
        payload["up"] = int(UP_KBPS)
    if DOWN_KBPS.isdigit():
        payload["down"] = int(DOWN_KBPS)

    if os_gateway:
        path = "/proxy/network/api/s/{0}/cmd/stamgr".format(urllib.parse.quote(site))
    else:
        path = "/api/s/{0}/cmd/stamgr".format(urllib.parse.quote(site))
    status, body, _, _ = unifi_call(path, payload, cookie, form_json=False, csrf=csrf)
    ok = False
    try:
        ok = json.loads(body).get("meta", {}).get("rc") == "ok"
    except json.JSONDecodeError:
        ok = False
    if not ok and not os_gateway:
        status, body, _, _ = unifi_call(path, payload, cookie, form_json=True, csrf=csrf)
        try:
            ok = json.loads(body).get("meta", {}).get("rc") == "ok"
        except json.JSONDecodeError:
            ok = False
    unifi_call("/api/auth/logout" if os_gateway else "/api/logout", {}, cookie, csrf=csrf)
    if not ok:
        log("authorize failed mac={0} status={1} body={2}".format(mac, status, body[:300]))
        return False, "UniFi didn't authorize this device. It may need a moment, then try again."
    return True, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "PalmerGuest/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log("%s - %s" % (self.address_string(), fmt % args))

    def send_html(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
            return
        if path in ("/", "/preview"):
            self.send_html(200, page("Welcome in.", form_body("", UNIFI_SITE, "", preview=True)))
            return
        if path == "/success":
            self.send_html(200, page("You're in.", success_body("https://palmer.home", DEFAULT_MINUTES)))
            return
        # UniFi 5.1 redirect: /guest/s/<site>/ or /guest/s/<site>/index.html
        site = UNIFI_SITE
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "guest" and parts[1] == "s":
            site = parts[2]
        qs = urllib.parse.parse_qs(parsed.query)
        mac = (qs.get("id") or [""])[0].lower()
        url = (qs.get("url") or ["http://neverssl.com"])[0]
        ssid = (qs.get("ssid") or [SSID_NAME])[0]
        if not MAC_RE.match(mac):
            self.send_html(200, page("Welcome in.", form_body("", site, url, preview=True)))
            return
        log(f"portal hit mac={mac} site={site} ssid={ssid}")
        self.send_html(200, page("Welcome in.", form_body(mac, site, url)))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/connect":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(min(length, 20000)).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw)
        mac = (form.get("mac") or [""])[0].lower()
        site = (form.get("site") or [UNIFI_SITE])[0]
        url = (form.get("url") or [""])[0]
        exp = (form.get("exp") or ["0"])[0]
        token = (form.get("token") or [""])[0]
        try:
            minutes = int((form.get("minutes") or [str(DEFAULT_MINUTES)])[0])
        except ValueError:
            minutes = DEFAULT_MINUTES
        minutes = max(15, min(minutes, MAX_MINUTES))

        def fail(msg: str) -> None:
            self.send_html(200, page("Welcome in.", form_body(mac, site, url, error=msg)))

        if not MAC_RE.match(mac):
            fail("This page lost the device address. Rejoin PalmerGuest and try again.")
            return
        try:
            exp_i = int(exp)
        except ValueError:
            exp_i = 0
        expected = sign(mac, site, url, exp_i)
        if exp_i < time.time() or not hmac.compare_digest(expected, token):
            fail("That form expired. Refresh the page and try again.")
            return
        ok, err = authorize(mac, site, minutes)
        if not ok:
            fail(err)
            return
        log(f"authorized mac={mac} site={site} minutes={minutes}")
        self.send_html(200, page("You're in.", success_body(url, minutes)))

    def serve_static(self, name: str) -> None:
        safe = Path(name).name
        target = STATIC / safe
        if not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        kind = "image/png" if safe.endswith(".png") else "image/jpeg" if safe.endswith(".jpg") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    host, _, port = LISTEN.partition(":")
    httpd = ThreadingHTTPServer((host, int(port or "8080")), Handler)
    log(f"listening on {host}:{port or '8080'} unifi={UNIFI_URL} site={UNIFI_SITE}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
