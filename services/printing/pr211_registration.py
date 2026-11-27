"""Opt-in wiring to EXISTING GRIT app and WMS session; NOT a new demo app.

Production remains completely off. The original WB print flow can run only
with a disposable PG17, synthetic identity and explicit isolated test switch.
Original PDF print mode cannot be silently activated on an employer warehouse.
"""
import os
import re


def register_pr211_isolated(app):
    flag=os.getenv('GRIT_PR211_ORIGINAL_FLOW','').strip()
    if not flag:
        return False
    if flag!='YES_ISOLATED_SYNTHETIC':
        raise RuntimeError('UNRECOGNIZED_PR211_PRINT_MODE')
    if os.getenv('GRIT_SYNTHETIC_PG_TEST')!='YES_ISOLATED':
        raise RuntimeError('PR211_SYNTHETIC_DATABASE_GATE_REQUIRED')
    url=os.getenv('PR211_SYNTHETIC_DATABASE_URL','')
    if not re.fullmatch(
        r'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_[a-z0-9_]+',
        url):
        raise RuntimeError('PR211_DISPOSABLE_DATABASE_REQUIRED')
    keyhex=os.getenv('PR211_TEST_AES_KEY_HEX','')
    if not re.fullmatch(r'[0-9a-fA-F]{64}',keyhex):
        raise RuntimeError('PR211_EPHEMERAL_AES_KEY_REQUIRED')
    import psycopg
    from services.printing.wb_original_pg17_outbox import initialize as init_queue
    from services.printing.wb_original_pg17_device_auth import initialize as init_devices
    from services.printing.wb_original_print_http_synthetic import (
        make_isolated_original_print_blueprint,
        verified_wms_manager_guard,verified_wms_picking_guard,
    )

    def connection_factory():
        return psycopg.connect(url,autocommit=True)

    with connection_factory() as db:
        init_queue(db)
        init_devices(db)

    app.register_blueprint(make_isolated_original_print_blueprint(
        connection_factory,key=bytes.fromhex(keyhex),
        manager_guard=verified_wms_manager_guard,
        worker_guard=verified_wms_picking_guard))
    return True
