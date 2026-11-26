"""Windows current-user DPAPI configuration; never tokens in argv or logs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager
import socket
import time

BASE_URL = "http://127.0.0.1:18769"
PRINTER = "ONIX-TEST-XP460B"


@contextmanager
def tunnel():
    """Reuse a listener or own one SSH forward for this command; never print."""
    if sys.platform != "win32":
        raise ValueError("Windows test PC required")
    def listening():
        try:
            with socket.create_connection(('127.0.0.1',18769),timeout=0.3):
                return True
        except OSError:
            return False
    process = None
    try:
        if not listening():
            process = subprocess.Popen(['ssh','-N','-o','ExitOnForwardFailure=yes',
                '-o','ConnectTimeout=8','-L','127.0.0.1:18769:127.0.0.1:18769','grit'],
                stdout=subprocess.DEVNULL)
            for _ in range(120):
                if process.poll() is not None:
                    raise ValueError("SSH tunnel failed")
                if listening():
                    break
                time.sleep(0.5)
            else:
                raise ValueError("SSH tunnel not ready; unlock SSH key")
        yield
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def validate(data):
    if not isinstance(data, dict) or set(data) != {"url","printer","token","stand_id","ticket_id"}:
        raise ValueError("Invalid bridge configuration")
    if data["url"] != BASE_URL or data["printer"] != PRINTER:
        raise ValueError("Only isolated loopback and dedicated test printer allowed")
    for key, size in (("stand_id",64),("ticket_id",32)):
        if not isinstance(data[key],str) or not re.fullmatch("[0-9a-f]{"+str(size)+"}",data[key]):
            raise ValueError("Invalid stand/ticket binding")
    if not isinstance(data["token"],str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}",data["token"]):
        raise ValueError("Invalid device credential")
    return data


def config_path():
    if sys.platform != "win32" or not os.environ.get("LOCALAPPDATA"):
        raise ValueError("Windows LOCALAPPDATA required")
    return Path(os.environ["LOCALAPPDATA"])/"GRIT-WMS"/"USB-Bridge-Config"/"binding.dpapi"


def _private_directory(path):
    import win32api
    import win32con
    import ntsecuritycon
    import win32security as sec
    path.mkdir(parents=True,exist_ok=True)
    for parent in (path, *path.parents):
        if parent.is_symlink() or (parent.stat().st_file_attributes & 0x400):
            raise ValueError("Reparse points forbidden in configuration path")
    token = sec.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:
        sid = sec.GetTokenInformation(token,sec.TokenUser)[0]
    finally:
        token.Close()
    acl = sec.ACL()
    acl.AddAccessAllowedAceEx(sec.ACL_REVISION,3,ntsecuritycon.FILE_ALL_ACCESS,sid)
    sec.SetNamedSecurityInfo(str(path),sec.SE_FILE_OBJECT,
                            sec.DACL_SECURITY_INFORMATION | sec.PROTECTED_DACL_SECURITY_INFORMATION,
                            None,None,acl,None)


def save(data):
    import win32crypt
    data = validate(data)
    path = config_path()
    _private_directory(path.parent)
    if path.exists() and (path.is_symlink() or path.stat().st_file_attributes & 0x400):
        raise ValueError("Reparse configuration refused")
    encrypted = win32crypt.CryptProtectData(json.dumps(data).encode(),"GRIT isolated USB",None,None,None,1)
    # New temporary file inherits current-user-only ACL; replace atomically.
    import tempfile
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as file:
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load():
    import win32crypt
    path = config_path()
    _private_directory(path.parent)
    if path.is_symlink() or path.stat().st_file_attributes & 0x400 or path.stat().st_size > 16384:
        raise ValueError("Unsafe configuration file")
    try:
        raw = win32crypt.CryptUnprotectData(path.read_bytes(),None,None,None,1)[1]
        return validate(json.loads(raw))
    except Exception:
        raise ValueError("Cannot decrypt current-user binding; enroll again") from None


def enrollment_from_ssh():
    # Fixed host alias and fixed file. stdout is captured, NEVER printed.
    remote = ("python3 -c \"import os,pathlib,stat; "
              "p=pathlib.Path('/root/.config/grit-usb-bridge/enrollment.json'); "
              "s=p.lstat(); "
              "assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_mode & 0o077==0; "
              "assert s.st_size<8192; print(p.read_text())\"")
    result = subprocess.run(["ssh","grit",remote],stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,timeout=60,check=False)
    if result.returncode or len(result.stdout)>8192:
        raise ValueError("Enrollment unavailable; prepare HOSTKEY test and unlock SSH key")
    try:
        return validate(json.loads(result.stdout))
    except Exception:
        raise ValueError("Invalid protected enrollment response") from None


def bind_api(api, config):
    config = validate(config)
    def bound(method,path,body=None):
        if path.endswith("/claim"):
            body = {"stand_id":config["stand_id"],"ticket_id":config["ticket_id"]}
        result = api(method,path,body)
        if path.endswith("/ping"):
            if result.get("stand_id") != config["stand_id"] or result.get("device") != PRINTER:
                raise ValueError("Wrong stand or printer; no print")
            if result.get("waiting") != 1 or result.get("ticket_id") != config["ticket_id"]:
                raise ValueError("Bound approval unavailable/already processed; no retry")
        if path.endswith("/claim") and result.get("state")=="CLAIMED" and result.get("ticket_id") != config["ticket_id"]:
            raise ValueError("Wrong claim; no print")
        return result
    return bound
