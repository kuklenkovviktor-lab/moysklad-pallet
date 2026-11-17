"""Synthetic Ozon FBO preflight tests; all MoySklad calls mocked."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal")
os.environ.setdefault("SECRET_KEY", "synthetic-key")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "preflight.sqlite")

from flask import Flask
from ozon_fbo import ozon_fbo_bp, _db, _details
from ozon_fbo_catalog_validation import (
    CatalogUnavailable, check_catalog, decision_key, lookup_article,
    resolved_manifest, resolved_plan_hash,
)
from wms import db_connect, query_all, query_one, sql


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
        article = issues[0]["article"]
        code = "2057054984679"
        with patch("ozon_fbo_catalog_validation.cards_by_barcode",
                   return_value=[{"id": "product", "article": article, "barcode": code}]):
            result, audit = resolved_manifest(plan, issues,
                 {issues[0]["key"]: {"barcode": code, "confirmed": True}}, [])
        self.assertEqual(result[0]["items"][0]["barcode"], "2057054984679")
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
                {issues[0]["key"]: "2057054984679"}, [])
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
             patch("ozon_fbo.check_catalog", return_value=self.issues):
            return self.client.post("/api/manager/ozon-fbo/import", data=fields)

    def test_import_rejects_unresolved_and_wrong_barcode(self):
        self.assertEqual(self._request().status_code, 409)
        self.assertEqual(self._request({self.issues[0]["key"]: "not-in-catalog"}).status_code, 409)

    def test_import_records_correction_without_mutating_source(self):
        code = "2057054984679"
        with patch("ozon_fbo_catalog_validation.cards_by_barcode", return_value=[
                {"id": "product", "article": self.issues[0]["article"], "barcode": code}]):
            response = self._request({self.issues[0]["key"]: {"barcode": code, "confirmed": True}})
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
        # Previous releases wrote only the original correction row.
        with db_connect() as conn:
            conn.cursor().execute(sql("""DELETE FROM ozon_fbo_correction_products WHERE correction_id IN
                (SELECT id FROM ozon_fbo_import_corrections WHERE supply_id=?)"""), (sid,))
            conn.commit()
        history = _details(sid)["corrections"][0]
        self.assertEqual(history["new_article"], history["article"])

    def test_import_excludes_entire_bad_cargo_and_recalculates_totals(self):
        response = self._request(excluded=[self.plan["cargo"][0]["barcode"]])
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json["places"], 1)
        self.assertEqual(response.json["qty"], 2)
        self.assertEqual(response.json["excluded"], 1)

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"})
    @patch("ozon_fbo_catalog_validation._wait_for_catalog_slot")
    def test_preview_and_import_three_confirmed_pairs_keep_12_places_and_4587_units(self, pacing):
        import json
        originals = [("A-old", "001"), ("B", "002-old"), ("C-old", "003-old")]
        products = [{"id": a, "article": a, "barcodes": [{"code128": code}]}
                    for a, code in [("A", "001"), ("B", "002"), ("C", "003")]]
        products.append({"id": "A2", "article": "A2", "barcodes": [{"code128": "001"}]})
        cargo = []
        for n in range(12):
            a, code = originals[n % 3]
            qty = 387 if n == 0 else 0
            qty += 350
            cargo.append({"barcode": f"cargo-{n}", "destination": "Тест", "label": str(n),
                          "qty": qty, "items": [{"article": a, "barcode": code,
                                                "qty": qty, "source": f"Тест!E{n+1}"}]})
        self.plan.update(cargo=cargo, places=12, qty=4587, unique_rows=12, source_rows=12)
        calls = []

        def read(url, *, params, **kwargs):
            self.assertTrue(url.endswith("/entity/product"))
            calls.append(params["filter"])
            # Include unrelated rows to exercise exact client-side filtering.
            return Mock(status_code=200, json=lambda: {"rows": products})

        fields = dict(title="Исправленная поставка", source_hash=self.plan["source_hash"],
                      plan_hash=self.plan["plan_hash"])
        endpoint = "/api/manager/ozon-fbo/import"
        with patch("ozon_fbo._manifest_from_request", return_value=self.plan), \
             patch("ozon_fbo_catalog_validation.requests.get", side_effect=read):
            preview = self.client.post("/api/manager/ozon-fbo/preview")
            self.assertEqual(preview.status_code, 200, preview.json)
            issues = {i["article"]: i for i in preview.json["discrepancies"]}
            self.assertEqual([c["id"] for c in issues["A-old"]["cards"]], ["A", "A2"])
            choices = {issues["A-old"]["key"]: {"article": "A", "id": "A", "confirmed": True},
                       issues["B"]["key"]: {"barcode": "002", "confirmed": True},
                       issues["C-old"]["key"]: {"article": "C", "barcode": "003", "confirmed": True}}
            fields["catalog_decisions"] = json.dumps(choices)
            removed = products.pop(2)
            self.assertEqual(self.client.post(endpoint, data=fields).status_code, 409)
            products.insert(2, removed)
            calls.clear()
            response = self.client.post(endpoint, data=fields)
            self.assertEqual(response.status_code, 201, response.json)
            self.assertEqual((response.json["places"], response.json["qty"]), (12, 4587))
            self.assertEqual((response.json["corrections"], response.json["excluded"]), (12, 0))
            self.assertCountEqual([c for c in calls if c.startswith("barcode=")],
                                  ["barcode=001", "barcode=002", "barcode=003"])
            self.assertEqual(self.client.post(endpoint, data=fields).status_code, 409)
        saved = query_all("""SELECT c.barcode,i.article,i.barcode,i.planned_qty FROM ozon_fbo_items i
            JOIN ozon_fbo_cargo c ON c.id=i.cargo_id WHERE c.supply_id=?""", (response.json["id"],))
        expected = [(p["barcode"], products[n%3]["article"], products[n%3]["barcodes"][0]["code128"],
                     p["qty"]) for n,p in enumerate(cargo)]
        self.assertCountEqual(saved, expected)
        audit = query_all("""SELECT c.article,c.old_barcode,p.new_article,c.new_barcode,
            c.source_cell,c.cargo_barcode,c.actor,c.created_at,p.product_id
            FROM ozon_fbo_import_corrections c JOIN ozon_fbo_correction_products p
            ON p.correction_id=c.id WHERE c.supply_id=?""", (response.json["id"],))
        self.assertEqual(len(audit), 12)
        for old, before, new, after, source, place, actor, at, product in audit:
            n = int(place.split("-")[1])
            self.assertEqual((old, before), originals[n%3])
            self.assertEqual((new, after), expected[n][1:3])
            self.assertEqual((source, product), (cargo[n]["items"][0]["source"], new))
            self.assertTrue(actor and at)

    def test_corrections_require_confirmation_exact_pair_and_unambiguous_card(self):
        key = self.issues[0]["key"]
        cards = [{"id": i, "article": "NEW", "barcode": "001"} for i in ("a", "b")]
        with patch("ozon_fbo_catalog_validation.cards_by_barcode", return_value=cards):
            choice = {"article": "NEW", "barcode": "001", "confirmed": True}
            for override in ({"id": "absent"}, {"confirmed": False}, {"confirmed": "true"},
                             {"article": "OTHER"}, {}):
                self.assertEqual(self._request({key: {**choice, **override}}).status_code, 409)
            self.assertEqual(self._request({key: {**choice, "id": "a"}}).status_code, 201)

    def test_duplicate_and_cross_article_barcode_after_correction_block_import(self):
        cargo = self.plan["cargo"]
        valid = cargo[1]["items"][0]
        for article in ("OTHER", valid["article"]):
            with self.subTest(article=article):
                if article == valid["article"]:
                    cargo[0]["items"].append(valid.copy())
                card = {"article": article, "barcode": valid["barcode"], "id": "chosen"}
                with patch("ozon_fbo_catalog_validation.cards_by_barcode", return_value=[card]):
                    choice = {**card, "confirmed": True}
                    self.assertEqual(self._request({self.issues[0]["key"]: choice}).status_code, 409)

    def test_corrected_pair_api_failure_and_stale_plan_block_import(self):
        choice = {self.issues[0]["key"]: {"article": "NEW", "confirmed": True}}
        with patch("ozon_fbo_catalog_validation.cards_by_barcode", side_effect=CatalogUnavailable("offline")):
            self.assertEqual(self._request(choice).status_code, 503)
        with patch("ozon_fbo._manifest_from_request", return_value=self.plan):
            response = self.client.post("/api/manager/ozon-fbo/import", data={
                "title": "Тест", "source_hash": self.plan["source_hash"], "plan_hash": "obsolete"})
            self.assertEqual(response.status_code, 409)

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
        self.assertEqual(self.client.get("/api/manager/ozon-fbo/catalog-cards?barcode=001").status_code, 403)


if __name__ == "__main__":
    unittest.main()
