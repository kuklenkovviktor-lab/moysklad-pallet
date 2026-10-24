"""Isolated demo for partner Excel pallet labels. Never imports or runs production app.py."""
import os
from flask import Flask
from partner_pallets import partner_bp, db_init, password_hash
from wms import db_connect, sql

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.register_blueprint(partner_bp)

@app.before_request
def ensure_demo_partner():
    db_init()
    name = os.environ.get("DEMO_PARTNER_LOGIN", "test-partner")
    password = os.environ.get("DEMO_PARTNER_PASSWORD")
    if not password:
        raise RuntimeError("DEMO_PARTNER_PASSWORD must be configured")
    with db_connect() as db:
        cur = db.cursor()
        existing = cur.execute(sql("SELECT id FROM partner_accounts WHERE name=?"), (name,)).fetchone()
        if not existing:
            import uuid
            from datetime import datetime, timezone
            cur.execute(sql("INSERT INTO partner_accounts(id,name,password_hash,created_at) VALUES (?,?,?,?)"),
                        (str(uuid.uuid4()), name, password_hash(password), datetime.now(timezone.utc).isoformat()))
            db.commit()

@app.get("/")
def root():
    from flask import redirect
    return redirect("/partners/login")

@app.get("/health")
def health():
    return {"ok": True, "mode": "isolated-partner-demo", "storage": "ephemeral-sqlite"}

if __name__ == "__main__":
    app.run()
