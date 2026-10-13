"""JLCONE, the page inside it, and the gate every call passes.

JLCONE, JLC's desktop app, is the jlcpcb.com website in Electron tabs that share one login; it has no
API of its own. jlc-agent starts it with a Chrome DevTools port bound to this machine, finds its
jlcpcb.com tab and runs each request inside that page, so every call carries the app's own login,
XSRF token and desktop identity, exactly as the page's own requests do. No API keys, no scraped
passwords, no second session.

The site uses POST for reads as well as writes, so the method says nothing about what a call does.
Every path is sorted before it is sent: reads run freely; calls that change the account need
`write=True`, and are logged; calls that pay, check out, create an order or move money are refused
whatever the flags. Paying is done by a person, in JLCONE's own window.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from websockets.sync.client import connect

SITE = "https://jlcpcb.com"
PORT = int(os.environ.get("JLC_AGENT_PORT", "9339"))
DEVTOOLS = f"http://127.0.0.1:{PORT}"
SYSTEM = platform.system()  # Windows, Darwin, Linux


def _data_dir() -> Path:
    if os.environ.get("JLC_AGENT_HOME"):
        return Path(os.environ["JLC_AGENT_HOME"])
    if SYSTEM == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "jlc-agent"
    if SYSTEM == "Darwin":
        return Path.home() / "Library" / "Application Support" / "jlc-agent"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "jlc-agent"


def _app_path() -> Path:
    if os.environ.get("JLCONE_PATH"):
        return Path(os.environ["JLCONE_PATH"])
    if SYSTEM == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "JLCONE" / "JLCONE.exe"
    if SYSTEM == "Darwin":
        return Path("/Applications/JLCONE.app/Contents/MacOS/JLCONE")
    return Path("/opt/JLCONE/jlcone")


DATA = _data_dir()
APP = _app_path()
WRITES_LOG = DATA / "writes.log"  # every account change made through this tool, with its body
LAUNCH_FLAGS = [f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1"]


class JlcError(RuntimeError):
    """A user-facing failure; never carries a cookie or token."""


# --- the gate ------------------------------------------------------------------------------

# Paths are the part after /api. The action is the last segment (the one before a /V2 suffix);
# a read starts with a verb the site only reads with and names no write anywhere in it.
READ_ACTION = re.compile(r"^(select|query|get|list|page|count|check|view|show|search|is|whether|enable|"
                         r"download|export|preview|find)(?=[A-Z0-9_]|$)|^detail$")
WRITE_WORD = re.compile(r"(Upload|Submit|Cancel|Delete|Create|Save|Reply|Remove|Receive|Update|Send|Bind|"
                        r"Apply|Replace|Edit|Add|Drop|Sign|Read|TradeNo)(?=[A-Z0-9_]|$)")
# Reads whose names carry no read verb.
KNOWN_READS = {
    "/overseas-core-platform/order/orderWipProcessGroupedProgress",
    "/overseas-pcb-order/v1/newOrder/orderWipProcess",
    "/overseas-pcb-order/v1/smtOrder/smtWipProcess",
    "/overseas-pcb-order/v1/newOrder/myOrderDetail",
    "/overseas-pcb-order/v1/newOrder/myOrderBatchList",
    "/overseas-core-platform/orderCenter/invoiceOrder",
    "/overseas-pcb-order/v1/newOrder/downInvoicePDF",
    "/overseas-pcb-order/v1/confirmFile/confirmFileDetail",
    "/overseas-flex-heater-platform/confirmFile/confirmFileDetail",
}
# Opening a message marks it read on the site, so it counts as a change.
NEVER_READ = {"selectMessageNoticeDetail"}
# Money moves or an order is created: never called, whatever the flags.
BLOCKED = re.compile(r"/orderTrade/|/jpayAccount/(?:save|reset|setRefunds)|[cC]heck[oO]ut|createOrder|"
                     r"submitPurchaseOrder|submitShoppingCart|payBatchOrder|paySupplement|payAfterReview|"
                     r"[wW]ithdraw|submitRefund|/balance/(?:submitApply|checkPassword)|getTradeNo|[rR]echarge|"
                     r"ApplePay|Adyen|/stampApply/submit")


def api_path(text: str) -> str:
    """`/a/b`, `a/b`, or Git Bash's rewrite of `/a/b` into `C:/Program Files/Git/a/b`, as `/a/b`."""
    text = re.sub(r"^[A-Za-z]:/.*?/Git(?:/usr)?(?=/)", "", text.replace("\\", "/"))
    return "/" + text.lstrip("/")


def is_read_path(path: str) -> bool:
    """True if `path` (after /api) is one the site only reads with."""
    path = path.split("?")[0].rstrip("/")
    if path in KNOWN_READS:
        return True
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    action = parts[-2] if re.fullmatch(r"[vV]\d+", parts[-1]) and len(parts) > 1 else parts[-1]
    if action in NEVER_READ or WRITE_WORD.search(action):
        return False
    return "checkWhiteList" in parts[-2:] or bool(READ_ACTION.search(action))


def classify(path: str) -> str:
    """'blocked' (pays, orders, withdraws: never), 'read' (free), or 'write' (needs write=True)."""
    if BLOCKED.search(path.split("?")[0]):
        return "blocked"
    return "read" if is_read_path(path) else "write"


def gate(path: str, write: bool) -> str:
    """The path's kind, or JlcError if the call may not be made: blocked always, a write without
    `write`."""
    kind = classify(path)
    if kind == "blocked":
        raise JlcError(f"{path.split('?')[0]} pays, checks out, creates an order or moves money; jlc-agent "
                       "never makes that call. Do it in the JLCONE window.")
    if kind == "write" and not write:
        raise JlcError(f"{path.split('?')[0]} changes the account; it needs write=True (--write), which is "
                       "for changes the account owner asked for.")
    return kind


# --- the app -------------------------------------------------------------------------------

def devtools_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DEVTOOLS}/json/version", timeout=2) as response:
            return "Electron" in json.load(response).get("User-Agent", "")
    except (OSError, ValueError):
        return False


def run(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    """A helper process that inherits nothing and opens no console window. Under the MCP server stdin
    and stdout are the protocol, so no child may read or write them."""
    return subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _launch(args: list[str]) -> None:
    """Start a program and leave it running, detached from this process and its streams."""
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                     start_new_session=SYSTEM != "Windows")


def app_running() -> bool:
    """JLCONE open at all, with or without the port."""
    if SYSTEM == "Windows":
        return "JLCONE.exe" in run(["tasklist", "/FI", "IMAGENAME eq JLCONE.exe", "/NH"]).stdout
    return run(["pgrep", "-x", APP.name]).returncode == 0


def _quit_app() -> None:
    if SYSTEM == "Windows":
        run(["taskkill", "/IM", "JLCONE.exe"])
    elif SYSTEM == "Darwin":
        run(["osascript", "-e", 'quit app "JLCONE"'])
    else:
        run(["pkill", "-x", APP.name])
    deadline = time.monotonic() + 15
    while app_running() and time.monotonic() < deadline:
        time.sleep(0.5)
    if app_running() and SYSTEM == "Windows":
        run(["taskkill", "/F", "/IM", "JLCONE.exe"])
        time.sleep(1)


def start(wait: float = 30.0, restart: bool = False) -> None:
    """Open JLCONE with the DevTools port bound to this machine only."""
    if devtools_up():
        return
    if app_running():
        # a single-instance app: a second launch hands its arguments to the first and exits, so the
        # port flag would be lost
        if not restart:
            raise JlcError("JLCONE is open without the DevTools port. Run `jlc-agent start --restart` to "
                           "close it and reopen it with the port.")
        _quit_app()
    if not APP.exists():
        raise JlcError(f"JLCONE is not installed at {APP} (get it at https://jlcone.com/download, or set "
                       "JLCONE_PATH).")
    _launch([str(APP), *LAUNCH_FLAGS])
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if devtools_up():
            return
        time.sleep(0.5)
    raise JlcError(f"JLCONE started but its DevTools port {PORT} did not come up.")


SHORTCUTS = [Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "JLCONE.lnk",
             Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "JLCONE.lnk"]


def set_shortcuts(on: bool) -> list[str]:
    """Windows: make JLCONE's own Start-menu and desktop shortcuts open it with (or without) the
    DevTools port. JLC's updater may recreate them without it; `start --restart` recovers."""
    if SYSTEM != "Windows":
        raise JlcError("shortcuts are Windows only; elsewhere launch JLCONE with "
                       f"`{APP} {' '.join(LAUNCH_FLAGS)}`, or run `jlc-agent start`.")
    arguments = " ".join(LAUNCH_FLAGS) if on else ""
    changed = []
    for shortcut in SHORTCUTS:
        if not shortcut.exists():
            continue
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JLC_LNK); "
                  "$s.Arguments = $env:JLC_ARGS; $s.Save()")
        result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                     env=dict(os.environ, JLC_LNK=str(shortcut), JLC_ARGS=arguments))
        if result.returncode != 0:
            raise JlcError(f"could not update {shortcut}: {result.stderr.strip()[:200]}")
        changed.append(str(shortcut))
    return changed


def targets() -> list[dict]:
    with urllib.request.urlopen(f"{DEVTOOLS}/json/list", timeout=5) as response:
        return json.load(response)


def open_tab(url: str) -> None:
    """Show `url` in JLCONE through the app's own jlcone:// link handler, starting JLCONE (with the
    port) first if it is closed."""
    if not app_running():
        start()
    link = "jlcone://open?url=" + urllib.parse.quote(url, safe="")
    if SYSTEM == "Darwin":
        run(["open", link])
    else:  # Windows and Linux: a second launch hands its argument to the running instance and exits
        _launch([str(APP), link])


def site_target(wait: float = 20.0) -> dict:
    """The app's jlcpcb.com tab, opening one if there is none (a login page does not count)."""
    if not devtools_up():
        raise JlcError("JLCONE is not running with the DevTools port; run `jlc-agent start`.")
    opened = False
    deadline = time.monotonic() + wait
    while True:
        for target in targets():
            if target.get("type") in ("page", "webview") and target.get("url", "").startswith(SITE + "/"):
                return target
        if time.monotonic() > deadline:
            raise JlcError("No jlcpcb.com tab in JLCONE. If it shows the login page, log in there first.")
        if not opened:
            open_tab(SITE + "/user-center/orders")
            opened = True
        time.sleep(1)


# --- DevTools protocol ---------------------------------------------------------------------

class Cdp:
    """Just enough of the DevTools protocol: send a command, wait for its answer, keep the events."""

    def __init__(self, ws_url: str):
        self._ws = connect(ws_url, max_size=None, open_timeout=10)
        self._next = 0
        self.events: list[dict] = []

    def __enter__(self) -> "Cdp":
        return self

    def __exit__(self, *exc) -> None:
        self._ws.close()

    def pump(self, seconds: float) -> None:
        """Collect events (network responses, page loads) for `seconds`."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                message = json.loads(self._ws.recv(timeout=left))
            except TimeoutError:
                return
            if "method" in message:
                self.events.append(message)

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._next += 1
        ident = self._next
        self._ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            try:
                if left <= 0:
                    raise TimeoutError
                message = json.loads(self._ws.recv(timeout=left))
            except TimeoutError:
                raise JlcError(f"{method}: no answer from JLCONE in {timeout:.0f} s") from None
            if message.get("id") == ident:
                if "error" in message:
                    raise JlcError(f"{method}: {message['error'].get('message')}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)

    def evaluate(self, expression: str, timeout: float = 60.0) -> Any:
        result = self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                "returnByValue": True}, timeout=timeout)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise JlcError(f"in the page: {text}")
        return result.get("result", {}).get("value")


# Runs inside the jlcpcb.com tab and mirrors the site's own request layer: the XSRF cookie echoed as a
# header, the rotating `secretkey` from /secret/update (about 30 minutes; a stale one comes back as
# success=false with 401 or 403), and the desktop identity the app injects as window.appClient.
PAGE_FETCH = r"""
(async (method, path, body, form, binary) => {
  const state = (window.__jlcAgent = window.__jlcAgent || {});
  const cookie = name => {
    const hit = document.cookie.split('; ').find(c => c.startsWith(name + '='));
    return hit ? decodeURIComponent(hit.slice(name.length + 1)) : '';
  };
  const headers = {'Content-Type': 'application/json', 'Accept': 'application/json'};
  if (cookie('XSRF-TOKEN')) headers['x-xsrf-token'] = cookie('XSRF-TOKEN');
  if (window.appClient) {
    headers['x-jlc-platform'] = 'desktop';
    try { const d = window.appClient.getDeviceId && window.appClient.getDeviceId(); if (typeof d === 'string' && d) headers['x-jlc-device'] = d; } catch (e) {}
  }
  const refresh = async () => {
    const r = await fetch('/api/overseas-core-platform/secret/update', {method: 'POST', credentials: 'include',
      headers, body: JSON.stringify({keyId: crypto.randomUUID().replace(/-/g, '')})});
    const j = await r.json().catch(() => null);
    state.secret = j && j.data && j.data.keyId; state.at = Date.now();
  };
  const go = async () => {
    const h = Object.assign({}, headers);
    if (state.secret) h.secretkey = state.secret;
    let payload;
    if (method !== 'GET') {
      if (form) { h['Content-Type'] = 'application/x-www-form-urlencoded'; payload = new URLSearchParams(body || {}).toString(); }
      else payload = JSON.stringify(body || {});
    }
    const r = await fetch('/api' + path, {method, credentials: 'include', headers: h, body: payload});
    if (binary) {
      const bytes = new Uint8Array(await r.arrayBuffer());
      let bin = '';
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      return {status: r.status, type: r.headers.get('content-type'), b64: btoa(bin)};
    }
    const text = await r.text();
    let json = null; try { json = JSON.parse(text); } catch (e) {}
    return {status: r.status, json, text: json === null ? text.slice(0, 300) : null};
  };
  if (!state.secret || Date.now() - state.at > 20 * 60 * 1000) await refresh();
  let out = await go();
  if (out.json && out.json.success === false && [401, 403].includes(out.json.code)) { await refresh(); out = await go(); }
  return out;
})
"""


class Session:
    """A connection to the app's jlcpcb.com tab: `with Session() as jlc: jlc.api(...)`."""

    def __init__(self):
        self.target = site_target()
        self.cdp = Cdp(self.target["webSocketDebuggerUrl"])

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.cdp.__exit__(*exc)

    def _call(self, method: str, path: str, body: Any, query: dict | None, write: bool, form: bool,
              binary: bool) -> dict:
        kind = gate(path, write)
        if query:
            path = path + ("&" if "?" in path else "?") + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
        if kind == "write":
            WRITES_LOG.parent.mkdir(parents=True, exist_ok=True)
            with WRITES_LOG.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds"),
                                      "method": method.upper(), "path": path, "body": body}, ensure_ascii=False) + "\n")
        call = (f"{PAGE_FETCH}({json.dumps(method.upper())}, {json.dumps(path)}, {json.dumps(body)}, "
                f"{json.dumps(form)}, {json.dumps(binary)})")
        out = self.cdp.evaluate(call)
        if out.get("status") == 460:
            raise JlcError("JLC session expired (HTTP 460): log in again in the JLCONE window.")
        return out

    def api(self, method: str, path: str, body: Any = None, query: dict | None = None, write: bool = False,
            form: bool = False) -> Any:
        """One call through the page; returns the envelope's data or raises."""
        out = self._call(method, path, body, query, write, form, binary=False)
        status, envelope = out.get("status"), out.get("json")
        if envelope is None:
            raise JlcError(f"{path.split('?')[0]}: HTTP {status}, not JSON: {out.get('text')!r}")
        if status == 200 and isinstance(envelope, dict) and "code" not in envelope:
            return envelope  # some endpoints answer with the data itself
        if isinstance(envelope, dict) and envelope.get("code") == 460:
            # JLC logs sessions out on its own ("For security reasons, you were automatically logged
            # out"), answering HTTP 200 with code 460; seen about 18 hours after a login. After the
            # login in the JLCONE window, this tab can keep answering 460 on its old session key until
            # it reloads: with the login cookie present, reload once and ask again.
            if not getattr(self, "_reloaded", False) and self.logged_in():
                self._reloaded = True
                self.cdp.send("Page.reload", {"ignoreCache": True})
                time.sleep(8)
                return self.api(method, path, body, query, write, form)
            raise JlcError("JLC logged this session out (code 460): log in again in the JLCONE window.")
        if status != 200 or envelope.get("code") != 200:
            message = envelope.get("message") or envelope.get("msg")
            raise JlcError(f"{path.split('?')[0]}: HTTP {status}, code {envelope.get('code')}: {message}")
        return envelope.get("data")

    def download(self, method: str, path: str, body: Any = None, query: dict | None = None,
                 form: bool = False, write: bool = False) -> tuple[bytes, str]:
        """A file the site serves (image, zip, PDF) and its content type."""
        out = self._call(method, path, body, query, write, form, binary=True)
        if out.get("status") != 200:
            raise JlcError(f"{path.split('?')[0]}: HTTP {out.get('status')}")
        return base64.b64decode(out["b64"]), out.get("type") or ""

    def page_value(self, expression: str) -> Any:
        return self.cdp.evaluate(expression)

    def logged_in(self) -> bool:
        """JLC sets this cookie only after a login (it is HttpOnly, so ask DevTools, not the page)."""
        cookies = self.cdp.send("Network.getCookies", {"urls": [SITE + "/"]}).get("cookies", [])
        return any(c.get("name") == "jlc_session_customer_code" and c.get("value") for c in cookies)


def status() -> dict:
    report: dict[str, Any] = {"app": str(APP), "installed": APP.exists(), "port": PORT, "devtools": devtools_up(),
                              "data": str(DATA), "python": sys.version.split()[0]}
    if not report["devtools"]:
        report["running"] = app_running()
        return report
    with urllib.request.urlopen(f"{DEVTOOLS}/json/version", timeout=5) as response:
        version = re.search(r"jlcone/([\d.]+)", json.load(response).get("User-Agent", ""))
    report["version"] = version.group(1) if version else None
    report["tabs"] = [t.get("url", "").split("?")[0] for t in targets() if t.get("type") in ("page", "webview")]
    with Session() as jlc:
        report["desktop_identity"] = bool(jlc.page_value("!!(window.appClient && window.appClient.getDeviceId)"))
        report["logged_in"] = jlc.logged_in()
    return report
