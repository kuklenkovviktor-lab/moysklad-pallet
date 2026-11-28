"""Offline unit/regression suite. PostgreSQL stress has a separate guarded runner."""
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if len(sys.argv) == 1:
        files = sorted((ROOT / "tests").glob("test_wb*.py"))
        files += [ROOT / "tests" / name for name in (
            "test_fbs_double_product_scan.py", "test_attendance.py", "test_shift_history.py")]
        passed = True
        for file in files:
            if file.name == "test_wb_fbs_pg_stress.py":
                continue
            print("SYNTHETIC FILE:", file.name, flush=True)
            # Isolate module globals and imported Flask blueprints between legacy fixtures.
            program = "import sys,runpy;sys.path=" + repr(sys.path) + ";runpy.run_path(" + repr(__file__) + ",run_name='__main__')"
            run = subprocess.run([sys.executable, "-c", program, file.name], timeout=120)
            passed = passed and run.returncode == 0
        return passed
    with tempfile.TemporaryDirectory(prefix="grit-wb-synthetic-") as directory:
        settings = dict(DATABASE_URL="", WMS_REQUIRE_POSTGRES="0",
            WMS_DB_PATH=str(Path(directory) / "test.sqlite"), SECRET_KEY="synthetic",
            WMS_BADGE_SECRET="synthetic", WMS_TERMINAL_SECRET="synthetic",
            WB_API_TOKEN="", WB_CONTENT_TOKEN="", MOYSKLAD_TOKEN="", WB_FBS_V11_ENABLED="0")
        with patch.dict(os.environ, settings), patch.object(socket.socket, "connect",
                side_effect=AssertionError("Network forbidden in synthetic suite")):
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), sys.argv[1])
            result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
            print("SYNTHETIC RESULT:", result.testsRun, "tests; success:", result.wasSuccessful(), flush=True)
            return result.wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
