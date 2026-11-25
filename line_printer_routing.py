"""Offline, fail-closed line-to-printer routing for GRIT WMS (#192).

Phase 1 is a *decision engine*, not a print queue or a production endpoint.
Callers must verify employee badge, printer QR and current catalog mappings
separately. No database access, outbound network, writes or actual printing.
"""
from dataclasses import dataclass
from typing import Mapping, Sequence

from warehouse_map import line_of


@dataclass(frozen=True)
class RouteDecision:
    accepted: bool
    reason: str
    order_id: str
    printer_id: str
    lines: tuple[str, ...] = ()
    missing_products: tuple[str, ...] = ()


# WB status is NOT a substitute for the local GRIT picking status.
_ALLOWED_WB_STATUS = frozenset({"confirm"})
_ALLOWED_LOCAL_STATUS = frozenset({"pending"})


def _text(value):
    return str(value or "").strip()


def _normalized_line(value):
    """Exact warehouse_map semantics; reject unrecognized addresses."""
    line = line_of(value)
    return line if line != "ПРОЧЕЕ" else None


def resolve_lines_for_product(product, locations):
    """Return (reason, lines) using verified, active product-cell assignments.

    locations is a sequence of records: {product_id, label, active, confirmed}.
    A product may occupy multiple cells *on the same line*. If present on
    different lines, routing is ambiguous and must be resolved by a manager.
    A stale/unverified location is never eligible for routing.
    """
    product = _text(product)
    if not product:
        return "PRODUCT_MISSING", ()
    matches = [r for r in locations if _text(r.get("product_id")) == product]
    if not matches:
        return "PRODUCT_UNMAPPED", ()
    valid = []
    for record in matches:
        if record.get("active") is not True or record.get("confirmed") is not True:
            continue
        line = _normalized_line(record.get("label"))
        if line:
            valid.append(line)
    if not valid:
        return "PRODUCT_LOCATION_UNVERIFIED", ()
    lines = tuple(sorted(set(valid)))
    if len(lines) > 1:
        return "PRODUCT_MULTIPLE_LINES", lines
    return "OK", lines


def route_order_to_printer(
    order: Mapping,
    printer: Mapping,
    locations: Sequence[Mapping],
    reservations: Mapping[str, str] | None = None,
):
    """Evaluate a requested printer. Never create reservations or print.

    order: {order_id, wb_status, local_status, products:[product_id,...]}.
    printer: {printer_id, enabled, lines:[line,...]}.
    reservations: existing order_id->printer_id assignments from authoritative
    caller storage; concurrency MUST be guarded transactionally by the caller.
    """
    order_id = _text(order.get("order_id"))
    printer_id = _text(printer.get("printer_id"))
    def reject(reason, lines=(), missing=()):
        return RouteDecision(False, reason, order_id, printer_id,
                             tuple(lines), tuple(missing))

    if not order_id or not printer_id:
        return reject("IDENTITY_MISSING")
    if printer.get("enabled") is not True:
        return reject("PRINTER_DISABLED")
    if _text(order.get("wb_status")).lower() not in _ALLOWED_WB_STATUS:
        return reject("WB_STATUS_NOT_PICKABLE")
    if _text(order.get("local_status")).lower() not in _ALLOWED_LOCAL_STATUS:
        return reject("ORDER_NOT_PENDING")

    products = order.get("products")
    if not isinstance(products, (list, tuple)) or not products:
        return reject("ORDER_PRODUCTS_MISSING")
    product_ids = tuple(_text(p) for p in products)
    if any(not p for p in product_ids):
        return reject("ORDER_PRODUCTS_MISSING")

    requested = printer.get("lines")
    if not isinstance(requested, (list, tuple, set, frozenset)) or not requested:
        return reject("PRINTER_LINES_MISSING")
    printer_lines = set()
    for raw in requested:
        # Printer configuration stores line codes, never full cell addresses.
        line = _text(raw).upper()
        if not line or line == "ПРОЧЕЕ" or line_of(line + "-01") != line:
            return reject("PRINTER_LINES_INVALID")
        printer_lines.add(line)

    required = set()
    for product in dict.fromkeys(product_ids):
        reason, lines = resolve_lines_for_product(product, locations)
        if reason != "OK":
            return reject(reason, sorted(required | set(lines)), [product])
        required.update(lines)
    if not required.issubset(printer_lines):
        return reject("PRINTER_DOES_NOT_COVER_ORDER", sorted(required))

    if reservations is not None:
        assigned_to = _text(reservations.get(order_id))
        if assigned_to and assigned_to != printer_id:
            return reject("ORDER_RESERVED_ELSEWHERE", sorted(required))
        if assigned_to == printer_id:
            return reject("ORDER_ALREADY_RESERVED", sorted(required))

    return RouteDecision(True, "ELIGIBLE_NOT_RESERVED", order_id, printer_id,
                         tuple(sorted(required)))
