# GRIT printer printing module: synthetic-only stage

Backend separation: `services/printing/synthetic_queue.py` is the isolated SQLite prototype; `services/printing/pg_queue.py` is the isolated PostgreSQL 17 queue. Both have no Flask routes, WB access or printer I/O. `frontend/pages/printer_management/` and `frontend/features/printer_scan/` will be introduced only when real UI is implemented. Tests live in `tests/printing/`.

Existing `line_printer_registry.py`, `line_printer_routing.py` and `line_printer_pg_trial.py` remain legacy root modules from draft PR #193. They will be migrated separately with compatibility tests, not in this refactor.

No live migrations, production feature flags or hardware printing. New imports: `services.printing.synthetic_queue` and `services.printing.pg_queue`.
