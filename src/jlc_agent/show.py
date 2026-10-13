"""Plain-text renderings of what `account` returns, for people. `--json` skips all of this."""

from __future__ import annotations

import json
from typing import Any


def money(value: Any) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else str(value or "?")


def orders(rows: list[dict]) -> str:
    lines = []
    for b in rows:
        waiting = sorted({w for o in b["orders"] for w in o["waiting_for_you"]})
        flags = [f"WAITING FOR YOU: {', '.join(waiting)}" if waiting else "",
                 "supplement due" if b["supplement"] else "", "UNPAID" if b["unpaid"] else "",
                 f"tracking {b['tracking']}" if b["tracking"] else ""]
        lines.append(f"{b['batch']}  {b['created']}  {b['status']}  {b['total']} {b['currency']}  "
                     f"{b['shipping'] or ''}  {'  '.join(f for f in flags if f)}".rstrip())
        for o in b["orders"]:
            lines.append(f"    {o['kind']:<8} {o['code']:<18} {o['status']}")
    return "\n".join(lines) or "no orders"


def order(view: dict) -> str:
    lines = [f"{view['batch']}  {view['status']}, paid {view['paid_at']}, {view['shipping'] or 'shipping not set'}, "
             f"{'tracking ' + view['tracking'] if view['tracking'] else 'no tracking yet'}"]
    if view["supplement"]:
        lines.append(f"  supplement due: {view['supplement_reason'] or 'see the order page'}")
    for o in view["orders"]:
        progress = o.get("progress") or {}
        head = f"  {o['kind']:<5} {o['code']:<16} {o['status']}"
        if progress.get("of"):
            head += f"  {progress['done']}/{progress['of']} steps ({progress['percent']}%)"
        if progress.get("eta"):
            head += f", finish {progress['eta']}"
        if o.get("factory_estimate"):
            head += f", factory estimate {o['factory_estimate']}"
        lines.append(head)
        steps = progress.get("steps") or []
        if not steps and "progress" in o:
            lines.append("        no production steps published yet")
        for step in steps:
            if step["done"]:
                lines.append(f"        done  {step['done']}  {step['step']}")
        pending = [s["step"] for s in steps if not s["done"]]
        if pending:
            lines.append(f"        next  {pending[0]}" + (f" (then {len(pending) - 1} more)" if len(pending) > 1 else ""))
        if progress.get("xray_images"):
            lines.append(f"        {progress['xray_images']} X-ray images attached")
        if o.get("waiting_for_you"):
            lines.append(f"        WAITING FOR YOU: {', '.join(o['waiting_for_you'])}")
    if not any(o.get("waiting_for_you") for o in view["orders"]):
        lines.append("  nothing is waiting for you")
    if view.get("timeline"):
        lines.append("  timeline (newest first):")
        lines += [f"    {t['time']}  {t['text']}" for t in view["timeline"]]
    return "\n".join(lines)


def messages(data: dict) -> str:
    counts = ", ".join(f"{k} {v}" for k, v in (data["unread"] or {}).items() if v)
    lines = [f"unread: {counts or 'none'}"]
    for m in data["messages"]:
        about = f"order {m['order']}" if m["order"] else m["kind"]
        lines.append(f"{m['time']}  {about:<12} {'' if m['read'] else 'unread':<6}  {m['title']}  [{m['id']}]")
    return "\n".join(lines)


def message(m: dict) -> str:
    return f"{m['time']}  {m['order'] or ''}  {m['title']}\n{m['text']}"


def parts(data: dict) -> str:
    lines = [f"{data['total']} found" if data["total"] is not None else "no results"]
    for p in data["parts"]:
        price = p["unit_price"] if p["unit_price"] is not None else "?"
        lines.append(f"{p['code']:<12} {p['library'] or '?':<9} stock {p['stock'] or 0:>9,}  ${price:<8} {p['model']}  "
                     f"{p['package'] or ''}  {p['maker'] or ''}".rstrip())
    return "\n".join(lines)


def track(t: dict) -> str:
    if not t["shipped"]:
        return f"{t['batch']}: not shipped yet ({t['shipping'] or 'carrier not set'})"
    return f"{t['batch']}: {t['shipping']} {t['tracking']}\n" + json.dumps(t["events"], ensure_ascii=False, indent=2)


def photos(p: dict) -> str:
    return "\n".join([f"saved {path}" for path in p["saved"]] + p["notes"]) or "nothing to save"


def confirmations(c: dict) -> str:
    lines = []
    for o in c["orders"]:
        if o["detail"] is None:
            lines.append(f"{o['kind']} {o['order']}: nothing to confirm")
        else:
            lines.append(f"{o['kind']} {o['order']} (status {o['status']}, waiting: {', '.join(o['waiting_for_you']) or 'no'}):"
                         f"\n    {json.dumps(o['detail'], ensure_ascii=False)[:1500]}")
    return "\n".join(lines) or "no orders in this batch"


def bom(b: dict) -> str:
    lines = []
    for o in b["orders"]:
        lines.append(f"{o['code']}: {o['boards']} boards, {o['side']} side, parts {money(o['parts_total'])}"
                     f" ({o['from_your_stock']} lines from your stock, {o['consigned']} consigned)")
        lines += [f"    {(p['designators'] or '')[:28]:<28} {p['code'] or '':<10} {p['library'] or '?':<8} "
                  f"{p['bought'] or 0:>5} x {money(p['unit_price']):>8} = {money(p['total']):>8}  {p['model'] or ''}"
                  for p in o["parts"]]
    return "\n".join(lines) or "no assembly orders in this batch"


def inventory(rows: list[dict]) -> str:
    return "\n".join(f"{r['code']}  {r['model'] or ''}  {r['in_stock'] if r['in_stock'] is not None else ''}"
                     for r in rows) or "none"


def parts_orders(rows: list[dict]) -> str:
    return "\n".join(
        f"{r['batch']}  {r['created']}  {money(r['total'])}  ready for assembly {r['ready_for_assembly'] or '?'}\n"
        + "\n".join(f"    {l['order']}  {l['where']}  {l['paid']}  {money(l['money'])}  {' '.join(l['parts'])}"
                    for l in r["lines"])
        for r in rows) or "none"


def billing(rows: list[dict]) -> str:
    return "\n".join(
        f"{r['date']}  {r['batch']}  {money(r['amount'])} {r['currency']}  {r['status']}"
        + (f"  (payments {', '.join(money(p) for p in r['payments'])})" if len(r["payments"]) > 1 else "")
        for r in rows) or "none"


def coupons(c: dict) -> str:
    return "\n".join(
        ["yours:"] + [f"    {money(x['off'])} off from {money(x['min_order'])}  until {x['expires']}  {x['title']}"
                      for x in c["mine"]]
        + ["JLCONE offers:"] + [f"    {money(x['off'])} off from {money(x['min_order'])}  {x['title']}"
                                for x in c["desktop_offers"]])


def cart(rows: list[dict]) -> str:
    return f"{len(rows)} items in the cart" + ("\n" + json.dumps(rows, ensure_ascii=False)[:2000] if rows else "")


def wallet(w: dict) -> str:
    if not w["jpay_account"] and w["balance"] is None:
        return "no JLC wallet (JPay) opened on this account"
    return f"balance {money(w['balance'])}, {len(w['history'])} wallet movements"


def endpoints(rows: list[dict], total: int, usage: bool = False) -> str:
    def line(r: dict) -> str:
        return (f"{r['method'] or '?':4} {r['kind']:7} {r['path']}"
                + "".join(f"\n       called as: {u}" for u in (r.get("usage") or [] if usage else [])))
    return "\n".join(line(r) for r in rows) + f"\n{len(rows)} of {total} endpoints"


def raw(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
