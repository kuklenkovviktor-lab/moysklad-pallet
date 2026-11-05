import unittest
from fbs_pdf_physical_test import prepare_test_orders


def report(code="59206990266", sku="SET-006", ean="2038719091772"):
    return {"valid_structure": True, "unique_sticker_barcodes": 1,
            "products": [{"article": sku, "product_barcode": ean,
                          "stickers": [{"barcode": code}]}]}


class PhysicalPdfTest(unittest.TestCase):
    def test_one_order(self):
        item = prepare_test_orders(report())[0]
        self.assertEqual(item["wb_barcode"], "59206990266")
        self.assertEqual(item["skus_json"], '["2038719091772"]')

    def test_invalid_pdf(self):
        data = report()
        data["valid_structure"] = False
        with self.assertRaises(ValueError):
            prepare_test_orders(data)

    def test_duplicate_sticker(self):
        data = report()
        data["products"][0]["stickers"].append({"barcode": "59206990266"})
        data["unique_sticker_barcodes"] = 2
        with self.assertRaises(ValueError):
            prepare_test_orders(data)

    def test_missing_item_barcode(self):
        with self.assertRaises(ValueError):
            prepare_test_orders(report(ean=""))

    def test_too_many_stickers(self):
        data = report()
        data["products"][0]["stickers"] = [
            {"barcode": str(59000000000+i)} for i in range(11)]
        data["unique_sticker_barcodes"] = 11
        with self.assertRaises(ValueError):
            prepare_test_orders(data)


if __name__ == "__main__":
    unittest.main()
