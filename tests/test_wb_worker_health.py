"""Worker lifecycle tests with a private state file and no web/DB/API."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wb_worker_health as health


class WorkerHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "state.json")
        with patch.object(health.time, "time", return_value=1000):
            self.worker = health.WorkerHealth(self.path)

    def check(self, now=1001, alive=lambda pid: True):
        return health.check(self.path, now=now, alive=alive)

    def test_start_without_web_server(self):
        self.assertEqual(self.check(), (True, "starting"))
        # Production healthcheck runs in Linux; Windows kill(pid, 0) is a
        # console signal rather than the POSIX existence probe.
        with patch.object(health.os, "kill") as probe:
            self.assertEqual(health.check(self.path, now=1001), (True, "starting"))
            probe.assert_called_once_with(self.worker.state['pid'], 0)

    def test_stopped_and_stalled(self):
        self.assertEqual(self.check(alive=lambda pid: False), (False, "stopped"))
        self.assertEqual(self.check(now=1301), (False, "stalled"))

    def test_success_transient_error_and_prolonged_outage(self):
        with patch.object(health.time, "time", return_value=1000):
            for name in ("new", "status"):
                self.worker.finish(name, True)
        self.assertEqual(self.check(), (True, "ok"))
        with patch.object(health.time, "time", return_value=1100):
            self.worker.finish("status", False)
        self.assertEqual(self.check(now=1101), (True, "degraded"))
        with patch.object(health.time, "time", return_value=1901):
            self.worker.touch()
        self.assertEqual(self.check(now=1901), (False, "sync_overdue"))

    def test_progress_cannot_hide_endless_job(self):
        with patch.object(health.time, "time", return_value=1000):
            self.worker.begin("full")
        with patch.object(health.time, "time", return_value=2801):
            self.worker.touch()
        self.assertEqual(self.check(now=2801), (False, "operation_timeout"))

    def test_missing_or_corrupt_state_fails_closed(self):
        Path(self.path).write_text("invalid", encoding="utf-8")
        self.assertEqual(self.check(), (False, "unavailable"))

    def test_worker_image_replaces_web_healthcheck(self):
        dockerfile = Path("deploy/worker.Dockerfile").read_text()
        self.assertIn('/app/wb_worker_health.py', dockerfile)
        self.assertNotIn('8000', dockerfile)
