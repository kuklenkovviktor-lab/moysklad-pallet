"""Run from the PR checkout. Argument: directory containing PostgreSQL 17 binaries."""
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import psycopg
from flask import Flask

BASE = "4e07408eaa59768de7e66ab00ea5f06e2bf6bb71"
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))


def import_fixture(module, old):
    article = "LEGACY" if old else "OLD-SKU"
    code = "0011" if old else "0022"
    plan = dict(cargo=[dict(barcode="GM", destination="Test", label="A", qty=7,
                            items=[dict(article=article, barcode="0000", qty=7, source="Test!E2")])],
                errors=[], warnings=[], filename="test.xlsx", source_hash=article, plan_hash=article)
    key = "test-key"
    issue = dict(key=key, article=article, excel_barcode="0000", barcodes=[code])
    choice = dict(article=article, barcode=code, confirmed=True) if old else dict(
        article="БАНКА_240_ОВАЛ_4шт", barcode="2038693831791", confirmed=True,
        id="kit", entity_type="bundle", identifier_type="code")
    app = Flask(module.__name__)
    app.secret_key = "synthetic"
    app.testing = True
    app.register_blueprint(module.ozon_fbo_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session["manager_web"] = True
    with patch.object(module, "_manifest_from_request", return_value=plan), \
         patch.object(module, "check_catalog", return_value=[issue]):
        return client.post("/api/manager/ozon-fbo/import", data=dict(
            title=article, source_hash=article, plan_hash=article,
            catalog_decisions=json.dumps({key: choice})))


def historical_module(directory, name):
    path = directory / (name + ".py")
    path.write_bytes(subprocess.check_output(["git", "show", BASE + ":" + path.name]))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


def regression(conn, directory):
    import wms
    legacy_catalog = historical_module(directory, "ozon_fbo_catalog_validation")
    with patch.dict(sys.modules, {"ozon_fbo_catalog_validation": legacy_catalog}):
        legacy = historical_module(directory, "ozon_fbo")
        legacy._db()
        with patch.object(legacy_catalog, "cards_by_barcode", return_value=[
                dict(id="product-old", article="LEGACY", barcode="0011")]):
            response = import_fixture(legacy, True)
    assert response.status_code == 201, response.json
    old_supply = response.json["id"]
    def rows(query, params=()):
        return conn.execute(query, params).fetchall()
    old_link = rows("SELECT correction_id,new_article,product_id FROM ozon_fbo_correction_products")
    assert len(old_link) == 1
    tables = ["ozon_fbo_supplies", "ozon_fbo_cargo", "ozon_fbo_items",
              "ozon_fbo_import_corrections", "ozon_fbo_import_warnings"]
    original = {t: rows("SELECT * FROM " + t) for t in tables}
    assert len(original["ozon_fbo_import_corrections"]) == 1
    import ozon_fbo as current
    for _ in range(2):
        current._ready = False
        current._db()
    assert original == {t: rows("SELECT * FROM " + t) for t in tables}
    history = current._details(old_supply)["corrections"]
    assert len(history) == 1 and history[0]["new_article"] == history[0]["article"] == "LEGACY"
    assert (history[0]["entity_type"], history[0]["identifier_type"]) == ("product", "barcode")
    assert (history[0]["old"], history[0]["new"]) == ("0000", "0011")
    print("PASS historical audit, data preservation and idempotent DDL")
    try:
        conn.execute("INSERT INTO ozon_fbo_correction_products(correction_id,new_article,product_id) VALUES ('orphan','A','P')")
    except psycopg.errors.ForeignKeyViolation:
        pass
    else:
        raise AssertionError("Missing FK")
    # A deferred failure fires at COMMIT, after both audit records were inserted.
    conn.execute("""CREATE FUNCTION reject_test_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$""")
    conn.execute("""CREATE CONSTRAINT TRIGGER fail_test_commit AFTER INSERT
        ON ozon_fbo_correction_products DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reject_test_commit()""")
    bundle = dict(id="kit", article="БАНКА_240_ОВАЛ_4шт", code="2038693831791", barcodes=[])
    def read(params, context, entity):
        return [bundle] if entity == "bundle" else []

    with patch("ozon_fbo_catalog_validation._read_rows", side_effect=read):
        result = import_fixture(current, False)
        assert result.status_code == 409, result.json
        assert original == {t: rows("SELECT * FROM " + t) for t in tables}
        assert rows("SELECT correction_id,new_article,product_id FROM ozon_fbo_correction_products") == old_link
        conn.execute("DROP TRIGGER fail_test_commit ON ozon_fbo_correction_products")
        result = import_fixture(current, False)
        assert result.status_code == 201, result.json
    audit = current._details(result.json["id"])["corrections"]
    assert len(audit) == 1
    assert (audit[0]["article"], audit[0]["old"], audit[0]["new_article"], audit[0]["new"]) == (
        "OLD-SKU", "0000", bundle["article"], bundle["code"])
    assert (audit[0]["entity_type"], audit[0]["identifier_type"]) == ("bundle", "code")
    assert audit[0]["actor"] and audit[0]["at"] and audit[0]["source"] == "Test!E2"
    assert rows("""SELECT p.product_id,i.article,i.barcode,i.planned_qty FROM ozon_fbo_correction_products p
        JOIN ozon_fbo_import_corrections c ON c.id=p.correction_id
        JOIN ozon_fbo_cargo g ON g.supply_id=c.supply_id JOIN ozon_fbo_items i ON i.cargo_id=g.id
        WHERE c.supply_id=%s""", (result.json["id"],)) == [("kit",bundle["article"],bundle["code"],7)]
    assert current._details(old_supply)["corrections"] == history
    wms._PG_POOL.close()
    print("PASS FK, atomic rollback at commit, retry and persisted corrected item/audit")


def main():
    bin_dir = Path(sys.argv[1]).resolve()
    if sys.flags.optimize:
        raise RuntimeError("Run without -O to enable assertions")
    with tempfile.TemporaryDirectory(prefix="grit-bundle-") as tmp:
        root = Path(tmp)
        data = root / "pgdata"
        password = secrets.token_hex(24)
        pwfile = root / "pw"
        pwfile.write_text(password)
        subprocess.run([bin_dir/"initdb", "-D", data, "--no-locale", "-E", "SQL_ASCII",
                        "-U", "bundle_test", "-A", "scram-sha-256", "--pwfile", pwfile], check=True)
        pwfile.unlink()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        dsn = f"postgresql://bundle_test:{password}@127.0.0.1:{port}/postgres"
        with (root/"pg.log").open("wb") as log:
            server = subprocess.Popen([bin_dir/"postgres", "-D", data, *(["-k", str(root)] if os.name != "nt" else []), "-h", "127.0.0.1",
                                       "-p", str(port)], stdout=log, stderr=log)
            try:
                for _ in range(80):
                    if server.poll() is not None:
                        raise RuntimeError((root/"pg.log").read_text(errors="replace"))
                    try:
                        admin = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
                        break
                    except psycopg.OperationalError:
                        time.sleep(.25)
                else:
                    raise RuntimeError("Test cluster did not start")
                with admin:
                    assert 170000 <= admin.info.server_version < 180000
                    print("PostgreSQL", admin.info.server_version)
                    admin.execute("CREATE DATABASE bundle_synthetic TEMPLATE template0 ENCODING 'UTF8'")
                os.environ.update(DATABASE_URL=dsn.rsplit("/",1)[0]+"/bundle_synthetic",
                                  WMS_REQUIRE_POSTGRES="1", WMS_BADGE_SECRET="synthetic",
                                  SECRET_KEY="synthetic", WMS_TERMINAL_SECRET="synthetic")
                with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as test:
                    assert test.info.dbname == "bundle_synthetic"
                    regression(test, root)
            finally:
                if server.poll() is None:
                    subprocess.run([bin_dir/"pg_ctl", "stop", "-D", data, "-m", "fast"], check=True)
                server.wait(timeout=30)


if __name__ == "__main__":
    main()
