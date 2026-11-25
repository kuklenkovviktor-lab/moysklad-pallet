"""Synthetic, offline tests for the printer-zone decision engine."""
import unittest

from line_printer_routing import resolve_lines_for_product, route_order_to_printer


class LinePrinterRoutingTests(unittest.TestCase):
    def setUp(self):
        self.order = {
            "order_id": "SYNTH-ORDER-1",
            "wb_status": "confirm",
            "local_status": "pending",
            "products": ["SKU-A", "SKU-B"],
        }
        self.printer = {
            "printer_id": "TEST-PRINTER-A-B", "enabled": True,
            "lines": ["А", "Б"],
        }
        self.locations = [
            {"product_id": "SKU-A", "label": "А-05-1", "active": True, "confirmed": True},
            {"product_id": "SKU-B", "label": "Б-08-2", "active": True, "confirmed": True},
        ]

    def route(self, **kwargs):
        order = dict(self.order, **kwargs)
        return route_order_to_printer(order, self.printer, self.locations)

    def test_all_order_lines_must_be_covered(self):
        decision = self.route()
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "ELIGIBLE_NOT_RESERVED")
        self.assertEqual(decision.lines, ("А", "Б"))

    def test_partial_coverage_is_rejected(self):
        printer = dict(self.printer, lines=["А"])
        decision = route_order_to_printer(self.order, printer, self.locations)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "PRINTER_DOES_NOT_COVER_ORDER")

    def test_multiple_cells_in_same_line_is_unambiguous(self):
        locations = self.locations + [
            {"product_id": "SKU-A", "label": "А-06-2", "active": True, "confirmed": True}
        ]
        self.assertEqual(resolve_lines_for_product("SKU-A", locations), ("OK", ("А",)))

    def test_product_mapped_to_multiple_lines_is_blocked(self):
        locations = self.locations + [
            {"product_id": "SKU-A", "label": "В-01", "active": True, "confirmed": True}
        ]
        result = route_order_to_printer(self.order, self.printer, locations)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "PRODUCT_MULTIPLE_LINES")

    def test_product_not_mapped_is_blocked(self):
        result = self.route(products=["SKU-A", "SKU-UNKNOWN"])
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "PRODUCT_UNMAPPED")
        self.assertEqual(result.missing_products, ("SKU-UNKNOWN",))

    def test_inactive_and_unconfirmed_locations_do_not_route(self):
        for updates in ({"active": False}, {"confirmed": False}, {"label": "РЕЗЕРВ"}):
            with self.subTest(updates=updates):
                bad = [dict(self.locations[0], **updates), self.locations[1]]
                result = route_order_to_printer(self.order, self.printer, bad)
                self.assertFalse(result.accepted)
                self.assertEqual(result.reason, "PRODUCT_LOCATION_UNVERIFIED")

    def test_cancelled_new_complete_or_locally_finished_order_is_rejected(self):
        for status in ("cancel", "new", "complete", "sold"):
            with self.subTest(status=status):
                self.assertEqual(self.route(wb_status=status).reason,
                                 "WB_STATUS_NOT_PICKABLE")
        self.assertEqual(self.route(local_status="complete").reason,
                         "ORDER_NOT_PENDING")

    def test_printer_disabled_or_bad_line_configuration_is_rejected(self):
        for overrides, reason in [
            ({"enabled": False}, "PRINTER_DISABLED"),
            ({"lines": []}, "PRINTER_LINES_MISSING"),
            ({"lines": ["ПРОЧЕЕ"]}, "PRINTER_LINES_INVALID"),
        ]:
            with self.subTest(overrides=overrides):
                decision = route_order_to_printer(
                    self.order, dict(self.printer, **overrides), self.locations)
                self.assertEqual(decision.reason, reason)
                self.assertFalse(decision.accepted)

    def test_existing_reservations_are_not_released_or_reused(self):
        for assigned, reason in [
            ("OTHER-PRINTER", "ORDER_RESERVED_ELSEWHERE"),
            ("TEST-PRINTER-A-B", "ORDER_ALREADY_RESERVED"),
        ]:
            with self.subTest(assigned=assigned):
                reservations = {"SYNTH-ORDER-1": assigned}
                decision = route_order_to_printer(
                    self.order, self.printer, self.locations, reservations)
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.reason, reason)
                self.assertEqual(reservations["SYNTH-ORDER-1"], assigned)

    def test_no_products_or_missing_identity_are_rejected(self):
        self.assertEqual(self.route(products=[]).reason, "ORDER_PRODUCTS_MISSING")
        self.assertEqual(self.route(order_id="").reason, "IDENTITY_MISSING")

    def test_input_data_not_mutated(self):
        order = dict(self.order)
        locations = [dict(x) for x in self.locations]
        printer = dict(self.printer)
        route_order_to_printer(order, printer, locations)
        self.assertEqual(order, self.order)
        self.assertEqual(locations, self.locations)
        self.assertEqual(printer, self.printer)


if __name__ == "__main__":
    unittest.main()
