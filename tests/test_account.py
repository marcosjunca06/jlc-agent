"""The account readers' offline logic: JLC's time zones and the order-list summary."""

import datetime
import unittest

from jlc_agent.account import batch_summary, inventory, smt_progress, when
from jlc_agent.app import JlcError


class FakeSession:
    """Answers every call with `result`, or raises it when it is an exception."""

    def __init__(self, result):
        self.result = result

    def api(self, method, path, body=None, query=None, write=False, form=False):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TimeTest(unittest.TestCase):
    def test_text_dates_are_china_time(self):
        # an order carries the same moment in both forms
        self.assertEqual(when("2026-09-27 05:16:31"), when(1790457391420))

    def test_milliseconds_are_local(self):
        expected = datetime.datetime.fromtimestamp(1790596800).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(when(1790596800000), expected)
        self.assertEqual(when("1790596800000"), expected)

    def test_empty(self):
        for value in (None, "", 0, "0"):
            self.assertIsNone(when(value))


class OrderListTest(unittest.TestCase):
    def test_waiting_and_unpaid_are_flagged(self):
        row = batch_summary({
            "batchNum": "W1", "batchStatus": "inProduce", "batchCreateTime": 1789766137000,
            "payUnionSecondVO": {"totalFee": 10.0, "orderInfoVOList": [
                {"orderType": "order_pcb", "orderCode": "Y1", "orderStatus": "inProduce",
                 "awaitConfirmationTypes": ["none"], "waitConfirm": False},
                {"orderType": "order_smt", "orderCode": "SMT1", "orderStatus": "waitConfirm",
                 "awaitConfirmationTypes": ["productionFile"], "waitConfirm": True}]},
            "unPayUnionSecondVO": {"orderInfoVOList": [{"orderType": "order_steel", "orderCode": "S1",
                                                        "awaitConfirmationTypes": None, "waitConfirm": True}]},
            "paymentInfoVO": {"hadSuppleFlag": True, "supplementReason": "panel"},
            "expressInfoVO": {"freightModeName": "DHL Express", "expressNo": ""}})
        self.assertEqual([o["waiting_for_you"] for o in row["orders"]], [[], ["productionFile"], ["confirmation"]])
        self.assertEqual([o["kind"] for o in row["orders"]], ["PCB", "PCBA", "stencil"])
        self.assertTrue(row["unpaid"])
        self.assertTrue(row["supplement"])
        self.assertIsNone(row["tracking"])


class ReaderEdgeTest(unittest.TestCase):
    def test_assembly_without_steps_yet(self):
        # minutes after paying, JLC answers HTTP 501 with an empty body until assembly has steps
        progress = smt_progress(FakeSession(JlcError("/overseas-pcb-order/v1/smtOrder/smtWipProcess: HTTP 501")), "u")
        self.assertEqual((progress["steps"], progress["note"]), ([], "no assembly steps published yet"))
        with self.assertRaises(JlcError):
            smt_progress(FakeSession(JlcError("HTTP 500")), "u")

    def test_an_empty_stock_line_stays_zero(self):
        rows = inventory(FakeSession({"list": [
            {"componentCode": "C1", "componentModel": "A", "privateStockCount": 0, "stockNumber": 7},
            {"componentCode": "C2", "componentModel": "B", "privateStockCount": 12}]}))
        self.assertEqual([r["in_stock"] for r in rows], [0, 12])


if __name__ == "__main__":
    unittest.main()
