"""Reading KiCad boards and footprints without KiCad: the S-expression files themselves.

KiCad 6 and later write boards (.kicad_pcb) and footprints (.kicad_mod) as S-expressions. A footprint's pads
sit at positions relative to its anchor in the footprint's own frame, for either side of the board; a pad's
angle in a board file is its absolute angle. Both were checked against KiCad's own pcbnew on real boards (KiCad
10, every rotation on both sides). Lengths are millimetres, board coordinates y-down as KiCad stores them.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable

from .app import JlcError

TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
RF_NET = re.compile(r"ANT|(^|_)RF(_|$)|FEED", re.I)
COPPER_TYPES = {"signal", "power", "mixed", "jumper"}


def parse(text: str) -> list:
    """A KiCad S-expression as nested lists of strings (quoted strings unquoted)."""
    stack: list[list] = [[]]
    for token in TOKEN.findall(text):
        if token == "(":
            stack.append([])
        elif token == ")":
            if len(stack) < 2:
                raise JlcError("unbalanced parentheses in a KiCad file")
            done = stack.pop()
            stack[-1].append(done)
        elif token.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]))
        else:
            stack[-1].append(token)
    if len(stack) != 1 or not stack[0]:
        raise JlcError("not a complete KiCad S-expression")
    return stack[0][0]


def load(path: Path, kind: str = "kicad_pcb") -> list:
    root = parse(Path(path).read_text(encoding="utf-8"))
    if kind and (not root or root[0] != kind):
        raise JlcError(f"{path}: not a KiCad {'board (.kicad_pcb)' if kind == 'kicad_pcb' else kind}")
    return root


def children(node: list, name: str) -> Iterable[list]:
    return (child for child in node[1:] if isinstance(child, list) and child and child[0] == name)


def child(node: list, name: str) -> list | None:
    return next(children(node, name), None)


def fields(node: list) -> dict[str, str]:
    """A footprint's fields: (property "Name" "Value"), and KiCad 5's (fp_text reference "U1")."""
    found = {}
    for prop in children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            found[prop[1]] = prop[2]
    for text in children(node, "fp_text"):
        if len(text) >= 3 and text[1] in ("reference", "value"):
            found.setdefault(text[1].capitalize(), text[2])
    return found


def footprints(root: list) -> list[list]:
    return [*children(root, "footprint"), *children(root, "module")]


def net_name(node: list) -> str:
    """A pad's, via's or track's net: (net "GND") in KiCad 10, (net 5 "GND") before."""
    net = child(node, "net")
    if not net or len(net) < 2:
        return ""
    return net[2] if len(net) > 2 else ("" if net[1].isdigit() else net[1])


def turn(point: tuple[float, float], degrees: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (point[0] * c - point[1] * s, point[0] * s + point[1] * c)


def _xy(node: list | None) -> tuple[float, float] | None:
    return (float(node[1]), float(node[2])) if node and len(node) >= 3 else None


# --- pads -------------------------------------------------------------------------------------

def placed_pads(fp: list) -> list[dict]:
    """A placed footprint's pads in board coordinates: centre, absolute angle, shape, size, layers, net."""
    at = child(fp, "at")
    fx, fy = float(at[1]), float(at[2])
    rotation = float(at[3]) if len(at) > 3 else 0.0
    ref = fields(fp).get("Reference", "")
    out = []
    for pad in children(fp, "pad"):
        pat = child(pad, "at")
        if pat is None or len(pad) < 4:
            continue
        ux, uy = turn((float(pat[1]), -float(pat[2])), rotation)
        size = child(pad, "size")
        layers = child(pad, "layers") or []
        rratio = child(pad, "roundrect_rratio")
        out.append({"ref": ref, "number": pad[1], "kind": pad[2], "shape": pad[3], "x": fx + ux, "y": fy - uy,
                    "angle": float(pat[3]) if len(pat) > 3 else 0.0,
                    "w": float(size[1]) if size else 0.0, "h": float(size[2]) if size and len(size) > 2 else 0.0,
                    "rratio": float(rratio[1]) if rratio else 0.25, "layers": list(layers[1:]),
                    "net": net_name(pad), "primitives": child(pad, "primitives")})
    return out


def _segment_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _arc_points(start, mid, end, steps: int = 24) -> list[tuple[float, float]]:
    """Points along the arc through start, mid and end (a straight line if they are in line)."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))
    span = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > span:  # the arc runs the other way round
        span -= 2 * math.pi
    return [(ux + r * math.cos(a0 + span * i / steps), uy + r * math.sin(a0 + span * i / steps)) for i in range(steps + 1)]


def _primitive_distance(prim: list, p: tuple[float, float]) -> float:
    """Distance from a pad-local point to one custom-pad primitive (0 inside a filled one)."""
    width = float((child(prim, "width") or child(child(prim, "stroke") or [], "width") or [0, 0])[1])
    fill = child(prim, "fill")
    filled = bool(fill) and fill[1] in ("yes", "solid")
    if prim[0] == "gr_poly":
        pts = [(float(xy[1]), float(xy[2])) for xy in children(child(prim, "pts") or [], "xy")]
        if not pts:
            return math.inf
        edge = min(_segment_distance(p, a, b) for a, b in zip(pts, pts[1:] + pts[:1])) - width / 2
        return min(edge, 0.0) if _inside(p, pts) else edge  # KiCad fills custom-pad polygons
    if prim[0] == "gr_circle":
        centre, rim = _xy(child(prim, "center")), _xy(child(prim, "end"))
        radius = math.hypot(rim[0] - centre[0], rim[1] - centre[1])
        d = math.hypot(p[0] - centre[0], p[1] - centre[1])
        return max(d - radius - width / 2, 0.0) if filled or width == 0 else abs(d - radius) - width / 2
    if prim[0] == "gr_line":
        return _segment_distance(p, _xy(child(prim, "start")), _xy(child(prim, "end"))) - width / 2
    if prim[0] == "gr_arc":
        pts = _arc_points(_xy(child(prim, "start")), _xy(child(prim, "mid")), _xy(child(prim, "end")))
        return min(_segment_distance(p, a, b) for a, b in zip(pts, pts[1:])) - width / 2
    if prim[0] == "gr_rect":
        (x0, y0), (x1, y1) = _xy(child(prim, "start")), _xy(child(prim, "end"))
        dx = max(min(x0, x1) - p[0], 0, p[0] - max(x0, x1))
        dy = max(min(y0, y1) - p[1], 0, p[1] - max(y0, y1))
        return math.hypot(dx, dy) - width / 2
    return math.inf


def pad_distance(pad: dict, x: float, y: float) -> float:
    """Distance from a board point to a pad's copper (0 or less inside it)."""
    # into the pad's own frame (y-down board numbers turned by the pad's angle, counter-clockwise on screen)
    lx, ly = turn((x - pad["x"], -(y - pad["y"])), -pad["angle"])
    ly = -ly
    w, h = pad["w"] / 2, pad["h"] / 2
    shape = pad["shape"]
    if shape == "circle":
        base = math.hypot(lx, ly) - w
    elif shape == "oval":
        if w >= h:
            base = math.hypot(max(abs(lx) - (w - h), 0.0), ly) - h
        else:
            base = math.hypot(lx, max(abs(ly) - (h - w), 0.0)) - w
    elif shape == "roundrect":
        r = pad["rratio"] * 2 * min(w, h)
        base = math.hypot(max(abs(lx) - (w - r), 0.0), max(abs(ly) - (h - r), 0.0)) - r
    else:  # rect, trapezoid (its bounding rectangle), chamfered rect, and a custom pad's anchor
        base = math.hypot(max(abs(lx) - w, 0.0), max(abs(ly) - h, 0.0))
    if shape == "custom" and pad["primitives"]:
        for prim in pad["primitives"][1:]:
            if isinstance(prim, list):
                base = min(base, _primitive_distance(prim, (lx, ly)))
    return base


# --- the board --------------------------------------------------------------------------------

def outline_points(root: list) -> list[tuple[float, float]]:
    """Points along every Edge.Cuts drawing (board coordinates), arcs and circles sampled."""
    pts: list[tuple[float, float]] = []
    for node in root[1:]:
        if not isinstance(node, list) or not node or not str(node[0]).startswith("gr_"):
            continue
        if (child(node, "layer") or [None, ""])[1] != "Edge.Cuts":
            continue
        kind = node[0]
        if kind in ("gr_line", "gr_rect"):
            a, b = _xy(child(node, "start")), _xy(child(node, "end"))
            pts += [a, b] if kind == "gr_line" else [a, b, (a[0], b[1]), (b[0], a[1])]
        elif kind == "gr_arc":
            pts += _arc_points(_xy(child(node, "start")), _xy(child(node, "mid")), _xy(child(node, "end")))
        elif kind == "gr_circle":
            c, e = _xy(child(node, "center")), _xy(child(node, "end"))
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        elif kind in ("gr_poly", "gr_curve"):
            for item in (child(node, "pts") or [])[1:]:
                if item[0] == "xy":
                    pts.append(_xy(item))
                elif item[0] == "arc":
                    pts += _arc_points(_xy(child(item, "start")), _xy(child(item, "mid")), _xy(child(item, "end")))
    return [p for p in pts if p]


def facts(path: Path) -> dict:
    """What the pre-order checks need from a board: copper layers, outline size, RF nets, vias, vias in SMD
    pads (and whether those pads get paste), sides with parts."""
    root = load(path)
    layers = [l for l in (child(root, "layers") or [])[1:]
              if isinstance(l, list) and len(l) >= 3 and str(l[1]).endswith(".Cu") and l[2] in COPPER_TYPES]
    pts = outline_points(root)
    outline = ([round(max(p[0] for p in pts) - min(p[0] for p in pts), 2),
                round(max(p[1] for p in pts) - min(p[1] for p in pts), 2)] if pts else [])
    nets: set[str] = set()

    def collect(node: list) -> None:
        for item in node[1:]:
            if isinstance(item, list) and item:
                if item[0] == "net" and len(item) >= 2:
                    nets.add(item[2] if len(item) > 2 else ("" if item[1].isdigit() else item[1]))
                else:
                    collect(item)
    collect(root)

    vias = []
    for via in children(root, "via"):
        at, size, drill = child(via, "at"), child(via, "size"), child(via, "drill")
        span = (child(via, "layers") or [])[1:]
        vias.append({"x": float(at[1]), "y": float(at[2]), "size": float(size[1]) if size else 0.0,
                     "drill": float(drill[1]) if drill else 0.0, "net": net_name(via),
                     "top": bool(span) and span[0] == "F.Cu", "bottom": bool(span) and span[-1] == "B.Cu"})

    in_pads, sides = [], set()
    for fp in footprints(root):
        layer = (child(fp, "layer") or [None, "F.Cu"])[1]
        sides.add("Bottom" if layer.startswith("B.") else "Top")
        for pad in placed_pads(fp):
            if pad["kind"] != "smd":
                continue
            on_top = "F.Cu" in pad["layers"] or "*.Cu" in pad["layers"]
            on_bottom = "B.Cu" in pad["layers"] or "*.Cu" in pad["layers"]
            reach = max(pad["w"], pad["h"]) / 2
            for via in vias:
                if via["net"] != pad["net"] or not ((on_top and via["top"]) or (on_bottom and via["bottom"])):
                    continue
                ring = via["size"] / 2  # the via's copper touching the pad's copper
                if abs(via["x"] - pad["x"]) > reach + ring + 5 or abs(via["y"] - pad["y"]) > reach + ring + 5:
                    continue
                if pad_distance(pad, via["x"], via["y"]) <= ring + 1e-6:
                    in_pads.append({"ref": pad["ref"], "pad": pad["number"], "net": pad["net"],
                                    "via_mm": [round(via["x"], 3), round(via["y"], 3)], "drill_mm": via["drill"],
                                    "pasted": any(l.endswith(".Paste") for l in pad["layers"])})
    return {"board": str(path), "copper_layers": len(layers), "outline_mm": outline,
            "rf_nets": sorted(n for n in nets if n and RF_NET.search(n) and not n.startswith("unconnected-")),
            "vias": len(vias), "min_via_drill_mm": round(min((v["drill"] for v in vias), default=0.0), 3),
            "vias_in_smd_pads": in_pads, "sides_with_parts": sorted(sides)}
