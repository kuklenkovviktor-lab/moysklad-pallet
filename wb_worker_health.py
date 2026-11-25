"""Local worker progress, never a web/DB/WB probe. No credentials in this file."""
import json
import os
from pathlib import Path
import time

STATE_PATH = "/tmp/grit-wb-worker-health.json"


class WorkerHealth:
    def __init__(self, path=STATE_PATH):
        self.path = Path(path)
        self.state = dict(pid=os.getpid(), started=time.time(), progress=time.time(),
                          operation="startup", successes={}, errors={})
        self.touch()

    def touch(self):
        self.state["progress"] = time.time()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state), encoding="utf-8")
        temporary.replace(self.path)

    def begin(self, operation):
        self.state["operation"] = operation
        self.state["operation_started"] = time.time()
        self.touch()

    def finish(self, operation, ok):
        self.state["successes" if ok else "errors"][operation] = time.time()
        self.state["operation"] = "idle"
        self.touch()


def check(path=STATE_PATH, now=None, alive=None):
    now = time.time() if now is None else now
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        if alive is None:
            os.kill(int(state["pid"]), 0)
        elif not alive(state["pid"]):
            return False, "stopped"
        if not 0 <= now - state["progress"] <= 300:
            return False, "stalled"
        if state["operation"] not in ("idle", "startup") and now - state["operation_started"] > 1800:
            return False, "operation_timeout"
        # A running loop alone is not evidence of a usable cache. Allow startup
        # and transient outages, then fail even if API retries keep progressing.
        successes = state["successes"]
        if any(now - successes.get(name, state["started"]) > 900 for name in ("new", "status")):
            return False, "sync_overdue"
        if not all(name in successes for name in ("new", "status")):
            return True, "starting"
        if any(at > successes.get(name, 0) for name, at in state["errors"].items()):
            return True, "degraded"
        return True, "ok"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "unavailable"


if __name__ == "__main__":
    healthy, status = check()
    print("WB worker: " + status)
    raise SystemExit(0 if healthy else 1)
