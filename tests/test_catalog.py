"""The endpoint extractor (catalog.cjs) on a synthetic chunk shaped like JLC's bundles."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from jlc_agent.catalog import EXTRACTOR, search

# A two-module webpack chunk shaped like JLC's: module 7 declares the service prefixes and exports them
# under letters, module 8 imports it as `o` and calls through `o.m`; module 9 imports module 8's export
# `Fc` and calls it. Module 10 declares a second prefix set whose letter m means something else, as the
# main site's bundle does, and must not be used for module 8's call.
CHUNK = r"""(window.webpackJsonp=window.webpackJsonp||[]).push([[1],{
7:function(e,t,n){"use strict";n.d(t,"m",(function(){return r})),n.d(t,"e",(function(){return c}));var r="/overseas-pcb-order",c="/overseas-core-platform"},
10:function(e,t,n){"use strict";n.d(t,"m",(function(){return r}));var r="/overseas-core-platform"},
8:function(e,t,n){"use strict";n.d(t,"Fc",(function(){return je}));var o=n(7),r=n(3),je=r.http.post("".concat(o.e,"/batchExpressQuery/getTrackingContent")),x=r.http.get("".concat(o.m,"/v1/smtOrder/smtWipProcess"))},
9:function(e,t,n){"use strict";var l=n(8);function f(e){var r={expressNo:e.no,batchNum:e.batch};return Object(l.Fc)(r,{shipHandle:!0})}}
}]);"""


@unittest.skipIf(shutil.which("node") is None, "needs node")
class ExtractorTest(unittest.TestCase):
    def run_on(self, *chunks):
        with tempfile.TemporaryDirectory() as folder:
            for index, text in enumerate(chunks):
                (Path(folder) / f"chunk{index}.js").write_text(text, encoding="utf-8")
            out = subprocess.run(["node", str(EXTRACTOR), f"site={folder}"], capture_output=True, text=True,
                                 check=True)
        return {e["path"]: e for e in json.loads(out.stdout)}

    def test_prefixes_resolve_through_the_import(self):
        entries = self.run_on(CHUNK)
        self.assertEqual(sorted(entries), ["/overseas-core-platform/batchExpressQuery/getTrackingContent",
                                           "/overseas-pcb-order/v1/smtOrder/smtWipProcess"])
        tracking = entries["/overseas-core-platform/batchExpressQuery/getTrackingContent"]
        self.assertEqual(tracking["method"], "POST")
        self.assertIn("expressNo:e.no", tracking["usage"][0])

    def test_a_file_that_is_not_a_chunk_is_skipped(self):
        hostile = "this.constructor.constructor('return process')().exit(3)"
        self.assertEqual(sorted(self.run_on(CHUNK, hostile, "not javascript at all (")),
                         ["/overseas-core-platform/batchExpressQuery/getTrackingContent",
                          "/overseas-pcb-order/v1/smtOrder/smtWipProcess"])


class SearchTest(unittest.TestCase):
    def test_words_and_kind(self):
        entries = [{"path": "/overseas-pcb-order/v1/smtOrder/smtWipProcess", "kind": "read"},
                   {"path": "/overseas-pcb-order/v1/orderTrade/paySupplement", "kind": "blocked"}]
        self.assertEqual(len(search(entries, ["SMT", "wip"])), 1)
        self.assertEqual(search(entries, [], "blocked")[0]["kind"], "blocked")


if __name__ == "__main__":
    unittest.main()
