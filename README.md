# jlc-agent

JLCPCB from the command line and from AI agents, through JLC's own desktop app.

`jlc-agent` reads your JLCPCB account the way you see it in the app: order batches with every
production step and its finish time, JLC's messages and engineering questions, parts search and
stock, your parts inventory, billing, coupons, tracking, finished-board photos and X-ray images. It
prepares PCB and assembly quotes in JLC's own quote pages and checks what JLC made of your files.

It also checks a board before you order it: every part's rotation and anchor against JLC's own
footprint for its LCSC number, with a corrected CPL, and the order spec against the board and its
files (via size, stack-up, via filling, finish, sides, rails, stale files, stock, DRC). Those checks
read KiCad files directly and need neither JLCONE nor KiCad.

It ships as a command line and as an [MCP](https://modelcontextprotocol.io) server, so Claude and other
agents can do all of this for you.

It never pays, checks out or places an order. Those calls are refused whatever the flags; you finish
orders in the JLCONE window.

> **Unofficial.** Not affiliated with, endorsed by or supported by JLCPCB. It drives JLC's website
> the way the website drives itself, and JLC can change that at any time. Read
> [Risks](#risks) before using it on an account you care about.

## How it works

[JLCONE](https://jlcone.com) is JLC's desktop app. It shows jlcpcb.com, the quote pages and
EasyEDA in tabs that share one login, and it has no API of its own. `jlc-agent start` opens JLCONE with
a Chrome DevTools port bound to `127.0.0.1`. Each request then runs **inside JLCONE's own jlcpcb.com
tab**, so it carries the app's login, XSRF token and desktop identity exactly as the page's own
requests do.

- **No API keys.** It does not use JLC's partner API (the Open API at open.jlcpcb.com), and it never
  sees your password: you log in to JLCONE yourself, once.
- **More than the Open API.** The Open API has no assembly endpoints. The site has order
  progress, messages, parts search, PCBA data, coupons and the quote wizard.
- **Desktop pricing.** Orders are finished in JLCONE, where JLC applies its desktop-only offers
  (cheaper shipping, app coupons).

## Install

1. Install JLCONE from [jlcone.com/download](https://jlcone.com/download) (Windows, macOS, Linux).
2. Install jlc-agent (Python 3.10+). Leave out `[mcp]` if you only want the command line:

   ```
   pip install "jlc-agent[mcp] @ git+https://github.com/marcosjunca06/jlc-agent"
   ```

   Or from a checkout: `pip install -e ".[mcp]"`.
3. Start JLCONE with the port, and log in in its window if it asks:

   ```
   jlc-agent start            # --restart if JLCONE is already open without the port
   jlc-agent status           # installed, port up, logged in?
   ```

On Windows, `jlc-agent shortcuts on` makes JLCONE's own Start-menu and desktop shortcuts open it with
the port, so you do not have to start it from the command line.

## Use it from Claude (MCP)

Claude Code:

```
claude mcp add --scope user jlc -- jlc-agent-mcp
```

Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jlc": { "command": "jlc-agent-mcp" }
  }
}
```

Give the full path to `jlc-agent-mcp` if Claude Desktop cannot find it (`where jlc-agent-mcp` on
Windows, `which jlc-agent-mcp` elsewhere). Any other MCP client runs the same stdio command.

Then ask for things like "what is JLC waiting on me for?", "when will my assembly order finish?",
"find a basic-library 3.3 V LDO in SOT-23 with stock over 10k", or "prepare this board's quote with
0.2 mm vias and a specified stack-up".

| Tool | What it does |
|---|---|
| `jlc_status`, `jlc_start`, `jlc_open` | Check or start JLCONE; show a batch or URL in its window |
| `jlc_orders`, `jlc_order` | Batches with what JLC waits on you for; one batch's production steps, ETA and timeline |
| `jlc_track`, `jlc_confirmations`, `jlc_photos` | Tracking; what awaits your confirmation; finished-board photos and X-rays saved locally |
| `jlc_bom`, `jlc_invoice` | Parts JLC bought and placed, at the price charged; invoice data |
| `jlc_messages`, `jlc_message` | Messages listed without opening them; one opened in full (JLC then marks it read) |
| `jlc_changes`, `jlc_alerts_log` | What changed since the last look; what the desktop alerts recorded |
| `jlc_parts`, `jlc_inventory`, `jlc_parts_orders` | Parts search with basic/extended, stock and price; your stock at JLC; parts orders |
| `jlc_billing`, `jlc_coupons`, `jlc_cart`, `jlc_wallet` | Payments, coupons (desktop offers included), cart, wallet |
| `jlc_quote` | Prepare an order in the quote pages from a spec; stops before the cart |
| `jlc_cpl_check` | Fit every part against JLC's own footprint; write a corrected CPL |
| `jlc_preflight` | Every check before ordering: files, BOM, placements, spec, stock, DRC |
| `jlc_endpoints`, `jlc_api`, `jlc_trace` | Search every call JLC's pages make; make any read; record what a page sends |
| `jlc_api_write` | A call that changes the account, for changes you asked for |

Read tools are annotated read-only. Everything else is not, so your client asks before running it.

## Use it from the command line

```
jlc-agent orders                       recent batches, flagging anything JLC waits on
jlc-agent order W2026...               every order's production steps, ETA and timeline
jlc-agent messages --unread            messages, listed without opening them
jlc-agent message <id>                 one message in full (JLC marks it read)
jlc-agent parts "AMS1117-3.3"          LCSC code, basic/extended, stock, unit price
jlc-agent track|photos|confirmations|bom|invoice W2026...
jlc-agent billing | parts-orders | inventory | coupons | cart | wallet
jlc-agent open W2026...                show it in JLCONE
jlc-agent watch [--every 15] [--notify]
jlc-agent quote path/to/jlc-order.json
jlc-agent cpl-check board.kicad_pcb cpl.csv --bom bom.csv --fetch
jlc-agent preflight path/to/jlc-order.json --board board.kicad_pcb --fetch [--stock] [--drc]
jlc-agent endpoints smt wip [--usage]
jlc-agent api POST overseas-core-platform/orderCenter/selectPersonBatch '{"currentPage":1,"pageRows":5}'
jlc-agent trace --open https://jlcpcb.com/user-center/orders
```

Add `--json` to any command for machine-readable output.

### Quotes

`jlc-agent quote <spec>` uploads your Gerbers to JLC's quote page in JLCONE and sets every option the
spec names. For assembly it also uploads the BOM and CPL. Then it reads back what JLC made of them:

- the options JLC settled on, and the price;
- the part matches, shortages, extended parts, parts soldered by hand and moisture-sensitive parts;
- the imported placements, compared part by part with your CPL, including any correction JLC made on
  its own.

It stops with the reason at the first problem, and stops before Save to Cart in any case. The report,
JLC's raw data and screenshots are saved locally.

A spec sits next to your release files. Rows and options are spelled exactly as the quote page shows
them, in page order:

```json
{
  "board": "example board rev B",
  "gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl-corrected.csv",
  "pcb": {"PCB Qty": "5", "Min via hole size/diameter": "0.3mm/(0.4/0.45mm)",
          "Specify Stackup": "Yes", "Layer Stackup": "JLC06161H-3313",
          "Via Covering": "Epoxy Filled & Capped", "Surface Finish": "ENIG",
          "Material Type": "FR4 TG155"},
  "assembly": {"PCBA Type": "Standard", "Assembly Side": "Top Side", "PCBA Qty": "5"},
  "accept": ["actual thickness of this stackup"]
}
```

Material Type comes last because choosing a stack-up resets it on the page; `jlc-agent preflight`
checks this and the rest of the spec against the board before you quote.

Leave out `assembly` for a bare board. When a choice makes JLC ask a question, the run answers Yes
only if the question contains a phrase listed under `accept`. A question whose answer is not Yes goes
under `answers`: a phrase from it and what to press, in order, a button or a tick box named as the page
shows it. Since October 2026, Confirm Parts Placement: Yes raises a window that confirms the placement
by itself after 72 hours unless you tick its box:

```json
"answers": {"will be automatically confirmed": ["Do not confirm automatically", "Confirm"]}
```

Any other question stops the run with JLC's own words. The run also stops when your drill file has a
smaller plated hole than the via option allows, a quietly defaulted option that costs a board.

Other spec keys:

- `remark` goes into the quote page's PCB Remark (200 characters); `assembly_remark` into the order
  page's Assembly Remark (500), which is where JLC reads assembly instructions.
- `pcb_build_time` and `assembly_build_time` pick the build times by label (`"2-3 days"`) or
  `"fastest"`. Left out, a paid build time the page chose by itself stops the run: the page remembers
  one picked on an earlier quote.

See [examples/jlc-order.example.json](examples/jlc-order.example.json).

### Placements: cpl-check

JLC's machine reads a CPL as an instruction about *its own* footprint for each LCSC part: Rotation turns
JLC's footprint from its zero, Mid X / Mid Y place JLC's anchor. A CPL exported from KiCad carries
KiCad's rotation and anchor, which is right only where the two libraries draw the part the same way.
Where they don't, the part goes down turned or shifted, and nothing in the Gerbers shows it.

```
jlc-agent cpl-check board.kicad_pcb cpl.csv --bom bom.csv --fetch
```

For each placed part with an LCSC number, this fits the board's pads onto the pads of JLC's footprint
for that number, by pad number, over the four right angles. It reports the turn, the fit and how far
the anchor moves, then writes `cpl-corrected.csv` beside the CPL with the corrections applied:

- Bottom-side parts are fitted in JLC's bottom view, the board turned over left-right, which is how
  JLC's placement preview draws them.
- A two-pad resistor, capacitor or inductor half a turn from JLC's footprint is the same placement and
  is left alone; diodes, LEDs and tantalum or electrolytic capacitors are corrected.
- A CPL that is already corrected checks clean, and a re-run never shifts a part twice.

JLC's footprints come from `--footprints` folders (`.kicad_mod` files with an `LCSC Part` property)
or, with `--fetch`, from [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py), run as a separate
program (`pip install easyeda2kicad`; it is AGPL-licensed and not bundled). `--pad-alias` handles a
JLC footprint that numbers a pad differently from your land.

The corrected CPL is a proposal: JLC's placement preview is the authority. `jlc-agent quote` then
compares JLC's imported placements with the CPL it sends, part by part.

### Before you order: preflight

```
jlc-agent preflight path/to/jlc-order.json --board board.kicad_pcb --fetch [--stock] [--drc]
```

Every check here is a mistake that has reached a JLC order before, on boards that passed DRC. Each
check prints PASS, WARN, FAIL or INFO with what it found and why it exists; the exit code is 1 on any
FAIL.

| Check | Catches |
|---|---|
| `quote.files` | the last quote was built from other files (an earlier release's Gerbers) |
| `quote.defaults` | rows JLC's page chose that the spec does not name |
| `bom.match` | BOM and CPL naming different parts, lines without an LCSC number |
| `cpl.board` | a CPL exported from another revision |
| `cpl.fit`, `cpl.bottom` | parts that would go down turned or shifted (see cpl-check); bottom-side polarised parts to walk first in the preview |
| `spec.via` | the board drills a smaller plated hole than the via option allows (component holes count) |
| `spec.stackup` | RF nets with no stack-up or impedance control; a stack-up for another layer count |
| `spec.material` | Material Type not named, or not last (choosing a stack-up resets it) |
| `spec.covering` | vias in pads that get solder, without "Epoxy Filled & Capped" |
| `spec.finish` | no surface finish named; OSP on a two-sided build |
| `spec.sides` | Assembly Side against the CPL's sides; PCBA and PCB quantities |
| `spec.impedance-note` | impedance control with no word on which traces, or a note asking for impedance the order does not buy |
| `spec.rails` | a board under 70 x 70 mm on Standard PCBA without a word on where rails may go |
| `stock.cover` (`--stock`) | live stock against boards x placements + attrition, through JLCONE |
| `drc.*` (`--drc`) | KiCad's DRC through kicad-cli on a copy of the board: errors, dangling copper on RF nets, warnings, and copper pours older than the last edit |

It does not read data sheets, the schematic's intent, decoupling distances, mechanical fit, or JLC's
own manufacturing limits (hole to copper, rings, silkscreen); those still need a person, or KiCad with
JLC's rules loaded.

### Alerts

`jlc-agent watch --notify` compares the account with the last look and shows a desktop notification
for each change. Clicking a notification opens the order in JLCONE on Windows. Changes include:

- a new batch, a status change, or a production step finished;
- a new timeline entry, such as an engineering hold;
- JLC waiting on you, a supplement payment, a tracking number, a new message;
- a parts order moving or its due date slipping, and stock arriving in (or leaving) My Parts.

On Windows, `jlc-agent alerts on` runs it every 15 minutes in Task Scheduler and opens JLCONE with
the port at login. On macOS and Linux, `jlc-agent alerts on` prints the lines to add to cron or
launchd. Every event also goes to `watch.log` in the data folder.

### Everything else: endpoints, api, trace

The named commands cover the everyday reads. For the rest:

- `jlc-agent endpoints` lists every API call JLC's own web apps make, about a thousand of them. It
  builds the list on your machine from JLC's current front-end code, which needs Node.js. With
  `--usage` it adds snippets showing how JLC's pages call each one, which shows their arguments.
- `jlc-agent api` makes any of those calls.
- `jlc-agent trace` records what a page sends and receives when you do something once in the app.

## Safety

Every call is sorted by its path before it is sent, because the site uses POST for reads as well as
writes:

- **read**: runs freely.
- **write** (reply, confirm, claim a coupon, open a message, cancel): needs `--write` on the command
  line or `jlc_api_write` over MCP. Every write is appended with its body to `writes.log` in the data
  folder.
- **blocked** (pay, check out, create an order, withdraw, refund, top up): refused, always.

The rules are in [`src/jlc_agent/app.py`](src/jlc_agent/app.py) (`classify`) and pinned by
[`tests/test_gate.py`](tests/test_gate.py).

## Risks

- **The DevTools port is a key to your logged-in JLC session.** It is bound to `127.0.0.1`, but while
  JLCONE runs with it, any program on your computer can use your JLC account through it. Do not use
  it on a shared machine. Close JLCONE to close the port.
- **Terms of service.** This automates JLC's website through JLC's own app. JLC may not allow that,
  and it can restrict an account. Use it at your own risk, at human pace; it does not scrape or
  poll aggressively, and the alerts check every 15 minutes.
- **It breaks when JLC changes.** Paths and page layouts are JLC's to change without notice. Run
  `jlc-agent endpoints --refresh` when a call that worked starts failing.
- **Logouts.** JLC ends sessions on its own, about a day after login. Every command then says so;
  log in again in JLCONE.
- **The endpoint catalogue evaluates JLC's front-end files in a Node `vm` context** to read their
  module tables. No module is run, the context is locked down, and the files come from jlcpcb.com
  over HTTPS, the same code JLCONE itself runs. Still, Node's `vm` is not a security boundary.

## Configuration

| Variable | Default | |
|---|---|---|
| `JLCONE_PATH` | Windows `%LOCALAPPDATA%\Programs\JLCONE\JLCONE.exe`, macOS `/Applications/JLCONE.app/Contents/MacOS/JLCONE`, Linux `/opt/JLCONE/jlcone` | where JLCONE is |
| `JLC_AGENT_PORT` | `9339` | the DevTools port |
| `JLC_AGENT_HOME` | Windows `%LOCALAPPDATA%\jlc-agent`, macOS `~/Library/Application Support/jlc-agent`, Linux `~/.local/share/jlc-agent` | watch state, logs, photos, quote reports, fetched footprints, the endpoint catalogue |
| `JLC_AGENT_KICAD_CLI` | `kicad-cli` on PATH, then KiCad's usual install places | kicad-cli for `preflight --drc` |

Tested on Windows 11 with JLCONE 1.0.71. The macOS and Linux paths come from JLC's own installers
and have not been tested yet; reports are welcome.

## Development

```
pip install -e ".[mcp]"
python -m unittest discover -s tests -v
```

The tests cover the gate, JLC's time zones, order summaries, change detection, the endpoint
extractor, the quote checks, the placement fit, the KiCad reader, the pre-order checks and the MCP
tool list, all without JLCONE, KiCad or a network. Anything that talks to JLC is exercised by hand
against a real account.

The KiCad reader was checked against KiCad's own pcbnew on real six-layer KiCad 10 boards: pad
positions on both sides at every rotation, pad angles, vias in pads, outline and layer count, all
identical. On boards that went through JLC's placement preview, `cpl-check` reproduces their corrected
CPLs row for row, bottom side included.

## License

MIT. JLCPCB, JLCONE and EasyEDA are trademarks of their respective owners.
