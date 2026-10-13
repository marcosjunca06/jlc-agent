"""The gate: what may be called freely, what needs write=True, and what is never called."""

import unittest

from jlc_agent.app import JlcError, api_path, classify, gate, is_read_path


class ReadsTest(unittest.TestCase):
    def test_reads_pass(self):
        for path in ["/overseas-core-platform/orderCenter/selectPersonBatch",
                     "/overseas-core-platform/orderCenter/selectPersonOrderDetail",
                     "/overseas-core-platform/order/orderWipProcessGroupedProgress",
                     "/overseas-pcb-order/v1/smtOrder/smtWipProcess?uuid=abc",
                     "/overseas-pcb-order/v1/msgCustomerMessage/selectCustomerNoticeList",
                     "/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList",
                     "/overseas-core-platform/bomMatchRecordController/checkWhiteList/idle_stock_buy",
                     "/overseas-core-platform/shoppingCart/smtGood/getDfmResultFile/V2",
                     "/overseas-core-platform/order/getSmtConfirmFileOpenCustomer"]:
            self.assertTrue(is_read_path(path), path)

    def test_writes_and_side_effects_are_not_reads(self):
        for path in ["/overseas-core-platform/secret/update",
                     "/overseas-smt-component-order-platform/v1/overseasSmtComponentOrder/purchaseOrder/submitPurchaseOrder",
                     "/overseas-core-platform/fileOperation/checkOrReUploadBomCplFile",
                     "/overseas-pcb-order/v1/msgCustomerMessage/replyMsgMessage",
                     "/overseas-pcb-order/v1/msgCustomerMessage/updateMsgMessageByParam",
                     "/overseas-pcb-order/v1/msgCustomerMessage/selectMessageNoticeDetail",
                     "/overseas-core-platform/cross-cookie/relay/issue",
                     "/overseas-pcb-order/v1/newOrder/getTradeNo",
                     "/overseas-core-platform/bomMatchRecordController/addBom",
                     "/overseas-core-platform/activityCoupons/receiveCouponsCommon",
                     "/overseas-pcb-order/v1/newOrder/cancelOrder",
                     "/overseas-core-platform/file/uploadGerber", ""]:
            self.assertFalse(is_read_path(path), path)


class KindsTest(unittest.TestCase):
    def test_paying_and_ordering_are_blocked(self):
        for path in ["/overseas-pcb-order/v1/orderTrade/paySupplement",
                     "/overseas-pcb-order/v1/orderTrade/getPayStatus",
                     "/overseas-core-platform/shoppingCart/createOrder",
                     "/overseas-smt-component-order-platform/v1/overseasSmtComponentOrder/smtComponentShopCart/checkoutCart",
                     "/overseas-smt-component-order-platform/v1/overseasSmtComponentOrder/presaleOrder/payBatchOrder",
                     "/overseas-core-platform/balance/applyWithdraw",
                     "/overseas-core-platform/jpayAccount/saveJpayAccount",
                     "/overseas-pcb-order/v1/newOrder/getTradeNo"]:
            self.assertEqual(classify(path), "blocked", path)

    def test_three_kinds(self):
        self.assertEqual(classify("/overseas-pcb-order/v1/finishedPicture/downloadSmtFinishedPicture?fileAccessId=x"), "read")
        self.assertEqual(classify("/overseas-core-platform/orderCenter/invoiceOrder"), "read")
        self.assertEqual(classify("/overseas-pcb-order/v1/confirmFile/confirmFileDetail"), "read")
        self.assertEqual(classify("/overseas-pcb-order/v1/confirmFile/confirmFile"), "write")
        self.assertEqual(classify("/overseas-pcb-order/v1/msgCustomerMessage/replyMsgMessage"), "write")
        self.assertEqual(classify("/overseas-core-platform/activityCoupons/receiveCouponsCommon"), "write")

    def test_gate_refuses_blocked_even_with_write(self):
        with self.assertRaisesRegex(JlcError, "never makes that call"):
            gate("/overseas-pcb-order/v1/orderTrade/paySupplement", write=True)
        with self.assertRaisesRegex(JlcError, "needs write=True"):
            gate("/overseas-pcb-order/v1/msgCustomerMessage/replyMsgMessage", write=False)
        self.assertEqual(gate("/overseas-pcb-order/v1/msgCustomerMessage/replyMsgMessage", write=True), "write")
        self.assertEqual(gate("/overseas-core-platform/orderCenter/selectPersonBatch", write=False), "read")


class PathTest(unittest.TestCase):
    def test_git_bash_path_rewrite_undone(self):
        self.assertEqual(api_path("C:/Program Files/Git/overseas-core-platform/x/selectY"),
                         "/overseas-core-platform/x/selectY")
        self.assertEqual(api_path("overseas-core-platform/x/selectY"), "/overseas-core-platform/x/selectY")
        self.assertEqual(api_path("/overseas-core-platform/x/selectY"), "/overseas-core-platform/x/selectY")


if __name__ == "__main__":
    unittest.main()
