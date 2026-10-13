"""Watching the account: what changed since the last look, as plain sentences and, optionally, as
desktop notifications that open the order in JLCONE.

State lives in the data directory (see `app.DATA`): watch.json is the last snapshot, watch.log every
event ever reported, and watch.down is present while JLC cannot be read, so an outage is reported
once rather than on every run. Besides the order batches and messages it follows the parts side: parts
orders moving and stock arriving in, or leaving, the account's parts library (My Parts).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from . import account
from .app import DATA, SITE, SYSTEM, JlcError, Session, run

LOG_FILE = DATA / "watch.log"
# Batches in these states have nothing left to watch.
SETTLED = {"shipped", "cancelled", "cancel", "delivered", "finished", "completed", "received", "signed"}
# The parts side of a snapshot; None when its pages could not be read this time.
PARTS_KEYS = ("parts_orders", "library")


def parts_side(jlc: Session) -> dict:
    """The parts `watch` follows besides orders: every parts-order line (where it is, how much is bought,
    the due date) and the stock JLC holds for the account. Parts bought through global sourcing only become
    usable once they sit in My Parts, so that stock count is the line that moves on arrival."""
    orders: dict[str, Any] = {}
    for batch in account.parts_orders(jlc, limit=20):
        for line in batch["lines"]:
            orders[str(line["order"] or batch["batch"])] = {
                "batch": batch["batch"], "where": line["where"], "status": line["status"], "paid": line["paid"],
                "ready": batch["ready_for_assembly"], "goods": line["goods"]}
    return {"parts_orders": orders,
            "library": {row["code"]: {"model": row["model"], "in_stock": row["in_stock"]}
                        for row in account.inventory(jlc, limit=100) if row["code"]}}


def snapshot(jlc: Session) -> dict:
    """What `watch` compares between runs: batch states, finished steps, the timeline, message ids,
    parts orders and parts stock."""
    snap: dict[str, Any] = {"batches": {}, "messages": {}}
    for batch in account.batches(jlc, limit=20):
        entry = {"status": batch["status"], "tracking": batch["tracking"], "supplement": batch["supplement"],
                 "unpaid": batch["unpaid"], "steps": {},
                 "waiting": sorted({w for o in batch["orders"] for w in o["waiting_for_you"]})}
        if (batch["status"] or "").lower() not in SETTLED:
            view = account.order(jlc, batch["batch"])
            for item in view["orders"]:
                entry["steps"][item["code"]] = [s["step"] for s in (item.get("progress") or {}).get("steps", [])
                                                if s["done"]]
            entry["timeline"] = [f"{t['time']} {t['text']}" for t in view["timeline"]]
        snap["batches"][batch["batch"]] = entry
    for item in account.messages(jlc, limit=30)["messages"]:
        snap["messages"][item["id"]] = item
    try:
        snap.update(parts_side(jlc))
    except JlcError:
        # not read this time: `check` keeps what it last saw, so a change is reported late, never lost,
        # and a hiccup on the parts pages does not pause the order and message alerts
        snap.update({key: None for key in PARTS_KEYS})
    return snap


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {word}{'es' if word.endswith(('ch', 'sh', 's', 'x')) else 's'}"


def parts_changes(before: dict, after: dict) -> list[str]:
    """Events on the parts side: a parts order moving, a due date slipping, stock arriving in My Parts
    or leaving it. Every field that can move is reported, and the stock count is the line that settles
    whether goods have landed."""
    now_orders, now_library = after.get("parts_orders"), after.get("library")
    was_orders, was_library = before.get("parts_orders"), before.get("library")
    if now_orders is None or now_library is None:  # the parts pages could not be read
        return []
    if was_orders is None or was_library is None:  # a snapshot from before they were watched would report all of it
        return [f"now also watching {_plural(len(now_orders), 'parts order')} and "
                f"{_plural(len(now_library), 'line')} in My Parts"]

    def label(number: str, line: dict) -> str:
        goods = ", ".join(f"{g['wanted']} x {g['model'] or g['code']}" for g in line["goods"]) or "no parts"
        return f"parts order {number} ({goods})"

    events = []
    for number, line in now_orders.items():
        was = was_orders.get(number)
        if was is None:
            events.append(f"{label(number, line)}: new, {line['where']}")
            continue
        if line["where"] != was.get("where"):
            events.append(f"{label(number, line)}: {was.get('where')} -> {line['where']}")
        if line["status"] != was.get("status"):
            events.append(f"{label(number, line)}: stage {was.get('status')} -> {line['status']}")
        if line["paid"] != was.get("paid"):
            events.append(f"{label(number, line)}: payment {was.get('paid')} -> {line['paid']}")
        if line["ready"] and line["ready"] != was.get("ready"):
            events.append(f"{label(number, line)}: ready for assembly {line['ready']}")
        was_goods = {g["code"]: g for g in was.get("goods", [])}
        for good in line["goods"]:
            old = was_goods.get(good["code"])
            if old and good["due"] != old["due"]:
                events.append(f"{label(number, line)}: {good['code']} due {old['due'] or 'unset'} -> "
                              f"{good['due'] or 'unset'}")
            if old and good["bought"] != old["bought"]:
                events.append(f"{label(number, line)}: {good['code']} bought {old['bought']} -> {good['bought']}")
    for number, line in was_orders.items():
        if number not in now_orders:
            events.append(f"{label(number, line)}: no longer listed")
    for code in sorted(set(now_library) | set(was_library)):
        have = (now_library.get(code) or {}).get("in_stock") or 0
        had = (was_library.get(code) or {}).get("in_stock") or 0
        if have != had:
            name = f"{code} {(now_library.get(code) or was_library[code]).get('model') or ''}".strip()
            events.append(f"My Parts: {name} arrived: {had} -> {have} in stock" if have > had
                          else f"My Parts: {name}: {had} -> {have} in stock")
    return events


def changes(before: dict, after: dict) -> list[str]:
    """Plain-language events between two snapshots."""
    events = []
    for number, now in after["batches"].items():
        was = before.get("batches", {}).get(number)
        if was is None:
            events.append(f"{number}: new order batch ({now['status']})")
            continue
        if now["status"] != was["status"]:
            events.append(f"{number}: {was['status']} -> {now['status']}")
        if now["tracking"] and now["tracking"] != was.get("tracking"):
            events.append(f"{number}: shipped, tracking {now['tracking']}")
        for reason in sorted(set(now["waiting"]) - set(was.get("waiting", []))):
            events.append(f"{number}: JLC is waiting for you ({reason})")
        if now["supplement"] and not was.get("supplement"):
            events.append(f"{number}: JLC asks for a supplement payment")
        if now["unpaid"] and not was.get("unpaid"):
            events.append(f"{number}: something in this batch is unpaid")
        for code, steps in now["steps"].items():
            for step in steps:
                if step not in was.get("steps", {}).get(code, []):
                    events.append(f"{number} {code}: {step} done")
        if "timeline" in was:  # a snapshot taken before the batch was watched would replay its history
            for line in now.get("timeline", []):
                if line not in was["timeline"]:
                    events.append(f"{number}: JLC says: {line.split(' ', 2)[-1]}")
    for ident, item in after["messages"].items():
        if ident not in before.get("messages", {}):
            about = f" ({item['order']})" if item["order"] else ""
            events.append(f"new {item['kind']} message{about}: {item['title']}")
    return events + parts_changes(before, after)


def check(name: str = "watch") -> list[str]:
    """One look: compare the account with the last snapshot, save the new one, log and return the
    events. An outage is one event when it starts and one when it ends. Each `name` keeps its own
    snapshot, so an agent asking what changed does not use up the events the alerts would show; only
    the default one writes watch.log."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    state, down = DATA / f"{name}.json", DATA / f"{name}.down"
    was_down = down.exists()
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with Session() as jlc:
            after = snapshot(jlc)
        before = json.loads(state.read_text(encoding="utf-8")) if state.exists() else None
        for key in PARTS_KEYS:  # the parts pages failed this time: carry over what was seen last time
            if after.get(key) is None and before:
                after[key] = before.get(key)
        state.write_text(json.dumps(after, ensure_ascii=False, indent=1), encoding="utf-8")
        down.unlink(missing_ok=True)
        if before is None:
            seen = [_plural(len(after["batches"]), "order batch"), _plural(len(after["messages"]), "message")]
            if all(after.get(key) is not None for key in PARTS_KEYS):
                seen += [_plural(len(after["parts_orders"]), "parts order"),
                         _plural(len(after["library"]), "line") + " in My Parts"]
            events = [f"watching {', '.join(seen)} (first run, nothing to compare yet)"]
        else:
            events = changes(before, after)
        if was_down:
            events.insert(0, "JLC alerts running again")
    except JlcError as error:
        events = [] if was_down else [f"JLC alerts paused, cannot read JLC: {error}"]
        down.write_text(stamp, encoding="utf-8")
    if events and name == "watch":
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.writelines(f"{stamp}  {event}\n" for event in events)
    return events


def recent(limit: int = 50) -> list[str]:
    """The last `limit` events the alerts logged, oldest first."""
    if not LOG_FILE.exists():
        return []
    return LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]


def watch(every_minutes: float | None = None, notify_desktop: bool = False,
          out: Callable[[str, str], None] | None = None) -> None:
    """Report changes since the last run; with `every_minutes`, keep going."""
    while True:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        events = check()
        for event in events:
            if out:
                out(stamp, event)
        if notify_desktop:
            for event in events[:3]:
                notify("JLC", event, link_for(event))
            if len(events) > 3:
                notify("JLC", f"{len(events) - 3} more JLC updates; see {LOG_FILE}", f"{SITE}/user-center/orders")
        if every_minutes is None:
            return
        time.sleep(every_minutes * 60)


def link_for(event: str) -> str:
    """What a click on the notification opens in JLCONE: the batch it names, else messages or orders."""
    batch = re.match(r"(W\d{10,})", event)
    if batch:
        return f"{SITE}/user-center/orderDetails?batchNum={batch.group(1)}"
    return f"{SITE}/user-center/messages" if "message" in event else f"{SITE}/user-center/orders"


def notify(title: str, body: str, url: str | None = None) -> None:
    """A desktop notification. On Windows a click opens `url` in JLCONE through its jlcone:// link;
    macOS and Linux show the text only (terminal-notifier, if installed, adds the click on macOS)."""
    link = "jlcone://open?url=" + urllib.parse.quote(url, safe="") if url else None
    if SYSTEM == "Windows":
        _windows_toast(title, body, link)
    elif SYSTEM == "Darwin":
        if shutil.which("terminal-notifier"):
            run(["terminal-notifier", "-title", title, "-message", body, *(["-open", link] if link else [])])
        else:
            script = "display notification (system attribute \"JLC_BODY\") with title (system attribute \"JLC_TITLE\")"
            run(["osascript", "-e", script], env=dict(os.environ, JLC_TITLE=title, JLC_BODY=body))
    elif shutil.which("notify-send"):
        run(["notify-send", title, body])


def _windows_toast(title: str, body: str, link: str | None) -> None:
    """Shown under PowerShell's name, so no app registration is needed."""
    def escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    launch = f' activationType="protocol" launch="{escape(link)}"' if link else ""
    xml = (f'<toast{launch}><visual><binding template="ToastGeneric"><text>{escape(title)}</text>'
           f'<text>{escape(body)}</text></binding></visual></toast>')
    script = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
              "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
              "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:JLC_TOAST); "
              "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'; "
              "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)"
              ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=dict(os.environ, JLC_TOAST=xml))


ALERT_TASKS = {"jlc-agent alerts": "-m jlc_agent watch --notify", "jlc-agent JLCONE at login": "-m jlc_agent start"}


def set_alerts(on: bool) -> list[str]:
    """Windows: two tasks in this user's Task Scheduler, `watch --notify` every 15 minutes and JLCONE
    opened with the DevTools port at login so the watch has something to read. Elsewhere this returns
    the lines to add to your own scheduler instead of changing anything."""
    python = Path(sys.executable)
    if SYSTEM != "Windows":
        return ["Not automated on this system. Add these to your scheduler (crontab -e, or a launchd agent):",
                f"*/15 * * * *  {python} {ALERT_TASKS['jlc-agent alerts']}",
                f"@reboot       {python} {ALERT_TASKS['jlc-agent JLCONE at login']}"]
    if not on:
        for name in ALERT_TASKS:
            run(["schtasks", "/Delete", "/TN", name, "/F"])
        return [f"removed {name}" for name in ALERT_TASKS]
    pythonw = python.with_name("pythonw.exe")
    script = ("$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
              "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10); "
              "$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
              "-RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650); "
              "$login = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
              "Register-ScheduledTask -Force -TaskName $env:JLC_TASK1 -Settings $settings -Trigger $every "
              "-Description 'Checks JLC through JLCONE and shows a notification for anything new (jlc-agent)' "
              "-Action (New-ScheduledTaskAction -Execute $env:JLC_PY -Argument $env:JLC_ARG1 -WorkingDirectory $env:JLC_DIR) | Out-Null; "
              "Register-ScheduledTask -Force -TaskName $env:JLC_TASK2 -Settings $settings -Trigger $login "
              "-Description 'Opens JLCONE with its DevTools port so the JLC alerts can read it (jlc-agent)' "
              "-Action (New-ScheduledTaskAction -Execute $env:JLC_PY -Argument $env:JLC_ARG2 -WorkingDirectory $env:JLC_DIR) | Out-Null")
    (name1, arg1), (name2, arg2) = ALERT_TASKS.items()
    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, JLC_PY=str(pythonw if pythonw.exists() else python), JLC_DIR=str(DATA),
               JLC_TASK1=name1, JLC_ARG1=arg1, JLC_TASK2=name2, JLC_ARG2=arg2)
    result = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env)
    if result.returncode != 0:
        raise JlcError(f"could not register the tasks: {result.stderr.strip()[:300]}")
    return [f"{name1}: every 15 minutes", f"{name2}: at login", f"events are logged to {LOG_FILE}"]
