"""Only mocked requests, no real MoySklad data."""
import os
import unittest
from unittest.mock import Mock, patch
import requests

from ozon_fbo_catalog_validation import lookup_article, CatalogUnavailable


class OzonFboMoySkladDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.pacing = patch("ozon_fbo_catalog_validation._wait_for_catalog_slot")
        self.pacing.start()
        self.addCleanup(self.pacing.stop)

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_intermittent_400_recovers_on_next_attempt(self):
        rejected=Mock(status_code=400,ok=False)
        ok=Mock(status_code=200,ok=True)
        ok.json.return_value={"rows":[{"article":"1022_BOROCCO","barcodes":[{"ean13":"1234567890123"}]}]}
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,ok]) as get, patch("ozon_fbo_catalog_validation.time.sleep"):
            result=lookup_article("1022_BOROCCO")
        self.assertEqual(result["status"],"found")
        self.assertEqual(get.call_count,2)

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
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,rejected,rejected,found]) as get:
            result=lookup_article("1022_BOROCCO")
        self.assertEqual(result,{"status":"found","barcodes":["1234567890123"]})
        self.assertEqual(get.call_args_list[0].kwargs["params"]["filter"],"article=1022_BOROCCO")
        self.assertEqual(get.call_args_list[3].kwargs["params"]["search"],"1022_BOROCCO")

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_fallback_fuzzy_only_does_not_pass_catalog_validation(self):
        rejected=Mock(status_code=400,ok=False)
        fuzzy=Mock(status_code=200,ok=True)
        fuzzy.json.return_value={"rows":[{"article":"1022_BOROCCO_EXTRA",
                                          "barcodes":[{"ean13":"1234567890123"}]}]}
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,rejected,rejected,fuzzy]):
            result=lookup_article("1022_BOROCCO")
        self.assertEqual(result,{"status":"missing","barcodes":[]})

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_fallback_error_exposes_only_codes_not_credentials(self):
        rejected=Mock(status_code=400,ok=False)
        fallback=Mock(status_code=403,ok=False)
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=[rejected,rejected,rejected,fallback]):
            with self.assertRaises(CatalogUnavailable) as raised:
                lookup_article("1022_BOROCCO")
        self.assertIn("резервный поиск HTTP 403",str(raised.exception))
        self.assertNotIn("SYNTHETIC-TOKEN",str(raised.exception))

    @patch.dict(os.environ, {"MOYSKLAD_TOKEN": "SYNTHETIC-TOKEN"})
    def test_timeout_error_after_bounded_retries(self):
        with patch("ozon_fbo_catalog_validation.requests.get",side_effect=requests.Timeout), patch("ozon_fbo_catalog_validation.time.sleep"):
            with self.assertRaisesRegex(CatalogUnavailable,"тайм-аут") as raised:
                lookup_article("SKU-TIMEOUT")
        self.assertIn("SKU-TIMEOUT",str(raised.exception))


if __name__ == "__main__":
    unittest.main()
