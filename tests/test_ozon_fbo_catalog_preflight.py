"""Synthetic Ozon FBO preflight tests; all MoySklad calls mocked."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal")
os.environ.setdefault("SECRET_KEY", "synthetic-key")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "preflight.sqlite")

from flask import Flask
from ozon_fbo import ozon_fbo_bp, _db
from ozon_fbo_catalog_validation import (
    CatalogUnavailable, check_catalog, decision_key, lookup_article,
    resolved_manifest, resolved_plan_hash,
)
from wms import query_all, query_one


def plan_with_mismatch():
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    return {
        "cargo": [
            {"barcode": a, "destination": "Москва", "label": "МСК 2", "qty": 50,
             "items": [{"article": "6794 НАБОР 6 GOLD", "barcode": "20570549844679",
                        "qty": 50, "source": "Москва!E2"}]},
            {"barcode": b, "destination": "Москва", "label": "МСК 3", "qty": 2,
             "items": [{"article": "VALID", "barcode": "4600000000001",
                        "qty": 2, "source": "Москва!I2"}]}
        ],
        "source_hash": str(uuid.uuid4()),
        "plan_hash": str(uuid.uuid4()),
        "filename": "test.xlsx", "warnings": [], "errors": [],
        "places": 2, "qty": 52, "unique_rows": 2,
        "source_rows": 2, "destinations": ["Москва"],
    }


def fake_lookup(article):
    return {"status": "found", "barcodes": {
        "6794 НАБОР 6 GOLD": ["2057054984679"],
        "VALID": ["4600000000001"],
    }[article]}


class CatalogValidationTests(unittest.TestCase):
    def test_wrong_barcode_is_rejected_until_manager_chooses_exact_candidate(self):
        plan = plan_with_mismatch()
        issues = check_catalog(plan, fake_lookup)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["sources"], ["Москва!E2"])
        self.assertEqual(issues[0]["excel_barcode"], "20570549844679")
        self.assertEqual(issues[0]["barcodes"], ["2057054984679"])
        with self.assertRaises(ValueError):
            resolved_manifest(plan, issues, {}, [])
        with self.assertRaises(ValueError):
            resolved_manifest(plan, issues, {issues[0]["key"]: "fake"}, [])
        result, audit = resolved_manifest(plan, issues,
             {issues[0]["key"]: {"article": "6794 НАБОР 6 GOLD", "barcode": "2057054984679"}}, [])
        self.assertEqual(result[0]["items"][0]["barcode"], "2057054984679")
        self.assertEqual(result[0]["items"][0]["article"], "6794 НАБОР 6 GOLD")
        self.assertEqual(plan["cargo"][0]["items"][0]["barcode"], "20570549844679")
        self.assertEqual(audit[0]["old"], "20570549844679")
        self.assertEqual(audit[0]["new"], "2057054984679")
        self.assertNotEqual(resolved_plan_hash(result), resolved_plan_hash(plan["cargo"]))

    def test_exclude_entire_bad_cargo_preserves_valid_cargo(self):
        plan = plan_with_mismatch()
        issues = check_catalog(plan, fake_lookup)
        result, audit = resolved_manifest(plan, issues, {}, [plan["cargo"][0]["barcode"]])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["barcode"], plan["cargo"][1]["barcode"])
        self.assertEqual(result[0]["qty"], 2)
        self.assertEqual(audit, [])
        with self.assertRaises(ValueError):
            resolved_manifest(plan, issues, {}, [c["barcode"] for c in plan["cargo"]])

    def test_unknown_article_or_ambiguous_article_cannot_be_auto_replaced(self):
        plan = plan_with_mismatch()
        issues = check_catalog(plan, lambda _: {"status": "missing", "barcodes": []})
        self.assertTrue(all(not x["barcodes"] for x in issues))
        with self.assertRaises(ValueError):
            resolved_manifest(plan, issues,
                {issues[0]["key"]: {"article": "6794 НАБОР 6 GOLD", "barcode": "2057054984679"}}, [])
        with self.assertRaises(ValueError):
            resolved_manifest(plan, issues, {}, [])

    def test_moysklad_unavailable_fails_closed_without_network(self):
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": ""}):
            with self.assertRaises(CatalogUnavailable):
                lookup_article("6794 НАБОР 6 GOLD")
        with self.assertRaises(CatalogUnavailable):
            check_catalog(plan_with_mismatch(),
                          lambda _: (_ for _ in ()).throw(CatalogUnavailable("offline")))

    def test_mocked_moysklad_exact_article_and_barcodes(self):
        class Response:
            status_code = 200
            ok = True
            def raise_for_status(self):
                pass
            def json(self):
                return {"rows": [{"article": "OTHER", "barcodes": [{"ean13": "bad"}]},
                                 {"article": "6794 НАБОР 6 GOLD",
                                  "barcodes": [{"ean13": "2057054984679"}]}]}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}):
            with patch("ozon_fbo_catalog_validation.requests.get", return_value=Response()) as get:
                result = lookup_article("6794 НАБОР 6 GOLD")
        self.assertEqual(result, {"status": "found", "barcodes": ["2057054984679"]})
        self.assertIn("article=6794 НАБОР 6 GOLD", get.call_args.kwargs["params"]["filter"])


class SelectedProductVerificationTests(unittest.TestCase):
    def test_selected_card_is_verified_by_id_and_barcode(self):
        from ozon_fbo_catalog_validation import resolve_catalog_choices

        class Response:
            ok = True
            status_code = 200
            def json(self):
                return {"id": "product-uuid", "article": "ACTUAL-SKU",
                        "barcodes": [{"ean13": "4601234567890"}]}

        issues = [{"key": "issue-key"}]
        supplied = {"issue-key": {"id": "product-uuid", "article": "FORGED-SKU",
                                    "barcode": "4601234567890"}}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \\
             patch("ozon_fbo_catalog_validation.requests.get", return_value=Response()) as get:
            choices = resolve_catalog_choices(supplied, issues)
        self.assertEqual(choices, {"issue-key": {
            "article": "ACTUAL-SKU", "barcode": "4601234567890"}})
        self.assertIn("/entity/product/product-uuid", get.call_args.args[0])

    def test_unverified_barcode_is_rejected(self):
        from ozon_fbo_catalog_validation import resolve_catalog_choices

        class Response:
            ok = True
            status_code = 200
            def json(self):
                return {"id": "product-uuid", "article": "ACTUAL-SKU",
                        "barcodes": [{"ean13": "4601234567890"}]}

        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \\
             patch("ozon_fbo_catalog_validation.requests.get", return_value=Response()):
            with self.assertRaises(ValueError):
                resolve_catalog_choices({"key": {"id": "product-uuid",
                    "barcode": "999999"}}, [{"key": "key"}])


class ImportEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.secret_key = "synthetic-flask-secret"
        cls.app.register_blueprint(ozon_fbo_bp)
        cls.app.testing = True
        _db()

    def setUp(self):
        self.client = self.app.test_client()
        self.plan = plan_with_mismatch()
        self.issues = check_catalog(self.plan, fake_lookup)
        with self.client.session_transaction() as session:
            session["manager_web"] = True

    def _request(self, decisions=None, excluded=None, plan=None):
        plan = plan or self.plan
        import json
        fields = {
            "title": "Синтетическая поставка",
            "source_hash": plan["source_hash"],
            "plan_hash": plan["plan_hash"],
            "catalog_decisions": json.dumps(decisions or {}),
            "excluded_cargo": json.dumps(excluded or []),
        }
        with patch("ozon_fbo._manifest_from_request", return_value=plan), \
             patch("ozon_fbo.check_catalog", return_value=self.issues), \
             patch("ozon_fbo.resolve_catalog_choices", side_effect=lambda decisions, issues: {k: {"article": "6794 НАБОР 6 GOLD", "barcode": "2057054984679"} for k, v in decisions.items() if isinstance(v, dict) and v.get("id") == "product-test-id"}):
            return self.client.post("/api/manager/ozon-fbo/import", data=fields)

    def test_import_rejects_unresolved_and_wrong_barcode(self):
        self.assertEqual(self._request().status_code, 409)
        self.assertEqual(self._request({self.issues[0]["key"]: "not-in-catalog"}).status_code, 409)

    def test_import_records_correction_without_mutating_source(self):
        response = self._request({self.issues[0]["key"]: {"id": "product-test-id", "article": "6794 НАБОР 6 GOLD", "barcode": "2057054984679"}})
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["corrections"], 1)
        sid = response.json["id"]
        saved = query_all("""
            SELECT i.article,i.barcode,i.source_cell FROM ozon_fbo_items i
            JOIN ozon_fbo_cargo c ON c.id=i.cargo_id
            WHERE c.supply_id=? ORDER BY article
        """, (sid,))
        self.assertIn(("6794 НАБОР 6 GOLD", "2057054984679", "Москва!E2"), saved)
        audit = query_one("""SELECT old_barcode,new_barcode,actor
            FROM ozon_fbo_import_corrections WHERE supply_id=?""", (sid,))
        self.assertEqual(audit[:2], ("20570549844679", "2057054984679"))
        self.assertEqual(self.plan["cargo"][0]["items"][0]["barcode"], "20570549844679")

    def test_import_excludes_entire_bad_cargo_and_recalculates_totals(self):
        response = self._request(excluded=[self.plan["cargo"][0]["barcode"]])
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["places"], 1)
        self.assertEqual(response.json["qty"], 2)
        self.assertEqual(response.json["excluded"], 1)

    def test_preview_and_import_fail_closed_if_moysklad_offline(self):
        with patch("ozon_fbo._manifest_from_request", return_value=self.plan), \
             patch("ozon_fbo.check_catalog", side_effect=CatalogUnavailable("offline")):
            response = self.client.post("/api/manager/ozon-fbo/preview")
            self.assertEqual(response.status_code, 503)
            response = self.client.post("/api/manager/ozon-fbo/import", data={
                "title": "Синтетическая поставка", "source_hash": self.plan["source_hash"],
                "plan_hash": self.plan["plan_hash"]
            })
            self.assertEqual(response.status_code, 503)

    def test_unauthorized_cannot_query_or_import(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.post("/api/manager/ozon-fbo/preview").status_code, 403)
        self.assertEqual(self.client.post("/api/manager/ozon-fbo/import").status_code, 403)


if __name__ == "__main__":
    unittest.main()
