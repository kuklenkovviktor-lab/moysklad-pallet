"""Regression checks for cross-market shift summary. No production DB access."""
import ast
import pathlib
import unittest
from unittest.mock import patch

source=pathlib.Path("wms.py").read_text(encoding="utf-8")
tree=ast.parse(source)
functions={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
selected=ast.Module(body=[functions["_ozon_fbo_units_for_shift"],functions["_with_market_details"]],type_ignores=[])
ast.fix_missing_locations(selected)
env={"now_iso":lambda:"2026-10-28T14:00:00+00:00"}
exec(compile(selected,"wms.py","exec"),env)


class CrossMarketSummaryTests(unittest.TestCase):
    def test_fbo_units_not_mixed_with_wb_orders(self):
        shift={"employee_id":"emp-1","started_at":"2026-10-28T06:00:00+00:00",
               "ended_at":None,"orders":275,"orders_per_hour":47.9}
        with patch.dict(env,{"query_all":lambda sql,params:[(120,28)]}):
            actual=env["_with_market_details"](shift)
        self.assertEqual(actual["wb_fbs_orders"],275)
        self.assertEqual(actual["ozon_fbo_units"],120)
        self.assertEqual(actual["ozon_fbo_scans"],28)
        self.assertEqual(actual["orders_per_hour"],47.9)

    def test_missing_fbo_table_returns_zero(self):
        shift={"employee_id":"emp-1","started_at":"2026-10-28T06:00:00+00:00",
               "ended_at":"2026-10-28T10:00:00+00:00","orders":0}
        def absent(*args):
            raise RuntimeError("no such table")
        with patch.dict(env,{"query_all":absent}):
            result=env["_with_market_details"](shift)
        self.assertEqual(result["ozon_fbo_units"],0)

    def test_query_scoped_to_employee_and_shift(self):
        shift={"employee_id":"person-a","started_at":"2026-10-28T06:00:00+00:00",
               "ended_at":"2026-10-28T10:00:00+00:00","orders":1}
        seen=[]
        def query(sql,params):
            seen.append((sql,params))
            return [(7,3)]
        with patch.dict(env,{"query_all":query}):
            env["_with_market_details"](shift)
        self.assertIn("voided_at IS NULL",seen[0][0])
        self.assertEqual(seen[0][1],("person-a","2026-10-28T06:00:00+00:00","2026-10-28T10:00:00+00:00"))


if __name__=="__main__":
    unittest.main()
