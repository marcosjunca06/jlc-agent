"""The quote run's checks on data alone: the drill file, JLC's BOM match, JLC's imported placements, the
charge panel and the order spec. The browser half needs JLCONE and is run by hand (`jlc-agent quote`);
these are the parts that decide what the run reports.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from jlc_agent.app import JlcError
from jlc_agent.quote import (Gone, Page, _hours, _merge_file_id, _same, bom_summary, charges, drill_sizes,
                             excellon_tools, load_spec, reconcile, via_check)


class ScriptedPage(Page):
    """A Page without a browser: each expression gets the next answer queued for the first key it
    contains (keys are checked in order, so the most specific comes first)."""

    def __init__(self, answers):
        self.answers = [(key, list(values)) for key, values in answers]
        self.clicks = []
        self.answered = []

    def js(self, expression, timeout=60.0):
        for key, values in self.answers:
            if key in expression:
                return values.pop(0) if len(values) > 1 else values[0]
        raise AssertionError(f"unexpected expression {expression[:120]}")

    def settle(self, seconds=1.0):
        pass

    def click_at(self, x, y):
        self.clicks.append((x, y))


POINT = {"x": 10, "y": 20, "w": 5, "h": 5}


class ClickTest(unittest.TestCase):
    def test_an_element_that_stays_is_clicked(self):
        page = ScriptedPage([("centre(el)", [POINT])])
        page.click("window.__jlcq.byText('Yes')", "Yes")
        self.assertEqual(page.clicks, [(10, 20)])

    def test_an_element_gone_after_the_scroll_raises_gone(self):
        page = ScriptedPage([("centre(el)", [POINT, None])])
        with self.assertRaises(Gone):
            page.click("window.__jlcq.byText('Yes')", "Yes")
        self.assertEqual(page.clicks, [])

    def test_a_window_that_closes_itself_is_not_an_error(self):
        # Specify Stackup: Yes opens "Layer Stackup", which picks its default and closes on its own
        # between finding JLC06161H-3313 in it and pressing it (seen 2026-09-24)
        page = ScriptedPage([
            ("centre(el)", [POINT, None]),
            ("classList.contains('cur')", [False]),
            ('!!window.__jlcq.modalButton("JLC06161H-3313")', [True]),
            ("modalTitle()", ["Layer Stackup"]),
            ("m.innerText", ["Layer Stackup JLC06161H-3313 JLC06161H-7628 Confirm", None]),
        ])
        page._answer_modals("Specify Stackup", "Yes", (), {"Layer Stackup": "JLC06161H-3313"})
        self.assertEqual(page.clicks, [])

    def test_an_unanswerable_question_still_stops_the_run(self):
        page = ScriptedPage([
            ("modalTitle()", ["Reminder"]),
            ("!!window.__jlcq.modalButton", [False]),
            ("!!window.__jlcq.modal()", [True]),
            ("m.innerText", ["Do you want to continue with a non-standard option?"]),
        ])
        with self.assertRaisesRegex(JlcError, "JLC asks: Do you want"):
            page._answer_modals("Specify Stackup", "Yes", (), {})

    def test_a_question_is_answered_with_the_named_box_and_button(self):
        # Confirm Parts Placement: Yes raises a window with a tick box and one button (2026-10)
        question = ("Unconfirmed files will be automatically confirmed after 72 hours. "
                    "Do not confirm automatically Confirm")
        page = ScriptedPage([
            ("centre(el)", [POINT]),
            ("classList.contains('is-checked')", [False, True]),
            ('!!window.__jlcq.modalCheckbox("Do not confirm automatically")', [True]),
            ('!!window.__jlcq.modalCheckbox("Confirm")', [False]),
            ('!!window.__jlcq.modalButton("Confirm")', [True]),
            ('!!window.__jlcq.modalButton("Yes")', [False]),
            ("modalTitle()", ["Reminder"]),
            ("m.innerText", [question, None]),
        ])
        page._answer_modals("Confirm Parts Placement", "Yes", (), {},
                            {"will be automatically confirmed": ["Do not confirm automatically", "Confirm"]})
        self.assertEqual(len(page.clicks), 2)  # the box, then the button
        self.assertEqual(len(page.answered), 1)
        self.assertIn("answered: Do not confirm automatically, then Confirm", page.answered[0])

    def test_a_box_that_will_not_stay_ticked_stops_the_run(self):
        page = ScriptedPage([
            ("centre(el)", [POINT]),
            ("classList.contains('is-checked')", [False]),
            ('!!window.__jlcq.modalCheckbox("Do not confirm automatically")', [True]),
            ('!!window.__jlcq.modalButton("Yes")', [False]),
            ("modalTitle()", ["Reminder"]),
            ("m.innerText", ["Files will be automatically confirmed after 72 hours."]),
        ])
        with self.assertRaisesRegex(JlcError, "would not stay ticked"):
            page._answer_modals("Confirm Parts Placement", "Yes", (), {},
                                {"automatically confirmed": ["Do not confirm automatically", "Confirm"]})


class FakeCdp:
    def __init__(self, events):
        self.events = events


class MergeIdPage:
    """Just enough of a Page for _merge_file_id: the API log, the DevTools events and page state."""

    def __init__(self, response=None, events=(), state=None):
        self.response, self.cdp, self.state = response, FakeCdp(list(events)), state

    def last_response(self, fragment):
        return self.response

    def js(self, expression, timeout=60.0):
        return self.state


class MergeIdTest(unittest.TestCase):
    def test_from_the_dfm_response(self):
        page = MergeIdPage(response={"body": {"code": 200, "data": {"bomMergeFileId": "abc123"}}})
        self.assertEqual(_merge_file_id(page), "abc123")

    def test_from_the_page_download_when_the_response_never_reached_the_log(self):
        # seen 2026-09-28: the wizard downloaded the merge file, but its DFM response was not in the log
        url = ("https://cart.jlcpcb.com/api/overseas-core-platform/shoppingCart/smtGood/"
               "downloadBomMergeZipFileByAccessId?fileAccessId=f00d42")
        page = MergeIdPage(events=[{"method": "Network.requestWillBeSent", "params": {"request": {"url": url}}}])
        self.assertEqual(_merge_file_id(page), "f00d42")

    def test_from_the_page_state_last(self):
        self.assertEqual(_merge_file_id(MergeIdPage(state="s7")), "s7")
        self.assertIsNone(_merge_file_id(MergeIdPage()))


# The head of a plated drill file as KiCad writes it: vias are marked as such.
KICAD_PTH = """M48
; DRILL file KiCad 9.0.0 date 2026-01-01T00:00:00
; #@! TF.FileFunction,Plated,1,4,PTH
FMAT,2
METRIC
; #@! TA.AperFunction,Plated,PTH,ViaDrill
T1C0.200
; #@! TA.AperFunction,Plated,PTH,ComponentDrill
T2C0.200
; #@! TA.AperFunction,Plated,PTH,ComponentDrill
T3C0.600
%
G90
T1
X100.775Y-102.175
"""


class DrillTest(unittest.TestCase):
    def test_kicad_marks_vias(self):
        self.assertEqual(excellon_tools(KICAD_PTH), [(0.2, True), (0.2, False), (0.6, False)])

    def test_inch_files_and_tools_after_the_header_are_ignored(self):
        text = "M48\nINCH,LZ\nT1C0.0118\n%\nT2C0.5000\n"
        self.assertEqual(excellon_tools(text), [(0.2997, False)])

    def test_zip_reads_plated_files_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "gerbers.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("board-PTH.drl", KICAD_PTH)
                archive.writestr("board-NPTH.drl", "M48\nMETRIC\nT1C0.100\n%\n")
                archive.writestr("board-F_Cu.gtl", "%FSLAX46Y46*%")
            self.assertEqual(drill_sizes(path), {"via": [0.2], "plated": [0.2, 0.6]})

    def test_via_option_against_the_drills(self):
        drills = {"via": [0.2], "plated": [0.2, 0.6]}
        # 0.2 mm vias quoted at JLC's 0.3 mm default: the board would not be built as designed
        self.assertIn("0.2 mm vias", via_check(drills, "0.3mm/(0.4/0.45mm)"))
        self.assertIsNone(via_check(drills, "0.2mm/(0.3/0.35mm)"))
        self.assertIsNone(via_check(drills, "0.15mm/(0.25/0.3mm)"))
        self.assertIn("plated component holes", via_check({"via": [], "plated": [0.25]}, "0.3mm/(0.4/0.45mm)"))
        self.assertIsNone(via_check({"via": [], "plated": []}, "0.3mm/(0.4/0.45mm)"))
        # 0.3 mm vias, but a module footprint's 0.2 mm thermal-pad holes (component holes) under a 0.3 mm option
        self.assertIn("0.2 mm plated component holes",
                      via_check({"via": [0.3], "plated": [0.2, 0.3, 0.6]}, "0.3mm/(0.4/0.45mm)"))
        self.assertIsNone(via_check({"via": [0.3], "plated": [0.3, 0.6]}, "0.3mm/(0.4/0.45mm)"))


def match_row(**overrides):
    row = {"componentCode": "C19702", "matchStatus": 1, "topDesignators": "C1", "bottomDesignators": "",
           "libType": "base", "stockStatus": "inStock", "shortStockNum": 0, "needAuditFlag": False,
           "smtAssembly": "smtWeld", "moistureSensitivityLevelEn": "MSL 1"}
    row.update(overrides)
    return row


class BomTest(unittest.TestCase):
    def test_a_board_that_can_be_built(self):
        result = {"bomTotal": 3, "shortageNum": 0, "noneMatchNum": 0, "componentList": [
            match_row(),
            match_row(componentCode="C5656610", topDesignators="U4", libType="expand", needAuditFlag=True),
            match_row(componentCode="C5251182", topDesignators="J3", libType="expand", smtAssembly="manualWeld",
                      moistureSensitivityLevelEn="MSL 3")]}
        summary = bom_summary(result)
        self.assertEqual((summary["matched"], summary["basic"], summary["extended"]), (3, 1, 2))
        self.assertEqual(summary["problems"], [])
        self.assertEqual(summary["hand_soldered"], ["C5251182 (J3)"])
        self.assertEqual(summary["moisture_sensitive"], ["C5251182 (J3) MSL 3"])
        self.assertEqual(summary["notes"], ["C5656610 (U4): JLC reviews this part by hand before building"])

    def test_unmatched_and_short_are_problems(self):
        summary = bom_summary({"bomTotal": 2, "shortageNum": 1, "noneMatchNum": 1, "componentList": [
            match_row(matchStatus=0, componentCode="", matchFailReason="not found", topDesignators="U9"),
            match_row(componentCode="C77877", topDesignators="J500", shortStockNum=3)]})
        self.assertEqual(summary["problems"], ["U9: no JLC part matched (not found)",
                                               "C77877 (J500): JLC is short by 3"])


def merge_with(*entries, bottom=()):
    return {"designator_info": [{"top": list(entries), "bot": list(bottom)}]}


def placed(des, x, y, ang, **extra):
    entry = {"des": des, "is_mount": True, "ec": None, "oc": {"x": str(x), "y": str(y), "ang": str(ang)}}
    entry.update(extra)
    return entry


def cpl(des, x, y, rotation, layer="Top"):
    return {"Designator": des, "Mid X": str(x), "Mid Y": str(y), "Layer": layer, "Rotation": str(rotation)}


class PlacementTest(unittest.TestCase):
    def test_as_sent(self):
        # JLC's `oc` equal to the CPL, no corrections of its own; -90 and 270 are the same turn
        result = reconcile(merge_with(placed("C521", 173.5, -106.25, 90), placed("U1", 104.95, -130.5, 270)),
                           [cpl("C521", 173.5, -106.25, 90), cpl("U1", 104.95, -130.5, -90)])
        self.assertEqual(result["problems"], [])
        self.assertEqual((result["placed"], result["top"], result["bottom"]), (2, 2, 0))

    def test_every_kind_of_difference(self):
        result = reconcile(
            merge_with(placed("D3", 120, -140, 90), placed("J1", 100, -150, 0, is_mount=False),
                       placed("Q1", 110, -120, 0, ec={"ang": "180"}), placed("X9", 1, 1, 0),
                       bottom=[placed("R1", 90, -100, 0)]),
            [cpl("D3", 120, -140, 270), cpl("J1", 100, -150, 0), cpl("Q1", 110, -120, 0),
             cpl("R1", 90, -100, 0, layer="Top"), cpl("TP1", 50, -50, 0)])
        self.assertEqual(result["problems"], [
            "D3: JLC has (120, -140) at 90 deg, the CPL (120, -140) at 270 deg",
            "J1: JLC will not mount it",
            "Q1: JLC moved or turned it by its own correction {'ang': '180'}",
            "X9: JLC places it, but the CPL has no such designator",
            "R1: the CPL has it on the Top side, JLC on the Bottom"])
        self.assertEqual(result["notes"], ["TP1: in the CPL but JLC places nothing there (not in the BOM?)"])
        self.assertEqual(result["corrected_by_jlc"], 1)


class ChargesTest(unittest.TestCase):
    def test_final_page_text(self):
        text = ("Charge Details\nPCB Price\n$100.71\nVia Covering:\n$0.00\nImpedance Control:\n$33.08\n"
                "Standard PCBA Price\n$296.64\nComponents(43 items):\n$176.95\nBuild Time:\nPCB: 8-9 days\n$0.00\n"
                "Total Price:\n$397.35\nWeight\n850.50g")
        parsed = charges(text)
        self.assertEqual(parsed["total"], 397.35)
        self.assertEqual(parsed["weight_g"], 850.5)
        self.assertEqual(parsed["items"]["Impedance Control"], 33.08)
        self.assertEqual(parsed["items"]["Components(43 items)"], 176.95)
        self.assertEqual(parsed["items"]["Build Time: PCB: 8-9 days"], 0.0)

    def test_quote_page_text(self):
        parsed = charges("Charge Details Special Offer $32.00 Calculated Price $1,032.50 Weight 0.24kg")
        self.assertEqual((parsed["total"], parsed["weight_g"]), (1032.5, 240.0))


class SpecTest(unittest.TestCase):
    def write(self, folder, spec, files=("gerbers.zip", "bom.csv", "cpl.csv")):
        for name in files:
            (Path(folder) / name).write_bytes(b"x")
        path = Path(folder) / "jlc-order.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_files_resolve_next_to_the_spec(self):
        with tempfile.TemporaryDirectory() as folder:
            spec = load_spec(self.write(folder, {"gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv",
                                                 "assembly": {"PCBA Type": "Standard"}}))
            self.assertEqual(spec["files"]["cpl"], (Path(folder) / "cpl.csv").resolve())

    def test_mistakes_are_caught_before_any_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            for spec, message in [({"gerbers": "gerbers.zip", "pcbs": {}}, "unknown keys"),
                                  ({"bom": "bom.csv"}, "'gerbers' is required"),
                                  ({"gerbers": "gerbers.zip", "assembly": {}}, "needs 'bom' and 'cpl'"),
                                  ({"gerbers": "missing.zip"}, "does not exist"),
                                  # JLC's PCB Remark box stops at 200 characters, the Assembly Remark at 500
                                  ({"gerbers": "gerbers.zip", "remark": "x" * 201}, "takes 200"),
                                  ({"gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv", "assembly": {},
                                    "assembly_remark": "x" * 501}, "takes 500"),
                                  ({"gerbers": "gerbers.zip", "assembly_remark": "reflow top first"},
                                   "needs an assembly order")]:
                with self.assertRaisesRegex(JlcError, message):
                    load_spec(self.write(folder, spec))

    def test_build_times_are_spec_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            spec = load_spec(self.write(folder, {"gerbers": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv",
                                                 "assembly": {"PCBA Type": "Standard"},
                                                 "pcb_build_time": "fastest", "assembly_build_time": "2-3 days"}))
            self.assertEqual(spec["pcb_build_time"], "fastest")

    def test_answers_name_the_button(self):
        with tempfile.TemporaryDirectory() as folder:
            steps = ["Do not confirm automatically", "Confirm"]  # a tick box, then the window's one button
            spec = load_spec(self.write(folder, {"gerbers": "gerbers.zip", "answers": {
                "will be automatically confirmed": steps}}))
            self.assertEqual(spec["answers"], {"will be automatically confirmed": steps})
            load_spec(self.write(folder, {"gerbers": "gerbers.zip", "answers": {"phrase": "OK"}}))
            for answers in (["Confirm"], {"phrase": ""}, {"": "Confirm"}, {"phrase": 1}, {"phrase": []},
                            {"phrase": ["Confirm", ""]}):
                with self.assertRaisesRegex(JlcError, "'answers' maps"):
                    load_spec(self.write(folder, {"gerbers": "gerbers.zip", "answers": answers}))

    def test_the_example_spec_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "jlc-order.example.json"
        spec = json.loads(example.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            load_spec(self.write(folder, spec, files=(spec["gerbers"], spec["bom"], spec["cpl"])))


class BuildTimeTest(unittest.TestCase):
    def test_labels_as_the_page_shows_them(self):
        # a six-layer board's list and a two-layer board's
        self.assertEqual(_hours("2-3 days"), 48)
        self.assertEqual(_hours(" 8-9 days"), 192)
        self.assertEqual(_hours("24 hours"), 24)
        self.assertEqual(_hours("24 hours PCBA Only"), 24)
        self.assertEqual(_hours("2 days"), 48)
        self.assertEqual(_hours("no time here"), float("inf"))
        self.assertEqual(min(["8-9 days", "4-5 days", "3-4 days", "2-3 days"], key=_hours), "2-3 days")
        # the order page spaces its assembly labels
        self.assertEqual(_hours("3 - 4 days"), 72)
        self.assertEqual(min(["3 - 4 days", "2 - 3 days"], key=_hours), "2 - 3 days")

    def test_labels_match_across_spacing(self):
        self.assertTrue(_same("2 - 3 days", "2-3 days"))
        self.assertTrue(_same(" 3 - 4 days ", "3-4 Days"))
        self.assertFalse(_same("2-3 days", "3-4 days"))


if __name__ == "__main__":
    unittest.main()
