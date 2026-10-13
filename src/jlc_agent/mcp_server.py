"""An MCP server over jlc-agent, so Claude and other agents can work with a JLCPCB account.

    jlc-agent mcp        (or: jlc-agent-mcp)

The tools mirror the command line. Reads are annotated read-only; a tool that changes anything is not,
so a client asks before running it. No tool can pay, check out or create an order: the gate in
`app.classify` refuses those calls for every tool, `jlc_api_write` included.
"""

from __future__ import annotations

import contextlib
import functools
import sys
from pathlib import Path
from typing import Any, Callable

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import account, catalog
from . import watch as watching
from .app import PORT, SITE, JlcError, Session, api_path, classify, gate, open_tab, start, status
from .quote import prepare, summary, trace
from . import orient, preflight

INSTRUCTIONS = """\
Works with the user's JLCPCB account through JLCONE, JLC's desktop app, as the user sees it: orders
with production progress, JLC's messages, parts search and stock, billing, coupons, and quotes.

Start with jlc_status. If JLCONE is not running with its DevTools port, call jlc_start; if it is not
logged in, ask the user to log in in the JLCONE window (never ask for their password). JLC sometimes
logs sessions out on its own; the error then says so, and the user logs in again.

Reads are free. Anything that changes the account (opening a message marks it read; jlc_api_write)
is only for changes the user asked for. Nothing can pay, check out or place an order: prepare it with
jlc_quote and the user finishes it in JLCONE, where the desktop-only prices apply.

For anything the named tools do not cover, find the call with jlc_endpoints and make it with jlc_api;
jlc_trace shows exactly what a page sends when the user does something once in the app."""

server = _Server(name="jlc-agent", instructions=INSTRUCTIONS)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def tool(annotations: ToolAnnotations, title: str) -> Callable:
    """Register a function as a tool. Anything it prints is kept off stdout (the protocol), and a
    JlcError reaches the client as a ToolError with its message ("log in again", "use jlc_api_write"),
    which mcp 2.x would otherwise replace with a bare "Error executing tool"."""
    def register(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with contextlib.redirect_stdout(sys.stderr):
                try:
                    return fn(*args, **kwargs)
                except JlcError as error:
                    raise ToolError(str(error)) from None
        return server.tool(name=fn.__name__, title=title, annotations=annotations)(wrapped)
    return register


def _with_session(read: Callable[[Session], Any]) -> Any:
    with Session() as jlc:
        return read(jlc)


# --- the app ---------------------------------------------------------------------------------

@tool(READ, "JLCONE status")
def jlc_status() -> dict:
    """Whether JLCONE (JLC's desktop app) is installed, running with the DevTools port, and logged in.
    Call this first."""
    return status()


@tool(ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
      "Start JLCONE")
def jlc_start(restart: bool = False) -> str:
    """Open JLCONE with its DevTools port, bound to this machine only, so the other tools can reach it.
    If JLCONE is already open without the port this fails; restart=True then closes it and reopens it
    with the port, losing anything half-done in its windows, so ask the user before restarting."""
    start(restart=restart)
    return f"JLCONE is up with DevTools on 127.0.0.1:{PORT}. If it shows a login page, the user logs in there."


@tool(CHANGE, "Show in JLCONE")
def jlc_open(what: str) -> str:
    """Show an order batch (its W... number) or any jlcpcb.com URL in the JLCONE window, for the user."""
    url = what if what.startswith("http") else f"{SITE}/user-center/orderDetails?batchNum={what}"
    open_tab(url)
    return f"opened in JLCONE: {url}"


# --- orders ----------------------------------------------------------------------------------

@tool(READ, "Order batches")
def jlc_orders(limit: int = 10, search: str | None = None) -> list[dict]:
    """Recent order batches, newest first. Each has its batch number (W...), status, total, shipping
    and tracking, flags for unpaid items and supplement payments, and its orders (PCB, PCBA, stencil,
    ...) with their codes, statuses and `waiting_for_you`: what JLC waits on the customer for.
    `search` takes a batch or order number."""
    return _with_session(lambda jlc: account.batches(jlc, limit, search))


@tool(READ, "One order batch")
def jlc_order(batch: str) -> dict:
    """One batch as the order page shows it: every order's production steps with the time each
    finished, progress, ETA, X-ray image count and what JLC waits on, plus the batch timeline in JLC's
    own words (engineering holds show up there, e.g. "is pending. Please check your email")."""
    return _with_session(lambda jlc: account.order(jlc, batch))


@tool(READ, "Shipment tracking")
def jlc_track(batch: str) -> dict:
    """The carrier and tracking events of a shipped batch (or that it has not shipped yet)."""
    return _with_session(lambda jlc: account.track(jlc, batch))


@tool(READ, "Pending confirmations")
def jlc_confirmations(batch: str) -> dict:
    """What JLC has put up for the customer to confirm on each order of a batch (production files,
    placements), as the order page's confirm dialogs show it. Reading it confirms nothing."""
    return _with_session(lambda jlc: account.confirmations(jlc, batch))


@tool(LOCAL, "Save board photos")
def jlc_photos(batch: str) -> dict:
    """Save JLC's finished-board photos and X-ray images of an assembly batch to the local data folder;
    returns the file paths (open them with an image-capable tool to look)."""
    return _with_session(lambda jlc: account.photos(jlc, batch))


@tool(READ, "Assembly parts as charged")
def jlc_bom(batch: str) -> dict:
    """The parts JLC bought and placed for each assembly order in a batch: designators, LCSC code,
    basic or extended, quantity bought, unit price and line total."""
    return _with_session(lambda jlc: account.bom(jlc, batch))


@tool(READ, "Invoice data")
def jlc_invoice(batch: str) -> Any:
    """The commercial invoice data JLC holds for a paid batch."""
    return _with_session(lambda jlc: account.invoice(jlc, batch))


# --- messages and changes --------------------------------------------------------------------

@tool(READ, "Messages")
def jlc_messages(limit: int = 20, unread_only: bool = False) -> dict:
    """JLC's messages to the account (engineering questions, notices), listed without opening them, so
    none is marked read: unread counts, and each message's id, time, order, kind, read flag and title.
    Read one in full with jlc_message."""
    return _with_session(lambda jlc: account.messages(jlc, limit, unread_only))


@tool(ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
      "Read one message")
def jlc_message(message_id: str) -> dict:
    """One message in full: title, time, order and text. Opening it marks it read on JLC, as the website
    does."""
    return _with_session(lambda jlc: account.message(jlc, message_id))


@tool(LOCAL, "What changed")
def jlc_changes() -> list[str]:
    """What changed on the account since this tool last ran, as sentences: new batches, status changes,
    production steps finished, new timeline entries, JLC waiting on the customer, supplement payments,
    tracking numbers, new messages. The first call records a baseline. It keeps its own snapshot, so it
    never hides events from the desktop alerts."""
    return watching.check("mcp-watch") or ["nothing changed since the last look"]


@tool(READ, "Recent alerts")
def jlc_alerts_log(limit: int = 50) -> list[str]:
    """The last events the scheduled desktop alerts recorded (when alerts are on), oldest first."""
    return watching.recent(limit)


# --- parts, money ----------------------------------------------------------------------------

@tool(READ, "Parts search")
def jlc_parts(keyword: str, limit: int = 10) -> dict:
    """Search JLC's assembly parts library by keyword or part number: LCSC code (C...), model, package,
    maker, basic or extended library, stock and unit price. Prefer basic parts: each extended part type
    adds a feeder fee to an assembly order."""
    return _with_session(lambda jlc: account.parts(jlc, keyword, limit))


@tool(READ, "Parts inventory")
def jlc_inventory(search: str = "", limit: int = 50) -> list[dict]:
    """Parts JLC holds in stock for this account (bought earlier), usable in assembly orders."""
    rows = _with_session(lambda jlc: account.inventory(jlc, search, limit))
    return [{k: v for k, v in r.items() if k != "row"} for r in rows]


@tool(READ, "Parts orders")
def jlc_parts_orders(search: str = "", limit: int = 10) -> list[dict]:
    """Parts orders (parts bought into the account's library): each line's parts, quantities, payment,
    and whether they are in stock at JLC or still being bought, with due dates."""
    return _with_session(lambda jlc: account.parts_orders(jlc, search, limit))


@tool(READ, "Billing")
def jlc_billing(search: str = "", limit: int = 20) -> list[dict]:
    """Every payment by batch: amount, currency, status, date, and the separate payments inside a batch
    (supplements)."""
    return _with_session(lambda jlc: account.billing(jlc, search, limit))


@tool(READ, "Coupons")
def jlc_coupons() -> dict:
    """The account's coupons, and the offers in JLCONE's own coupon list (desktop-only ones included)."""
    return _with_session(account.coupons)


@tool(READ, "Cart")
def jlc_cart() -> list[dict]:
    """What is in the cart right now. Worth checking before an order: an old revision's files can sit
    there."""
    return _with_session(account.cart)


@tool(READ, "Wallet")
def jlc_wallet() -> dict:
    """The JLC wallet (JPay) balance and its movements, if the account has one."""
    return _with_session(account.wallet)


# --- quotes ----------------------------------------------------------------------------------

@tool(CHANGE, "Prepare a quote")
def jlc_quote(spec_path: str) -> dict:
    """Prepare an order in JLCONE's quote pages from an order spec (a JSON file next to the release
    files; see examples/jlc-order.example.json in the jlc-agent repository). Uploads the Gerbers (and
    the BOM and CPL for assembly) to JLC, sets every option the spec names, and reads back JLC's price,
    part matches and shortages and its placement preview, stopping with the reason at the first
    problem. It stops before Save to Cart: nothing is ordered or paid, and the user finishes the order
    in JLCONE. Takes a few minutes."""
    lines: list[str] = []
    report = prepare(Path(spec_path).expanduser().resolve(), log=lines.append)
    return {"summary": summary(report), "log": lines, "report": report}


@tool(CHANGE, "Check placements against JLC's footprints")
def jlc_cpl_check(board: str, cpl: str, bom: str | None = None, footprints: list[str] | None = None,
                  fetch: bool = False, refs: list[str] | None = None, pad_alias: list[str] | None = None,
                  out: str | None = None) -> dict:
    """Check a CPL's rotations and anchors against JLC's own footprints and write a corrected CPL beside it
    (or at `out`). For each placed part with an LCSC number (from `bom`, a JLC BOM, or the board's LCSC
    fields), the board's pads are fitted onto the pads of JLC's footprint for that number over the four
    right angles, by pad number; a part whose fit needs a turn or an anchor shift is corrected. Bottom-side
    parts are fitted in JLC's bottom view. `footprints` are folders of .kicad_mod files with an 'LCSC Part'
    property (easyeda2kicad's output); `fetch` runs easyeda2kicad for missing ones (it must be installed).
    Works offline and needs no JLCONE. The corrected CPL is a proposal: JLC's placement preview decides,
    and jlc_quote compares JLC's imported placements with the CPL it sends."""
    base = Path(cpl).expanduser().resolve()
    report = orient.check(Path(board).expanduser().resolve(), base, Path(bom).expanduser().resolve() if bom else None,
                          [Path(f).expanduser().resolve() for f in footprints or []], fetch, refs,
                          orient.parse_alias(pad_alias or []))
    written = orient.write_cpl(report, Path(out).expanduser().resolve() if out else orient.default_out(base))
    report = dict(orient.public(report), corrected_cpl=str(written))
    return {"summary": orient.summary(report), "report": report}


@tool(CHANGE, "Check a board before ordering")
def jlc_preflight(spec_path: str, board: str | None = None, fetch: bool = False, footprints: list[str] | None = None,
                  pad_alias: list[str] | None = None, stock: bool = False, boards: int | None = None,
                  drc: bool = False) -> dict:
    """Run every check before a board is ordered at JLC, from the order spec jlc_quote takes (and the files it
    names) and the routed board (.kicad_pcb, strongly advised): the last quote was built from these files;
    the BOM and CPL name the same parts with LCSC numbers; the CPL is this board's and places every part as
    JLC's own footprint needs; the via option covers the smallest plated hole; stack-up and impedance control
    for RF nets; Material Type named last; vias in pasted pads filled and capped; a surface finish chosen;
    assembly side and quantities; the impedance note; edge rails under 70 mm. `stock` reads live stock
    through JLCONE for `boards` boards; `drc` runs KiCad's DRC with kicad-cli (as saved and refilled).
    Returns one line per check (PASS, WARN, FAIL, INFO) with what it found and why the check exists. A FAIL
    is fixed, or accepted by the user by name, before ordering. Nothing is uploaded."""
    options = dict(board=Path(board).expanduser().resolve() if board else None, fetch=fetch,
                   footprints=[Path(f).expanduser().resolve() for f in footprints or []],
                   pad_alias=orient.parse_alias(pad_alias or []), stock_boards=boards, drc=drc)
    spec = Path(spec_path).expanduser().resolve()
    if stock:
        with Session() as jlc:
            report = preflight.run(spec, stock_lookup=preflight.jlcone_stock(jlc), **options)
    else:
        report = preflight.run(spec, **options)
    return {"summary": preflight.summary(report), "report": report}


# --- everything else -------------------------------------------------------------------------

@tool(READ, "Search JLC's API")
def jlc_endpoints(words: list[str] | None = None, kind: str | None = None, usage: bool = False,
                  limit: int = 50) -> dict:
    """Search the catalogue of every API call JLC's own web pages make (about a thousand) by words that
    must all appear in the path, e.g. ["smt", "wip"]. kind: "read" (jlc_api can call it), "write"
    (jlc_api_write) or "blocked" (payments and ordering, never callable). usage=True adds up to three
    minified snippets of how JLC's pages call each one, which show its arguments. The first call builds
    the catalogue from JLC's current front-end code (needs Node.js; about a minute)."""
    entries = catalog.load(log=lambda line: None)["endpoints"]
    found = catalog.search(entries, words or [], kind)
    shown = [e if usage else {k: v for k, v in e.items() if k != "usage"} for e in found[:limit]]
    return {"matching": len(found), "of": len(entries), "endpoints": shown}


def _call(method: str, path: str, body: Any, query: dict | None, form: bool, write: bool,
          save_to: str | None = None) -> Any:
    path = api_path(path)
    gate(path, write)  # refuse before connecting; the session checks again
    with Session() as jlc:
        if save_to:
            data, kind = jlc.download(method, path, body, query, form, write)
            target = Path(save_to).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return {"saved": str(target), "bytes": len(data), "type": kind}
        return jlc.api(method, path, body, query, write, form)


@tool(READ, "Call a JLC read endpoint")
def jlc_api(method: str, path: str, body: Any = None, query: dict[str, str] | None = None, form: bool = False,
            save_to: str | None = None) -> Any:
    """Call any JLC site endpoint that only reads, through JLCONE's logged-in page (find it with
    jlc_endpoints). path: the part after /api, e.g. /overseas-core-platform/orderCenter/selectPersonBatch.
    method: GET or POST (the site reads with POST too). body: the JSON body. query: URL parameters.
    form: send the body form-encoded, as a few endpoints want. save_to: write a file response (image,
    PDF, zip) to this local path instead of returning JSON. Returns the response's data. Calls that
    change the account are refused here (see jlc_api_write)."""
    if classify(api_path(path)) == "write":
        raise JlcError(f"{path.split('?')[0]} changes the account; if the user asked for that change, "
                       "use jlc_api_write")
    return _call(method.upper(), path, body, query, form, write=False, save_to=save_to)


@tool(WRITE, "Change the JLC account")
def jlc_api_write(method: str, path: str, body: Any = None, query: dict[str, str] | None = None,
                  form: bool = False) -> Any:
    """Call a JLC endpoint that changes the account: answer an engineering question, confirm production
    files, claim a coupon, cancel an unpaid order, and so on. Only for a change the user asked for in
    this conversation, after showing them exactly what will be sent. Every call is logged locally with
    its body. Paying, checking out, creating orders, withdrawals and refunds are refused whatever the
    arguments."""
    return _call(method.upper(), path, body, query, form, write=True)


@tool(CHANGE, "Trace JLCONE traffic")
def jlc_trace(seconds: float = 30, match: str | None = None, open_url: str | None = None) -> dict:
    """Record the API calls JLCONE's pages make for `seconds`: while the user does something once in the
    app, or after opening open_url (a jlcpcb.com URL) in JLCONE. Returns each call's method, path, what
    was sent and a preview of the answer; the full record goes to a local file. The way to learn an
    endpoint's exact arguments before calling it. match limits it to tabs whose URL contains it."""
    return trace(seconds, match, show=lambda line: None, open_url=open_url)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
