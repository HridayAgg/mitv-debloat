#!/usr/bin/env python3
"""Offline tests. They run the real CLI against tests/fake_adb, so no TV needed."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MITV = ROOT / "mitv.py"
FAKE = ROOT / "tests" / "fake_adb"


class MitvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.fake = base / "fake.json"
        os.chmod(FAKE, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mitv(self, *args):
        env = dict(os.environ)
        env["ADB"] = str(FAKE)
        env["MITV_STATE"] = str(self.state)
        env["FAKE_ADB_STATE"] = str(self.fake)
        env.pop("FAKE_ADB_HOME", None)
        return subprocess.run(
            [sys.executable, str(MITV)] + list(args),
            capture_output=True, text=True, env=env, timeout=120,
        )

    def removed(self):
        if not self.fake.exists():
            return set()
        return set(json.loads(self.fake.read_text()))

    def scan_json(self):
        cp = self.run_mitv("scan", "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        blob = json.loads(cp.stdout[cp.stdout.index("{"):])
        return blob, {i["package"]: i for i in blob["items"]}

    # -- tests ------------------------------------------------------------
    def test_classification(self):
        blob, items = self.scan_json()
        self.assertIn("Mi TV", blob["model"])
        self.assertEqual(items["com.mitv.videoworld"]["risk"], "safe")
        self.assertEqual(items["com.mitv.tvhome"]["risk"], "caution")
        self.assertEqual(items["com.google.android.gms"]["risk"], "protected")
        self.assertEqual(items["com.mitv.upgrade"]["risk"], "caution")
        unknown = {u["package"] for u in blob["unknown"]}
        self.assertIn("com.example.mysteryapp", unknown)

    def test_hard_block_wins(self):
        cp = self.run_mitv("apply", "--packages", "com.google.android.gms", "--yes")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("[block]", cp.stdout)
        self.assertEqual(self.removed(), set())

    def test_patchwall_is_gated_without_launcher(self):
        cp = self.run_mitv("apply", "--profile", "patchwall", "--yes")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("[gate] com.mitv.tvhome", cp.stdout)
        self.assertNotIn("com.mitv.tvhome", self.removed())

    def test_patchwall_allowed_with_replacement_launcher(self):
        env = dict(os.environ)
        env["ADB"] = str(FAKE)
        env["MITV_STATE"] = str(self.state)
        env["FAKE_ADB_STATE"] = str(self.fake)
        env["FAKE_ADB_HOME"] = "com.spocky.projengmenu"
        cp = subprocess.run(
            [sys.executable, str(MITV), "apply", "--profile", "patchwall", "--yes"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("com.mitv.tvhome", self.removed())

    def test_dry_run_changes_nothing(self):
        cp = self.run_mitv("apply", "--profile", "light", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("dry run", cp.stdout)
        self.assertEqual(self.removed(), set())

    def test_apply_then_restore(self):
        cp = self.run_mitv("apply", "--profile", "light", "--yes")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        gone = self.removed()
        for pkg in ("com.mitv.videoworld", "com.mitv.mtservices", "com.mitv.shop"):
            self.assertIn(pkg, gone)
        self.assertNotIn("com.google.android.gms", gone)
        self.assertNotIn("com.mitv.tvhome", gone)  # not in light profile

        cp = self.run_mitv("restore", "--last")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(self.removed(), set())

    def test_restore_list(self):
        self.run_mitv("apply", "--profile", "light", "--yes")
        cp = self.run_mitv("restore", "--list")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("com.mitv.videoworld", cp.stdout)

    def test_inspect_finds_ad_sdks(self):
        cp = self.run_mitv("inspect", "com.mitv.videoworld")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("AppLovin", cp.stdout)
        self.assertIn("Pangle", cp.stdout)
        ev = self.state / "reports"
        files = list(ev.rglob("apk-inspection.json"))
        self.assertTrue(files, "inspection evidence should be written")
        data = json.loads(files[0].read_text())
        self.assertEqual(data[0]["package"], "com.mitv.videoworld")
        self.assertEqual(len(data[0]["sha256"]), 64)

    def test_doctor(self):
        cp = self.run_mitv("doctor")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("connected", cp.stdout)

    def test_snapshot_backup(self):
        cp = self.run_mitv("snapshot")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        files = list((self.state / "backups").rglob("packages.txt"))
        self.assertTrue(files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
