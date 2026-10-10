"""Preserve paid state during resume without changing the normal fresh-run path."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import run_mailbox


class MailboxResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mailbox = self.root / "exam"
        (self.mailbox / "batch_01").mkdir(parents=True)
        (self.mailbox / "batches.json").write_text(json.dumps({"batches": [
            {"batch": "batch_01", "as_of": "2034-01-31"}]}))
        self.state = self.root / "state"
        self.cache = self.state / "extractions/paid.json"
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('{"origin":"previous paid extraction"}')
        self.harness = self.root / "fake_harness.py"
        self.harness.write_text('''
import json
import sys
from pathlib import Path
args = sys.argv[1:]
state = Path(args[args.index("--state-dir") + 1])
cache = state / "extractions/paid.json"
if args[0] == "ingest":
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        cache.write_text('{"origin":"new extraction"}')
elif args[0] == "report":
    assert cache.exists()
    print(json.dumps({"as_of": args[args.index("--as-of") + 1], "mode": "supervised",
        "findings": [], "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}))
''')

    def test_real_resume_cli_preserves_paid_state_and_writes_an_exam_sheet_on_repeat(self):
        before = self.cache.read_bytes()
        report = self.root / "exam_report.json"
        sheet = self.root / "answer_sheet.json"
        command = [sys.executable, str(run_mailbox.ROOT / "tools/run_mailbox.py"),
                   "--data", str(self.mailbox), "--harness", str(self.harness),
                   "--state-dir", str(self.state), "--resume", "--out", str(report), "--sheet", str(sheet)]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.cache.read_bytes(), before)
            value = json.loads(sheet.read_text())
            self.assertEqual(value["mailbox"], "exam")
            self.assertEqual(value["as_of"], "2034-01-31")
            self.assertEqual(value["totals"]["total_savings"], "0.00")

    def test_default_run_still_starts_with_empty_state(self):
        report, errors = run_mailbox.run(str(self.harness), self.mailbox, "2034-01-31",
                                        "supervised", self.state, log=lambda value: None)
        self.assertEqual(errors, [])
        self.assertIsNotNone(report)
        self.assertEqual(json.loads(self.cache.read_text())["origin"], "new extraction")

    def test_missing_resume_state_stops_before_invoking_the_harness(self):
        with patch.object(run_mailbox, "step", side_effect=AssertionError("Must not start new paid work")):
            report, errors = run_mailbox.run(str(self.harness), self.mailbox, "2034-01-31",
                                            "supervised", self.root / "missing", resume=True)
        self.assertIsNone(report)
        self.assertIn("cannot resume", errors[0])
