# SPDX-License-Identifier: AGPL-3.0-only
"""Proof provenance is isolated from caller-supplied evidence and worker code."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import support
from ameesh.modules import it
from ameesh.modules.it import proof_service
from ameesh.modules.it.execution import execute


class ProofHelperTest(unittest.TestCase):
    def payload(self, **changes):
        return dict(project="acme", lot=1, artifact="/artifact", runner=None,
                    arguments=[], timeout=60, **changes)

    def test_caller_cannot_supply_evidence_fields(self):
        for fields in ({"output": "invented"}, {"exit_code": 0}, {"command": ["true"]},
                       {"sha256": "a" * 64}, {"binding_id": 1}):
            with self.subTest(fields=fields), mock.patch.object(it, "prove") as prove:
                with self.assertRaisesRegex(ValueError, "execution inputs only"):
                    proof_service.produce(mock.Mock(), dict(self.payload(), **fields),
                                          actor="uid:1000", execution_user="worker")
                prove.assert_not_called()

    def test_unapproved_runner_cannot_turn_true_into_execution_evidence(self):
        with mock.patch.object(it, "prove") as prove:
            with self.assertRaisesRegex(ValueError, "not approved"):
                proof_service.produce(mock.Mock(), dict(self.payload(), runner="/bin/true"),
                                      actor="uid:1000", execution_user="worker",
                                      allowed_runners=["/usr/bin/python3"])
        prove.assert_not_called()

    def test_project_is_checked_and_observed_execution_supplies_evidence(self):
        with mock.patch.object(it, "item", return_value={"app": "other"}), \
                mock.patch.object(it, "prove") as prove:
            with self.assertRaisesRegex(ValueError, "does not own"):
                proof_service.produce(mock.Mock(), self.payload(), actor="uid:1000", execution_user="worker")
            prove.assert_not_called()
        with mock.patch.object(it, "item", return_value={"app": "acme"}), \
                mock.patch.object(it, "prove", return_value={"id": 17}) as prove:
            result = proof_service.produce(mock.Mock(), self.payload(), actor="uid:1000", execution_user="worker")
        self.assertEqual(result, {"id": 17})
        prove.assert_called_once_with(mock.ANY, 1, "/artifact", actor="uid:1000", runner=None,
                                      arguments=[], timeout=60., execution_user="worker")

    def test_cli_uses_fixed_privileged_entry_without_evidence_or_config_arguments(self):
        process = mock.Mock(returncode=0, stdout=json.dumps({"milestone": {"id": 17}}))
        with mock.patch.object(proof_service.subprocess, "run", return_value=process) as run:
            self.assertEqual(proof_service.request("acme", 1, "/artifact"), {"id": 17})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["sudo", "-n", proof_service.HELPER])
        self.assertEqual(set(json.loads(kwargs["input"])), set(self.payload()))
        self.assertNotIn("env", kwargs)

    def test_worker_has_no_parent_environment_credentials_or_groups(self):
        account = mock.Mock(pw_uid=65534, pw_gid=65534, pw_dir="/nonexistent")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        artifact = Path(folder.name, "artifact")
        artifact.write_text("artifact")
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.wait.return_value = 0
        process.stdout.read.side_effect = [b"smoke", b""]
        with mock.patch("pwd.getpwnam", return_value=account), \
                mock.patch("os.geteuid", return_value=0), \
                mock.patch("subprocess.Popen", return_value=process) as popen:
            execute(artifact, execution_user="worker")
        _, options = popen.call_args
        self.assertEqual(options["user"], 65534)
        self.assertEqual(options["group"], 65534)
        self.assertEqual(options["extra_groups"], [])
        self.assertEqual(set(options["env"]), {"PATH", "HOME", "LANG"})
        self.assertEqual(options["cwd"], "/")

    def test_helper_rejects_unprivileged_start_and_writable_configuration(self):
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(ValueError, "requires root"):
                proof_service.main()
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder, "project.json")
            config.write_text("{}")
            with self.assertRaises(ValueError):
                proof_service.private_path(config)
