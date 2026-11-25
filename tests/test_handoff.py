"""CLI-level regression tests: ``python3 -B -m unittest discover -s tests``.

Every test drives ``python3 -m handoff`` the way an operator would, with one
JSON object per call, and asserts on the single JSON object it prints.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHANNELS = {
    "T": {"unit": "℃", "calibration": "y=x+1/2"},
    "D": {"unit": "mm", "calibration": "y=x"},
}
F7 = [7, 0, None, "T7", "D7"]
F8 = [8, 4, 9, "T8", "D8"]


class HandoffCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = os.path.join(self.directory.name, "bench")

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, command, payload=None, expect=0):
        argv = [sys.executable, "-B", "-m", "handoff", self.store, command]
        if payload is not None:
            argv += ["--json", json.dumps(payload, ensure_ascii=False)]
        completed = subprocess.run(
            argv, cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            expect,
            msg=f"{command} -> {completed.returncode}\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        return json.loads(completed.stdout)

    def open_shift(self, channels=None):
        return self.run_cli("open-shift", {
            "shift": "S1",
            "channels": channels or CHANNELS,
        })

    def load(self, frames):
        return self.run_cli("load", {"shift": "S1", "frames": frames})

    def intake(self, channel, frame_no=None, expect=0):
        payload = {"shift": "S1", "channel": channel}
        if frame_no is not None:
            payload["frame_no"] = frame_no
        return self.run_cli("intake", payload, expect=expect)

    def archive(self, frame_no, channel, simulate_failure=False, expect=0):
        payload = {"shift": "S1", "frame_no": frame_no, "channel": channel}
        if simulate_failure:
            payload["simulate_failure"] = True
        return self.run_cli("archive-submit", payload, expect=expect)

    def handoff(self, frame_no, expect=0):
        return self.run_cli("handoff",
                            {"shift": "S1", "frame_no": frame_no},
                            expect=expect)

    def claim(self, handoff_no, expect=0):
        return self.run_cli("claim", {"handoff_no": handoff_no}, expect=expect)

    # ------------------------------------------------------------------ tests
    def test_open_freezes_and_reuses_or_refuses(self):
        opened = self.open_shift()
        self.assertTrue(opened["created"])
        self.assertEqual(opened["frozen"]["T"]["formula"], "y=x+1/2")
        self.assertEqual(opened["frozen"]["D"]["unit"], "mm")

        reused = self.open_shift({
            "T": {"unit": "℃", "calibration": {"a": 1, "b": "2/4"}},
            "D": {"unit": "mm", "calibration": "y=x"},
        })
        self.assertFalse(reused["created"])
        self.assertEqual(reused["frozen"]["T"]["b"], "1/2")

        conflict = self.run_cli("open-shift", {
            "shift": "S1",
            "channels": {
                "T": {"unit": "℃", "calibration": "y=x+1/3"},
                "D": {"unit": "mm", "calibration": "y=x"},
            },
        }, expect=1)
        self.assertEqual(conflict["error"]["code"], "calibration_conflict")

    def test_bad_frame_is_refused_without_advancing(self):
        self.open_shift()
        result = self.run_cli(
            "load",
            {"shift": "S1", "frames": [F7, [8, "4", 9, "T8", "D8"], F8]},
            expect=1,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["accepted"], [7])
        self.assertEqual(result["rejected"][0]["error"]["code"], "invalid_raw")
        self.assertEqual(result["current_frame"], 7)
        status = self.run_cli("status", {"shift": "S1"})
        self.assertEqual([f["frame_no"] for f in status["loaded"]], [7])

    def test_full_walkthrough_with_lost_ack_and_retry(self):
        self.open_shift()
        loaded = self.load([F7, F8])
        self.assertEqual(loaded["accepted"], [7, 8])

        self.assertEqual(self.intake("T")["raw"], 0)
        missing = self.intake("D")
        self.assertIsNone(missing["raw"])
        self.assertEqual(missing["source"], "D7")

        lost = self.run_cli(
            "release",
            {"shift": "S1", "simulate_failure": "after"},
            expect=1,
        )
        self.assertEqual(lost["error"]["code"], "release_ack_unknown")
        receipt = self.run_cli(
            "release-status", {"shift": "S1", "frame_no": 7}
        )
        self.assertTrue(receipt["released"])
        self.assertTrue(receipt["ack_matches_intakes"])

        self.assertEqual(self.intake("D")["raw"], 9)

        prepared = self.run_cli("prepare", {"shift": "S1", "frame_no": 7})
        values = {item["channel"]: item["value"] for item in prepared["items"]}
        self.assertEqual(values, {"T": "1/2", "D": None})

        self.archive(7, "T")
        waiting = self.handoff(7)
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["missing"], ["D"])

        failed = self.archive(7, "D", simulate_failure=True, expect=1)
        self.assertEqual(failed["error"]["code"], "archive_not_applied")
        self.assertEqual(self.handoff(7)["status"], "waiting")

        self.archive(7, "D")
        issued = self.handoff(7)
        self.assertEqual(issued["status"], "issued")

        self.run_cli("close-source", {"shift": "S1"})
        shut = self.claim(issued["handoff_no"], expect=1)
        self.assertEqual(shut["error"]["code"], "archive_closed")

        self.run_cli("open-archive", {"shift": "S1"})
        delivered = self.claim(issued["handoff_no"])
        self.assertEqual(delivered["channels"]["T"]["value"], "1/2")
        self.assertEqual(delivered["channels"]["T"]["value_display"], "1/2 ℃")
        self.assertEqual(delivered["channels"]["T"]["source"], "T7")
        self.assertEqual(delivered["channels"]["D"]["state"], "missing")
        self.assertIsNone(delivered["channels"]["D"]["value"])
        self.assertEqual(delivered["channels"]["D"]["source"], "D7")
        tokens = {item["channel"]: item["first_execution_token"]
                  for item in self.run_cli(
                      "prepare", {"shift": "S1", "frame_no": 7})["items"]}
        self.assertEqual(
            delivered["channels"]["T"]["first_execution_token"], tokens["T"]
        )
        self.assertEqual(
            delivered["channels"]["D"]["first_execution_token"], tokens["D"]
        )

        repeat = self.claim(issued["handoff_no"])
        self.assertFalse(repeat["channels"]["T"]["first_claim"])
        self.assertEqual(
            repeat["channels"]["D"]["first_execution_token"],
            delivered["channels"]["D"]["first_execution_token"],
        )
        missing_no = self.claim("H-DOES-NOT-EXIST", expect=1)
        self.assertEqual(missing_no["error"]["code"], "handoff_not_found")

    def test_release_needs_both_and_keeps_frame_on_before_failure(self):
        self.open_shift()
        self.load([F7, F8])
        self.intake("T")
        incomplete = self.run_cli("release", {"shift": "S1"}, expect=1)
        self.assertEqual(incomplete["error"]["code"], "intake_incomplete")
        self.assertEqual(incomplete["error"]["missing"], ["D"])

        self.intake("D")
        before = self.run_cli(
            "release",
            {"shift": "S1", "simulate_failure": "before"},
            expect=1,
        )
        self.assertEqual(before["error"]["code"], "release_not_applied")
        status = self.run_cli("status", {"shift": "S1"})
        self.assertEqual(status["current_frame"], 7)
        self.assertEqual(status["releases"], {})

        self.run_cli("release", {"shift": "S1"})
        status = self.run_cli("status", {"shift": "S1"})
        self.assertEqual(status["current_frame"], 8)
        self.assertEqual(sorted(status["releases"]), ["7"])

    def test_archive_tokens_and_conflicts(self):
        self.open_shift()
        self.load([F7])
        self.intake("T")
        self.intake("D")
        first = self.archive(7, "T")
        self.assertTrue(first["first_execution"])
        again = self.archive(7, "T")
        self.assertEqual(again["token"], first["token"])
        self.assertEqual(again["status"], "reclaimed")
        conflict = self.run_cli("archive-submit", {
            "shift": "S1", "frame_no": 7, "channel": "T",
            "evidence": {"raw": 99},
        }, expect=1)
        self.assertEqual(conflict["error"]["code"], "archive_conflict")

    def test_reopen_intake_and_release_are_idempotent(self):
        self.open_shift()
        self.load([F7])
        self.intake("T")
        before = self.run_cli("release", {"shift": "S1"}, expect=1)
        self.assertEqual(before["error"]["code"], "intake_incomplete")
        self.intake("D")
        self.run_cli("release", {"shift": "S1"})
        again = self.run_cli("release", {"shift": "S1", "frame_no": 7})
        self.assertEqual(again["status"], "already_released")
        reopened = self.intake("T", frame_no=7)
        self.assertEqual(reopened["status"], "already_taken")
        self.assertEqual(reopened["raw"], 0)
        rewrite = self.run_cli("intake", {
            "shift": "S1", "frame_no": 7, "channel": "T", "raw": 5,
        }, expect=1)
        self.assertEqual(rewrite["error"]["code"], "intake_conflict")


if __name__ == "__main__":
    unittest.main()
