#!/usr/bin/env python3
"""Arm PR210 automatic synthetic printing in the EXISTING PR208 demo only.

Keeps current disposable PostgreSQL17 and current per-demo USB agent token,
so Windows DPAPI enrollment survives. No production or physical printing.
Changes only the isolated demo app container. Rollback on failed health check.
"""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import urllib.request

from prepare_usb_physical_test import inspect_demo, docker, PRIVATE, STATE

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18769"


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Dedicated isolated demo administrator required")
    if not re.fullmatch(r"/tmp/grit-pr21[0-9]-[A-Za-z0-9_-]+",str(ROOT)):
        # A path outside the known temp test-checkout convention is refused.
        raise RuntimeError("Refusing non-temporary PR210 source checkout")
    values, app, env, _ = inspect_demo()
    if env.get("GRIT_PR210_AUTO_USB_SYNTHETIC") == "YES_EXPLICIT":
        raise RuntimeError("Isolated demo already auto-armed; refusing duplicate swap")
    token = env.get("GRIT_PR192_USB_AGENT_TOKEN", "")
    if not 32 <= len(token) <= 256:
        raise RuntimeError("Existing device token missing; never generate a replacement silently")
    if app["Config"].get("Image") != "grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4":
        raise RuntimeError("Unexpected demo image; refuses upgrade")
    if not (ROOT/"line_printer_demo_app.py").is_file():
        raise RuntimeError("PR210 source missing")
    import fcntl
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(PRIVATE/"pr210.lock","a") as lock:
        os.chmod(PRIVATE/"pr210.lock",0o600)
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Validate again while holding the lock.
        values, app, env, _ = inspect_demo()
        old = values["APP"]
        new = "grit-ui-pr192demo-auto-" + secrets.token_hex(6)
        fd, filename = tempfile.mkstemp(prefix="grit-pr210-env-",dir=PRIVATE)
        os.chmod(filename,0o600)
        try:
            env = dict(env, GRIT_PR210_AUTO_USB_SYNTHETIC="YES_EXPLICIT")
            with os.fdopen(fd,"w") as secret_file:
                for key,val in env.items():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",key) or "\n" in val:
                        raise RuntimeError("Invalid inherited demo environment")
                    secret_file.write(key+"="+val+"\n")
                secret_file.flush()
                os.fsync(secret_file.fileno())
            # Do not erase previous disposable app. Roll back on failure.
            docker("stop",old)
            try:
                docker("run","-d","--name",new,"--network",values["NET"],
                       "--no-healthcheck","--read-only","--cap-drop","ALL",
                       "--security-opt","no-new-privileges:true",
                       "--tmpfs","/tmp:rw,size=128m",
                       "-p","127.0.0.1:18769:8000",
                       "-v",str(ROOT)+":/app:ro","-w","/app",
                       "--env-file",filename,"--entrypoint","python",
                       app["Image"],"-m","gunicorn","--bind","0.0.0.0:8000",
                       "--workers","1","--timeout","60","line_printer_demo_app:app")
                probe=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(30):
                    try:
                        with probe.open(BASE+"/health",timeout=2) as response:
                            if json.load(response)=={"ok":True,"isolation":"synthetic","production":False}:
                                break
                    except Exception:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("Replacement demo failed health check")
                # Ensure auto flag is present only inside the newly started
                # synthetic app, not in any production environment.
                new_env=json.loads(docker("inspect",new))[0]["Config"]["Env"]
                if "GRIT_PR210_AUTO_USB_SYNTHETIC=YES_EXPLICIT" not in new_env:
                    raise RuntimeError("New demo not armed")
                state_text="".join(k+"="+(new if k=="APP" else values[k])+"\n"
                                   for k in ("APP","DB","NET","PORT"))
                temp_state=STATE.with_name(STATE.name+".tmp-"+secrets.token_hex(4))
                with open(temp_state,"x") as state_file:
                    os.chmod(temp_state,0o600)
                    state_file.write(state_text)
                    state_file.flush()
                    os.fsync(state_file.fileno())
                os.replace(temp_state,STATE)
            except BaseException:
                subprocess.run(["docker","rm","-f",new],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                docker("start",old)
                raise RuntimeError("Auto-arm FAILED; previous isolated demo restored") from None
        finally:
            Path(filename).unlink(missing_ok=True)
    print("PR210 ISOLATED DEMO AUTO MODE ARMED: preserved PG17, USB token and stand.")
    print("Only synthetic printer QR can approve automatic USB jobs.")
    print("No live WB, production, main merge, or physical printing happened.")


if __name__ == "__main__":
    main()
