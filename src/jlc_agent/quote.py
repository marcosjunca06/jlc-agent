"""Prepare a JLC order in JLCONE's quote pages, the way a person would, and stop before the cart.

Drives a JLCONE tab through its DevTools port: uploads the Gerbers, sets every option the order spec
names, switches PCB assembly on, uploads the BOM and CPL, and reads back what JLC made of them: the
options it settled on, its price, its part matches and shortages, and its placement preview. The
page's own API responses are the record; screenshots are for eyes.

It never saves to the cart, pays, confirms or submits. The prepared quote stays open in JLCONE for a
person to check and order there, which is also where JLC's desktop-only prices apply.

An order spec sits next to the release files. Rows and options are spelled exactly as the quote page
shows them, in page order, because some rows only appear after an earlier choice (Layer Stackup
after Specify Stackup: Yes). Rows left out keep JLC's default; the report lists every row as set.

    {"board": "my board rev B",
     "gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv",
     "pcb": {"PCB Qty": "5", "Min via hole size/diameter": "0.2mm/(0.3/0.35mm)",
             "Specify Stackup": "Yes", "Layer Stackup": "JLC06161H-3313"},
     "assembly": {"PCBA Type": "Standard", "Assembly Side": "Top Side"},
     "accept": ["actual thickness of this stackup"]}

Leave out "assembly" for a bare board. Some choices raise a JLC question (JLC06161H-3313: "The actual
thickness of this stackup is 1.5384mm ... Do you still want to select this stackup?"); the run
answers Yes only to questions containing a phrase listed under "accept", which is where a person
records having read and agreed to it, and otherwise stops and quotes the question.

A question whose answer is not Yes goes under "answers": a phrase from it, and what to press in its
window, in order, each named exactly as the page shows it: a button, or a tick box by its label, which
is ticked and checked to show ticked. JLC's window for Confirm Parts Placement: Yes (since October
2026: confirm within 72 hours or production starts by itself) has one button, Confirm, and a box
"Do not confirm automatically":
{"will be automatically confirmed": ["Do not confirm automatically", "Confirm"]}.
The row is checked afterwards like any other choice, and the report notes each answer.

"remark" is typed into the quote page's PCB Remark (200 characters at most), "assembly_remark" into
the order page's Assembly Remark (500 at most), which is where JLC reads assembly instructions.

Build time. "pcb_build_time" and "assembly_build_time" pick the build times, by label ("2-3 days") or
"fastest"; left out, a paid time the page chose by itself stops the run (the page remembers one picked
on an earlier quote). What else decides when boards arrive, seen on a six-layer board in September 2026:
- Impedance Control took the faster PCB times away: 8-9 days alone, where the board without it was
  offered 2-3 to 8-9 days, the faster ones at a price.
- "Confirm Production file: Yes" and "Confirm Parts Placement: Yes" hold production until someone
  approves JLC's file; "No" starts it the day of payment.
- Production starts the day of payment when paid before 18:00 GMT+8, Monday to Saturday; JLC's
  holiday closures are announced on the order page. The carrier is chosen at checkout.

The run also stops, with the reason, when an option cannot be set, the via option allows bigger holes
than the drill file has, JLC cannot match or stock a part, or JLC's imported placements differ from
the CPL. Report, JLC's raw data and screenshots go to <data dir>/quotes/<folder>-<time>/.
"""

from __future__ import annotations

import base64
import csv
import datetime
import hashlib
import io
import json
import re
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any, Callable

from .app import DATA, Cdp, JlcError, devtools_up, open_tab, targets

QUOTE_URL = "https://cart.jlcpcb.com/quote"
REPORTS = DATA / "quotes"
TRACES = DATA / "traces"
VIA_ROW = "Min via hole size/diameter"

# Helpers installed in the page before every call (a page load drops them). Option rows on the quote
# pages are div.main-btn-group: a <label> naming the row and <button>s for the choices, the chosen
# one carrying class `cur`.
PAGE_HELPERS = r"""
window.__jlcq = (() => {
  // offsetParent is null for position:fixed elements (every modal), so ask for boxes instead
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const words = s => (s || '').replace(/\s+/g, ' ').trim();
  const first = s => words((s || '').split('\n')[0]);
  const rowLabel = row => {
    const label = row.querySelector(':scope > label');
    if (!label) return null;
    const own = [...label.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
    return words(own) || first(label.innerText);
  };
  const rows = () => [...document.querySelectorAll('.main-btn-group')].filter(visible);
  const row = label => rows().find(r => rowLabel(r) === label) || null;
  const buttons = r => [...r.querySelectorAll('button')].filter(visible);
  const isOff = b => b.disabled || /(^|\s)(disabled|is-disabled|disable)(\s|$)/.test(b.className);
  const option = (label, text) => {
    const r = row(label);
    if (!r) return null;
    return buttons(r).find(b => first(b.innerText) === text || words(b.innerText) === text) || null;
  };
  const describe = r => ({
    label: rowLabel(r),
    options: buttons(r).map(b => first(b.innerText)),
    selected: buttons(r).filter(b => b.classList.contains('cur')).map(b => first(b.innerText)),
    disabled: buttons(r).filter(isOff).map(b => first(b.innerText)),
    values: [...r.querySelectorAll('input')].filter(i => visible(i) && i.type !== 'file').map(i => i.value)
  });
  const centre = el => {
    el.scrollIntoView({block: 'center', inline: 'center'});
    const box = el.getBoundingClientRect();
    return {x: box.left + box.width / 2, y: box.top + box.height / 2, w: box.width, h: box.height};
  };
  const byText = (text, selector) => [...document.querySelectorAll(selector || 'button, a, span, div, p, h2, label')]
    .filter(el => visible(el) && first(el.innerText) === text)
    .sort((a, b) => a.getElementsByTagName('*').length - b.getElementsByTagName('*').length)[0] || null;
  // modal boxes wait for an answer; toasts (.el-message, .el-notification) only inform
  const dialogs = () => [...document.querySelectorAll('.el-dialog__wrapper, .el-message-box__wrapper')]
    .filter(visible).map(d => words(d.innerText).slice(0, 400)).filter(Boolean);
  const toasts = () => [...document.querySelectorAll('.el-message, .el-notification')]
    .filter(visible).map(d => words(d.innerText).slice(0, 200)).filter(Boolean);
  // rows like PCB Qty show one value and keep their choices in a pop-up that Element UI moves to the
  // end of <body> when it opens, so it is looked for there, not in the row
  const value = label => {
    const r = row(label);
    if (!r) return null;
    const picked = buttons(r).filter(b => b.classList.contains('cur')).map(b => first(b.innerText));
    if (picked.length) return picked.join(', ');
    const shown = r.querySelector('.qtySelect, .el-select .el-input__inner');
    if (shown && visible(shown)) return first(shown.tagName === 'INPUT' ? shown.value : shown.innerText);
    return null;
  };
  const opener = label => {
    const g = row(label) && row(label).querySelector('.formgroup');
    return g ? [...g.querySelectorAll('.qtySelect, .el-popover__reference, .el-select, input')].find(visible) || null : null;
  };
  const popupOption = text => [...document.querySelectorAll('.el-popover, .el-popper, .el-select-dropdown')]
    .filter(visible).flatMap(p => [...p.querySelectorAll('button, li, .el-select-dropdown__item')])
    .find(e => visible(e) && first(e.innerText) === text) || null;
  // some choices (Layer Stackup) open a modal that repeats the options and wants a Confirm, and may
  // stack a reminder on top of it; the top one is the one with the highest z-index
  const modal = () => [...document.querySelectorAll('.el-dialog__wrapper, .el-message-box__wrapper')].filter(visible)
    .sort((a, b) => (+getComputedStyle(b).zIndex || 0) - (+getComputedStyle(a).zIndex || 0))[0] || null;
  const modalButton = text => { const m = modal(); return m ? [...m.querySelectorAll('button')]
    .find(b => visible(b) && first(b.innerText) === text) || null : null; };
  const modalCheckbox = text => { const m = modal(); return m ? [...m.querySelectorAll('.el-checkbox')]
    .find(b => visible(b) && first(b.innerText) === text) || null : null; };
  const modalTitle = () => { const m = modal(); const t = m && m.querySelector('.el-dialog__title, .el-message-box__title');
    return t ? words(t.innerText) : null; };
  return {visible, words, first, rowLabel, rows, row, buttons, isOff, option, describe, centre, byText, dialogs,
          toasts, value, opener, popupOption, modal, modalButton, modalCheckbox, modalTitle};
})();
true
"""


# Downloads a file with the page's session (XSRF cookie echoed, fresh `secretkey`), as base64.
FETCH_BYTES = r"""
(async (method, path, form) => {
  const cookie = name => { const hit = document.cookie.split('; ').find(c => c.startsWith(name + '='));
                           return hit ? decodeURIComponent(hit.slice(name.length + 1)) : ''; };
  const headers = {'x-xsrf-token': cookie('XSRF-TOKEN')};
  const secret = await fetch('/api/overseas-core-platform/secret/update', {method: 'POST', credentials: 'include',
    headers: Object.assign({'Content-Type': 'application/json'}, headers),
    body: JSON.stringify({keyId: crypto.randomUUID().replace(/-/g, '')})}).then(r => r.json()).catch(() => null);
  if (secret && secret.data) headers.secretkey = secret.data.keyId;
  const init = {method, credentials: 'include', headers};
  if (form) { init.body = new URLSearchParams(form).toString(); headers['Content-Type'] = 'application/x-www-form-urlencoded'; }
  const r = await fetch(path, init);
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return {status: r.status, b64: btoa(bin)};
})
"""


class Gone(JlcError):
    """An element is not on the page, or no longer: the page redrew it, or a window closed itself."""


class Page:
    """One JLCONE tab, driven with real mouse and keyboard events (the quote page ignores
    synthetic `.click()` on its buttons) and watched through its network traffic."""

    def __init__(self, target: dict):
        self.target = target
        self.cdp = Cdp(target["webSocketDebuggerUrl"])
        self.cdp.send("Network.enable", {"maxPostDataSize": 65536})
        self.cdp.send("Page.enable")
        # The quote page ignores clicks while it believes it is unfocused, which it is whenever
        # JLCONE shows another tab or window; measured 2026-09-23: 0 of 4 clicks took without this.
        self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._seen = 0
        self.answered: list[str] = []  # JLC's questions answered from the spec's "answers", for the report
        self._pending: dict[str, dict] = {}  # requests sent, not yet finished; kept across polls
        self.responses: list[dict] = []  # {url, method, post, body}

    def close(self) -> None:
        self.cdp.__exit__(None, None, None)

    # -- page state
    def js(self, expression: str, timeout: float = 60.0) -> Any:
        self.cdp.evaluate(PAGE_HELPERS)
        return self.cdp.evaluate(expression, timeout=timeout)

    def url(self) -> str:
        return self.cdp.evaluate("location.href")

    def settle(self, seconds: float = 1.0) -> None:
        """Let the page work, collecting its API responses."""
        self.cdp.pump(seconds)
        self._collect()

    def _collect(self) -> None:
        pending = self._pending
        for event in self.cdp.events[self._seen:]:
            params = event.get("params", {})
            if event["method"] == "Network.requestWillBeSent" and "/api/" in params["request"]["url"]:
                pending[params["requestId"]] = {"url": params["request"]["url"], "method": params["request"]["method"],
                                                "post": params["request"].get("postData")}
            elif event["method"] == "Network.loadingFailed":
                pending.pop(params.get("requestId"), None)
            elif event["method"] == "Network.loadingFinished" and params["requestId"] in pending:
                record = pending.pop(params["requestId"])
                try:
                    body = self.cdp.send("Network.getResponseBody", {"requestId": params["requestId"]}, timeout=15)
                    text = body.get("body", "")
                    record["body"] = json.loads(text) if text[:1] in "{[" else text[:500]
                except (JlcError, ValueError):
                    record["body"] = None
                self.responses.append(record)
        self._seen = len(self.cdp.events)

    def last_response(self, url_part: str) -> dict | None:
        self._collect()
        return next((r for r in reversed(self.responses) if url_part in r["url"]), None)

    def wait_for(self, condition: Callable[[], Any], timeout: float, what: str) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            self.settle(0.5)
        raise JlcError(f"timed out after {timeout:.0f} s waiting for {what}")

    # -- input
    def click_at(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.cdp.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                       "clickCount": 0 if kind == "mouseMoved" else 1})

    def click(self, locator_js: str, what: str, tries: int = 3) -> None:
        """Click the element `locator_js` (an expression using __jlcq) evaluates to. It is found,
        scrolled into view and found again before the press, and the page may redraw or close it in
        between (JLC's stack-up window closes itself once it has picked the default; a run crashed on
        that on 2026-09-24), so a miss is retried and one that lasts raises Gone."""
        locate = f"(() => {{ const el = {locator_js}; return el ? window.__jlcq.centre(el) : null; }})()"
        for _ in range(tries):
            point = self.js(locate)
            if point and point["w"]:
                self.settle(0.3)  # let the scroll land before pressing
                point = self.js(locate)
                if point and point["w"]:
                    self.click_at(point["x"], point["y"])
                    self.settle(0.5)
                    return
            self.settle(0.5)
        raise Gone(f"cannot find {what} on the page")

    def advance(self, button: str, ready: Callable[[], Any], what: str, tries: int = 3, wait: float = 30.0) -> Any:
        """Press a wizard button until the next step shows `ready`. A step's NEXT does nothing while
        the step is still loading (the PCB step's preview), so a press that changed nothing is
        repeated rather than trusted."""
        for attempt in range(tries):
            self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})  # again: a page load came between
            self.click(button, what)
            try:
                return self.wait_for(ready, wait, what)
            except JlcError:
                if attempt == tries - 1:
                    raise
        return None

    def type_text(self, text: str) -> None:
        self.cdp.send("Input.insertText", {"text": text})

    def upload(self, selector: str, path: Path) -> None:
        handle = self.cdp.send("Runtime.evaluate", {"expression": f"document.querySelector({json.dumps(selector)})"})
        object_id = handle.get("result", {}).get("objectId")
        if not object_id:
            raise JlcError(f"no file input {selector} on the page")
        self.cdp.send("DOM.setFileInputFiles", {"files": [str(path.resolve())], "objectId": object_id})

    def screenshot(self, path: Path, full: bool = False) -> Path:
        params: dict[str, Any] = {"format": "png"}
        if full:
            size = self.js("({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})")
            params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": size["w"], "height": size["h"],
                                                            "scale": 1})
        shot = self.cdp.send("Page.captureScreenshot", params, timeout=30)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(shot["data"]))
        return path

    def snapshot(self, path: Path) -> str | None:
        """A screenshot when the tab can give one. A JLCONE tab behind another tab or window may paint
        no frame, and a picture is never worth failing the run for."""
        try:
            self.cdp.send("Page.bringToFront", timeout=10)
            self.settle(2)
            return str(self.screenshot(path))
        except JlcError:
            return None

    def fetch_bytes(self, method: str, path: str, form: dict | None = None) -> bytes:
        """A file the page could download, fetched with the page's own session."""
        result = self.js(f"{FETCH_BYTES}({json.dumps(method)}, {json.dumps(path)}, {json.dumps(form)})")
        if result.get("status") != 200:
            raise JlcError(f"{path.split('?')[0]}: HTTP {result.get('status')}")
        return base64.b64decode(result["b64"])

    # -- quote form
    def rows(self) -> list[dict]:
        return self.js("window.__jlcq.rows().map(window.__jlcq.describe)")

    def choose(self, label: str, option: str, accept: tuple[str, ...] = (), later: dict | None = None,
               wait: float = 15.0, attempts: int = 3, answers: dict | None = None) -> str:
        """Pick `option` in the row `label` and check that it took. Waits for the row to render,
        opens a pop-up list when the row keeps its choices in one (PCB Qty), answers the modals the
        choice raises (see `_answer_modals`), and clicks again if a click was lost. Returns what the
        row shows afterwards."""
        q = f"{json.dumps(label)}, {json.dumps(option)}"
        button = f"window.__jlcq.option({q})"
        shows = lambda: self.js(f"window.__jlcq.value({json.dumps(label)})") or ""
        taken = lambda: option in [s.strip() for s in shows().split(",")]
        deadline = time.monotonic() + wait
        while not self.js(f"!!window.__jlcq.row({json.dumps(label)})"):
            if time.monotonic() > deadline:
                raise JlcError(f"no row '{label}' on the page")
            self.settle(0.5)
        for _ in range(attempts):
            if taken():
                return shows()
            state = self.js(f"(() => {{ const b = {button}; return b ? {{off: window.__jlcq.isOff(b)}} : null; }})()")
            if state is None and self.js(f"!!window.__jlcq.opener({json.dumps(label)})"):
                self.click(f"window.__jlcq.opener({json.dumps(label)})", f"the list in '{label}'")
                self.settle(1)
                if self.js(f"!!window.__jlcq.popupOption({json.dumps(option)})"):
                    self.click(f"window.__jlcq.popupOption({json.dumps(option)})", f"{label}: {option}")
                    self.settle(2)
                    continue
            if state is None:
                offered = self.js(f"window.__jlcq.describe(window.__jlcq.row({json.dumps(label)})).options")
                raise JlcError(f"row '{label}' has no option '{option}' (it offers {offered})")
            if state["off"]:
                raise JlcError(f"'{label}: {option}' is greyed out for this board")
            try:
                self.click(button, f"{label}: {option}")
            except Gone:
                continue  # the row redrew itself; look at it again
            self.settle(2)
            self._answer_modals(label, option, accept, later or {}, answers or {})
        return shows()

    def _answer_modals(self, label: str, option: str, accept: tuple[str, ...], later: dict,
                       answers: dict | None = None) -> None:
        """Answer what the click raised, top modal first. A chooser window gets the spec's option
        picked and Confirm: the one for this row, or the one for a row the spec sets later when the
        window carries that row's name (Specify Stackup: Yes opens "Layer Stackup" at once). A
        reminder gets Yes only if its text contains a phrase from `accept`, the spec's record of JLC
        warnings someone has read and agreed to; a question with a phrase from `answers` gets the
        button named there, and only that one. Anything else stops the run with JLC's own words, the
        window left open for a person."""
        for _ in range(6):
            text = self.js("(() => { const m = window.__jlcq.modal(); "
                           "return m ? window.__jlcq.words(m.innerText).slice(0, 700) : null; })()")
            if not text:
                return
            title = self.js("window.__jlcq.modalTitle()")
            wanted = str(later[title]) if title in later and title != label else option
            chooser = f"window.__jlcq.modalButton({json.dumps(wanted)})"
            if self.js(f"!!{chooser}"):
                if not self.js(f"{chooser}.classList.contains('cur')"):
                    try:
                        self.click(chooser, f"{wanted} in the '{title or label}' window")
                    except Gone:
                        pass  # the window closed or redrew itself; the next look decides
                    self.settle(1.5)
                    continue  # a reminder may now sit on top
                answer = ("Confirm", "OK", "Yes")
            elif named := next((b for phrase, b in (answers or {}).items() if phrase.lower() in text.lower()), None):
                steps = [named] if isinstance(named, str) else list(named)
                for step in steps:
                    self._press_in_modal(step, label, text)
                self.answered.append(f"JLC asked, choosing '{label}: {option}': {text[:160]} -- answered: "
                                     + ", then ".join(steps))
                self.settle(2)
                continue
            elif any(phrase.lower() in text.lower() for phrase in accept):
                answer = ("Yes", "Confirm", "OK")
            else:
                raise JlcError(f"choosing '{label}: {option}', JLC asks: {text}")
            word = next((w for w in answer if self.js(f"!!window.__jlcq.modalButton({json.dumps(w)})")), None)
            if word is None:
                if not self.js("!!window.__jlcq.modal()"):
                    continue  # it closed while we read it
                raise JlcError(f"choosing '{label}: {option}', JLC shows a window with no way to answer: {text}")
            try:
                self.click(f"window.__jlcq.modalButton({json.dumps(word)})", f"{word} in the '{label}' window")
            except Gone:
                pass
            self.settle(2)
        raise JlcError(f"choosing '{label}: {option}' kept raising windows")

    def _press_in_modal(self, step: str, label: str, text: str) -> None:
        """Press `step` in the top window: a tick box with that label (left ticked, and checked to be),
        or else a button with that text."""
        box = f"window.__jlcq.modalCheckbox({json.dumps(step)})"
        button = f"window.__jlcq.modalButton({json.dumps(step)})"
        if self.js(f"!!{box}"):
            for _ in range(3):
                if self.js(f"(() => {{ const b = {box}; return !!b && b.classList.contains('is-checked'); }})()"):
                    return
                self.click(box, f"the box '{step}' in the '{label}' window")
                self.settle(1)
            raise JlcError(f"choosing '{label}', the box '{step}' would not stay ticked: {text}")
        if self.js(f"!!{button}"):
            try:
                self.click(button, f"{step} in the '{label}' window")
            except Gone:
                pass  # the window closed as it was pressed
            return
        raise JlcError(f"choosing '{label}', JLC's window has no button or box '{step}': {text}")


def cart_tabs() -> list[dict]:
    return [t for t in targets() if t.get("type") == "page" and t.get("url", "").startswith("https://cart.jlcpcb.com/")]


def quote_tab(fresh: bool = True, wait: float = 30.0) -> dict:
    """The JLCONE tab for quoting. JLCONE keeps a single quote tab (a second jlcone:// link to the
    quote page only reloads it, wherever it has wandered, e.g. into the assembly wizard), so reuse
    that tab and send it to a blank quote; open one only when there is none."""
    if not devtools_up():
        raise JlcError("JLCONE is not running with the DevTools port; run `jlc-agent start`.")
    existing = cart_tabs()
    if existing:
        target = existing[0]
        if fresh or not target["url"].startswith(QUOTE_URL):
            with Cdp(target["webSocketDebuggerUrl"]) as cdp:
                cdp.send("Page.enable")
                cdp.send("Page.navigate", {"url": QUOTE_URL})
    else:
        open_tab(QUOTE_URL)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for target in cart_tabs():
            if target["url"].startswith(QUOTE_URL):
                with Cdp(target["webSocketDebuggerUrl"]) as cdp:
                    ready = "document.readyState === 'complete'" + (" && !!document.querySelector('#uploadFile')"
                                                                    if fresh else "")
                    if cdp.evaluate(ready):
                        return target
        time.sleep(1)
    raise JlcError("JLCONE did not open the quote page")


def trace(seconds: float, match: str | None = None, out_file: Path | None = None,
          show: Callable[[str], None] = print, open_url: str | None = None) -> dict:
    """Record every API call JLCONE's pages make for `seconds`: the way to learn what any page or button
    sends, by doing it once in the app (or opening a page with `open_url`). Shows a line per call and
    writes the full requests and responses to `out_file` as JSON lines."""
    if not devtools_up():
        raise JlcError("JLCONE is not running with the DevTools port; run `jlc-agent start`.")
    out_file = out_file or TRACES / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.jsonl"
    if open_url:
        open_tab(open_url)
        match = match or open_url.split("?")[0]
        time.sleep(1)
    pages: dict[str, list] = {}
    calls: list[dict] = []
    deadline = time.monotonic() + seconds
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("a", encoding="utf-8") as record:
        while time.monotonic() < deadline:
            for target in targets():  # pages opened while tracing are picked up too
                url = target.get("url", "")
                if (target["id"] not in pages and target.get("type") == "page" and url.startswith("http")
                        and (not match or match in url)):
                    pages[target["id"]] = [Page(target), 0]
            for entry in pages.values():
                page, seen = entry
                try:
                    page.settle(0.25)
                except JlcError:
                    continue
                for call in page.responses[seen:]:
                    path = call["url"].split("/api", 1)[-1]
                    body = call["body"]
                    brief = json.dumps(body, ensure_ascii=False)[:160] if isinstance(body, (dict, list)) else str(body)[:80]
                    show(f"{call['method']} {path}" + (f"\n    sent: {call['post'][:300]}" if call.get("post") else "")
                         + f"\n    got:  {brief}")
                    record.write(json.dumps(dict(call, time=datetime.datetime.now().isoformat(timespec="seconds")),
                                            ensure_ascii=False) + "\n")
                    calls.append({"method": call["method"], "path": path.split("?")[0], "sent": call.get("post"),
                                  "got": brief})
                entry[1] = len(page.responses)
    for page, _ in pages.values():
        page.close()
    return {"calls": calls, "file": str(out_file)}


# --- the checks, on data alone (pinned by the tests) -----------------------------------------

# "remark": typed into the quote page's PCB Remark (200 characters at most): where edge rails may go.
# "assembly_remark": typed into the order page's Assembly Remark (500 at most): the reflow order, anything for assembly.
SPEC_KEYS = {"board", "gerbers", "bom", "cpl", "pcb", "assembly", "accept", "answers", "note", "remark",
             "assembly_remark",
             # the charge panel's build-time lists: a label as the page shows it ("2-3 days"), or "fastest"
             "pcb_build_time", "assembly_build_time"}


def load_spec(path: Path) -> dict:
    """An order spec: files relative to the spec, then quote-page rows and the option to pick in
    each, in the order the page shows them (some rows only appear after an earlier choice)."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise JlcError(f"{path}: unknown keys {sorted(unknown)} (allowed: {sorted(SPEC_KEYS)})")
    if not spec.get("gerbers"):
        raise JlcError(f"{path}: 'gerbers' is required")
    if spec.get("assembly") is not None and not (spec.get("bom") and spec.get("cpl")):
        raise JlcError(f"{path}: an assembly order needs 'bom' and 'cpl'")
    if len(spec.get("remark") or "") > REMARK_MAX:
        raise JlcError(f"{path}: the remark is {len(spec['remark'])} characters; JLC's PCB Remark takes {REMARK_MAX}")
    if len(spec.get("assembly_remark") or "") > ASSEMBLY_REMARK_MAX:
        raise JlcError(f"{path}: the assembly remark is {len(spec['assembly_remark'])} characters; JLC's Assembly "
                          f"Remark takes {ASSEMBLY_REMARK_MAX}")
    if spec.get("assembly_remark") and spec.get("assembly") is None:
        raise JlcError(f"{path}: an assembly remark needs an assembly order")
    answers = spec.get("answers") or {}
    steps_ok = lambda v: (isinstance(v, str) and v) or (isinstance(v, list) and v and all(isinstance(x, str) and x
                                                                                          for x in v))
    if not isinstance(answers, dict) or not all(isinstance(k, str) and k and steps_ok(v) for k, v in answers.items()):
        raise JlcError(f"{path}: 'answers' maps a phrase of JLC's question to what to press in its window "
                          "(a button's or a tick box's text, or a list of them)")
    files = {}
    for key in ("gerbers", "bom", "cpl"):
        if spec.get(key):
            file = (path.parent / spec[key]).resolve()
            if not file.is_file():
                raise JlcError(f"{path}: {key} file {file} does not exist")
            files[key] = file
    return dict(spec, files=files)


def excellon_tools(text: str) -> list[tuple[float, bool]]:
    """(diameter in mm, is a via) for each tool in an Excellon header. KiCad marks vias with a
    `; #@! TA.AperFunction,Plated,PTH,ViaDrill` line before the tool."""
    inch, function, tools = False, "", []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("; #@! TA.AperFunction"):
            function = line
            continue
        if line.startswith("INCH"):
            inch = True
        elif line.startswith("METRIC"):
            inch = False
        match = re.match(r"T\d+(?:F\d+S\d+)?C([\d.]+)", line)
        if match:
            tools.append((round(float(match.group(1)) * (25.4 if inch else 1.0), 4), "ViaDrill" in function))
            function = ""
        if line in ("%", "M95"):
            break
    return tools


def drill_sizes(gerbers: Path) -> dict:
    """Plated hole diameters in the Gerber zip's drill files, vias apart when the CAD marked them."""
    vias, plated = set(), set()
    with zipfile.ZipFile(gerbers) as archive:
        for name in archive.namelist():
            if not re.search(r"\.(drl|xln|exc|txt)$", name, re.I) or re.search(r"npth|non.?plated", name, re.I):
                continue
            text = archive.read(name).decode("utf-8", "replace")
            if "M48" not in text or "NonPlated" in text:
                continue
            for size, via in excellon_tools(text):
                plated.add(size)
                if via:
                    vias.add(size)
    return {"via": sorted(vias), "plated": sorted(plated)}


def via_check(drills: dict, chosen: str | None) -> str | None:
    """A problem when the board drills a smaller plated hole than the quote's via option allows (a board
    with 0.2 mm vias quoted at JLC's 0.3 mm default is the mistake this catches). Every plated hole counts,
    not only the ones KiCad marks as vias: JLC prices the smallest plated hole, and a module footprint's
    thermal-pad holes are component holes in KiCad's drill file (twelve 0.2 mm ones once passed a check
    that looked at vias alone)."""
    holes = sorted(set(drills["via"]) | set(drills["plated"]))
    match = re.match(r"\s*([\d.]+)\s*mm", chosen or "")
    if not holes or not match:
        return None
    if holes[0] < float(match.group(1)) - 1e-6:
        kind = "vias" if holes[0] in drills["via"] else "plated component holes"
        return f"the board has {holes[0]:g} mm {kind}, but the quote's '{VIA_ROW}' is {chosen}"
    return None


def bom_summary(result: dict) -> dict:
    """JLC's match of the BOM (bomCpl/analyze/result): what cannot be built as sent, and what costs."""
    rows = result.get("componentList") or []
    problems, notes = [], []
    for row in rows:
        where = row.get("topDesignators") or row.get("bottomDesignators") or "?"
        code = row.get("componentCode") or "(no part)"
        if row.get("matchStatus") != 1:
            problems.append(f"{where}: no JLC part matched ({row.get('matchFailReason') or 'no reason given'})")
        if (row.get("shortStockNum") or 0) > 0:
            problems.append(f"{code} ({where}): JLC is short by {row['shortStockNum']}")
        elif row.get("stockStatus") not in (None, "inStock"):
            problems.append(f"{code} ({where}): stock status '{row['stockStatus']}'")
        if row.get("needAuditFlag"):
            notes.append(f"{code} ({where}): JLC reviews this part by hand before building")
        if row.get("orderLimitHint"):
            notes.append(f"{code} ({where}): {row['orderLimitHint']}")
    extended = [r for r in rows if r.get("libType") == "expand"]

    def label(r: dict) -> str:
        return f"{r.get('componentCode')} ({r.get('topDesignators') or r.get('bottomDesignators')})"

    return {"lines": result.get("bomTotal", len(rows)),
            "matched": sum(1 for r in rows if r.get("matchStatus") == 1),
            "short": result.get("shortageNum") or 0, "unmatched": result.get("noneMatchNum") or 0,
            "basic": len(rows) - len(extended), "extended": len(extended),
            "hand_soldered": [label(r) for r in rows if r.get("smtAssembly") == "manualWeld"],
            "moisture_sensitive": [f"{label(r)} {r.get('moistureSensitivityLevelEn')}" for r in rows
                                   if (r.get("moistureSensitivityLevelEn") or "MSL 1") != "MSL 1"],
            "problems": problems, "notes": notes}


def reconcile(merge: dict, cpl_rows: list[dict]) -> dict:
    """JLC's imported placements (the wizard's bom_merge.json) against the CPL that was sent: every
    part placed where and how the CPL says, mounted, and not moved by JLC's own correction (`ec`)."""
    info = (merge.get("designator_info") or [{}])[0]
    placed = ([dict(e, side="Top") for e in info.get("top") or []]
              + [dict(e, side="Bottom") for e in info.get("bot") or info.get("bottom") or []])
    ours = {row["Designator"].strip(): row for row in cpl_rows if (row.get("Designator") or "").strip()}
    problems, notes = [], []
    for entry in placed:
        des, origin = entry.get("des"), entry.get("oc") or {}
        row = ours.get(des)
        if row is None:
            problems.append(f"{des}: JLC places it, but the CPL has no such designator")
            continue
        if not entry.get("is_mount"):
            problems.append(f"{des}: JLC will not mount it")
        if entry.get("ec"):
            problems.append(f"{des}: JLC moved or turned it by its own correction {entry['ec']}")
        try:
            dx = abs(float(origin["x"]) - float(row["Mid X"]))
            dy = abs(float(origin["y"]) - float(row["Mid Y"]))
            da = abs((float(origin["ang"]) - float(row["Rotation"]) + 180) % 360 - 180)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{des}: cannot compare JLC's {origin} with the CPL row")
            continue
        if dx > 0.01 or dy > 0.01 or da > 0.01:
            problems.append(f"{des}: JLC has ({origin['x']}, {origin['y']}) at {origin['ang']} deg, "
                            f"the CPL ({row['Mid X']}, {row['Mid Y']}) at {row['Rotation']} deg")
        side = (row.get("Layer") or "").strip()[:1].upper()
        if side in ("T", "B") and side != entry["side"][0]:
            problems.append(f"{des}: the CPL has it on the {row['Layer']} side, JLC on the {entry['side']}")
    for des in sorted(set(ours) - {e.get("des") for e in placed}):
        notes.append(f"{des}: in the CPL but JLC places nothing there (not in the BOM?)")
    return {"placed": len(placed), "top": sum(1 for e in placed if e["side"] == "Top"),
            "bottom": sum(1 for e in placed if e["side"] == "Bottom"),
            "corrected_by_jlc": sum(1 for e in placed if e.get("ec")), "problems": problems, "notes": notes}


def charges(text: str) -> dict:
    """The charge panel as {label: dollars}, with the total and the weight when shown."""
    flat = re.sub(r"\s+", " ", text or "").replace("Charge Details", " ")
    items: dict[str, float] = {}
    for match in re.finditer(r"([A-Za-z][A-Za-z0-9 ()&/.,:+-]*?)\s*:?\s*\$\s?([\d,]+(?:\.\d+)?)", flat):
        items[match.group(1).strip(" :")] = float(match.group(2).replace(",", ""))
    total = next((items[k] for k in ("Total Price", "Calculated Price") if k in items), None)
    weight = re.search(r"Weight\s*:?\s*([\d.]+)\s*(kg|g)\b", flat)
    grams = None if not weight else float(weight.group(1)) * (1000 if weight.group(2) == "kg" else 1)
    return {"items": items, "total": total, "weight_g": grams}


# --- the run --------------------------------------------------------------------------------

NEXT_BUTTON = ("[...document.querySelectorAll('button')].find(b => window.__jlcq.visible(b) "
               "&& window.__jlcq.words(b.innerText) === 'NEXT')")
ASSEMBLY_SWITCH = "[...document.querySelectorAll('.switch-box')].find(b => /PCB Assembly/.test(b.innerText))"
PROCESS_BUTTON = ("[...document.querySelectorAll('button')].find(b => window.__jlcq.visible(b) "
                  "&& window.__jlcq.words(b.innerText) === 'Process BOM & CPL')")
BOTTOM_TOGGLE = ("[...document.querySelectorAll('div, span, button')].filter(e => window.__jlcq.visible(e) "
                 "&& e.children.length === 0 && e.textContent.trim() === 'Bottom')[0]")
DETECTED = (r"(() => { const m = document.body.innerText.match(/Detected (\d+) layer board of ([\d.]+)\s*[x×*]\s*"
            r"([\d.]+)\s*mm/); return m ? {layers: +m[1], length_mm: +m[2], width_mm: +m[3]} : null; })()")
CHARGE_PANEL = (r"(() => { const boxes = [...document.querySelectorAll('div')].filter(d => window.__jlcq.visible(d) "
                r"&& /Charge Details/.test(d.innerText) && /(Calculated Price|Total Price)/.test(d.innerText)); "
                r"boxes.sort((a, b) => a.innerText.length - b.innerText.length); "
                r"return boxes.length ? boxes[0].innerText : ''; })()")
BOM_FILE_INPUTS = "input[type=file][accept='.xls,.xlsx,.csv']"
# The build-time choices (read 2026-09-29). They are not option rows, so the spec names them apart (pcb_build_time,
# assembly_build_time), and they come in two shapes:
# - the quote page's charge panel, under "PCB Build Time": an <li> per choice with a round icon, "on" when chosen. The
#   click has to land on the icon: one on the middle of the row does nothing.
# - the order page, under "Build Time:": a .package-date per product titled "PCB:" or "Assembly:", a .date-wrapper per
#   choice. Assembly's are Element radios (is-checked when chosen); PCB's is one bare line, the time fixed on the
#   quote page.
# The lists depend on the board: PCB 2-3 to 8-9 days and assembly 2-3 or 3-4 days for a six-layer board (and PCB
# 8-9 days alone once Impedance Control is set), 24 hours and "24 hours PCBA Only" for a two-layer board.
BUILD_TIME_WALK = r"""
  const groups = {};
  const words = window.__jlcq.words;
  const add = (el, label, price, on) => (groups[current] = groups[current] || []).push({el, label: words(label),
                                                                                       price: words(price), on});
  let current = null;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === 3) {
      const t = n.textContent.replace(/\s+/g, ' ').trim();
      if (/^(PCB Build Time|PCB\s*:?)$/i.test(t)) current = 'pcb';
      else if (/^Assembly\s*:?$/i.test(t)) current = 'assembly';
      continue;
    }
    if (!current) continue;
    if (n.tagName === 'LI' && n.querySelector('.choose-active-date-icon') && window.__jlcq.visible(n)) {
      const icon = n.querySelector('.choose-active-date-icon');
      add(icon, (n.querySelector('.inline-block') || n).innerText,
          (n.querySelector('.pull-right') || {innerText: ''}).innerText, icon.classList.contains('on'));
    } else if (n.classList.contains('date-wrapper') && window.__jlcq.visible(n)) {
      const radio = n.querySelector('.el-radio');
      add(radio ? radio.querySelector('.el-radio__inner') || radio : n, (n.querySelector('.line-label') || n).innerText,
          (n.querySelector('.line-price') || {innerText: ''}).innerText, radio ? radio.classList.contains('is-checked') : true);
    }
  }
"""
BUILD_TIMES = ("(() => {" + BUILD_TIME_WALK + " const out = {}; for (const k in groups) out[k] = groups[k].map("
               "o => ({label: o.label, price: o.price, on: o.on})); return out; })()")
# What to click for a choice: the quote page's icon, the order page's radio
BUILD_TIME_OPTION = ("((which, label) => {" + BUILD_TIME_WALK + " const norm = s => s.replace(/\\s+/g, '').toLowerCase();"
                     " const hit = (groups[which] || []).find(o => norm(o.label) === norm(label));"
                     " return hit ? hit.el : null; })")
PRICE_BUSY = ("[...document.querySelectorAll('img')].filter(i => /loading/.test(i.src) "
              "&& window.__jlcq.visible(i)).length")
REMARK_MAX = 200
REMARK_BOX = "document.querySelector('textarea[name=orderDetailsRemark]')"
REMARK_PENCIL = ("[...document.querySelectorAll('div.pad030')].find(d => /PCB Remark/.test(d.textContent))"
                 ".querySelector('img.cursor')")
# The order page's "Remark" (above Save to Cart) opens an Assembly Remark of 500 characters and repeats the PCB Remark
ASSEMBLY_REMARK_MAX = 500
ASSEMBLY_REMARK_BOX = "document.querySelector('.pcba-remark-section textarea')"
ASSEMBLY_REMARK_OPENER = ("[...document.querySelectorAll('div.cursor-pointer')].find(d => window.__jlcq.visible(d) "
                          "&& window.__jlcq.words(d.innerText) === 'Remark')")
# When the walk finds no list, the smallest visible block naming a build time, kept in the report to fix the walk by
BUILD_TIME_MARKUP = (r"(() => { const boxes = [...document.querySelectorAll('div, ul')].filter(d => "
                     r"window.__jlcq.visible(d) && /Build Time/.test(d.innerText)); "
                     r"boxes.sort((a, b) => a.innerText.length - b.innerText.length); "
                     r"return boxes.length ? boxes[0].outerHTML.slice(0, 8000) : null; })()")
# The placement step's merge-file id as the wizard holds it: its Vuex store and components' data.
MERGE_ID_IN_STATE = r"""(() => {
  const seen = new WeakSet();
  function walk(o, depth) {
    if (!o || typeof o !== 'object' || seen.has(o) || depth > 7) return null;
    seen.add(o);
    for (const k of Object.keys(o)) {
      let v; try { v = o[k]; } catch (e) { continue; }
      if (k === 'bomMergeFileId' && v) return String(v);
      if (v && typeof v === 'object' && !(v instanceof Node)) { const f = walk(v, depth + 1); if (f) return f; }
    }
    return null;
  }
  try { const f = walk(window.$nuxt.$store.state, 0); if (f) return f; } catch (e) {}
  for (const el of document.querySelectorAll('*')) {
    if (el.__vue__) { try { const f = walk(el.__vue__.$data, 0); if (f) return f; } catch (e) {} }
  }
  return null;
})()"""


def _merge_file_id(page: Page) -> str | None:
    """The id of JLC's imported-placements file, however this run sees it. Until 2026-09-27 the wizard's
    getDfmResultFile/V2 response carried it and reached the page's network log. On 2026-09-28 the page
    still made that call, then downloaded the merge file, but the response never reached the log, so the run
    waited out its 90 s, pressed NEXT again and skipped past the placement step. Read the page's own download
    request and its state as well."""
    data = ((page.last_response("getDfmResultFile/V2") or {}).get("body") or {})
    data = data.get("data") if isinstance(data, dict) else None
    if isinstance(data, dict) and data.get("bomMergeFileId"):
        return str(data["bomMergeFileId"])
    for event in reversed(page.cdp.events):
        params = event.get("params") or {}
        url = (params.get("request") or {}).get("url") or (params.get("response") or {}).get("url") or ""
        found = re.search(r"downloadBomMergeZipFileByAccessId\?fileAccessId=(\w+)", url)
        if found:
            return found.group(1)
    return page.js(MERGE_ID_IN_STATE) or None


def _text_has(page: Page, pattern: str) -> bool:
    return bool(page.js(f"/{pattern}/.test(document.body.innerText)"))


def _apply(page: Page, report: dict, label: str, option: str, log: Callable[[str], None],
           accept: tuple[str, ...] = (), later: dict | None = None, answers: dict | None = None) -> None:
    try:
        shown = page.choose(label, option, accept, later, answers=answers)
    except JlcError as error:
        report["problems"].append(str(error))
        log(f"  {label}: {error}")
        return
    finally:
        for note in page.answered:
            report["notes"].append(note)
            log(f"  {note}")
        page.answered.clear()
    ok = option in [s.strip() for s in shown.split(",")]
    report["options"].append({"row": label, "wanted": option, "shown": shown, "ok": ok})
    if not ok:
        report["problems"].append(f"'{label}' shows '{shown}' after choosing '{option}'")
    log(f"  {label}: {shown}")


def _selected(page: Page) -> dict[str, str]:
    pairs = page.js("window.__jlcq.rows().map(r => window.__jlcq.rowLabel(r))"
                    ".map(l => [l, window.__jlcq.value(l) || ''])")
    return {label: value for label, value in pairs if label}


def _hours(label: str) -> float:
    """A build-time label's shortest time in hours: '2-3 days' is 48, '24 hours' is 24."""
    match = re.search(r"(\d+)\s*(?:-\s*\d+\s*)?(day|hour)", label or "", re.I)
    if not match:
        return float("inf")
    return int(match.group(1)) * (24 if match.group(2).lower() == "day" else 1)


def _dollars(text: str) -> float:
    match = re.search(r"([\d,]+(?:\.\d+)?)", text or "")
    return float(match.group(1).replace(",", "")) if match else 0.0


def _same(a: str | None, b: str | None) -> bool:
    return re.sub(r"\s+", "", a or "").lower() == re.sub(r"\s+", "", b or "").lower()


def _remark(page: Page, report: dict, text: str, log: Callable[[str], None]) -> None:
    """Type the spec's remark into the quote page's PCB Remark, under Advanced Options: a pencil opens a box of 200
    characters (2026-09-29; JLC asks for PCBA and stencil remarks elsewhere). What is in the box is replaced."""
    if len(text) > REMARK_MAX:
        report["problems"].append(f"the remark is {len(text)} characters; JLC's PCB Remark takes {REMARK_MAX}")
        return
    if not page.js(f"!!{REMARK_BOX}"):
        page.click(REMARK_PENCIL, "the PCB Remark pencil")
        page.wait_for(lambda: page.js(f"!!{REMARK_BOX}"), 10, "the PCB Remark box")
    page.click(REMARK_BOX, "the PCB Remark box")
    page.js(f"{REMARK_BOX}.select()")  # so the typing replaces what was there
    page.type_text(text)
    page.settle(1)
    shown = page.js(f"{REMARK_BOX}.value")
    ok = shown == text
    report["options"].append({"row": "PCB Remark", "wanted": text, "shown": shown, "ok": ok})
    if not ok:
        report["problems"].append(f"the PCB Remark box holds {shown!r} after typing the remark")
    log(f"  PCB Remark: {'typed' if ok else 'NOT as typed'} ({len(text)} characters)")


def _assembly_remark(page: Page, report: dict, text: str, log: Callable[[str], None]) -> None:
    """Type the spec's assembly remark into the order page's Assembly Remark (the "Remark" above Save to Cart opens it):
    JLC reads assembly instructions there, not in the PCB Remark. What is in the box is replaced."""
    if not page.js(f"(() => {{ const t = {ASSEMBLY_REMARK_BOX}; return !!t && window.__jlcq.visible(t); }})()"):
        page.click(ASSEMBLY_REMARK_OPENER, "the order page's Remark")
        page.wait_for(lambda: page.js(f"(() => {{ const t = {ASSEMBLY_REMARK_BOX}; "
                                      "return !!t && window.__jlcq.visible(t); })()"), 10, "the Assembly Remark box")
    page.click(ASSEMBLY_REMARK_BOX, "the Assembly Remark box")
    page.js(f"{ASSEMBLY_REMARK_BOX}.select()")
    page.type_text(text)
    page.settle(1)
    shown = page.js(f"{ASSEMBLY_REMARK_BOX}.value")
    ok = shown == text
    report["options"].append({"row": "Assembly Remark", "wanted": text, "shown": shown, "ok": ok})
    if not ok:
        report["problems"].append(f"the Assembly Remark box holds {shown!r} after typing the assembly remark")
    log(f"  Assembly Remark: {'typed' if ok else 'NOT as typed'} ({len(text)} characters)")


def _calm(page: Page, timeout: float = 30.0) -> None:
    """Wait for the price panel to finish recalculating: no spinner, and the same charges twice running."""
    last, steady, deadline = None, 0, time.monotonic() + timeout
    while time.monotonic() < deadline and steady < 2:
        text = page.js(CHARGE_PANEL)
        steady = steady + 1 if text == last and not page.js(PRICE_BUSY) else 0
        last = text
        page.settle(1.0)


def _build_time(page: Page, report: dict, which: str, wanted: str | None, log: Callable[[str], None]) -> None:
    """Record the build-time choices the page offers for `which` ('pcb' or 'assembly') with their prices, and pick
    `wanted`: a label as the page shows it, or 'fastest' (the shortest; the cheaper of two equals). A pick that does
    not show as chosen afterwards is a problem, as for any option row. With nothing wanted, a paid choice already
    made is a problem too: the page remembers a build time picked on an earlier quote (2026-09-29: a paid 2-3 days
    came back by itself once impedance control was off)."""
    options = (page.js(BUILD_TIMES) or {}).get(which) or []
    report[f"{which}_build_times"] = options
    offered = "; ".join(f"{o['label']} {o['price']}" for o in options)
    if not wanted:
        chosen = next((o for o in options if o["on"]), None)
        if chosen:
            log(f"  {which} build time: {chosen['label']} (offered: {offered})")
            if _dollars(chosen["price"]):
                report["problems"].append(f"the page chose the {which} build time {chosen['label']} at "
                                          f"{chosen['price']}, which the spec does not name ({which}_build_time)")
        return
    if not options:
        report["problems"].append(f"{which} build time {wanted!r}: the page offers no build-time choice here")
        report.setdefault("build_time_markup", {})[which] = page.js(BUILD_TIME_MARKUP)
        return
    if wanted == "fastest":
        target = min(options, key=lambda o: (_hours(o["label"]), _dollars(o["price"])))
    else:
        target = next((o for o in options if _same(o["label"], wanted)), None)
    if target is None:
        report["problems"].append(f"{which} build time {wanted!r} is not offered (offered: {offered})")
        return
    if not target["on"]:
        page.click(f"({BUILD_TIME_OPTION})({json.dumps(which)}, {json.dumps(target['label'])})",
                   f"the {which} build time {target['label']}")
        try:
            page.wait_for(lambda: any(o["on"] and _same(o["label"], target["label"])
                                      for o in (page.js(BUILD_TIMES) or {}).get(which) or []),
                          15, f"the {which} build time to show {target['label']}")
        except JlcError:
            pass  # decided below
        _calm(page)
    after = (page.js(BUILD_TIMES) or {}).get(which) or []
    report[f"{which}_build_times"] = after
    shown = next((o["label"] for o in after if o["on"]), None)
    ok = _same(shown, target["label"])
    report["options"].append({"row": f"{'PCB' if which == 'pcb' else 'Assembly'} Build Time", "wanted": wanted,
                              "shown": shown, "ok": ok})
    if not ok:
        report["problems"].append(f"the {which} build time shows {shown!r} after choosing {target['label']!r}")
    log(f"  {which} build time: {shown} {target['price']} (offered: {offered})")


def _wait_for_form(page: Page, layers: int) -> None:
    """JLC redraws the whole form a moment after it announces the board, and a click made during
    that redraw is lost: wait until the form carries the Gerbers' layer count and the price holds."""
    page.wait_for(lambda: page.js("window.__jlcq.value('Layers')") == str(layers), 90,
                  "the form to take the Gerbers' layer count")
    last, steady, deadline = None, 0, time.monotonic() + 60
    while time.monotonic() < deadline and steady < 2:
        total = charges(page.js(CHARGE_PANEL))["total"]
        steady = steady + 1 if total and total == last else 0
        last = total
        page.settle(1.5)


def _set_file(page: Page, index: int, path: Path) -> None:
    handle = page.cdp.send("Runtime.evaluate",
                           {"expression": f"document.querySelectorAll({json.dumps(BOM_FILE_INPUTS)})[{index}]"})
    object_id = handle.get("result", {}).get("objectId")
    if not object_id:
        raise JlcError("the BOM step has no file inputs")
    page.cdp.send("DOM.setFileInputFiles", {"files": [str(path)], "objectId": object_id})


def prepare(spec_path: Path, log: Callable[[str], None] = print) -> dict:
    """Walk JLC's quote pages in JLCONE for the order `spec_path` describes; stop before the cart."""
    spec = load_spec(spec_path)
    files = spec["files"]
    out = REPORTS / f"{spec_path.parent.name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "board": spec.get("board") or spec_path.parent.name, "spec": str(spec_path), "report_dir": str(out),
        "files": {k: {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for k, p in files.items()},
        "options": [], "problems": [], "notes": [], "dialogs": [], "screenshots": [], "stopped_at": None,
        "remark": spec.get("remark")}
    report["drills_mm"] = drill_sizes(files["gerbers"])
    accept = tuple(spec.get("accept") or ())
    answers = dict(spec.get("answers") or {})
    page = Page(quote_tab(fresh=True))
    try:
        log(f"uploading {files['gerbers'].name} to the quote page")
        page.upload("#uploadFile", files["gerbers"])
        report["detected"] = page.wait_for(lambda: page.js(DETECTED), 180, "JLC to read the Gerbers")
        detected = report["detected"]
        log(f"JLC reads {detected['layers']} layers, {detected['length_mm']:g} x {detected['width_mm']:g} mm")
        _wait_for_form(page, detected["layers"])
        items = list((spec.get("pcb") or {}).items())
        for index, (label, option) in enumerate(items):
            _apply(page, report, label, str(option), log, accept, dict(items[index + 1:]), answers)
        page.settle(2)
        report["pcb_options"] = _selected(page)
        for item in report["options"]:  # a later choice can quietly undo an earlier one
            if item["ok"] and item["wanted"] not in report["pcb_options"].get(item["row"], ""):
                report["problems"].append(f"'{item['row']}' went back to '{report['pcb_options'].get(item['row'])}'")
        problem = via_check(report["drills_mm"], report["pcb_options"].get(VIA_ROW))
        if problem:
            report["problems"].append(problem)
        if spec.get("remark"):
            _remark(page, report, spec["remark"], log)
        # the build time last: every option above can change the list (six layers: 2-3 to 8-9 days)
        if spec.get("assembly") is None:
            _build_time(page, report, "pcb", spec.get("pcb_build_time"), log)
        else:
            report["pcb_build_times_before_assembly"] = (page.js(BUILD_TIMES) or {}).get("pcb") or []
        report["pcb_charges"] = charges(page.js(CHARGE_PANEL))
        report["dialogs"] += page.js("window.__jlcq.dialogs()")
        if spec.get("assembly") is None or report["problems"]:
            report["stopped_at"] = "PCB quote" + (" (problems to fix first)" if report["problems"] else "")
            if shot := page.snapshot(out / "quote.png"):
                report["screenshots"].append(shot)
            return report

        if not page.js("!!window.__jlcq.row('PCBA Type')"):
            page.click(ASSEMBLY_SWITCH, "the PCB Assembly switch")
            page.wait_for(lambda: page.js("!!window.__jlcq.row('PCBA Type')"), 30, "the assembly options")
        log("assembly on")
        items = list((spec.get("assembly") or {}).items())
        for index, (label, option) in enumerate(items):
            _apply(page, report, label, str(option), log, accept, dict(items[index + 1:]), answers)
        report["assembly_options"] = {k: v for k, v in _selected(page).items() if k not in report["pcb_options"]}
        # with assembly on the PCB list can gain choices ("24 hours PCBA Only" on a two-layer board)
        _build_time(page, report, "pcb", spec.get("pcb_build_time"), log)
        report["pcb_charges"] = charges(page.js(CHARGE_PANEL))
        if report["problems"]:
            report["stopped_at"] = "assembly options (problems to fix first)"
            return report

        # While the price panel recalculates, its loading overlay lies over NEXT and takes the click. Once
        # (2026-09-29) it never finished: three presses over three minutes went into the overlay.
        _calm(page, 60)
        if page.js(PRICE_BUSY):
            report["problems"].append("JLC's price panel is still calculating after 60 s and its overlay covers NEXT: "
                                      "run the quote again")
            report["stopped_at"] = "assembly options (the price panel hung)"
            return report
        page.advance(NEXT_BUTTON, lambda: "/smt-order" in page.url() and _text_has(page, "Bill of Materials"),
                     "JLC's assembly wizard", wait=60)
        page.wait_for(lambda: _text_has(page, "Add BOM File") or _text_has(page, "Change PCB specifications"),
                      60, "the wizard's first step")
        if not _text_has(page, "Add BOM File"):  # the wizard opens on its PCB step
            page.settle(3)  # its NEXT does nothing until the board preview has loaded
            page.advance(NEXT_BUTTON, lambda: _text_has(page, "Add BOM File"), "the BOM step")
        log(f"uploading {files['bom'].name} and {files['cpl'].name}")
        _set_file(page, 0, files["bom"])
        page.settle(2)
        _set_file(page, 1, files["cpl"])
        page.settle(3)
        page.click(PROCESS_BUTTON, "Process BOM & CPL")
        result = page.wait_for(lambda: ((page.last_response("bomCpl/analyze/result") or {}).get("body") or {})
                               .get("data") or (page.js("window.__jlcq.dialogs()") or None), 300,
                               "JLC to match the BOM")
        if not isinstance(result, dict):
            report["dialogs"] += result
            report["problems"].append(f"JLC stopped at the BOM step: {' | '.join(result)}")
            report["stopped_at"] = "Bill of Materials"
            return report
        (out / "bom-analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        report["bom"] = bom_summary(result)
        report["problems"] += report["bom"]["problems"]
        report["notes"] += report["bom"]["notes"]
        bom = report["bom"]
        log(f"BOM: {bom['matched']}/{bom['lines']} matched, {bom['short']} short, {bom['unmatched']} unmatched, "
            f"{bom['extended']} extended")
        if bom["problems"] or bom["short"] or bom["unmatched"]:
            report["stopped_at"] = "Bill of Materials (parts to fix first)"
            return report

        merge_id = page.advance(NEXT_BUTTON, lambda: _merge_file_id(page), "JLC's placement preview", wait=90)
        raw = page.fetch_bytes("GET", "/api/overseas-core-platform/shoppingCart/smtGood/"
                                      f"downloadBomMergeZipFileByAccessId?fileAccessId={merge_id}")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            merge = json.loads(json.loads(archive.read(archive.namelist()[0]))["data"])
        (out / "bom_merge.json").write_text(json.dumps(merge, ensure_ascii=False, indent=1), encoding="utf-8")
        with files["cpl"].open(encoding="utf-8-sig", newline="") as handle:
            report["placements"] = reconcile(merge, list(csv.DictReader(handle)))
        placements = report["placements"]
        report["problems"] += placements["problems"]
        report["notes"] += placements["notes"]
        log(f"placements: {placements['placed']} placed, {len(placements['problems'])} differ from the CPL, "
            f"{placements['corrected_by_jlc']} corrected by JLC")
        page.settle(4)  # let the preview draw; its 3D parts only render while JLCONE shows the tab
        if shot := page.snapshot(out / "placement-top.png"):
            report["screenshots"].append(shot)
        if placements["bottom"] and page.js(f"!!{BOTTOM_TOGGLE}"):
            # the toggle can miss while the 3D preview is still drawing (2026-09-28: a "bottom" shot that
            # showed the top), so press it until the parts list has switched to the bottom side
            page.advance(BOTTOM_TOGGLE, lambda: _text_has(page, "Bottom Designator"), "the Bottom view", wait=15)
            page.settle(3)
            if shot := page.snapshot(out / "placement-bottom.png"):
                report["screenshots"].append(shot)
        if placements["problems"]:
            report["stopped_at"] = "Component Placements (differences to check first)"
            return report

        page.advance(NEXT_BUTTON, lambda: _text_has(page, "Total Price"), "the final price", wait=60)
        page.settle(2)
        _build_time(page, report, "assembly", spec.get("assembly_build_time"), log)
        final_pcb = next((o["label"] for o in (page.js(BUILD_TIMES) or {}).get("pcb") or [] if o["on"]), None)
        chosen_pcb = next((o["label"] for o in report.get("pcb_build_times") or [] if o["on"]), None)
        report["order_page_pcb_build_time"] = final_pcb
        if final_pcb and chosen_pcb and not _same(final_pcb, chosen_pcb):
            report["problems"].append(f"the order page builds the PCB in {final_pcb}, the quote page chose {chosen_pcb}")
        if spec.get("assembly_remark"):
            _assembly_remark(page, report, spec["assembly_remark"], log)
        report["charges"] = charges(page.js(CHARGE_PANEL))
        report["dialogs"] += page.js("window.__jlcq.dialogs()")
        if shot := page.snapshot(out / "quote.png"):
            report["screenshots"].append(shot)
        report["stopped_at"] = "Quote & Order, before Save to Cart"
        log(f"total {report['charges']['total']}")
        return report
    except JlcError as error:
        report["problems"].append(str(error))
        report["stopped_at"] = report["stopped_at"] or "an error (see problems)"
        return report
    except Exception as error:  # a page state nobody foresaw: stop there and keep the report
        report["problems"].append(f"unexpected {type(error).__name__}: {error}")
        report["traceback"] = traceback.format_exc()
        report["stopped_at"] = report["stopped_at"] or "an unexpected error (see problems and traceback)"
        return report
    finally:
        page.close()
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def summary(report: dict) -> str:
    """The report in a few lines, problems first."""
    lines = [f"{report['board']}: prepared in JLCONE up to {report['stopped_at']}. Nothing was saved to the cart."]
    if report["problems"]:
        lines.append("NEEDS ATTENTION:")
        lines += [f"  - {p}" for p in report["problems"]]
    detected = report.get("detected")
    if detected:
        lines.append(f"board: {detected['layers']} layers, {detected['length_mm']:g} x {detected['width_mm']:g} mm "
                     f"as JLC reads it; smallest via {min(report['drills_mm']['via'] or report['drills_mm']['plated'] or [0]):g} mm, "
                     f"quote option {report.get('pcb_options', {}).get(VIA_ROW, '?')}")
    if report["options"]:
        lines.append("set: " + "; ".join(f"{o['row']} = {o['shown']}" for o in report["options"]))
    bom = report.get("bom")
    if bom:
        lines.append(f"parts: {bom['matched']}/{bom['lines']} matched, {bom['short']} short, {bom['unmatched']} unmatched; "
                     f"{bom['basic']} basic, {bom['extended']} extended")
        if bom["hand_soldered"]:
            lines.append(f"hand-soldered: {', '.join(bom['hand_soldered'])}")
        if bom["moisture_sensitive"]:
            lines.append(f"moisture-sensitive: {', '.join(bom['moisture_sensitive'])}")
    placements = report.get("placements")
    if placements:
        lines.append(f"placements: {placements['placed']} ({placements['top']} top, {placements['bottom']} bottom), "
                     f"{len(placements['problems'])} differ from the CPL, {placements['corrected_by_jlc']} corrected by JLC")
    for which in ("pcb", "assembly"):
        times = report.get(f"{which}_build_times") or []
        if times:
            chosen = next((o["label"] for o in times if o["on"]), "?")
            lines.append(f"{which} build time: {chosen} (offered: "
                         + "; ".join(f"{o['label']} {o['price']}" for o in times) + ")")
    money = report.get("charges") or report.get("pcb_charges")
    if money and money.get("total") is not None:
        lines.append(f"price: ${money['total']:.2f}" + (f", {money['weight_g']:g} g" if money.get("weight_g") else ""))
    if report["notes"]:
        lines += [f"note: {n}" for n in report["notes"]]
    if report["dialogs"]:
        lines += [f"JLC said: {d}" for d in report["dialogs"]]
    lines.append(f"report and screenshots: {report['report_dir']}")
    if report["stopped_at"].startswith("Quote & Order"):
        lines.append("The quote is open in JLCONE: choose the product description there, then Save to Cart and pay.")
    return "\n".join(lines)
