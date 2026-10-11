# SPDX-License-Identifier: AGPL-3.0-only
"""Harness versions per host (lot L120, #119): read by the runners, shown
by `ameesh harness versions` with the differences between hosts and the
latest release. Read only: nothing is updated."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest import mock

from ameesh import harness_versions as hv
from ameesh import storage
from ameesh.runner import Runner

from .support import FAKEBIN, PgTestCase

FAKE_ENV = {"AMEESH_CLAUDE_BIN": os.path.join(FAKEBIN, "claude"),
            "AMEESH_CODEX_BIN": os.path.join(FAKEBIN, "codex"),
            "AMEESH_DSH_BIN": os.path.join(FAKEBIN, "dsh")}


class VersionTest(unittest.TestCase):
    def test_parse_version(self):
        for text, expected in (("2.1.286 (Claude Code)", "2.1.286"),
                               ("codex-cli 0.159.3", "0.159.3"),
                               ("0.2.0-rc.2", "0.2.0-rc.2"),
                               ("dsh v1.4", "1.4"),
                               ("no version here", None), ("", None), (None, None)):
            with self.subTest(text=text):
                self.assertEqual(hv.parse_version(text), expected)

    def test_version_order(self):
        ordered = [None, "0.2.0-rc.2", "0.2.0", "0.10.0", "1.0", "1.0.1", "2.1.286"]
        self.assertEqual(sorted(reversed(ordered), key=hv.version_key), ordered)

    def test_probe_fake_harnesses(self):
        with mock.patch.dict(os.environ, FAKE_ENV):
            for harness, version in (("claude", "9.9.9"), ("codex", "9.9.9"),
                                     ("deepseek", "9.9.9")):
                with self.subTest(harness=harness):
                    item = hv.probe(harness)
                    self.assertEqual(item["version"], version, item)
                    self.assertIsNone(item["error"])
                    self.assertTrue(item["path"].startswith(FAKEBIN))

    def test_probe_never_raises(self):
        with mock.patch.dict(os.environ, {"AMEESH_CLAUDE_BIN": "/no/such/claude"}):
            item = hv.probe("claude")
        self.assertIsNone(item["version"])
        self.assertIn("not found", item["error"])

        def timeout(*_a, **_k):
            raise subprocess.TimeoutExpired("claude", 1)

        def silent(*_a, **_k):
            return subprocess.CompletedProcess([], 2, stdout="", stderr="boom\n")

        with mock.patch.dict(os.environ, FAKE_ENV):
            self.assertEqual(hv.probe("claude", run=timeout)["error"], "TimeoutExpired")
            item = hv.probe("claude", run=silent)
        self.assertEqual(item["raw"], "boom")
        self.assertEqual(item["error"], "exit 2, no version")

    def test_latest(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return json.dumps({"name": "x", "version": "2.1.296"})

        self.assertEqual(hv.latest("claude", fetch=fetch), "2.1.296")
        self.assertEqual(urls, ["https://registry.npmjs.org/@anthropic-ai/claude-code/latest"])
        self.assertIsNone(hv.latest("deepseek", fetch=fetch))
        self.assertIsNone(hv.latest("codex", fetch=lambda _u: "not json"))

        def offline(_url):
            raise OSError("offline")

        self.assertIsNone(hv.latest("codex", fetch=offline))

    def test_matrix(self):
        rows = [{"harness": "claude", "host": "pc", "version": "2.1.286", "path": "/c",
                 "raw": "2.1.286 (Claude Code)", "observed_ts": 1000.0},
                {"harness": "claude", "host": "vm", "version": "2.1.290", "path": "/c",
                 "raw": "", "observed_ts": 1900.0},
                {"harness": "codex", "host": "pc", "version": "0.159.3", "path": "/x",
                 "raw": "", "observed_ts": 1000.0}]
        table = {t["harness"]: t for t in hv.matrix(rows, now=2000.0)}
        self.assertTrue(table["claude"]["differs"])
        self.assertEqual(table["claude"]["newest"], "2.1.290")
        self.assertEqual(table["claude"]["behind"], ["pc"])
        self.assertEqual(table["claude"]["hosts"]["pc"]["age_s"], 1000)
        self.assertFalse(table["codex"]["differs"])
        self.assertEqual(table["codex"]["behind"], [])
        table = {t["harness"]: t for t in hv.matrix(
            rows, {"claude": "2.1.296", "codex": None}, now=2000.0)}
        self.assertEqual(table["claude"]["behind"], ["pc", "vm"])
        self.assertEqual(table["claude"]["latest"], "2.1.296")
        self.assertEqual(table["codex"]["behind"], [])


class VersionDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM harness_versions")

    def test_record_replaces(self):
        hosts = storage.of(self.db).hosts
        hosts.record_harness_version("pc", "claude", path="/a", version="1.0", raw="1.0")
        hosts.record_harness_version("pc", "claude", path="/b", version="1.1", raw="1.1")
        hosts.record_harness_version("vm", "claude", path="/a", version=None, raw="?")
        rows = hosts.harness_versions()
        self.assertEqual([(r["host"], r["version"], r["path"]) for r in rows],
                         [("pc", "1.1", "/b"), ("vm", None, "/a")])
        self.assertIsNotNone(rows[0]["observed_ts"])
        self.assertEqual(len(hosts.harness_versions("vm")), 1)

    def test_runner_reads_the_versions_of_its_harnesses(self):
        self.register("v1", "claude")
        self.register("v2", "deepseek")
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.dict(os.environ, FAKE_ENV):
            found = runner.harness_versions_once(now=100.0)
            # not due again before 6 h
            self.assertIsNone(runner.harness_versions_once(now=200.0))
        self.assertEqual(sorted(i["harness"] for i in found), ["claude", "deepseek"])
        rows = storage.of(self.db).hosts.harness_versions(self.cfg.host)
        self.assertEqual({(r["harness"], r["version"]) for r in rows},
                         {("claude", "9.9.9"), ("deepseek", "9.9.9")})

    def test_cli(self):
        storage.of(self.db).hosts.record_harness_version(
            "other-host", "claude", path="/c", version="1.0.0", raw="1.0.0")
        proc = self.cli("harness", "versions", "--probe", "--json", env=self.env(**FAKE_ENV))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh-harness-versions/1")
        claude = {t["harness"]: t for t in out["harnesses"]}["claude"]
        self.assertTrue(claude["differs"])
        self.assertEqual(claude["behind"], ["other-host"])
        text = self.cli("harness", "versions", env=self.env(**FAKE_ENV))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("hosts differ", text.stdout)
        self.assertIn("behind: other-host", text.stdout)


if __name__ == "__main__":
    unittest.main()
