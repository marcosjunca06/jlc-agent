"""jlc-agent: JLCPCB from the command line, through JLC's own desktop app (JLCONE).

    jlc-agent start [--restart]         open JLCONE with its DevTools port (log in there once)
    jlc-agent status                    app, port, login
    jlc-agent orders                    recent order batches, flagging anything JLC waits on
    jlc-agent order <batch>             one batch: every order's production steps, ETA and timeline
    jlc-agent messages [--unread]       JLC's messages, listed without opening (so none turns read)
    jlc-agent parts <keyword>           parts search: LCSC code, basic/extended, stock, price
    jlc-agent track|photos|confirmations|bom|invoice <batch>
    jlc-agent billing|parts-orders|inventory|coupons|cart|wallet
    jlc-agent watch [--notify]          what changed since the last run
    jlc-agent alerts on|off             Windows: `watch --notify` every 15 minutes, JLCONE at login
    jlc-agent open <batch or URL>       show it in JLCONE
    jlc-agent quote <order.json>        prepare an order in the quote pages; stops before the cart
    jlc-agent cpl-check <board> <cpl>   rotations and anchors against JLC's own footprints; corrected CPL
    jlc-agent preflight <order.json>    every check before ordering: files, BOM, CPL, spec, stock, DRC
    jlc-agent endpoints smt wip         search every call JLC's own pages make
    jlc-agent api POST <path> [json]    make any of them (reads freely; account changes need --write)
    jlc-agent trace --open <URL>        record what a page sends and gets back
    jlc-agent mcp                       run the MCP server (stdio) for Claude and other agents

Add --json for machine-readable output. Calls that pay, check out, create an order or move money are
refused whatever the flags: paying is done by a person, in JLCONE's own window.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, account, show
from .app import PORT, SITE, JlcError, Session, api_path, open_tab, set_shortcuts, start, status


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    parser = argparse.ArgumentParser(prog="jlc-agent", description=__doc__.split("\n\n")[0],
                                     epilog="Unofficial; not affiliated with JLCPCB.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"jlc-agent {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    add = lambda name, text: commands.add_parser(name, help=text, parents=[common], description=text)

    add("start", "open JLCONE with the DevTools port").add_argument(
        "--restart", action="store_true", help="close a JLCONE that is open without the port first")
    add("shortcuts", "Windows: make JLCONE's own shortcuts open it with the port (on) or not (off)").add_argument(
        "state", choices=["on", "off"])
    add("status", "app, port, login")
    listing = add("orders", "recent order batches")
    listing.add_argument("--limit", type=int, default=10)
    listing.add_argument("--search", help="batch or order number, as the order page's search box takes it")
    add("order", "one batch with every order's production steps").add_argument("batch")
    inbox = add("messages", "JLC's messages, listed without opening them")
    inbox.add_argument("--limit", type=int, default=20)
    inbox.add_argument("--unread", action="store_true")
    add("message", "open one message in full (the site then marks it read)").add_argument(
        "id", help="the id `messages` shows in brackets")
    search = add("parts", "keyword search of JLC's parts library")
    search.add_argument("keyword")
    search.add_argument("--limit", type=int, default=10)
    for name, text in (("track", "the carrier's tracking events for a shipped batch"),
                       ("photos", "save JLC's finished-board photos and X-ray images of a batch"),
                       ("confirmations", "what JLC has put up for you to confirm on a batch (reading confirms nothing)"),
                       ("bom", "the parts JLC bought and placed for a batch's assembly, at the price charged"),
                       ("invoice", "the invoice data of a paid batch")):
        add(name, text).add_argument("batch")
    for name, text in (("inventory", "parts JLC holds for the account"), ("parts-orders", "parts orders"),
                       ("billing", "every payment, supplements included")):
        add(name, text).add_argument("--search", default="")
    add("coupons", "the account's coupons and JLCONE's coupon offers")
    add("cart", "what is in the cart now")
    add("wallet", "JLC wallet balance and history")
    watcher = add("watch", "what changed since the last run")
    watcher.add_argument("--every", type=float, metavar="MINUTES", help="keep watching at this interval")
    watcher.add_argument("--notify", action="store_true", help="also show each change as a desktop notification")
    add("alerts", "Windows: run `watch --notify` every 15 minutes and open JLCONE at login").add_argument(
        "state", choices=["on", "off"])
    add("open", "show a batch or a jlcpcb.com URL in JLCONE").add_argument("what")
    add("quote", "prepare an order in JLCONE's quote pages from a spec; stops before the cart").add_argument(
        "spec", type=Path, help="the order spec JSON (see examples/jlc-order.example.json)")
    fitting = add("cpl-check", "check a CPL's rotations and anchors against JLC's own footprints and write a "
                               "corrected CPL (works offline; needs no JLCONE)")
    fitting.add_argument("board", type=Path, help="the routed board, .kicad_pcb (KiCad 6 or later)")
    fitting.add_argument("cpl", type=Path, help="the CPL you would send: JLC's columns or KiCad's position-file columns")
    fitting.add_argument("--bom", type=Path, help="a JLC BOM with LCSC numbers (otherwise the board's LCSC fields)")
    fitting.add_argument("--footprints", type=Path, action="append", default=[], metavar="DIR",
                         help="a folder of JLC footprints: .kicad_mod files with an 'LCSC Part' property, as "
                              "easyeda2kicad writes them (repeatable)")
    fitting.add_argument("--fetch", action="store_true",
                         help="fetch missing JLC footprints with easyeda2kicad (pip install easyeda2kicad)")
    fitting.add_argument("--refs", help="only these designators, comma-separated")
    fitting.add_argument("--pad-alias", action="append", default=[], metavar="LCSC:JLC=BOARD,...",
                         help="JLC numbers a pad differently from the board's land, e.g. C7529386:8=10,9=12")
    fitting.add_argument("--out", type=Path, help="the corrected CPL (default: <cpl>-corrected.csv beside it)")
    checks = add("preflight", "checks before ordering: the spec and its files, BOM against CPL, placements against "
                              "JLC's footprints, vias, stack-up, finish, sides, rails; optionally stock and DRC")
    checks.add_argument("spec", type=Path, help="the order spec JSON that `quote` takes")
    checks.add_argument("--board", type=Path, help="the routed board, .kicad_pcb (strongly advised)")
    checks.add_argument("--fetch", action="store_true", help="fetch missing JLC footprints with easyeda2kicad")
    checks.add_argument("--footprints", type=Path, action="append", default=[], metavar="DIR",
                        help="a folder of JLC footprints (.kicad_mod with an 'LCSC Part' property; repeatable)")
    checks.add_argument("--pad-alias", action="append", default=[], metavar="LCSC:JLC=BOARD,...",
                        help="JLC numbers a pad differently from the board's land")
    checks.add_argument("--stock", action="store_true", help="read live stock through JLCONE (needs it running)")
    checks.add_argument("--boards", type=int, help="boards to cover in the stock check (default: the spec's PCBA Qty)")
    checks.add_argument("--drc", action="store_true",
                        help="run KiCad's DRC with kicad-cli on a copy of the board, as saved and refilled")
    catalogue = add("endpoints", "search every call JLC's own web apps make")
    catalogue.add_argument("words", nargs="*", help="all must appear in the path, e.g. smt wip")
    catalogue.add_argument("--kind", choices=["read", "write", "blocked"])
    catalogue.add_argument("--refresh", action="store_true", help="re-read JLC's current front-end code first")
    catalogue.add_argument("--usage", action="store_true",
                           help="also show how JLC's own pages call each one (their arguments, minified)")
    raw = add("api", "call any site endpoint (see `endpoints`); prints the raw JSON")
    raw.add_argument("method", type=str.upper, choices=["GET", "POST"])
    raw.add_argument("path", help="the part after /api, e.g. overseas-core-platform/orderCenter/selectPersonBatch")
    raw.add_argument("body", nargs="?", help="JSON body for POST")
    raw.add_argument("--query", action="append", default=[], metavar="KEY=VALUE", help="URL parameter (repeatable)")
    raw.add_argument("--form", action="store_true", help="send the body as a form, as some endpoints want")
    raw.add_argument("--save", type=Path, help="write the response to this file as-is (images, zips, PDFs)")
    raw.add_argument("--write", action="store_true", help="allow a call that changes the account (logged)")
    tracer = add("trace", "record what JLCONE's pages send and get back")
    tracer.add_argument("--seconds", type=float, default=30)
    tracer.add_argument("--match", help="only tabs whose URL contains this")
    tracer.add_argument("--open", dest="open_url", help="open this jlcpcb.com URL in JLCONE and trace it")
    add("mcp", "run the MCP server over stdio")
    return parser


# name -> (reader, text formatter); each reader takes an open Session and the parsed arguments
READERS = {
    "orders": (lambda s, a: account.batches(s, a.limit, a.search), show.orders),
    "order": (lambda s, a: account.order(s, a.batch), show.order),
    "messages": (lambda s, a: account.messages(s, a.limit, a.unread), show.messages),
    "message": (lambda s, a: account.message(s, a.id), show.message),
    "parts": (lambda s, a: account.parts(s, a.keyword, a.limit), show.parts),
    "track": (lambda s, a: account.track(s, a.batch), show.track),
    "photos": (lambda s, a: account.photos(s, a.batch), show.photos),
    "confirmations": (lambda s, a: account.confirmations(s, a.batch), show.confirmations),
    "bom": (lambda s, a: account.bom(s, a.batch), show.bom),
    "invoice": (lambda s, a: account.invoice(s, a.batch), show.raw),
    "inventory": (lambda s, a: account.inventory(s, a.search), show.inventory),
    "parts-orders": (lambda s, a: account.parts_orders(s, a.search), show.parts_orders),
    "billing": (lambda s, a: account.billing(s, a.search), show.billing),
    "coupons": (lambda s, a: account.coupons(s), show.coupons),
    "cart": (lambda s, a: account.cart(s), show.cart),
    "wallet": (lambda s, a: account.wallet(s), show.wallet),
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.stdout:  # None under pythonw, which is how the scheduled alerts run
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def emit(data, text) -> None:
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text(data))

    try:
        if args.command == "start":
            start(restart=args.restart)
            print(f"JLCONE is up with DevTools on 127.0.0.1:{PORT}. Log in there if it asks.")
        elif args.command == "shortcuts":
            changed = set_shortcuts(args.state == "on")
            print("\n".join(changed) if changed else "no JLCONE shortcuts found")
        elif args.command == "status":
            print(json.dumps(status(), indent=2))
        elif args.command == "watch":
            from .watch import watch
            out = (lambda stamp, event: print(json.dumps({"time": stamp, "event": event}, ensure_ascii=False)
                                              if args.json else f"{stamp}  {event}", flush=True))
            watch(args.every, args.notify, out)
        elif args.command == "alerts":
            from .watch import set_alerts
            print("\n".join(set_alerts(args.state == "on")))
        elif args.command == "open":
            url = args.what if args.what.startswith("http") else f"{SITE}/user-center/orderDetails?batchNum={args.what}"
            open_tab(url)
            print(f"opened in JLCONE: {url}")
        elif args.command == "quote":
            from .quote import prepare, summary
            report = prepare(args.spec.resolve(), log=lambda line: print(line, flush=True))
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "\n" + summary(report))
            return 1 if report["problems"] else 0
        elif args.command == "cpl-check":
            from . import orient
            report = orient.check(args.board, args.cpl, args.bom, args.footprints, args.fetch,
                                  [r.strip() for r in args.refs.split(",")] if args.refs else None,
                                  orient.parse_alias(args.pad_alias))
            out = orient.write_cpl(report, args.out or orient.default_out(args.cpl))
            report = dict(orient.public(report), corrected_cpl=str(out))
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
                  orient.summary(report) + f"\ncorrected CPL: {out} (a proposal: JLC's placement preview decides)")
            needs_work = report["corrections"] or any(r["note"] and not r["ok"] for r in report["rows"])
            return 1 if needs_work else 0
        elif args.command == "preflight":
            from . import orient, preflight
            options = dict(board=args.board.resolve() if args.board else None, fetch=args.fetch,
                           footprints=[f.resolve() for f in args.footprints],
                           pad_alias=orient.parse_alias(args.pad_alias), stock_boards=args.boards, drc=args.drc)
            if args.stock:
                with Session() as jlc:
                    report = preflight.run(args.spec, stock_lookup=preflight.jlcone_stock(jlc), **options)
            else:
                report = preflight.run(args.spec, **options)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else preflight.summary(report))
            return 0 if report["pass"] else 1
        elif args.command == "endpoints":
            from . import catalog
            entries = catalog.refresh() if args.refresh else catalog.load()["endpoints"]
            found = catalog.search(entries, args.words, args.kind)
            emit(found, lambda rows: show.endpoints(rows, len(entries), args.usage))
        elif args.command == "trace":
            from .quote import trace
            result = trace(args.seconds, args.match, show=lambda line: print(line, flush=True), open_url=args.open_url)
            print(f"{len(result['calls'])} calls, full record in {result['file']}")
        elif args.command == "mcp":
            from .mcp_server import main as serve
            serve()
        else:
            with Session() as jlc:
                if args.command in READERS:
                    reader, text = READERS[args.command]
                    emit(reader(jlc, args), text)
                else:  # api
                    body = json.loads(args.body) if args.body else None
                    query = dict(item.split("=", 1) for item in args.query) or None
                    if args.save:
                        data, kind = jlc.download(args.method, api_path(args.path), body, query, args.form, args.write)
                        args.save.write_bytes(data)
                        print(f"{len(data)} bytes ({kind}) -> {args.save}")
                    else:
                        print(show.raw(jlc.api(args.method, api_path(args.path), body, query, args.write, args.form)))
    except JlcError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
