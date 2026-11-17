# SPDX-License-Identifier: AGPL-3.0-only
"""#7682 — a persona memory repository the isolated Git environment cannot open.

HTTPS can never authenticate there (global/system configuration and credential
helpers are deliberately ignored), and SSH needs `persona_memory.ssh_key` on
the host. The failure must be refused before Git runs and made visible in the
turn, not only in the runner log.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import hostcheck
from ameesh import persona_memory as pm

HTTPS = "https://github.com/manaty/avatar-essai-memoire.git"
SSH = "git@github.com:manaty/avatar-essai-memoire.git"


class TransportGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7682-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = os.path.join(self.tmp, "deploy-key")
        with open(self.key, "w", encoding="utf-8") as stream:
            stream.write("not a real key")
        os.chmod(self.key, 0o600)
        self.state = os.path.join(self.tmp, "state")

    def test_https_is_refused_before_any_git_call(self):
        with mock.patch.object(pm, "_clone") as clone, \
                mock.patch.object(pm, "_git") as git:
            with self.assertRaisesRegex(pm.MemoryError, "cannot authenticate"):
                pm.open_copy(HTTPS, self.state, "avatar", cfg_raw={"ssh_key": self.key})
        clone.assert_not_called()
        git.assert_not_called()
        self.assertFalse(os.path.exists(pm.copy_dir(self.state, "avatar")))

    def test_ssh_without_host_key_is_refused(self):
        with self.assertRaisesRegex(pm.MemoryError, "persona_memory.ssh_key"):
            pm.open_copy(SSH, self.state, "avatar", cfg_raw={})

    def test_ssh_with_missing_key_names_the_file(self):
        absent = os.path.join(self.tmp, "absent")
        with self.assertRaisesRegex(pm.MemoryError, "does not exist"):
            pm.open_copy(SSH, self.state, "avatar", cfg_raw={"ssh_key": absent})

    def test_supported_transports_pass_the_guard(self):
        self.assertIsNone(pm.transport_problem(SSH, {"ssh_key": self.key}))
        self.assertIsNone(pm.transport_problem(
            "ssh://git@github.com/manaty/avatar-essai-memoire.git",
            {"ssh_key": self.key}))
        # A local path needs no credential (tests, mirrors) and is not judged.
        self.assertIsNone(pm.transport_problem(self.tmp, {}))

    def test_failure_codes_are_stable(self):
        self.assertEqual("https_transport", pm.failure_code(pm.transport_problem(HTTPS, {})))
        self.assertEqual("ssh_key_missing",
                         pm.failure_code(pm.transport_problem(SSH, {})))
        self.assertEqual("ssh_key_unreadable", pm.failure_code(
            pm.transport_problem(SSH, {"ssh_key": os.path.join(self.tmp, "absent")})))

    def test_unavailable_preface_is_a_visible_bounded_line(self):
        text = pm.unavailable_preface("x" * 1000)
        self.assertIn("PERSONA MEMORY UNAVAILABLE for this turn:", text)
        self.assertLessEqual(len(text.splitlines()[0]), 350)

    def test_credentials_in_a_repository_url_are_never_echoed(self):
        problem = pm.transport_problem(
            "https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz@github.com/a/b.git",
            {})
        self.assertIn("cannot authenticate", problem)
        self.assertNotIn("ghp_", problem)
        self.assertNotIn("x-access-token", problem)
        self.assertIn("<redacted>@github.com", problem)


class CanonFindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7682-canon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = os.path.join(self.tmp, "deploy-key")
        with open(self.key, "w", encoding="utf-8") as stream:
            stream.write("not a real key")

    def _canon(self, repository=HTTPS):
        agent = types.SimpleNamespace(
            title="avatar-essai", memory_repository=repository,
            fiche=types.SimpleNamespace(member="home",
                                        path="agents/avatar-essai.md",
                                        untrusted=False))
        return types.SimpleNamespace(agents=[agent])

    def _verdict(self, host="ameesh-agents-1", ok=True):
        return types.SimpleNamespace(agent="avatar-essai", host=host, ok=ok)

    def test_canon_check_warns_for_an_admitted_agent(self):
        cfg = types.SimpleNamespace(host="ameesh-agents-1",
                                    persona_memory={"ssh_key": self.key})
        with mock.patch("ameesh.placement.verdicts",
                        return_value=[self._verdict()]):
            findings = pm.canon_findings(cfg, self._canon(), "ameesh-agents-1")
        self.assertEqual(["agent-memory-transport"], [f.code for f in findings])
        self.assertEqual([canon_mod.WARNING], [f.severity for f in findings])
        self.assertEqual("avatar-essai", findings[0].agent)
        self.assertEqual("ameesh-agents-1", findings[0].host)
        self.assertIn("HTTPS", findings[0].message)

    def test_refused_placement_has_no_memory_finding(self):
        cfg = types.SimpleNamespace(host="ameesh-agents-1",
                                    persona_memory={"ssh_key": self.key})
        with mock.patch("ameesh.placement.verdicts",
                        return_value=[self._verdict(ok=False)]):
            self.assertEqual([], pm.canon_findings(cfg, self._canon(), "ameesh-agents-1"))

    def test_another_host_never_judges_its_key_file(self):
        # The SSH URL is fine; only the other host's key file is unknown here,
        # so a check on this host must not invent a missing-key defect.
        cfg = types.SimpleNamespace(host="ameesh-agents-2", persona_memory={})
        with mock.patch("ameesh.placement.verdicts",
                        return_value=[self._verdict()]):
            self.assertEqual([], pm.canon_findings(cfg, self._canon(SSH),
                                                   "ameesh-agents-1"))
            # HTTPS is refused everywhere: no host configuration can fix it.
            https = pm.canon_findings(cfg, self._canon(HTTPS), "ameesh-agents-1")
        self.assertEqual(1, len(https))

    def test_doctor_prints_the_host_defect(self):
        cfg = types.SimpleNamespace(host="ameesh-agents-1",
                                    persona_memory={"ssh_key": self.key})
        with mock.patch("ameesh.placement.verdicts",
                        return_value=[self._verdict()]):
            lines = hostcheck.memory_transport_lines(cfg, [self._canon()])
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("memory     : "))
        self.assertIn("avatar-essai", lines[0])
        self.assertIn("cannot be cloned", lines[0])


if __name__ == "__main__":
    unittest.main()
