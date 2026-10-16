# SPDX-License-Identifier: AGPL-3.0-only
"""Enrolled version-retention provider wired into the daily expiry helper.

The provider document is protected, names exactly one object host, and is only
resolved when a candidate still has an undeleted remote member. Credential
values never reach an argument, a receipt or a printed error. No Scaleway
request is made: the HTTP transport is simulated.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests import support  # noqa: F401  (isolates the runner environment)


DEPLOY = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour"
RETENTION_PATH = DEPLOY / "retention-privee.py"
PROVIDER_PATH = DEPLOY / "retention-scaleway.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


retention = load("retention_provider_wiring", RETENTION_PATH)
backend = load("retention_provider_transport", PROVIDER_PATH)

ACCESS = "SYNTHETIC-ACCESS"
SECRET = "SYNTHETIC-SECRET"


class ProviderWiringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-retention-provider-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.staging = self.root / "staging"
        self.objects = {"v1": b"pinned private dump", "v2": b"newer dump"}
        self.sha = hashlib.sha256(self.objects["v1"]).hexdigest()
        self.requests = []
        self.credentials = self.root / "retention.json"
        self.credentials.write_text(json.dumps(dict(
            access_key=ACCESS, secret_key=SECRET, bucket="fixture-private", region="fr-par",
            purpose="retention-versions", allowed_hosts=["test-host"])))
        self.credentials.chmod(0o600)
        self.enrollment = self.root / "retention-provider.json"
        self.enrolled(host="test-host")
        # The helper loads its fixed sibling; the test uses a private copy of
        # the real module at the installed mode instead of the shared clone.
        self.lib = self.root / "lib"
        self.lib.mkdir(mode=0o700)
        shutil.copyfile(PROVIDER_PATH, self.lib / (retention.PROVIDER_MODULE + ".py"))
        (self.lib / (retention.PROVIDER_MODULE + ".py")).chmod(0o644)
        location = mock.patch.object(retention, "__file__",
                                     str(self.lib / "ameesh-retention-privee.py"))
        location.start()
        self.addCleanup(location.stop)
        patcher = mock.patch.object(backend.subprocess, "run", side_effect=self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enrolled(self, *, host, credentials=None, staging=None, budget=4096):
        self.enrollment.write_text(json.dumps(dict(
            host=host, credentials=str(credentials or self.credentials),
            staging=str(staging or self.staging), max_bytes=budget)))
        self.enrollment.chmod(0o600)
        return self.enrollment

    def transport(self, args, **kw):
        if args == ["/usr/bin/curl", "--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="curl 8.4.0 (fixture)")
        self.requests.append(list(args))
        self.assertEqual(args[:2], ["/usr/bin/curl", "--disable"])
        self.assertNotIn("--location", args)
        self.assertEqual(kw["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        for canary in (ACCESS, SECRET):
            self.assertNotIn(canary, repr(args))
        config = Path(args[args.index("--config") + 1])
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertIn(SECRET, config.read_text())
        url = urlsplit(args[-1])
        self.assertEqual(url.hostname, "fixture-private.s3.fr-par.scw.cloud")
        version = parse_qs(url.query)["versionId"][0]
        method = args[args.index("--request") + 1]
        body = Path(args[args.index("--output") + 1])
        headers = Path(args[args.index("--dump-header") + 1])
        if method == "DELETE":
            self.objects.pop(version, None)
            status = "204"
        elif version in self.objects:
            body.write_bytes(self.objects[version])
            headers.write_text("HTTP/1.1 200 OK\r\nx-amz-version-id: " + version + "\r\n\r\n")
            status = "200"
        else:
            body.write_bytes(b'<Error xmlns="fixture"><Code>NoSuchVersion</Code></Error>')
            status = "404"
        return subprocess.CompletedProcess(args, 0, stdout=status)

    def artifact(self, *, artifact="release-old", host="test-host", version="v1", versioned=True):
        name = artifact + ".dump"
        path = self.root / name
        path.write_bytes(self.objects[version])
        path.chmod(0o600)
        reference = dict(object_id=host + "/" + name, sha256=self.sha)
        if versioned:
            reference["version_id"] = version
        retention.register(self.root, artifact, [name], expires_at=10, rollback_until=20,
                           remote=[reference])
        retention.protect(self.root, artifact, accepted=True)
        return path

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["ameesh-retention-privee.py", *argv]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = retention.main()
        return code, output.getvalue(), errors.getvalue()

    def prune(self):
        return self.cli("--root", str(self.root), "prune", "--now", "30",
                        "--provider-enrollment", str(self.enrollment))

    def test_enrolled_provider_expires_only_the_pinned_version_then_local(self):
        path = self.artifact()
        code, output, errors = self.prune()
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), {"deleted": ["release-old"]})
        self.assertFalse(path.exists())
        self.assertEqual(self.objects, {"v2": b"newer dump"})
        methods = [request[request.index("--request") + 1] for request in self.requests]
        self.assertEqual(methods.count("DELETE"), 1)
        self.assertEqual(list(self.staging.iterdir()), [])
        catalog = (self.root / ".retention.json").read_text()
        self.assertIn("v1", catalog)
        self.assertNotIn(SECRET, catalog)

    def test_provider_is_resolved_only_when_a_remote_member_must_be_deleted(self):
        name = "local-only.dump"
        path = self.root / name
        path.write_bytes(b"local private dump")
        path.chmod(0o600)
        retention.register(self.root, "local-only", [name], expires_at=10, rollback_until=20)
        retention.protect(self.root, "local-only", accepted=True)

        def never():
            raise AssertionError("provider resolved without a remote member to expire")

        self.assertEqual(retention.prune(self.root, now=30, remote_provider=never), ["local-only"])
        self.assertFalse(path.exists())
        self.assertEqual(self.requests, [])

    def test_enrollment_must_be_private_exact_and_bounded(self):
        self.enrollment.chmod(0o644)
        with self.assertRaisesRegex(retention.RetentionError, "private owned regular file"):
            retention.provider_adapter(self.enrollment)
        self.enrollment.chmod(0o600)
        document = json.loads(self.enrollment.read_text())
        for changed in (dict(document, extra="x"), {key: value for key, value in document.items() if key != "host"}):
            self.enrollment.write_text(json.dumps(changed))
            self.enrollment.chmod(0o600)
            with self.assertRaisesRegex(retention.RetentionError, "^exact provider enrollment required$"):
                retention.provider_adapter(self.enrollment)
        for changed in (dict(document, host="bad host"), dict(document, host=""),
                        dict(document, credentials="relative.json"),
                        dict(document, staging=str(self.root / ".." / "staging")),
                        dict(document, max_bytes=0), dict(document, max_bytes=True)):
            self.enrollment.write_text(json.dumps(changed))
            self.enrollment.chmod(0o600)
            with self.assertRaises(retention.RetentionError):
                retention.provider_adapter(self.enrollment)
        self.enrolled(host="test-host", credentials=self.root / "missing.json")
        with self.assertRaisesRegex(retention.RetentionError, "^provider retention adapter unavailable$"):
            retention.provider_adapter(self.enrollment)

    def test_linked_enrollment_and_module_are_refused(self):
        self.artifact()
        link = self.root / "linked-provider.json"
        link.symlink_to(self.enrollment)
        with self.assertRaisesRegex(retention.RetentionError, "^private provider enrollment required$"):
            retention.provider_adapter(link)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        with mock.patch.object(retention, "__file__", str(elsewhere / "ameesh-retention-privee.py")):
            with self.assertRaisesRegex(retention.RetentionError, "^provider retention module unavailable$"):
                retention.provider_adapter(self.enrollment)
        untrusted = elsewhere / (retention.PROVIDER_MODULE + ".py")
        untrusted.write_text("raise AssertionError('must not load')\n")
        untrusted.chmod(0o666)
        with mock.patch.object(retention, "__file__", str(elsewhere / "ameesh-retention-privee.py")):
            with self.assertRaisesRegex(retention.RetentionError, "^untrusted provider retention module$"):
                retention.provider_adapter(self.enrollment)

    def test_root_owned_installed_provider_module_is_accepted(self):
        # The installer copies the fixed module as root (0755) under
        # /usr/local/sbin while the helper runs as the enrolled user: the
        # installed case is root-owned and must load.
        self.artifact()
        real = retention.os.lstat
        def root_owned(path):
            info = real(path)
            if Path(path).name == retention.PROVIDER_MODULE + ".py":
                fields = list(info); fields[4] = 0
                return os.stat_result(fields)
            return info
        with mock.patch.object(retention.os, "lstat", side_effect=root_owned):
            loaded = retention.provider_module()
        self.assertTrue(callable(getattr(loaded, "VersionRetention", None)))

    def test_host_outside_credential_allowlist_refused_without_request(self):
        self.artifact()
        self.enrolled(host="other-host")
        with self.assertRaisesRegex(retention.RetentionError, "outside credential host scope"):
            retention.provider_adapter(self.enrollment)
        self.assertEqual(self.requests, [])
        self.assertTrue((self.root / "release-old.dump").exists())

    def test_object_outside_the_enrolled_host_is_never_addressed(self):
        self.credentials.write_text(json.dumps(dict(
            access_key=ACCESS, secret_key=SECRET, bucket="fixture-private", region="fr-par",
            purpose="retention-versions", allowed_hosts=["test-host", "other-host"])))
        self.credentials.chmod(0o600)
        path = self.artifact(host="other-host")
        code, output, errors = self.prune()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertNotIn(SECRET, errors)
        self.assertIn("private retention failed", errors)
        self.assertTrue(path.exists())
        self.assertEqual(self.requests, [])
        record = json.loads((self.root / ".retention.json").read_text())["release-old"]
        self.assertEqual(record["state"], "retained")
        self.assertTrue(record["remote"][0]["deleted"] is False)

    def test_unqualified_version_stays_protected(self):
        path = self.artifact(versioned=False)
        code, _, errors = self.prune()
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, errors)
        self.assertTrue(path.exists())
        self.assertEqual([request for request in self.requests
                          if request[request.index("--request") + 1] == "DELETE"], [])

    def test_without_enrollment_a_remote_set_is_never_guessed(self):
        path = self.artifact()
        code, output, errors = self.cli("--root", str(self.root), "prune", "--now", "30")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("private retention failed", errors)
        self.assertTrue(path.exists())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.objects, {"v1": b"pinned private dump", "v2": b"newer dump"})

    def test_daily_job_and_installer_use_the_fixed_provider_paths(self):
        daily = (DEPLOY.parent / "serveur/sauvegarder-base.sh").read_text()
        self.assertIn("--provider-enrollment /etc/ameesh/retention-provider.json", daily)
        self.assertIn("/usr/local/sbin/ameesh-retention-privee.py", daily)
        installer = (DEPLOY.parent / "serveur/installer-serveur.sh").read_text()
        self.assertIn('install -m 0755 "$SRC/deploy/mise-a-jour/retention-scaleway.py" '
                      "/usr/local/sbin/retention-scaleway.py", installer)


if __name__ == "__main__":
    unittest.main()
