"""The placement check on synthetic boards: KiCad file reading, the right-angle fit, the bottom view, and the
corrected CPL. The footprints here are made up; the method was checked against KiCad's own pcbnew and against
real boards' JLC footprints before these tests were written."""

import csv
import tempfile
import unittest
from pathlib import Path

from jlc_agent import orient
from jlc_agent.app import JlcError


def pad(number, x, y):
    return f'(pad "{number}" smd rect (at {x} {y}) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))'


def footprint(ref, lcsc, x, y, rotation, pads, layer="F.Cu"):
    lcsc_field = f'(property "LCSC Part" "{lcsc}")' if lcsc else ""
    at = f"(at {x} {y} {rotation})" if rotation else f"(at {x} {y})"
    return (f'(footprint "lib:{ref}" (layer "{layer}") {at} (property "Reference" "{ref}") '
            f'(property "Value" "v") {lcsc_field} {" ".join(pad(*p) for p in pads)})')


def vendor(lcsc, pads):
    return f'(footprint "jlc" (layer "F.Cu") (property "LCSC Part" "{lcsc}") {" ".join(pad(*p) for p in pads)})'


# KiCad pad positions are y-down; a SOT-23-like land with pad 1 bottom left, 2 bottom right, 3 top
SOT = [("1", -0.95, 1.1), ("2", 0.95, 1.1), ("3", 0, -1.1)]


def spun(pads, degrees):
    """The same land turned by `degrees` (counter-clockwise, y up), back in KiCad's y-down numbers."""
    out = []
    for number, x, y in pads:
        tx, ty = orient.turn((x, -y), degrees)
        out.append((number, round(tx, 6), round(-ty, 6)))
    return out


class ParseTest(unittest.TestCase):
    def test_nested_lists_and_quoted_strings(self):
        tree = orient.parse('(a "b c" (d 1.5 "e \\"q\\"") f)')
        self.assertEqual(tree, ["a", "b c", ["d", "1.5", 'e "q"'], "f"])

    def test_unbalanced(self):
        with self.assertRaises(JlcError):
            orient.parse("(a (b)")


class FitTest(unittest.TestCase):
    def test_a_turned_land_is_found_at_its_right_angle(self):
        theirs = orient.pad_map(orient.parse(vendor("C1", SOT)))
        for degrees in (0, 90, 180, 270):
            mine = orient.pad_map(orient.parse(vendor("C1", spun(SOT, degrees))))
            found, shift, residual, shared = orient.fit(mine, theirs)
            self.assertEqual((found, shared), (degrees, 3))
            self.assertLess(residual, 1e-6)
            self.assertLess(abs(shift[0]) + abs(shift[1]), 1e-6)

    def test_nothing_in_common(self):
        self.assertIsNone(orient.fit({"1": (0, 0)}, {"A": (0, 0)}))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        # never the footprints a developer has fetched before
        self.saved_cache, orient.FOOTPRINTS = orient.FOOTPRINTS, self.dir / "no-cache"
        lib = self.dir / "jlc"
        lib.mkdir()
        (lib / "a.kicad_mod").write_text(vendor("C100", SOT), encoding="utf-8")  # same land as the board's
        (lib / "b.kicad_mod").write_text(vendor("C200", spun(SOT, 90)), encoding="utf-8")  # JLC draws it turned
        (lib / "c.kicad_mod").write_text(vendor("C300", [(n, x + 1.0, y) for n, x, y in SOT]), encoding="utf-8")
        (lib / "d.kicad_mod").write_text(vendor("C400", [("1", -1, 0), ("A", 1, 0)]), encoding="utf-8")
        bottom_land = [(n, -x, y) for n, x, y in spun(SOT, 90)]  # seen from below, JLC's land turned 90
        board = ("(kicad_pcb (version 20240108) "
                 + footprint("U1", "C100", 10, 10, 90, SOT)
                 + footprint("U2", "C200", 20, 10, 0, SOT)
                 + footprint("U3", "C300", 30, 10, 0, SOT)
                 + footprint("U4", "C100", 40, 10, 0, bottom_land, layer="B.Cu")
                 + footprint("D1", "C400", 50, 10, 0, [("1", -1, 0), ("2", 1, 0)])
                 + footprint("J1", "C999", 60, 10, 0, [("1", 0, 0)])
                 + footprint("H1", None, 70, 10, 0, [("1", 0, 0)]) + ")")
        self.board = self.dir / "b.kicad_pcb"
        self.board.write_text(board, encoding="utf-8")
        self.cpl = self.dir / "cpl.csv"
        rows = [("U1", 10, -10, "Top", 90), ("U2", 20, -10, "Top", 0), ("U3", 30, -10, "Top", 0),
                ("U4", 40, -10, "Bottom", 0), ("D1", 50, -10, "Top", 0), ("J1", 60, -10, "Top", 0),
                ("H1", 70, -10, "Top", 0)]
        with self.cpl.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
            writer.writerows(rows)
        self.lib = lib

    def tearDown(self):
        orient.FOOTPRINTS = self.saved_cache
        self.folder.cleanup()

    def test_every_kind_of_part(self):
        report = orient.check(self.board, self.cpl, vendor=[self.lib], pad_alias={"C400": {"A": "2"}})
        rows = {r["ref"]: r for r in report["rows"]}
        # the same land, placed at 90: nothing to do
        self.assertTrue(rows["U1"]["ok"])
        # JLC draws it a quarter turn round: the CPL needs JLC's footprint turned back
        self.assertEqual((rows["U2"]["turn_deg"], rows["U2"]["want_rotation"]), (-90, 270))
        # JLC's anchor sits 1 mm to the left of the pads' centre on JLC's land
        self.assertEqual(rows["U3"]["turn_deg"], 0)
        self.assertEqual(rows["U3"]["move_mm"], 1.0)
        self.assertEqual(rows["U3"]["want_xy"], [29.0, -10.0])
        # bottom side: fitted in the turned-over view, so the angle is JLC's rotation there
        self.assertEqual((rows["U4"]["side"], rows["U4"]["want_rotation"]), ("Bottom", 90))
        self.assertIn("bottom side", rows["U4"]["note"])
        # JLC numbers D1's second pad "A": the alias maps it onto the board's pad 2
        self.assertTrue(rows["D1"]["ok"])
        self.assertEqual(rows["D1"]["shared_pads"], 2)
        # no JLC footprint, and no LCSC number at all
        self.assertIn("no JLC footprint", rows["J1"]["note"])
        self.assertNotIn("H1", rows)
        self.assertEqual(report["corrections"], ["U2", "U3", "U4"])
        self.assertEqual(report["unchecked"], ["J1"])

    def test_the_corrected_cpl_keeps_every_other_row(self):
        report = orient.check(self.board, self.cpl, vendor=[self.lib])
        out = orient.write_cpl(report, orient.default_out(self.cpl))
        self.assertEqual(out.name, "cpl-corrected.csv")
        with out.open(encoding="utf-8") as handle:
            rows = {r["Designator"]: r for r in csv.DictReader(handle)}
        self.assertEqual(list(rows), ["U1", "U2", "U3", "U4", "D1", "J1", "H1"])
        self.assertEqual((rows["U2"]["Rotation"], rows["U2"]["Mid X"]), ("270", "20"))
        self.assertEqual((rows["U3"]["Rotation"], rows["U3"]["Mid X"]), ("0", "29"))
        self.assertEqual(rows["U4"]["Rotation"], "90")
        self.assertEqual(rows["U1"], {"Designator": "U1", "Mid X": "10", "Mid Y": "-10", "Layer": "Top",
                                      "Rotation": "90"})

    def test_kicad_position_file_columns_and_a_bom(self):
        pos = self.dir / "board-pos.csv"
        pos.write_text("Ref,Val,Package,PosX,PosY,Rot,Side\nU2,v,SOT,20,-10,0,top\nR1,v,R,1,1,0,top\n",
                       encoding="utf-8")
        bom = self.dir / "bom.csv"
        bom.write_text('Comment,Designator,Footprint,LCSC Part #\nv,"U2,R1",SOT,C200\n', encoding="utf-8")
        report = orient.check(self.board, pos, bom=bom, vendor=[self.lib])
        rows = {r["ref"]: r for r in report["rows"]}
        self.assertEqual(rows["U2"]["want_rotation"], 270)
        self.assertEqual(rows["R1"]["note"], "not on the board")
        corrected = {r["Ref"]: r for r in report["corrected_rows"]}
        self.assertEqual(corrected["U2"]["Rot"], "270")

    def test_a_corrected_cpl_checks_clean_and_is_never_shifted_twice(self):
        first = orient.check(self.board, self.cpl, vendor=[self.lib])
        corrected = orient.write_cpl(first, orient.default_out(self.cpl))
        again = orient.check(self.board, corrected, vendor=[self.lib])
        self.assertEqual(again["corrections"], [])
        self.assertEqual(again["corrected_rows"], first["corrected_rows"])

    def test_a_cpl_that_keeps_kicads_y_down(self):
        # parts at different heights, so the two conventions can be told apart; the CPL's origin is elsewhere too
        board, down = self.dir / "tall.kicad_pcb", self.dir / "down.csv"
        board.write_text("(kicad_pcb " + footprint("U1", "C100", 10, 10, 0, SOT) + footprint("U3", "C300", 30, 25, 0, SOT)
                         + footprint("U5", "C100", 50, 40, 0, SOT) + ")", encoding="utf-8")
        down.write_text("Designator,Mid X,Mid Y,Layer,Rotation\nU1,5,8,Top,0\nU3,25,23,Top,0\nU5,45,38,Top,0\n",
                        encoding="utf-8")
        report = orient.check(board, down, vendor=[self.lib])
        self.assertEqual(report["cpl_frame"]["origin"], [5.0, 2.0])
        self.assertFalse(report["cpl_frame"]["y_up"])
        rows = {r["ref"]: r for r in report["rows"]}
        self.assertEqual(rows["U3"]["want_xy"], [24.0, 23.0])  # the anchor shift, read in the CPL's own frame

    def test_half_turns(self):
        lib, board, cpl = self.dir / "two", self.dir / "two.kicad_pcb", self.dir / "two.csv"
        lib.mkdir()
        land = [("1", -0.8, 0), ("2", 0.8, 0)]
        (lib / "r.kicad_mod").write_text(vendor("C500", land), encoding="utf-8")
        values = {"R1": "10k", "C1": "100nF", "D1": "BAT54", "C2": "10uF tantalum"}
        parts = [footprint(ref, "C500", 10 * i, 10, 0, land).replace('"Value" "v"', f'"Value" "{value}"')
                 for i, (ref, value) in enumerate(values.items(), 1)]
        board.write_text("(kicad_pcb " + " ".join(parts) + ")", encoding="utf-8")
        rows = [f"{ref},{10 * i},-10,Top,180" for i, ref in enumerate(values, 1)]
        cpl.write_text("\n".join(["Designator,Mid X,Mid Y,Layer,Rotation", *rows]) + "\n", encoding="utf-8")
        report = orient.check(board, cpl, vendor=[lib])
        # the resistor and the ceramic capacitor are the same part turned round; the diode and the tantalum are not
        self.assertEqual(report["half_turns"], ["C1", "R1"])  # in natural order, as every list here
        self.assertEqual(report["corrections"], ["C2", "D1"])

    def test_alias_syntax(self):
        self.assertEqual(orient.parse_alias(["C7529386:8=10,9=12"]), {"C7529386": {"8": "10", "9": "12"}})
        for bad in ("8=10", "C1:8", "C1:=2"):
            with self.assertRaises(JlcError):
                orient.parse_alias([bad])


if __name__ == "__main__":
    unittest.main()
