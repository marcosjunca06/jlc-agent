"""Checks to run before a board goes to JLC. Each one is a mistake that has reached a JLC order before, and
every board below passed KiCad's DRC: DRC asks whether the copper is connected, not whether the order is
the board you meant.

    jlc-agent preflight <jlc-order.json> --board <board.kicad_pcb> [--fetch] [--stock] [--drc]

It reads the order spec `jlc-agent quote` takes, the files it names (Gerbers, BOM, CPL) and the routed board,
and gives one line per check: PASS, WARN, FAIL or INFO, with what it found and the mistake the check exists
for. Nothing is uploaded and nothing is changed. Three checks reach outside, each only when asked: --fetch
downloads JLC's footprints with easyeda2kicad, --stock reads JLC's live stock through JLCONE, and --drc runs
KiCad's own DRC with kicad-cli on a copy of the board.

What it does not check: footprints against data sheets, the schematic's intent, decoupling distances,
mechanical fit, and JLC's own manufacturing limits (hole to copper, rings, silkscreen). Those need a person,
or KiCad with JLC's rules loaded.
"""

from __future__ import annotations

import collections
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import kicad, orient
from .app import JlcError
from .quote import REPORTS, VIA_ROW, drill_sizes, load_spec, via_check

ATTRITION = 5  # spare parts a BOM line may consume at JLC, on top of boards x placements
STANDARD_PCBA_MIN_MM = 70  # under this both ways, Standard PCBA adds rails to the board
# rows of JLC's quote page that are facts about the board rather than choices
PAGE_FACTS = {"Layers", "Dimensions", "PCB Qty", "Base Material", "Different Design", "Delivery Format", "Product Type"}

LESSON = {
    "files": "A quote built from an earlier release's Gerbers is an order for the wrong board.",
    "bom": "A placement without a BOM line and an LCSC number is a part JLC leaves off.",
    "cpl": "KiCad's rotation and anchor are not JLC's where the two libraries draw a part differently: parts go "
           "down turned or shifted, and nothing in the Gerbers shows it.",
    "spec.via": "JLC's quote page defaults to 0.3 mm vias; a board drilled at 0.2 mm and quoted that way is not "
                "the board you designed.",
    "spec.stackup": "Without a named stack-up and impedance control, nobody controls a 50 ohm feed's width.",
    "spec.material": "Picking a stack-up resets Material Type on JLC's page: identical quote runs came out FR4 "
                     "TG135 and TG155.",
    "spec.covering": "An open via in a pad drains the joint's solder; filling it is a decision, not a default.",
    "spec.finish": "A spec that names no surface finish gets whatever the page picks.",
    "spec.sides": "The CPL's sides and the order's Assembly Side must agree, or a side goes unplaced.",
    "spec.impedance-note": "Impedance control ordered with no word on which traces is a question from JLC's "
                           "engineers; a note asking for impedance the order does not buy is one too.",
    "spec.rails": "Under 70 x 70 mm, Standard PCBA adds rails; a remark JLC cannot follow invites a question.",
    "stock": "Parts run out between design and order; a line JLC cannot fill holds the build.",
    "drc": "DRC warnings are easy to ship: dangling stubs on an RF feed were listed as warnings, and ordered.",
    "drc.pours": "Gerbers carry the copper pours as last filled; a board edited after its last refill ships "
                 "stale copper.",
    "quote": "Every row JLC's page picks by itself is a decision nobody made.",
}


class Checks(list):
    def add(self, cid: str, title: str, status: str, detail: str = "") -> None:
        lesson = LESSON.get(cid) or LESSON.get(cid.split(".")[0], "")
        self.append({"id": cid, "title": title, "status": status, "detail": detail, "lesson": lesson})


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def bom_lines(path: Path) -> list[dict]:
    """A JLC BOM's lines: designators and LCSC number."""
    rows = _rows(path)
    names = list(rows[0]) if rows else []
    ref_col = next((n for n in names if n.strip().lower() == "designator"), None)
    lcsc_col = next((n for n in names if n.strip().lower() in {c.lower() for c in orient.BOM_LCSC_COLUMNS}), None)
    lines = []
    for row in rows:
        refs = [r for r in re.split(r"[,\s]+", (row.get(ref_col) or "") if ref_col else "") if r]
        lines.append({"refs": refs, "lcsc": (row.get(lcsc_col) or "").strip() if lcsc_col else "",
                      "comment": row.get("Comment") or ""})
    return lines


# --- the checks ---------------------------------------------------------------------------------

def check_quote_record(ck: Checks, spec_path: Path, spec: dict) -> None:
    """The last quote run for this spec against the files as they are now, and the rows its page chose."""
    reports = []
    for path in REPORTS.glob("*/report.json") if REPORTS.exists() else []:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if Path(report.get("spec", "")).resolve() == spec_path.resolve():
            reports.append((path.stat().st_mtime, path, report))
    if not reports:
        ck.add("quote.files", "the last quote was built from these files", "INFO",
               "no quote run for this spec yet (jlc-agent quote)")
        return
    _, path, report = max(reports, key=lambda r: r[0])
    changed = [k for k, f in spec["files"].items()
               if (report.get("files") or {}).get(k, {}).get("sha256") != _sha(f)]
    ck.add("quote.files", "the last quote was built from these files", "FAIL" if changed else "PASS",
           (f"{', '.join(changed)} changed since the quote in {path.parent.name}: quote again before ordering"
            if changed else f"{path.parent.name}, stopped at {report.get('stopped_at')}"))
    named = set(spec.get("pcb") or {}) | PAGE_FACTS
    rest = {k: v for k, v in (report.get("pcb_options") or {}).items() if k not in named}
    ck.add("quote.defaults", "rows JLC's page chose that the spec does not name", "INFO",
           "; ".join(f"{k}: {v}" for k, v in rest.items()) or "none")


def check_bom(ck: Checks, spec: dict, cpl: dict[str, dict]) -> list[dict]:
    lines = bom_lines(spec["files"]["bom"])
    refs = {r for line in lines for r in line["refs"]}
    no_lcsc = [",".join(line["refs"]) or line["comment"] or "?" for line in lines
               if not orient.LCSC_NUMBER.match(line["lcsc"])]
    problems = ([f"no LCSC number: {no_lcsc}"] if no_lcsc else []) + \
               ([f"in the BOM, not placed: {sorted(refs - set(cpl), key=orient._natural)}"] if refs - set(cpl) else []) + \
               ([f"placed, not in the BOM: {sorted(set(cpl) - refs, key=orient._natural)}"] if set(cpl) - refs else [])
    ck.add("bom.match", "BOM and CPL name the same parts, each with an LCSC number", "FAIL" if problems else "PASS",
           "; ".join(problems) or f"{len(lines)} lines, {len(refs)} placements")
    return lines


def check_cpl(ck: Checks, spec: dict, board: Path, fetch: bool, footprints: list[Path],
              pad_alias: dict | None) -> dict:
    report = orient.check(board, spec["files"]["cpl"], spec["files"]["bom"], footprints, fetch,
                          pad_alias=pad_alias)
    rows = report["rows"]
    if report["cpl_frame"] is None and rows:
        ck.add("cpl.board", "the CPL is this board's", "FAIL",
               "the CPL's positions do not follow the board's: it was exported from another revision or board")
    elif rows:
        frame = report["cpl_frame"]
        ck.add("cpl.board", "the CPL is this board's", "PASS",
               f"{frame['parts']} positions match the board to {frame['deviation_mm']:g} mm")
    wrong = report["corrections"]
    unfit = [r["ref"] for r in rows if r["residual_mm"] is not None and r["residual_mm"] >= orient.FIT_MM]
    unchecked = report["unchecked"]
    if wrong:
        status, detail = "FAIL", (f"{len(wrong)} parts would go down turned or shifted: {', '.join(wrong)}; "
                                  f"`jlc-agent cpl-check` writes the corrected CPL")
    elif unfit or unchecked:
        status = "WARN"
        detail = "; ".join(([f"pads that fit JLC's part at no right angle: {', '.join(unfit)}"] if unfit else []) +
                           ([f"not checked (no JLC footprint{'' if fetch else '; try --fetch'}): {', '.join(unchecked)}"]
                            if unchecked else []))
    else:
        status, detail = "PASS", f"{report['checked']} parts placed as JLC's own footprints need"
    if report.get("half_turns"):
        detail += (f"; {len(report['half_turns'])} two-pad unpolarised parts sit a half turn from JLC's footprint, "
                   f"the same placement ({', '.join(report['half_turns'][:8])}{' ...' if len(report['half_turns']) > 8 else ''})")
    ck.add("cpl.fit", "every part sits as JLC's own footprint needs", status, detail)
    bottom = [r["ref"] for r in rows if r["side"] == "Bottom" and r["turn_deg"] is not None
              and (r.get("pads_board", 0) >= 3 or re.match(r"(D|LED)\d", r["ref"]))]
    ck.add("cpl.bottom", "bottom-side polarised parts", "WARN" if bottom else "PASS",
           (f"{', '.join(bottom)}: walk these first in JLC's placement preview (it draws the bottom turned over "
            "left-right)") if bottom else "none")
    return report


def check_spec(ck: Checks, spec: dict, facts: dict | None, sides: set[str]) -> None:
    pcb, assembly = spec.get("pcb") or {}, spec.get("assembly")
    drills = drill_sizes(spec["files"]["gerbers"])
    holes = sorted(set(drills["via"]) | set(drills["plated"]))
    smallest = holes[0] if holes else None
    chosen = pcb.get(VIA_ROW)
    if not chosen:
        ck.add("spec.via", "the via option covers the smallest plated hole", "FAIL" if smallest else "PASS",
               f"'{VIA_ROW}' not named; the page defaults to 0.3 mm and the smallest plated hole here is {smallest} mm"
               if smallest else "no plated holes")
    elif via_check(drills, chosen):
        ck.add("spec.via", "the via option covers the smallest plated hole", "FAIL", via_check(drills, chosen))
    else:
        option = float(re.match(r"\s*([\d.]+)", chosen).group(1))
        smaller = bool(smallest) and option < smallest - 1e-6
        ck.add("spec.via", "the via option covers the smallest plated hole", "WARN" if smaller else "PASS",
               f"smallest plated hole {smallest} mm, order {chosen}" +
               (f": the order buys {option:g} mm vias the board does not use (they cost more)" if smaller else ""))

    code = pcb.get("Layer Stackup", "")
    if facts is None:
        ck.add("spec.stackup", "stack-up and impedance control for a controlled feed", "WARN",
               "no --board given: layer count and RF nets unchecked")
    else:
        problems = []
        match = re.match(r"JLC(\d\d)", code)
        layers = facts["copper_layers"]
        if match and int(match.group(1)) != layers:
            problems.append(f"{code} is a {int(match.group(1))}-layer stack-up; the board has {layers} copper layers")
        if facts["rf_nets"]:
            if pcb.get("Specify Stackup") != "Yes" or not code:
                problems.append(f"RF nets {facts['rf_nets']} but no stack-up named, so nobody controls the feed's width")
            impedance = pcb.get("Impedance Control", "")
            if not impedance or impedance.strip().lower().startswith("no"):  # the page's word for none
                problems.append("no impedance control ordered for the RF feed (where JLC offers faster builds only "
                                "without it, trading it for time is a decision to write down)")
        ck.add("spec.stackup", "stack-up and impedance control for a controlled feed", "FAIL" if problems else "PASS",
               "; ".join(problems) or (f"{code}, impedance {pcb.get('Impedance Control')}, {layers} layers" if code
                                       else f"no controlled feed ({layers} layers)"))

    keys = list(pcb)
    if code and "Material Type" not in pcb:
        ck.add("spec.material", "Material Type named, last", "FAIL",
               "not named: choosing a stack-up resets it on the page")
    elif code and keys[-1] != "Material Type":
        ck.add("spec.material", "Material Type named, last", "FAIL",
               f"'{keys[-1]}' comes after it and can reset it: move Material Type to the end of 'pcb'")
    else:
        ck.add("spec.material", "Material Type named, last", "PASS" if "Material Type" in pcb else "INFO",
               pcb.get("Material Type") or "not named (no stack-up named either)")

    if facts is not None:
        pasted = [v for v in facts["vias_in_smd_pads"] if v["pasted"]]
        cover = pcb.get("Via Covering", "")
        by_ref = collections.Counter(v["ref"] for v in pasted).most_common(8)
        where = ", ".join(f"{r} {n}" for r, n in by_ref) + (" ..." if len({v["ref"] for v in pasted}) > 8 else "")
        if pasted and "fill" not in cover.lower():
            ck.add("spec.covering", "vias in pasted pads are filled and capped", "FAIL",
                   f"{len(pasted)} vias sit in pads that get solder ({where}); Via Covering is "
                   f"{cover or 'not named'}: ask for 'Epoxy Filled & Capped'")
        else:
            ck.add("spec.covering", "vias in pasted pads are filled and capped", "PASS",
                   f"{len(pasted)} vias in pasted pads ({where}), Via Covering {cover}" if pasted else
                   f"no via in a pasted pad (Via Covering {cover or 'not named'})")

    finish = pcb.get("Surface Finish")
    if not finish:
        ck.add("spec.finish", "surface finish chosen, not inherited", "FAIL",
               "not named: the page picks it. ENIG is the conservative choice for fine-pitch, LGA or two-sided "
               "builds; name one")
    elif "OSP" in finish.upper() and len(sides) > 1:
        ck.add("spec.finish", "surface finish chosen, not inherited", "WARN",
               f"{finish} on a two-sided build: the second side reflows onto OSP already heated once")
    else:
        ck.add("spec.finish", "surface finish chosen, not inherited", "PASS", finish)

    if assembly is not None:
        want = "Both Sides" if sides == {"Top", "Bottom"} else "Bottom Side" if sides == {"Bottom"} else "Top Side"
        have = assembly.get("Assembly Side")
        problems = [f"the CPL places on {'/'.join(sorted(sides))}, so the order needs '{want}', not {have!r}"] * (have != want)
        try:
            pcb_qty, pcba_qty = int(pcb.get("PCB Qty", 0)), int(assembly.get("PCBA Qty", 0))
        except ValueError:
            pcb_qty, pcba_qty = 0, 0
        if not pcba_qty or pcba_qty > pcb_qty:
            problems.append(f"PCBA Qty {assembly.get('PCBA Qty')!r} against PCB Qty {pcb.get('PCB Qty')!r}")
        ck.add("spec.sides", "assembly side and quantities agree with the CPL", "FAIL" if problems else "PASS",
               "; ".join(problems) or f"{have}, {pcba_qty} assembled of {pcb_qty}")
    if spec.get("accept") or spec.get("answers"):
        ck.add("spec.accept", "JLC's questions answered in advance", "INFO",
               "; ".join(f"{k}: {spec[k]}" for k in ("accept", "answers") if spec.get(k)))

    remark = (spec.get("remark") or "").lower()
    impedance = pcb.get("Impedance Control", "")
    with zipfile.ZipFile(spec["files"]["gerbers"]) as archive:
        notes = [n for n in archive.namelist() if "imped" in n.lower()]
    if impedance and not impedance.strip().lower().startswith("no"):
        said = bool(notes) or "impedance" in remark
        ck.add("spec.impedance-note", "the impedance requirement goes to JLC with the order", "PASS" if said else "FAIL",
               f"{', '.join(notes) or 'no note in the Gerbers zip'}; remark {'names it' if 'impedance' in remark else 'silent'}"
               if said else "impedance control ordered with no word on which traces, what impedance, which "
                            "reference layer: put a note in the Gerbers zip or say it in the spec's remark")
    elif notes or "impedance" in remark:
        ck.add("spec.impedance-note", "no impedance request where the order buys no impedance control", "FAIL",
               f"{', '.join(notes) or 'the remark'} asks JLC for impedance control the order does not buy: a "
               "question that holds the order. Drop the note or order impedance control")

    size = (facts or {}).get("outline_mm") or []
    if assembly is not None and assembly.get("PCBA Type") == "Standard" and size and min(size) < STANDARD_PCBA_MIN_MM:
        both = all(s < STANDARD_PCBA_MIN_MM for s in size[:2])
        limits = both and "rail" in remark and re.search(r"\bonly\b", remark)
        ck.add("spec.rails", "edge rails on a board under JLC's 70 x 70 mm Standard PCBA minimum",
               "WARN" if limits or "rail" not in remark else "PASS",
               f"{size[0]:g} x {size[1]:g} mm: JLC adds rails; " + (
                   "both sides are under 70 mm, so JLC rails all four edges: a remark keeping rails off an edge "
                   "cannot be followed; ask instead for a gap where a part overhangs" if limits else
                   "the remark says where" if "rail" in remark else
                   "say in the remark which edges may take them (not a connector's or an antenna's)"))


def check_stock(ck: Checks, lines: list[dict], boards: int, lookup: Callable[[str], dict | None]) -> None:
    """Live stock for every BOM line against boards x placements + attrition."""
    per_board = collections.Counter()
    for line in lines:
        if orient.LCSC_NUMBER.match(line["lcsc"]):
            per_board[line["lcsc"]] += len(line["refs"])
    rows, missing, extended = [], [], 0
    for code, count in per_board.items():
        found = lookup(code)
        if not found:
            missing.append(code)
            continue
        extended += found.get("library") == "extended"
        need, stock = count * boards + ATTRITION, found.get("stock") or 0
        rows.append((stock / need, code, stock, need, found.get("model") or ""))
    rows.sort()
    short = [r for r in rows if r[0] < 1]
    scarce = ", ".join(f"{c} {model} {stock} for {need}" for _, c, stock, need, model in rows[:3])
    ck.add("stock.cover", f"JLC stock covers {boards} boards now", "FAIL" if short or missing else "PASS",
           "; ".join(([f"short: " + ", ".join(f"{c} {model} has {stock}, needs {need}" for _, c, stock, need, model in short)]
                      if short else []) + ([f"not found at JLC: {missing}"] if missing else []))
           or f"scarcest: {scarce} (public stock; parts you hold in My Parts count only when they cover the order)")
    ck.add("stock.extended", "Extended parts (each a feeder fee)", "INFO", f"{extended} of {len(per_board)} lines")


def kicad_cli() -> str | None:
    """kicad-cli: $JLC_AGENT_KICAD_CLI, then PATH, then KiCad's usual install places."""
    if os.environ.get("JLC_AGENT_KICAD_CLI"):
        return os.environ["JLC_AGENT_KICAD_CLI"]
    found = shutil.which("kicad-cli")
    if found:
        return found
    windows = sorted(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe")) if os.name == "nt" else []
    for path in [*reversed(windows), Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
                 Path("/usr/bin/kicad-cli")]:
        if path.exists():
            return str(path)
    return None


def _drc(cli: str, board: Path, refill: bool) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / board.name
        shutil.copy2(board, copy)
        for sibling in (board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_dru")):
            if sibling.exists():
                shutil.copy2(sibling, Path(folder) / sibling.name)
        out = Path(folder) / "drc.json"
        command = [cli, "pcb", "drc", "--format", "json", "--severity-all", "--output", str(out), str(copy)]
        if refill:
            command.insert(3, "--refill-zones")
        done = subprocess.run(command, capture_output=True, text=True, timeout=900,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.exists():
            raise JlcError(f"kicad-cli DRC wrote no report: {(done.stderr or done.stdout).strip()[:300]}")
        return json.loads(out.read_text(encoding="utf-8"))


def _signature(report: dict) -> list:
    return sorted([(v.get("type", ""), v.get("severity", ""), tuple(sorted(i.get("description", "")
                                                                         for i in v.get("items", []))))
                   for v in report.get("violations", [])]
                  + [("unconnected", str(len(report.get("unconnected_items", []))), ())])


def check_drc(ck: Checks, board: Path) -> None:
    cli = kicad_cli()
    if not cli:
        ck.add("drc.clean", "DRC: no error, no unconnected item", "WARN",
               "kicad-cli not found: set JLC_AGENT_KICAD_CLI or put it on PATH")
        return
    saved = _drc(cli, board, refill=False)
    violations = saved.get("violations", [])
    errors = [v for v in violations if v.get("severity") == "error"]
    unconnected = saved.get("unconnected_items", [])
    by_type = collections.Counter(v.get("type") for v in errors)
    ck.add("drc.clean", "DRC: no error, no unconnected item", "FAIL" if errors or unconnected else "PASS",
           f"{len(errors)} errors {dict(by_type)}, {len(unconnected)} unconnected")
    nets = lambda v: [n for i in v.get("items", []) for n in re.findall(r"\[([^\]]+)\]", i.get("description", ""))]
    dangling = [v for v in violations if v.get("type") in ("track_dangling", "via_dangling")]
    rf = [v for v in dangling if any(kicad.RF_NET.search(n) for n in nets(v))]
    ck.add("drc.rf-stubs", "no dangling copper on an RF net", "FAIL" if rf else "PASS",
           "; ".join(i.get("description", "") for v in rf for i in v.get("items", [])) or "none")
    rest = [v for v in violations if v.get("severity") != "error" and v not in rf]
    ck.add("drc.warnings", "every other DRC warning read", "WARN" if rest else "PASS",
           ", ".join(f"{n} {t}" for t, n in collections.Counter(v.get("type") for v in rest).most_common()) or "none")
    try:
        refilled = _drc(cli, board, refill=True)
    except (JlcError, subprocess.SubprocessError) as error:
        ck.add("drc.pours", "copper pours current: a refill changes nothing", "INFO", f"not checked: {error}")
        return
    same = _signature(saved) == _signature(refilled)
    ck.add("drc.pours", "copper pours current: a refill changes nothing", "PASS" if same else "FAIL",
           "the saved fills are what a refill makes" if same else
           "a refill changes the DRC result: refill the zones, then export the Gerbers again")


# --- the run ------------------------------------------------------------------------------------

def run(spec_path: Path, board: Path | None = None, fetch: bool = False, footprints: list[Path] | None = None,
        pad_alias: dict | None = None, stock_boards: int | None = None,
        stock_lookup: Callable[[str], dict | None] | None = None, drc: bool = False) -> dict:
    spec_path = Path(spec_path).resolve()
    ck = Checks()
    started = time.time()
    spec = load_spec(spec_path)  # raises with the reason when the spec or a file it names is wrong
    ck.add("files.spec", "the spec and every file it names", "PASS",
           ", ".join(f"{k} {p.name}" for k, p in spec["files"].items()))
    check_quote_record(ck, spec_path, spec)
    facts = kicad.facts(board) if board else None
    cpl_sides: set[str] = set()
    cpl_report = None
    if spec.get("assembly") is not None:
        rows, _, col = orient.read_cpl(spec["files"]["cpl"])
        cpl = {r[col["ref"]].strip(): r for r in rows}
        cpl_sides = {"Bottom" if (r.get(col["side"]) or "").strip().lower().startswith("b") else "Top"
                     for r in rows}
        lines = check_bom(ck, spec, cpl)
        if board:
            cpl_report = check_cpl(ck, spec, board, fetch, footprints or [], pad_alias)
        else:
            ck.add("cpl.fit", "every part sits as JLC's own footprint needs", "WARN",
                   "no --board given: placements not checked against JLC's footprints")
        if stock_lookup is not None:
            boards = stock_boards or int((spec.get("assembly") or {}).get("PCBA Qty") or 0) or 5
            check_stock(ck, lines, boards, stock_lookup)
    sides = cpl_sides or set((facts or {}).get("sides_with_parts") or [])
    check_spec(ck, spec, facts, sides)
    if drc:
        if board:
            check_drc(ck, board)
        else:
            ck.add("drc.clean", "DRC: no error, no unconnected item", "WARN", "--drc needs --board")
    order = {"FAIL": 0, "WARN": 1, "PASS": 2, "INFO": 3}
    return {"spec": str(spec_path), "board": str(board) if board else None, "ran_s": round(time.time() - started, 1),
            "pass": not any(c["status"] == "FAIL" for c in ck), "checks": list(ck),
            "counts": dict(collections.Counter(c["status"] for c in ck)),
            "worst": min((c["status"] for c in ck), key=order.get, default="PASS"),
            "facts": {k: v for k, v in (facts or {}).items() if k != "vias_in_smd_pads"} or None,
            "cpl_corrections": (cpl_report or {}).get("corrections")}


def jlcone_stock(jlc: Any, pause: float = 0.4) -> Callable[[str], dict | None]:
    """Live stock through JLCONE's own parts search, one LCSC number at a time, at a human pace."""
    from . import account

    def lookup(code: str) -> dict | None:
        time.sleep(pause)
        return next((p for p in account.parts(jlc, code, 5)["parts"] if p["code"] == code), None)
    return lookup


def summary(report: dict, lessons: bool = True) -> str:
    lines = []
    for c in report["checks"]:
        lines.append(f"{c['status']:4}  {c['id']:20}  {c['detail']}")
        if lessons and c["status"] in ("FAIL", "WARN") and c["lesson"]:
            lines.append(f"{'':26}why: {c['lesson']}")
    counts = report["counts"]
    lines.append("")
    lines.append(", ".join(f"{counts[s]} {s}" for s in ("FAIL", "WARN", "PASS", "INFO") if counts.get(s)) +
                 (" - fix every FAIL before ordering" if not report["pass"] else " - no FAIL"))
    return "\n".join(lines)
