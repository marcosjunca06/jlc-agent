"""Change detection between two snapshots, and where a notification's click leads."""

import unittest

from jlc_agent.watch import changes, link_for, parts_changes


class ChangesTest(unittest.TestCase):
    def snapshot(self, **batch):
        base = {"status": "inProduce", "tracking": None, "supplement": False, "unpaid": False, "waiting": [],
                "steps": {"SMT1": ["Data Preparation"]}}
        base.update(batch)
        return {"batches": {"W1": base}, "messages": {"m1": {"kind": "notice", "order": None, "title": "old"}}}

    def test_nothing_changed(self):
        self.assertEqual(changes(self.snapshot(), self.snapshot()), [])

    def test_every_kind_of_change(self):
        after = self.snapshot(status="shipped", tracking="123", supplement=True, waiting=["productionFile"],
                              steps={"SMT1": ["Data Preparation", "Data Finished"]})
        after["messages"]["m2"] = {"kind": "order", "order": "Y1", "title": "Engineering question"}
        self.assertEqual(changes(self.snapshot(), after), [
            "W1: inProduce -> shipped",
            "W1: shipped, tracking 123",
            "W1: JLC is waiting for you (productionFile)",
            "W1: JLC asks for a supplement payment",
            "W1 SMT1: Data Finished done",
            "new order message (Y1): Engineering question"])

    def test_new_batch(self):
        before = {"batches": {}, "messages": {}}
        self.assertEqual(changes(before, {"batches": {"W2": {"status": "waitPay"}}, "messages": {}}),
                         ["W2: new order batch (waitPay)"])

    def test_timeline_lines_become_events_in_jlc_words(self):
        hold = "2026-09-21 12:25 Y1-0000000A is pending. Please check your email for details."
        before = self.snapshot(timeline=["2026-09-18 23:15 Your order has been submitted."])
        after = self.snapshot(timeline=[hold, "2026-09-18 23:15 Your order has been submitted."])
        self.assertEqual(changes(before, after),
                         ["W1: JLC says: Y1-0000000A is pending. Please check your email for details."])
        # a snapshot taken before the batch was watched must not replay its whole history
        self.assertEqual(changes(self.snapshot(), after), [])


def parts(where="being bought", status=1, paid="paid", ready=None, due="2026-10-20", bought=0, stock=0):
    return {"parts_orders": {"P1": {"batch": "PB1", "where": where, "status": status, "paid": paid, "ready": ready,
                                    "goods": [{"code": "C1", "model": "SOCKET-1", "wanted": 10, "bought": bought,
                                               "due": due}]}},
            "library": {"C1": {"model": "SOCKET-1", "in_stock": stock}}}


class PartsChangesTest(unittest.TestCase):
    def test_nothing_changed(self):
        self.assertEqual(parts_changes(parts(), parts()), [])

    def test_an_order_moving_and_its_stock_arriving(self):
        events = parts_changes(parts(), parts(where="in stock at JLC", status=3, ready="2026-10-21", bought=10,
                                              due="2026-10-22", stock=10))
        self.assertEqual(events, [
            "parts order P1 (10 x SOCKET-1): being bought -> in stock at JLC",
            "parts order P1 (10 x SOCKET-1): stage 1 -> 3",
            "parts order P1 (10 x SOCKET-1): ready for assembly 2026-10-21",
            "parts order P1 (10 x SOCKET-1): C1 due 2026-10-20 -> 2026-10-22",
            "parts order P1 (10 x SOCKET-1): C1 bought 0 -> 10",
            "My Parts: C1 SOCKET-1 arrived: 0 -> 10 in stock"])

    def test_stock_used_and_orders_gone(self):
        before = parts(stock=10)
        after = {"parts_orders": {}, "library": {"C1": {"model": "SOCKET-1", "in_stock": 4}}}
        self.assertEqual(parts_changes(before, after), [
            "parts order P1 (10 x SOCKET-1): no longer listed",
            "My Parts: C1 SOCKET-1: 10 -> 4 in stock"])

    def test_unread_or_newly_watched_parts_pages(self):
        # the parts pages failed this time: nothing to say, the last look is kept
        self.assertEqual(parts_changes(parts(), {"parts_orders": None, "library": None}), [])
        # a snapshot from before parts were watched must not report every line as new
        self.assertEqual(parts_changes({}, parts()), ["now also watching 1 parts order and 1 line in My Parts"])

    def test_batch_changes_include_the_parts_side(self):
        before = dict(parts(), batches={}, messages={})
        after = dict(parts(stock=5), batches={}, messages={})
        self.assertEqual(changes(before, after), ["My Parts: C1 SOCKET-1 arrived: 0 -> 5 in stock"])


class LinkTest(unittest.TestCase):
    def test_links(self):
        self.assertEqual(link_for("W2026010112345678: inProduce -> shipped"),
                         "https://jlcpcb.com/user-center/orderDetails?batchNum=W2026010112345678")
        self.assertEqual(link_for("new order message (Y1): hello"), "https://jlcpcb.com/user-center/messages")
        self.assertEqual(link_for("JLC alerts running again"), "https://jlcpcb.com/user-center/orders")


if __name__ == "__main__":
    unittest.main()
