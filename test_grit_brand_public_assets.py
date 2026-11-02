"""Prevent app authentication middleware from redirecting static brand images to employee login."""
from pathlib import Path

def test_grit_brand_assets_public():
    s=Path("app.py").read_text(encoding="utf-8")
    public=s.split("public_paths = {",1)[1].split("\n    }",1)[0]
    for path in ("/grit-logo.png","/pwa-icon-192.png","/pwa-icon-512.png"):
        assert f'"{path}"' in public

def test_grit_logo_route_exists():
    s=Path("wms.py").read_text(encoding="utf-8")
    assert '@wms_bp.get("/grit-logo.png")' in s
    assert '_GRIT_LOGO_PATH.read_bytes()' in s
