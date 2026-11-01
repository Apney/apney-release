"""Run the actual alarm shell against synthetic HTTP responses."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest


class HealthAlarmTest(unittest.TestCase):
    now = 1793534400  # 2026-11-01T12:00:00Z

    @classmethod
    def setUpClass(cls):
        workflow = (
            Path(__file__).resolve().parents[1] / "workflows/health-alarm.yml"
        ).read_text()
        marker = "        run: |\n"
        if workflow.count(marker) != 1:
            raise ValueError("expected exactly one multiline alarm script")
        cls.script = textwrap.dedent(workflow.split(marker, 1)[1])
        cls.real_date = shutil.which("date")
        if not cls.real_date or not shutil.which("jq"):
            raise RuntimeError("the alarm tests require GNU date and jq")

    def run_alarm(self, responses):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mocks = {
                "curl": """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
counter = Path(os.environ["ALARM_TEST_ATTEMPTS"])
attempt = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(attempt + 1))
responses = json.loads(os.environ["ALARM_TEST_RESPONSES"])
response = responses[min(attempt, len(responses) - 1)]
if response is None:
    sys.exit(22)
print(json.dumps(response))
""",
                "sleep": "#!/bin/sh\nexit 0\n",
                "date": """#!/bin/sh
if [ "$*" = "-u +%s" ]; then
    printf '%s\\n' "$ALARM_TEST_NOW"
else
    exec REAL_DATE "$@"
fi
""".replace("REAL_DATE", shlex.quote(self.real_date)),
            }
            for name, script in mocks.items():
                executable = root / name
                executable.write_text(script)
                executable.chmod(0o755)
            counter = root / "attempts"
            env = os.environ.copy()
            env.update(
                PATH=f"{root}{os.pathsep}{env['PATH']}",
                ALARM_TEST_NOW=str(self.now),
                ALARM_TEST_RESPONSES=json.dumps(responses),
                ALARM_TEST_ATTEMPTS=str(counter),
            )
            result = subprocess.run(
                ["bash", "-e", "-c", self.script],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, int(counter.read_text())

    def healthy(self, **changes):
        return {
            "neo4j": True,
            "graph_sync_failing": False,
            "graph_heartbeat_at": "2026-11-01T11:59:45Z",
            **changes,
        }

    def assert_rejected(self, response, reason):
        result, attempts = self.run_alarm([response])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"::error::{reason}", result.stdout)
        self.assertNotIn("healthy:", result.stdout)
        self.assertEqual(attempts, 3)

    def test_missing_heartbeat_is_rejected(self):
        response = self.healthy()
        del response["graph_heartbeat_at"]
        self.assert_rejected(response, "the Neo4j heartbeat is missing")

    def test_null_heartbeat_is_rejected(self):
        self.assert_rejected(
            self.healthy(graph_heartbeat_at=None), "the Neo4j heartbeat is missing"
        )

    def test_empty_heartbeat_is_rejected(self):
        self.assert_rejected(
            self.healthy(graph_heartbeat_at=""), "the Neo4j heartbeat is missing"
        )

    def test_invalid_heartbeat_is_rejected_with_a_reason(self):
        self.assert_rejected(
            self.healthy(graph_heartbeat_at="not-a-timestamp"),
            "the Neo4j heartbeat timestamp is invalid",
        )

    def test_stale_heartbeat_is_rejected(self):
        self.assert_rejected(
            self.healthy(graph_heartbeat_at="2026-11-01T11:29:59Z"),
            "the last Neo4j heartbeat was 30 minutes ago",
        )

    def test_fresh_heartbeat_passes(self):
        result, attempts = self.run_alarm([self.healthy()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("healthy:", result.stdout)
        self.assertEqual(attempts, 1)

    def test_exactly_thirty_minutes_is_not_older_than_thirty_minutes(self):
        result, _ = self.run_alarm(
            [self.healthy(graph_heartbeat_at="2026-11-01T11:30:00Z")]
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disconnected_neo4j_is_rejected(self):
        self.assert_rejected(self.healthy(neo4j=False), "Neo4j is not connected")

    def test_failing_sync_is_rejected(self):
        self.assert_rejected(
            self.healthy(graph_sync_failing=True), "the graph sync is failing"
        )

    def test_unreachable_engine_is_rejected(self):
        self.assert_rejected(None, "the engine did not answer")

    def test_startup_can_recover_on_the_next_retry(self):
        warming = self.healthy()
        del warming["graph_heartbeat_at"]
        result, attempts = self.run_alarm([warming, self.healthy()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
