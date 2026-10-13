"""The board reader and the pre-order checks on a synthetic board, spec and package. No JLCONE, no KiCad and
no network: the checks that reach outside (stock, DRC, footprint fetching) take their data from fakes here."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from jlc_agent import kicad, orient, preflight
from jlc_agent.app import JlcError

PTH = """M48
; #@! TF.FileFunction,Plated,1,4,PTH
METRIC
; #@! TA.AperFunction,Plated,PTH,ViaDrill
T1C0.300
; #@! TA.AperFunction,Plated,PTH,ComponentDrill
T2C0.600
%
"""


def pad(number, kind, shape, x, y, w, h, net, paste=True, angle=None, extra=""):
    layers = '"F.Cu" "F.Mask"' + (' "F.Paste"' if paste else "")
    at = f"(at {x} {y} {angle})" if angle is not None else f"(at {x} {y})"
    return f'(pad "{number}" {kind} {shape} {at} (size {w} {h}) (layers {layers}) {extra} (net "{net}"))'


def footprint(ref, lcsc, x, y, rotation, pads, layer="F.Cu"):
    at = f"(at {x} {y} {rotation})" if rotation else f"(at {x} {y})"
    return (f'(footprint "lib:{ref}" (layer "{layer}") {at} (property "Reference" "{ref}") '
            f'(property "Value" "v") (property "LCSC Part" "{lcsc}") {" ".join(pads)})')


def via(x, y, net, size=0.5, drill=0.3):
    return f'(via (at {x} {y}) (size {size}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "{net}"))'


U1_PADS = [
    pad("1", "smd", "rect", 0, 0, 2, 2, "GND"),
    pad("2", "smd", "roundrect", 5, 0, 2, 2, "GND", extra="(roundrect_rratio 0.25)"),
    pad("3", "smd", "custom", 10, 0, 0.5, 0.5, "GND", paste=False,
        extra="(primitives (gr_poly (pts (xy -1 -1) (xy 3 -1) (xy 3 1) (xy -1 1)) (width 0) (fill yes)))"),
    pad("4", "smd", "circle", 15, 0, 1, 1, "SIG"),
]
# U2 turned 90: its pad at (2, 0) lands at (130, 118), its 3 mm length along the board's x
U2_PADS = [pad("1", "smd", "rect", 2, 0, 1, 3, "GND", angle=90), pad("2", "smd", "rect", -2, 0, 1, 3, "SIG", angle=90)]
D1_PADS = [pad("1", "smd", "rect", -1, 0, 1, 1, "SIG"), pad("2", "smd", "rect", 1, 0, 1, 1, "GND")]

BOARD = ("(kicad_pcb (version 20260206) "
         '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" power) (2 "B.Cu" signal) (13 "F.Paste" user) '
         '(25 "Edge.Cuts" user)) '
         + footprint("U1", "C1001", 110, 110, 0, U1_PADS)
         + footprint("U2", "C1002", 130, 120, 90, U2_PADS)
         + footprint("D1", "C1003", 140, 130, 0, D1_PADS, layer="B.Cu")
         + via(110.5, 110.5, "GND")  # inside U1.1: counted, pasted
         + via(109.5, 109.5, "OTHER")  # inside U1.1 but another net: not counted
         + via(116.1, 111.1, "GND")  # beside U1.2's rounded corner: a square pad would count it
         + via(122.5, 110, "GND")  # inside U1.3's polygon: counted, no paste
         + via(125.6, 110, "SIG")  # its ring touches U1.4: counted
         + via(131.2, 118, "GND")  # along U2.1's long side once turned: counted
         + via(130, 119.2, "GND")  # across U2.1's short side: not counted
         + via(140, 120, "ANT_FEED")
         + '(gr_rect (start 100 100) (end 150 140) (stroke (width 0.05)) (layer "Edge.Cuts")))')


def jlc_footprint(lcsc, pads):
    return f'(footprint "jlc" (layer "F.Cu") (property "LCSC Part" "{lcsc}") {" ".join(pads)})'


class FactsTest(unittest.TestCase):
    def test_board_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            board = Path(folder) / "b.kicad_pcb"
            board.write_text(BOARD, encoding="utf-8")
            facts = kicad.facts(board)
        self.assertEqual(facts["copper_layers"], 4)
        self.assertEqual(facts["outline_mm"], [50.0, 40.0])
        self.assertEqual(facts["rf_nets"], ["ANT_FEED"])
        self.assertEqual(facts["vias"], 8)
        self.assertEqual(facts["min_via_drill_mm"], 0.3)
        found = {(v["ref"], v["pad"]): v["pasted"] for v in facts["vias_in_smd_pads"]}
        self.assertEqual(found, {("U1", "1"): True, ("U1", "3"): False, ("U1", "4"): True, ("U2", "1"): True})
        self.assertEqual(facts["sides_with_parts"], ["Bottom", "Top"])

    def test_not_a_board(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.kicad_pcb"
            path.write_text("(footprint x)", encoding="utf-8")
            with self.assertRaises(JlcError):
                kicad.facts(path)


GOOD_SPEC = {
    "board": "test", "gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv",
    "pcb": {"PCB Qty": "5", "Min via hole size/diameter": "0.3mm/(0.4/0.45mm)", "Specify Stackup": "Yes",
            "Layer Stackup": "JLC04161H-7628", "Impedance Control": "±10% (±5Ω if value≤50Ω)",
            "Via Covering": "Epoxy Filled & Capped", "Surface Finish": "ENIG", "Material Type": "FR4 TG155"},
    "assembly": {"PCBA Type": "Standard", "Assembly Side": "Both Sides", "PCBA Qty": "5"},
    "remark": "Impedance 50 ohm on ANT_FEED. Rails on all four edges.",
}


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        self.board = self.dir / "b.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")
        with zipfile.ZipFile(self.dir / "gerbers.zip", "w") as archive:
            archive.writestr("b-PTH.drl", PTH)
        (self.dir / "bom.csv").write_text("Comment,Designator,Footprint,LCSC Part #\nu,U1,x,C1001\nu,U2,x,C1002\n"
                                          "d,D1,x,C1003\n", encoding="utf-8")
        # KiCad's anchors (y up); D1 on the bottom at JLC's bottom-view rotation for its land
        (self.dir / "cpl.csv").write_text("Designator,Mid X,Mid Y,Layer,Rotation\nU1,110,-110,Top,0\n"
                                          "U2,130,-120,Top,90\nD1,140,-130,Bottom,180\n", encoding="utf-8")
        lib = self.dir / "jlc"
        lib.mkdir()
        (lib / "u1.kicad_mod").write_text(jlc_footprint("C1001", U1_PADS), encoding="utf-8")
        (lib / "u2.kicad_mod").write_text(jlc_footprint("C1002", U2_PADS), encoding="utf-8")
        (lib / "d1.kicad_mod").write_text(jlc_footprint("C1003", D1_PADS), encoding="utf-8")
        self.lib = lib
        self.reports = self.dir / "quotes"
        self.saved_reports, preflight.REPORTS = preflight.REPORTS, self.reports
        self.saved_cache, orient.FOOTPRINTS = orient.FOOTPRINTS, self.dir / "no-cache"

    def tearDown(self):
        preflight.REPORTS = self.saved_reports
        orient.FOOTPRINTS = self.saved_cache
        self.folder.cleanup()

    def run_with(self, changes=None, drop=(), **options):
        spec = json.loads(json.dumps(GOOD_SPEC))
        def place(path):  # "pcb/Min via hole size/diameter": the section, then the row (which may hold a /)
            section, _, row = path.partition("/")
            return (spec[section], row) if row else (spec, section)
        for path, value in (changes or {}).items():
            where, key = place(path)
            where[key] = value
        for path in drop:
            where, key = place(path)
            where.pop(key)
        spec_path = self.dir / "jlc-order.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        report = preflight.run(spec_path, board=self.board, footprints=[self.lib], **options)
        return {c["id"]: c for c in report["checks"]}, report

    def test_a_good_package(self):
        checks, report = self.run_with()
        statuses = {k: c["status"] for k, c in checks.items()}
        self.assertTrue(report["pass"], statuses)
        for cid in ("files.spec", "bom.match", "cpl.board", "cpl.fit", "spec.via", "spec.stackup", "spec.material",
                    "spec.covering", "spec.finish", "spec.sides", "spec.impedance-note"):
            self.assertEqual(statuses[cid], "PASS", (cid, checks[cid]["detail"]))
        self.assertEqual(statuses["quote.files"], "INFO")
        self.assertEqual(statuses["cpl.bottom"], "WARN")  # D1 is a polarised bottom-side part
        self.assertEqual(statuses["spec.rails"], "PASS")  # under 70 mm both ways, and the remark says where
        self.assertIn("3 vias in pasted pads", checks["spec.covering"]["detail"])

    def test_every_spec_mistake(self):
        checks, report = self.run_with(
            {"pcb/Min via hole size/diameter": "0.4mm/(0.5/0.55mm)", "pcb/Layer Stackup": "JLC06161H-3313",
             "pcb/Surface Finish": "HASL(with lead)", "assembly/Assembly Side": "Top Side", "remark": ""},
            drop=("pcb/Via Covering", "pcb/Impedance Control"))
        self.assertFalse(report["pass"])
        for cid, words in [("spec.via", "0.3 mm vias"), ("spec.stackup", "6-layer"), ("spec.stackup", "no impedance"),
                           ("spec.covering", "3 vias sit in pads"), ("spec.sides", "'Both Sides'")]:
            self.assertEqual(checks[cid]["status"], "FAIL", cid)
            self.assertIn(words, checks[cid]["detail"])
        self.assertEqual(checks["spec.rails"]["status"], "WARN")

    def test_material_must_come_last_and_impedance_needs_a_note(self):
        spec_pcb = dict(GOOD_SPEC["pcb"])
        spec_pcb.pop("Material Type")
        spec_pcb = {"Material Type": "FR4 TG155", **spec_pcb}
        checks, _ = self.run_with({"pcb": spec_pcb, "remark": "rails anywhere"})
        self.assertEqual(checks["spec.material"]["status"], "FAIL")
        self.assertEqual(checks["spec.impedance-note"]["status"], "FAIL")

    def test_bom_and_cpl_disagree(self):
        (self.dir / "bom.csv").write_text("Comment,Designator,Footprint,LCSC Part #\nu,U1,x,C1001\nu,U2,x,\n"
                                          "r,R9,x,C25804\n", encoding="utf-8")
        checks, _ = self.run_with()
        detail = checks["bom.match"]["detail"]
        self.assertEqual(checks["bom.match"]["status"], "FAIL")
        for words in ("no LCSC number", "in the BOM, not placed: ['R9']", "placed, not in the BOM: ['D1']"):
            self.assertIn(words, detail)

    def test_a_turned_part_and_a_cpl_from_another_board(self):
        (self.dir / "cpl.csv").write_text("Designator,Mid X,Mid Y,Layer,Rotation\nU1,110,-110,Top,90\n"
                                          "U2,130,-120,Top,90\nD1,140,-130,Bottom,180\n", encoding="utf-8")
        checks, report = self.run_with()
        self.assertEqual(checks["cpl.fit"]["status"], "FAIL")
        self.assertEqual(report["cpl_corrections"], ["U1"])
        (self.dir / "cpl.csv").write_text("Designator,Mid X,Mid Y,Layer,Rotation\nU1,11,-17,Top,0\n"
                                          "U2,52,-3,Top,90\nD1,9,-95,Bottom,180\n", encoding="utf-8")
        checks, _ = self.run_with()
        self.assertEqual(checks["cpl.board"]["status"], "FAIL")

    def test_the_last_quote_was_built_from_other_files(self):
        spec_path = self.dir / "jlc-order.json"
        self.run_with()  # writes the spec
        record = self.reports / "test-20260101-000000"
        record.mkdir(parents=True)
        files = {k: {"sha256": preflight._sha(self.dir / name)} for k, name in
                 (("gerbers", "gerbers.zip"), ("bom", "bom.csv"), ("cpl", "cpl.csv"))}
        report = {"spec": str(spec_path), "files": files, "stopped_at": "Quote & Order, before Save to Cart",
                  "pcb_options": {"Layers": "4", "Surface Finish": "ENIG", "Gold Fingers": "No"}}
        (record / "report.json").write_text(json.dumps(report), encoding="utf-8")
        checks, _ = self.run_with()
        self.assertEqual(checks["quote.files"]["status"], "PASS")
        self.assertEqual(checks["quote.defaults"]["detail"], "Gold Fingers: No")
        (self.dir / "bom.csv").write_text((self.dir / "bom.csv").read_text(encoding="utf-8") + "\n", encoding="utf-8")
        checks, _ = self.run_with()
        self.assertEqual(checks["quote.files"]["status"], "FAIL")
        self.assertIn("bom changed", checks["quote.files"]["detail"])

    def test_stock(self):
        stock = {"C1001": {"stock": 100, "library": "basic", "model": "A"},
                 "C1002": {"stock": 3, "library": "extended", "model": "B"}}
        checks, _ = self.run_with(stock_lookup=stock.get, stock_boards=5)
        self.assertEqual(checks["stock.cover"]["status"], "FAIL")
        self.assertIn("C1002 B has 3, needs 10", checks["stock.cover"]["detail"])
        self.assertIn("not found at JLC: ['C1003']", checks["stock.cover"]["detail"])
        self.assertEqual(checks["stock.extended"]["detail"], "1 of 3 lines")

    def test_no_board(self):
        spec_path = self.dir / "jlc-order.json"
        spec_path.write_text(json.dumps(GOOD_SPEC), encoding="utf-8")
        report = preflight.run(spec_path)
        checks = {c["id"]: c for c in report["checks"]}
        self.assertEqual(checks["cpl.fit"]["status"], "WARN")
        self.assertEqual(checks["spec.stackup"]["status"], "WARN")
        self.assertIn("FAIL", preflight.summary(report) + "FAIL")


if __name__ == "__main__":
    unittest.main()
