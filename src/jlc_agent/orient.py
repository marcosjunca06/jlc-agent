"""Check a board's placements against JLC's own footprints, and write a corrected CPL.

JLC's placement machine reads a CPL as an instruction about *its own* library's footprint for each LCSC
part: the Rotation column turns that footprint from its zero orientation, and Mid X / Mid Y place its
anchor. A CPL exported from KiCad carries KiCad's rotation and anchor, which is right only where the two
libraries draw the part the same way. Where the board uses a footprint taken from JLC's library they agree
by construction; where it uses a generic KiCad footprint they can differ by a right angle or sit on a
different anchor, and the part goes down turned or shifted. The Gerbers are drawn from the board's own
footprint, so nothing in them shows it.

JLC's library is the EasyEDA/LCSC one. For each part with an LCSC number, this fits the board's pads onto
the pads of JLC's footprint for that number, by pad number, over the four right angles, and reports the
fit, the rotation the CPL needs and how far the anchor moves. It then writes a corrected CPL with those
corrections applied. JLC's footprints are read from .kicad_mod files that carry an "LCSC Part" property,
as easyeda2kicad writes them; with `fetch`, easyeda2kicad is run for the ones missing.

The corrected CPL is a proposal, not a release: JLC's placement preview is the authority, and it draws the
same parts. Compare the two before uploading either (`jlc-agent quote` compares JLC's imported placements
with the CPL it sends, part by part).

Bottom-side parts are fitted in the bottom view, the board turned over left-right as JLC's machine sees
it, and the rotation written is the one JLC's zero orientation needs in that view. That convention was read
off JLC's own placement preview (September 2026: parts at 90 turned counter-clockwise in the turned-over
view, a pin-1 dot at 180 on pad 1's corner) and agrees with KiBot's JLC rule. Walk the bottom side first
in JLC's preview all the same.

Some JLC footprints number a pad differently from the board's land (a SIM holder's ground tabs, say);
`pad_alias` maps JLC's pad numbers onto the board's for one LCSC number.

A two-pad resistor, capacitor or inductor that sits a half turn from JLC's footprint is the same placement
and is left as it is, with a note; diodes, LEDs and capacitors whose value says tantalum or electrolytic are
corrected like any other part.

The board is read straight from the .kicad_pcb file (KiCad 6 and later), so this needs neither KiCad nor
its Python.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from .app import DATA, JlcError
from .kicad import child as _child, children as _children, fields as _fields, load, parse, turn

SQUARE_MM = 0.05  # an anchor this close counts as the same point
FIT_MM = 0.25  # a fit worse than this is not the same land pattern
FOOTPRINTS = DATA / "footprints"  # easyeda2kicad's output, one folder per LCSC number
LCSC_NUMBER = re.compile(r"^C\d{2,}$")
LCSC_FIELD = re.compile(r"lcsc|jlc", re.I)
# Two-pad parts these designators name are the same part either way round: a half turn from JLC's footprint
# places them just the same. Diodes, LEDs and polarised capacitors are not (the value tells those apart).
UNPOLARISED = re.compile(r"^(R|RN|C|L|FB)\d")
POLARISED_VALUE = re.compile(r"tant|electro|polar|alum|e-?cap", re.I)

# CPL column names: JLC's own, and the ones KiCad's position file uses
CPL_COLUMNS = {"ref": ("Designator", "Ref"), "x": ("Mid X", "PosX"), "y": ("Mid Y", "PosY"),
               "rotation": ("Rotation", "Rot"), "side": ("Layer", "Side")}
BOM_LCSC_COLUMNS = ("LCSC Part #", "LCSC Part", "LCSC", "JLCPCB Part #", "JLCPCB Part", "LCSC#")


# --- KiCad files ------------------------------------------------------------------------------

def _lcsc(fields: dict[str, str]) -> str | None:
    for name, value in fields.items():
        if LCSC_FIELD.search(name) and LCSC_NUMBER.match(value.strip()):
            return value.strip()
    return None


def pad_map(node: list) -> dict[str, tuple[float, float]]:
    """The centre of every numbered pad of a footprint, in mm, relative to its anchor, in the footprint's own
    frame (its placed rotation undone) with y up. Pads sharing a number (a split thermal pad) are averaged.
    KiCad stores pad positions this way in board and footprint files alike, for either side of the board."""
    grouped: dict[str, list[tuple[float, float]]] = {}
    for pad in _children(node, "pad"):
        number = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
        at = _child(pad, "at")
        if not number or at is None:
            continue
        grouped.setdefault(number, []).append((float(at[1]), -float(at[2])))
    return {n: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for n, v in grouped.items()}


def bottom_view(pads: dict[str, tuple[float, float]], rotation: float) -> dict[str, tuple[float, float]]:
    """A bottom-side part's pads as JLC's machine sees them: placed (its rotation kept), then the board turned
    over left-right."""
    return {n: (-x, y) for n, (x, y) in ((n, turn(p, rotation)) for n, p in pads.items())}


def board_parts(board: Path) -> dict[str, dict]:
    """Every footprint on a .kicad_pcb: reference, LCSC number (from any field naming LCSC or JLC), side,
    rotation, library name and pad map."""
    root = load(Path(board))
    parts = {}
    for node in [*_children(root, "footprint"), *_children(root, "module")]:
        fields = _fields(node)
        ref = fields.get("Reference")
        at = _child(node, "at")
        if not ref or at is None:
            continue
        layer = (_child(node, "layer") or [None, "F.Cu"])[1]
        rotation = float(at[3]) % 360 if len(at) > 3 else 0.0
        pads = pad_map(node)
        parts[ref] = {"ref": ref, "lcsc": _lcsc(fields), "value": fields.get("Value", ""),
                      "footprint": node[1] if len(node) > 1 and isinstance(node[1], str) else "",
                      "side": "Bottom" if layer.startswith("B.") else "Top", "rotation": rotation,
                      "x": float(at[1]), "y": float(at[2]), "pads": pads, "bottom_view": bottom_view(pads, rotation)}
    return parts


def vendor_footprints(folders: Iterable[Path]) -> dict[str, Path]:
    """LCSC number -> .kicad_mod, from footprints carrying an "LCSC Part" property (easyeda2kicad's)."""
    found: dict[str, Path] = {}
    for folder in folders:
        for path in sorted(Path(folder).rglob("*.kicad_mod")):
            fields = _fields(parse(path.read_text(encoding="utf-8")))
            lcsc = _lcsc(fields)
            if lcsc:
                found.setdefault(lcsc, path)
    return found


def fetch(lcsc: str, cache: Path = FOOTPRINTS) -> Path | None:
    """JLC's footprint for one LCSC number, converted by easyeda2kicad into its own folder (two parts can share
    one EasyEDA package name, and easyeda2kicad names files after the package). Needs easyeda2kicad
    (`pip install easyeda2kicad`), which is run as a separate program."""
    if not LCSC_NUMBER.match(lcsc):
        raise JlcError(f"{lcsc!r} is not an LCSC part number")
    folder = cache / lcsc
    existing = sorted(folder.rglob("*.kicad_mod"))
    if existing:
        return existing[0]
    command = [sys.executable, "-m", "easyeda2kicad", f"--lcsc_id={lcsc}", "--footprint",
               "--output", str(folder / "jlc"), "--overwrite"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise JlcError(f"easyeda2kicad did not run for {lcsc}: {error}") from error
    if done.returncode != 0 and "No module named easyeda2kicad" in (done.stderr or ""):
        shutil.rmtree(folder, ignore_errors=True)
        raise JlcError("easyeda2kicad is not installed: pip install easyeda2kicad")
    made = sorted(folder.rglob("*.kicad_mod"))
    if not made:
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return made[0]


# --- the fit ----------------------------------------------------------------------------------

def fit(mine: dict, theirs: dict) -> tuple[int, tuple[float, float], float, int] | None:
    """The right angle R and shift t with mine(n) = Rot(R).theirs(n) + t over the pad numbers both have:
    (R, t, the worst pad's residual in mm, how many pads were compared)."""
    shared = sorted(set(mine) & set(theirs))
    if not shared:
        return None
    best = None
    for degrees in (0, 90, 180, 270):
        spun = {n: turn(theirs[n], degrees) for n in shared}
        tx = sum(mine[n][0] - spun[n][0] for n in shared) / len(shared)
        ty = sum(mine[n][1] - spun[n][1] for n in shared) / len(shared)
        residual = max(math.hypot(mine[n][0] - spun[n][0] - tx, mine[n][1] - spun[n][1] - ty) for n in shared)
        if best is None or residual < best[2]:
            best = (degrees, (tx, ty), residual, len(shared))
    return best


# --- CPL and BOM ------------------------------------------------------------------------------

def _columns(fieldnames: list[str], wanted: dict[str, tuple[str, ...]], path: Path) -> dict[str, str]:
    lower = {name.strip().lower(): name for name in fieldnames}
    found = {}
    for key, names in wanted.items():
        hit = next((lower[n.lower()] for n in names if n.lower() in lower), None)
        if hit is None:
            raise JlcError(f"{path}: no {' or '.join(repr(n) for n in names)} column")
        found[key] = hit
    return found


def read_cpl(path: Path) -> tuple[list[dict], list[str], dict[str, str]]:
    """The CPL's rows, its column names in order, and which column holds what."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames, _columns(fieldnames, CPL_COLUMNS, path)


def read_bom_lcsc(path: Path) -> dict[str, str]:
    """Designator -> LCSC number from a JLC-style BOM (Comment, Designator, Footprint, LCSC Part #)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        names = list(reader.fieldnames or [])
    lower = {n.strip().lower(): n for n in names}
    ref_col = lower.get("designator")
    lcsc_col = next((lower[n.lower()] for n in BOM_LCSC_COLUMNS if n.lower() in lower), None)
    if ref_col is None or lcsc_col is None:
        raise JlcError(f"{path}: a BOM needs a Designator column and an LCSC part column ({', '.join(BOM_LCSC_COLUMNS)})")
    found = {}
    for row in rows:
        lcsc = (row.get(lcsc_col) or "").strip()
        if not LCSC_NUMBER.match(lcsc):
            continue
        for ref in re.split(r"[,\s]+", row.get(ref_col) or ""):
            if ref:
                found[ref] = lcsc
    return found


def _number(text: str) -> float:
    return float(str(text).strip().lower().replace("mm", "").replace("deg", ""))


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


# --- the check --------------------------------------------------------------------------------

def cpl_frame(parts: dict[str, dict], placed: dict[str, dict], col: dict[str, str]) -> dict | None:
    """How the CPL's coordinates relate to the board's: Mid X = x - ox and Mid Y = -(y - oy) for KiCad's
    position files and JLC's (y up), or +(y - oy) for an export that kept KiCad's y down. Fitted on the parts
    both name (top side first), as medians; None when the CPL's positions do not follow the board's at all
    (a CPL from another revision, or from another board)."""
    pairs = []
    for side in ("Top", None):
        pairs = [(parts[r]["x"], parts[r]["y"], _number(row[col["x"]]), _number(row[col["y"]]))
                 for r, row in placed.items() if r in parts and (side is None or parts[r]["side"] == side)]
        if len(pairs) >= 3:
            break
    if not pairs:
        return None
    best = None
    for sign in (-1.0, 1.0):
        ox = statistics.median(x - cx for x, y, cx, cy in pairs)
        oy = statistics.median(y - sign * cy for x, y, cx, cy in pairs)
        deviation = statistics.median(math.hypot(x - ox - cx, y - oy - sign * cy) for x, y, cx, cy in pairs)
        if best is None or deviation < best["deviation_mm"]:
            best = {"origin": [round(ox, 4), round(oy, 4)], "y_up": sign < 0, "deviation_mm": round(deviation, 4),
                    "parts": len(pairs)}
    return best if best["deviation_mm"] <= 0.01 else None


def _anchor_in_cpl(part: dict, frame: dict) -> tuple[float, float]:
    ox, oy = frame["origin"]
    return (part["x"] - ox, (oy - part["y"]) if frame["y_up"] else (part["y"] - oy))

def check(board: Path, cpl: Path, bom: Path | None = None, vendor: Iterable[Path] = (), fetch_missing: bool = False,
          refs: Iterable[str] | None = None, pad_alias: dict[str, dict[str, str]] | None = None) -> dict:
    """Fit every placed part that has an LCSC number against JLC's footprint for that number. Returns the
    report: one row per part checked, the corrections, and the rows a corrected CPL would carry."""
    parts = board_parts(Path(board))
    rows_in, fieldnames, col = read_cpl(Path(cpl))
    placed = {row[col["ref"]].strip(): row for row in rows_in}
    frame = cpl_frame(parts, placed, col)
    numbers = read_bom_lcsc(Path(bom)) if bom else {}
    library = vendor_footprints(list(vendor) + ([FOOTPRINTS] if FOOTPRINTS.exists() else []))
    wanted = set(refs) if refs else None

    rows = []
    for ref in sorted(placed, key=_natural):
        if wanted is not None and ref not in wanted:
            continue
        part = parts.get(ref)
        lcsc = numbers.get(ref) or (part or {}).get("lcsc")
        if not lcsc:
            continue  # nothing to compare with: JLC places by LCSC number
        cpl_row = placed[ref]
        row: dict[str, Any] = {
            "ref": ref, "lcsc": lcsc, "value": (part or {}).get("value", ""), "footprint": (part or {}).get("footprint", ""),
            "side": (part or {}).get("side") or cpl_row.get(col["side"], ""), "vendor_file": "",
            "cpl_rotation": _number(cpl_row[col["rotation"]]), "cpl_xy": [_number(cpl_row[col["x"]]), _number(cpl_row[col["y"]])],
            "turn_deg": None, "residual_mm": None, "shared_pads": 0, "shift_mm": None, "want_rotation": None,
            "want_xy": None, "move_mm": 0.0, "ok": False, "note": ""}
        rows.append(row)
        if part is None:
            row["note"] = "not on the board"
            continue
        path = library.get(lcsc)
        if path is None and fetch_missing:
            path = fetch(lcsc)
        if path is None:
            row["note"] = "no JLC footprint for this LCSC number" + ("" if fetch_missing else " (use --fetch)")
            continue
        row["vendor_file"] = str(path)
        alias = (pad_alias or {}).get(lcsc, {})
        theirs = {alias.get(n, n): p for n, p in pad_map(parse(path.read_text(encoding="utf-8"))).items()}
        bottom = part["side"] == "Bottom"
        mine = part["bottom_view"] if bottom else part["pads"]
        result = fit(mine, theirs)
        if result is None:
            row["note"] = "no pad numbers in common with JLC's footprint"
            continue
        degrees, shift, residual, shared = result
        angle = part["rotation"]
        if bottom:
            # fitted in the bottom view with the part's own turn kept, so the angle is the rotation JLC's zero
            # orientation needs there, and the anchor shift is in that view (x mirrored back to the board's)
            want_rotation = degrees % 360
            moved = (-shift[0], shift[1])
            turned = (want_rotation - angle) % 360
        else:
            want_rotation = (angle + degrees) % 360
            moved = turn(shift, angle)
            turned = degrees
        move = math.hypot(*shift)
        # KiCad's anchor in the CPL's own coordinates, and JLC's anchor beside it when the libraries differ by
        # more than the squaring tolerance (under it they draw the same land with different pad lengths)
        anchor = _anchor_in_cpl(part, frame) if frame else tuple(row["cpl_xy"])
        y_up = frame["y_up"] if frame else True
        if move >= SQUARE_MM:
            anchor = (anchor[0] + moved[0], anchor[1] + (moved[1] if y_up else -moved[1]))
        row.update({"turn_deg": turned if turned <= 180 else turned - 360, "residual_mm": round(residual, 3),
                    "shared_pads": shared, "pads_board": len(mine), "pads_jlc": len(theirs),
                    "shift_mm": [round(shift[0], 3), round(shift[1], 3)], "want_rotation": want_rotation,
                    "want_xy": [round(anchor[0], 4), round(anchor[1], 4)], "move_mm": round(move, 3)})
        rotation_ok = abs((want_rotation - row["cpl_rotation"] + 180) % 360 - 180) < 1e-6
        place_ok = math.hypot(row["cpl_xy"][0] - anchor[0], row["cpl_xy"][1] - anchor[1]) < SQUARE_MM
        row["ok"] = rotation_ok and place_ok and residual < FIT_MM
        half = abs((want_rotation - row["cpl_rotation"]) % 360 - 180) < 1e-6
        if (not row["ok"] and half and place_ok and residual < FIT_MM and len(mine) == 2
                and UNPOLARISED.match(ref) and not POLARISED_VALUE.search(row["value"] or "")):
            row["ok"], row["half_turn"] = True, True
        notes = []
        if residual >= FIT_MM:
            notes.append("the pads do not fit JLC's part at any right angle; compare by hand")
        elif len(mine) != len(theirs):
            notes.append(f"pad numbering differs ({len(mine)} on the board, {len(theirs)} in JLC's); "
                         f"fitted on {shared} shared pads")
        if row.get("half_turn"):
            notes.append("a half turn from JLC's footprint: the same placement for an unpolarised part")
        if bottom:
            notes.append("bottom side: rotation in JLC's bottom view; walk it in the preview")
        row["note"] = "; ".join(notes)

    corrections = {r["ref"]: r for r in rows if not r["ok"] and r["want_rotation"] is not None
                   and r["residual_mm"] < FIT_MM}
    corrected = []
    for cpl_row in rows_in:
        out = dict(cpl_row)
        fix = corrections.get(cpl_row[col["ref"]].strip())
        if fix is not None:
            # a shift under the squaring tolerance is the two libraries drawing the same land pattern with
            # slightly different pad lengths, not a different anchor, so the position is left alone
            if fix["move_mm"] >= SQUARE_MM:
                out[col["x"]], out[col["y"]] = _fmt(fix["want_xy"][0]), _fmt(fix["want_xy"][1])
            out[col["rotation"]] = _fmt(fix["want_rotation"])
        corrected.append(out)
    return {"board": str(board), "cpl": str(cpl), "bom": str(bom) if bom else None, "cpl_frame": frame,
            "checked": sum(1 for r in rows if r["turn_deg"] is not None),
            "matching": sum(1 for r in rows if r["ok"]), "corrections": sorted(corrections, key=_natural),
            "half_turns": [r["ref"] for r in rows if r.get("half_turn")],
            "unchecked": [r["ref"] for r in rows if r["turn_deg"] is None],
            "rows": rows, "fieldnames": fieldnames, "corrected_rows": corrected}


def _natural(ref: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref))


def write_cpl(report: dict, out: Path) -> Path:
    """The corrected CPL, in the input's own columns and order."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=report["fieldnames"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(report["corrected_rows"])
    out = Path(out)
    out.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return out


def summary(report: dict) -> str:
    lines = [f"{'ref':8}{'lcsc':12}{'turn':>6}{'fit mm':>8}{'anchor moves':>14}{'rotation':>14}  verdict"]
    for r in report["rows"]:
        t = "--" if r["turn_deg"] is None else f"{r['turn_deg']:.0f}"
        fit_mm = "--" if r["residual_mm"] is None else f"{r['residual_mm']:.3f}"
        if r["want_rotation"] is None or r["ok"] or abs(r["want_rotation"] - r["cpl_rotation"]) < 1e-6:
            rotation = f"{r['cpl_rotation']:g}"
        else:
            rotation = f"{r['cpl_rotation']:g} -> {r['want_rotation']:g}"
        verdict = "ok" if r["ok"] else ("CORRECT" if r["ref"] in report["corrections"] else "CHECK")
        lines.append(f"{r['ref']:8}{r['lcsc']:12}{t:>6}{fit_mm:>8}{r['move_mm']:>14.3f}{rotation:>14}  "
                     f"{verdict} {r['note']}".rstrip())
    lines.append("")
    lines.append(f"{report['matching']} of {report['checked']} checked parts are placed as JLC's own footprints need")
    if report.get("cpl_frame") is None and report["checked"]:
        lines.append("the CPL's positions do not follow this board's (another revision?): anchor shifts are taken "
                     "from the CPL's own numbers")
    lines.append(f"corrections: {', '.join(report['corrections']) or 'none'}")
    if report["unchecked"]:
        lines.append(f"not checked: {', '.join(report['unchecked'])}")
    return "\n".join(lines)


def public(report: dict) -> dict:
    """The report without the corrected CPL's rows (those go to the file)."""
    return {k: v for k, v in report.items() if k not in ("fieldnames", "corrected_rows")}


def parse_alias(items: Iterable[str]) -> dict[str, dict[str, str]]:
    """`C7529386:8=10,9=12` (JLC's pad 8 is the board's 10, ...) as {"C7529386": {"8": "10", "9": "12"}}."""
    found: dict[str, dict[str, str]] = {}
    for item in items:
        lcsc, _, pairs = item.partition(":")
        if not LCSC_NUMBER.match(lcsc.strip()) or not pairs:
            raise JlcError(f"pad alias {item!r}: write it as LCSC:JLC_PAD=BOARD_PAD,... (e.g. C7529386:8=10,9=12)")
        for pair in pairs.split(","):
            theirs, _, ours = pair.partition("=")
            if not theirs.strip() or not ours.strip():
                raise JlcError(f"pad alias {item!r}: {pair!r} is not JLC_PAD=BOARD_PAD")
            found.setdefault(lcsc.strip(), {})[theirs.strip()] = ours.strip()
    return found


def default_out(cpl: Path) -> Path:
    cpl = Path(cpl)
    return cpl.with_name(f"{cpl.stem}-corrected{cpl.suffix or '.csv'}")
