"""Synthetic bundle catalog tests: no live MoySklad or production PostgreSQL."""
import os
import tempfile
import unittest
from unittest.mock import patch

_temp = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal")
os.environ.setdefault("SECRET_KEY", "synthetic-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_temp.name, "bundles.sqlite")

from fbs_catalog import (init_fbs_catalog_db, save_fbs_catalog_snapshot,
                         ozon_catalog_snapshot, search_ozon_catalog_snapshot)
from bundle_catalog import (persist_bundle_snapshot, recent_bundles,
                            refresh_bundle_catalog)
from ozon_fbo_catalog_validation import (resolve_catalog_choices,
                                         _snapshot_article_result, CatalogUnavailable)


class BundleCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_fbs_catalog_db()

    def setUp(self):
        save_fbs_catalog_snapshot(
            {"2038693836680": {"id": "unit-1", "article": "БАНКА_240_ОВАЛ_1шт",
                                "name": "Банка один"}},
            {},
        )
        persist_bundle_snapshot([])

    def _bundle(self):
        return {"id": "bundle-4", "name": "Банка четыре",
                "article": "БАНКА_240_ОВАЛ_4шт",
                "code": "2038693831791", "barcodes": []}

    def test_bundle_code_is_not_silent_product_barcode(self):
        persist_bundle_snapshot([self._bundle()])
        entries = recent_bundles()
        self.assertEqual(entries[0]["code"], "2038693831791")
        self.assertEqual(entries[0]["barcodes"], [])
        snapshot = ozon_catalog_snapshot()
        self.assertEqual(snapshot["банка_240_овал_4шт"]["bundle:bundle-4"]["barcodes"], set())
        result = _snapshot_article_result(snapshot, "БАНКА_240_ОВАЛ_4шт")
        self.assertEqual(result["status"], "no_barcodes")
        searched = search_ozon_catalog_snapshot("2038693831791")
        self.assertEqual(searched[0]["entity_type"], "bundle")
        self.assertEqual(searched[0]["code"], "2038693831791")

    def test_bundle_and_unit_article_not_automatically_equivalent(self):
        persist_bundle_snapshot([self._bundle()])
        snapshot = ozon_catalog_snapshot()
        self.assertIn("банка_240_овал_1шт", snapshot)
        self.assertIn("банка_240_овал_4шт", snapshot)

    def test_validated_bundle_code_routes_to_correct_entity(self):
        persist_bundle_snapshot([self._bundle()])

        class Response:
            status_code = 200
            ok = True
            headers = {}
            def json(self):
                return {"id": "bundle-4", "article": "БАНКА_240_ОВАЛ_4шт",
                        "code": "2038693831791", "barcodes": []}
        choice = {"issue": {"id": "bundle-4", "entity_type": "bundle",
                 "identifier_type": "code", "barcode": "2038693831791"}}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("ozon_fbo_catalog_validation.requests.get",
                   return_value=Response()) as get:
            result = resolve_catalog_choices(choice, [{"key": "issue"}])
        self.assertEqual(result["issue"]["barcode"], "2038693831791")
        self.assertIn("/entity/bundle/bundle-4", get.call_args.args[0])

    def test_bundle_code_rejects_wrong_value(self):
        class Response:
            status_code = 200
            ok = True
            headers = {}
            def json(self):
                return {"id": "bundle-4", "article": "БАНКА_240_ОВАЛ_4шт",
                        "code": "2038693831791", "barcodes": []}
        choice = {"issue": {"id": "bundle-4", "entity_type": "bundle",
                 "identifier_type": "code", "barcode": "WRONG"}}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("ozon_fbo_catalog_validation.requests.get",
                   return_value=Response()):
            with self.assertRaisesRegex(ValueError, "не подтверждены"):
                resolve_catalog_choices(choice, [{"key": "issue"}])

    def test_bundle_search_uses_bundle_endpoint_when_local_cache_misses(self):
        from ozon_fbo_catalog_validation import search_catalog_products

        class Response:
            status_code = 200
            ok = True
            headers = {}
            def json(self):
                return {"rows": [{
                    "id": "bundle-4", "name": "Банка четыре",
                    "article": "БАНКА_240_ОВАЛ_4шт",
                    "code": "2038693831791", "barcodes": []
                }]}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("ozon_fbo_catalog_validation.requests.get",
                   return_value=Response()) as get:
            products = search_catalog_products("БАНКА_240_ОВАЛ_4шт")
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["entity_type"], "bundle")
        self.assertEqual(products[0]["code"], "2038693831791")
        self.assertIn("/entity/bundle", get.call_args.args[0])

    def test_failed_bundle_refresh_preserves_prior_snapshot(self):
        persist_bundle_snapshot([self._bundle()])
        class Response:
            ok = False
            status_code = 403
            headers = {}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("ozon_fbo_catalog_validation.requests.get",
                   return_value=Response()):
            with self.assertRaises(CatalogUnavailable):
                refresh_bundle_catalog()
        self.assertEqual(recent_bundles()[0]["article"], "БАНКА_240_ОВАЛ_4шт")

    def test_paginated_bundle_refresh(self):
        first = [dict(self._bundle(), id="bundle-" + str(i)) for i in range(100)]
        class Response:
            ok = True
            status_code = 200
            headers = {}
            def __init__(self, rows): self.rows = rows
            def json(self): return {"rows": self.rows}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("ozon_fbo_catalog_validation.requests.get",
                   side_effect=[Response(first), Response([self._bundle()])]) as get:
            count = refresh_bundle_catalog()
        self.assertEqual(count, 101)
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
