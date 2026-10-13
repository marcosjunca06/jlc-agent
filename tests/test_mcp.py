"""The MCP server's tool list and annotations, and its refusals, which happen before any connection."""

import asyncio
import importlib.util
import unittest

HAVE_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAVE_MCP, "needs the mcp extra")
class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jlc_agent import mcp_server
        cls.server = mcp_server
        cls.tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}

    def test_every_tool_is_described_and_annotated(self):
        self.assertGreaterEqual(len(self.tools), 25)
        for name, tool in self.tools.items():
            self.assertTrue(name.startswith("jlc_"), name)
            self.assertTrue((tool.description or "").strip(), name)
            self.assertIsNotNone(tool.annotations, name)

    def hints(self, name):
        # as the client receives them; mcp 1.x names the fields in camelCase, 2.x in snake_case
        return self.tools[name].annotations.model_dump(by_alias=True, exclude_none=True)

    def test_only_reads_claim_to_be_read_only(self):
        read_only = {n for n in self.tools if self.hints(n).get("readOnlyHint")}
        for name in ("jlc_api_write", "jlc_message", "jlc_quote", "jlc_start", "jlc_open", "jlc_trace",
                     "jlc_cpl_check", "jlc_preflight"):
            self.assertNotIn(name, read_only)
        for name in ("jlc_orders", "jlc_order", "jlc_messages", "jlc_parts", "jlc_api", "jlc_endpoints"):
            self.assertIn(name, read_only)
        self.assertTrue(self.hints("jlc_api_write")["destructiveHint"])

    def test_refusals_come_before_any_connection_with_their_reason(self):
        # a ToolError is what the SDK passes to the client with its message intact
        with self.assertRaisesRegex(self.server.ToolError, "use jlc_api_write"):
            self.server.jlc_api("POST", "/overseas-pcb-order/v1/msgCustomerMessage/replyMsgMessage")
        for call in (self.server.jlc_api, self.server.jlc_api_write):
            with self.assertRaisesRegex(self.server.ToolError, "never makes that call"):
                call("POST", "overseas-pcb-order/v1/orderTrade/paySupplement")


if __name__ == "__main__":
    unittest.main()
