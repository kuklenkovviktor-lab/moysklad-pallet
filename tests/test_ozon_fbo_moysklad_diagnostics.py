"""Only mocked requests, no real MoySklad data."""
import os
import unittest
from unittest.mock import Mock, patch
import requests

from ozon_fbo_catalog_validation import lookup_article, CatalogUnavailable


class OzonFboMoySkladDiagnosticsTests(unittest.TestCase):
    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_valid_product(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"rows":[{"article":"TEST-1","barcodes":[{"ean13":"1234567890123"}]}]}
        with patch("ozon_fbo_catalog_validation.requests.get", return_value=response):
            result = lookup_article("TEST-1")
        self.assertEqual(result, {"status":"found","barcodes":["1234567890123"]})

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_http_error_contains_code_and_article_but_not_token(self):
        response=Mock(status_code=403,ok=False)
        with patch("ozon_fbo_catalog_validation.requests.get", return_value=response):
            with self.assertRaises(CatalogUnavailable) as raised:
                lookup_article("SKU-403")
        self.assertIn("HTTP 403",str(raised.exception))
        self.assertIn("SKU-403",str(raised.exception))
        self.assertNotIn("SYNTHETIC-TOKEN",str(raised.exception))

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_429_retries_with_limit(self):
        blocked=Mock(status_code=429,ok=False)
        success=Mock(status_code=200,ok=True)
        success.json.return_value={"rows":[]}
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[blocked,success]) as get, patch("ozon_fbo_catalog_validation.time.sleep"):
            result=lookup_article("SKU")
        self.assertEqual(get.call_count,2)
        self.assertEqual(result["status"],"missing")

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_article_filter_400_falls_back_to_exact_search(self):
        rejected=Mock(status_code=400,ok=False)
        found=Mock(status_code=200,ok=True)
        found.json.return_value={"rows":[
            {"article":"1022_BOROCCO_EXTRA","barcodes":[{"ean13":"0000000000001"}]},
            {"article":"1022_BOROCCO","barcodes":[{"ean13":"1234567890123"}]},
        ]}
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,found]) as get:
            result=lookup_article("1022_BOROCCO")
        self.assertEqual(result,{"status":"found","barcodes":["1234567890123"]})
        self.assertEqual(get.call_args_list[0].kwargs["params"]["filter"],"article=1022_BOROCCO")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["search"],"1022_BOROCCO")

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_fallback_fuzzy_only_does_not_pass_catalog_validation(self):
        rejected=Mock(status_code=400,ok=False)
        fuzzy=Mock(status_code=200,ok=True)
        fuzzy.json.return_value={"rows":[{"article":"1022_BOROCCO_EXTRA",
                                          "barcodes":[{"ean13":"1234567890123"}]}]}
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,fuzzy]):
            result=lookup_article("1022_BOROCCO")
        self.assertEqual(result,{"status":"missing","barcodes":[]})

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_fallback_error_exposes_only_codes_not_credentials(self):
        rejected=Mock(status_code=400,ok=False)
        fallback=Mock(status_code=403,ok=False)
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,fallback]):
            with self.assertRaises(CatalogUnavailable) as raised:
                lookup_article("1022_BOROCCO")
        self.assertIn("резервный поиск HTTP 403",str(raised.exception))
        self.assertNotIn("SYNTHETIC-TOKEN",str(raised.exception))

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_fallback_finds_exact_article_on_second_page(self):
        rejected = Mock(status_code=400, ok=False)
        first = Mock(status_code=200, ok=True)
        first.json.return_value = {"rows": [
            {"article": "SKU-" + str(i), "barcodes": []} for i in range(100)
        ], "meta": {"size": 101}}
        second = Mock(status_code=200, ok=True)
        second.json.return_value = {"rows": [
            {"article": "TARGET", "barcodes": [{"ean13": "1234567890123"}]}
        ], "meta": {"size": 101}}
        with patch("ozon_fbo_catalog_validation.requests.get",
                   side_effect=[rejected, first, second]) as get:
            result = lookup_article("TARGET")
        self.assertEqual(result, {"status": "found", "barcodes": ["1234567890123"]})
        self.assertEqual(get.call_args_list[2].kwargs["params"]["offset"], 100)

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_incomplete_search_never_reports_missing(self):
        rejected = Mock(status_code=400, ok=False)
        full = Mock(status_code=200, ok=True)
        full.json.return_value = {"rows": [
            {"article": "OTHER-" + str(i), "barcodes": []} for i in range(100)
        ], "meta": {"size": 1500}}
        with patch("ozon_fbo_catalog_validation.requests.get",
                   side_effect=[rejected] + [full] * 10) as get:
            with self.assertRaisesRegex(CatalogUnavailable, "слишком много результатов"):
                lookup_article("TARGET")
        self.assertEqual(get.call_count, 11)

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_paginated_duplicate_exact_articles_are_ambiguous(self):
        first = Mock(status_code=200, ok=True)
        first.json.return_value = {"rows": [
            {"article": "TARGET", "barcodes": [{"ean13": "1234567890123"}]}
        ] + [{"article": "OTHER-" + str(i)} for i in range(99)],
            "meta": {"size": 101}}
        second = Mock(status_code=200, ok=True)
        second.json.return_value = {"rows": [
            {"article": "TARGET", "barcodes": [{"ean13": "9999999999999"}]}
        ], "meta": {"size": 101}}
        with patch("ozon_fbo_catalog_validation.requests.get",
                   side_effect=[first, second]):
            self.assertEqual(lookup_article("TARGET"),
                             {"status": "ambiguous", "barcodes": []})

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_timeout_error_after_bounded_retries(self):
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=requests.Timeout), patch("ozon_fbo_catalog_validation.time.sleep"):
            with self.assertRaisesRegex(CatalogUnavailable,"тайм-аут") as raised:
                lookup_article("SKU-TIMEOUT")
        self.assertIn("SKU-TIMEOUT",str(raised.exception))


if __name__ == "__main__":
    unittest.main()
