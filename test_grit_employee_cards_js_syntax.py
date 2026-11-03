"""Ensure employee analytics script parses before deploying to production."""
from pathlib import Path
import shutil
import subprocess
import unittest

class EmployeeCardsJavaScriptTests(unittest.TestCase):
    def test_javascript_syntax(self):
        page = Path("shift_history_ui.py").read_text(encoding="utf-8")
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("function renderCards()", script)
        self.assertIn("function esc(x)", script)
        if not shutil.which("node"):
            self.skipTest("Node.js not installed")
        proc = subprocess.run(
            ["node", "--check", "--input-type=commonjs"],
            input=script, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

if __name__ == "__main__":
    unittest.main()
