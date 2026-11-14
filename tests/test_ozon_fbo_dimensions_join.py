"""Read-only dimension import and virtual join tests using synthetic spreadsheets."""
from io import BytesIO
from unittest.mock import patch
import unittest

from flask import Flask
from openpyxl import Workbook
from ozon_fbo_dimensions import read_dimensions_xlsx, parse_synthetic_basket, join_virtual_basket
from ozon_fbo_route_lab import route_lab_bp


def fake_book(conflict=False):
    book=Workbook()
    dim=book.active;dim.title="Габариты"
    dim.append(["Артикул","Длина ","Ширина ","Высота","Вес","ВПП"])
    dim.append(["TEST-L",50,40,35,"4,2","-"])
    dim.append(["TEST-S",8,8,22,"0,5","-"])
    dim.append(["TEST-NO-DIMS",None,None,None,"0,2","-"])
    if conflict:
        dim.append(["TEST-L",20,30,30,"1","-"])
    summary=book.create_sheet("Сводный")
    summary.append(["Пояснение"])
    summary.append(["Мой артикул","Баркод"])
    summary.append(["TEST-L","2098765432100"])
    summary.append(["TEST-S","2012345678901"])
    target=BytesIO();book.save(target);book.close()
    return target.getvalue()


class DimensionsJoinTests(unittest.TestCase):
    def test_parses_exact_workbook_and_preserves_missing_values(self):
        result=read_dimensions_xlsx(fake_book())
        self.assertEqual(len(result["items"]),2)
        self.assertEqual(len(result["incomplete"]),1)
        self.assertEqual(result["items"]["test-l"]["weight_kg"],4.2)
        self.assertEqual(result["barcodes"]["test-l"],["2098765432100"])
        self.assertNotIn("test-no-dims",result["items"])

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError,"Конфликт габаритов"):
            read_dimensions_xlsx(fake_book(conflict=True))

    def test_no_guessed_addresses_and_ambiguous_mapping(self):
        cat=read_dimensions_xlsx(fake_book())
        matches,issues=join_virtual_basket(cat,[("TEST-L",2),("TEST-S",1)],
            [("TEST-L","А-04")])
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]["location"],"А-04")
        self.assertEqual(issues[0]["reason"],"Ячейка на карте не привязана")
        _,issues=join_virtual_basket(cat,[("TEST-L",1)],
            [("TEST-L","А-04"),("TEST-L","Б-01")])
        self.assertEqual(issues[0]["reason"],"Артикул привязан к нескольким ячейкам")

    def test_synthetic_basket_and_invalid_entries(self):
        self.assertEqual(parse_synthetic_basket("TEST-L;2\nTEST-S;1"),[("TEST-L",2),("TEST-S",1)])
        for value in ("TEST-L;0","TEST-L;1\nTEST-L;2","TEST-L","TEST-L;nope"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_synthetic_basket(value)

    def test_manager_preview_uses_mock_map_no_real_db_or_network(self):
        app=Flask(__name__);app.secret_key="test-dims"
        app.testing=True;app.register_blueprint(route_lab_bp)
        client=app.test_client()
        def data():
            return {"catalog":(BytesIO(fake_book()),"synthetic.xlsx"),
                    "basket":"TEST-L;2\nTEST-S;1","mode":"hybrid"}
        self.assertEqual(client.post("/api/manager/ozon-fbo/route-lab/preview",data=data()).status_code,403)
        with client.session_transaction() as sess:
            sess["manager_web"]=True
        with patch("ozon_fbo_route_lab._map_assignments_for_preview",
                   return_value=[("TEST-L","А-04"),("TEST-S","Ж-12")]) as fake_map:
            result=client.post("/api/manager/ozon-fbo/route-lab/preview",data=data(),
                               content_type="multipart/form-data")
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json["requested"],2)
        self.assertEqual(result.json["matched"],2)
        self.assertEqual(result.json["catalog_incomplete"],1)
        self.assertEqual([i["article"] for i in result.json["route"]["items"]],["TEST-L","TEST-S"])
        self.assertTrue(result.json["synthetic_basket"])
        self.assertFalse(result.json["saved"])
        fake_map.assert_called_once()

    def test_manager_page_does_not_mutate_jobs(self):
        app=Flask(__name__);app.secret_key="test-dims"
        app.register_blueprint(route_lab_bp);client=app.test_client()
        with client.session_transaction() as sess:sess["manager_web"]=True
        page=client.get("/manager/ozon-fbo/route-lab").get_data(as_text=True)
        self.assertIn("Синхронизировать Google Sheets",page)
        self.assertIn("Синтетическая палета",page)
        self.assertIn("#09121e",page)


if __name__=="__main__":
    unittest.main()
