# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated Git snapshots and simulated dependency builds; no host activation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/environnement-versionne.py"
SPEC = importlib.util.spec_from_file_location("versioned_environment", PATH)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


BACKEND = '''from pathlib import Path
import zipfile
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    version = Path("release.txt").read_text().strip()
    name = "ameesh-1.0-py3-none-any.whl"
    info = "ameesh-1.0.dist-info/"
    files = {"ameesh/__init__.py": "", "ameesh/budget.py": "", "ameesh/projects.py": "",
             info + "METADATA": "Metadata-Version: 2.1\\nName: ameesh\\nVersion: 1.0\\nRequires-Dist: psycopg==" + version + "\\n",
             info + "WHEEL": "Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    files[info + "RECORD"] = "".join(path + ",,\\n" for path in [*files, info + "RECORD"])
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
        for path, content in files.items():
            wheel.writestr(path, content)
    return name
'''


class VersionedEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-release-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.store = self.root / "releases"
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="test", GIT_COMMITTER_NAME="test",
                        GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "-q")
        self.a = self.commit("A")
        self.b = self.commit("B")
        self.wheel = self.root / "dependency-1-py3-none-any.whl"
        self.wheel.write_bytes(b"synthetic wheel")
        self.manifest = {"commit": self.a, "python": sys.executable,
                         "python_sha256": release.digest(sys.executable),
                         "python_version": "3.test", "pip_version": "test",
                         "wheels": [{"path": str(self.wheel), "sha256": release.digest(self.wheel)}]}
        self.calls = []
        self.actual_run = release.run

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              check=True, text=True, capture_output=True).stdout.strip()

    def commit(self, content):
        (self.repo / "release.txt").write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", content)
        return self.git("rev-parse", "HEAD")

    def build(self, args, *, env=None, cwd=None):
        if args[0] == "git":
            return self.actual_run(args, env=env, cwd=cwd)
        self.calls.append(args)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
        self.assertNotIn("PYTHONPATH", env)
        if args[1:3] == ["-m", "venv"]:
            executable = Path(args[-1]) / "bin/python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
        if args[-1] == "import platform; print(platform.python_version())":
            return "3.test"
        if args[-1] == "import pip; print(pip.__version__)":
            return "test"
        if "install" in args:
            self.assertIn("--no-index", args)
            self.assertIn("--no-deps", args)
            if Path(args[-1]).is_dir():
                (Path(args[0]).parents[1] / "installed-release").write_text(
                    (Path(args[-1]) / "release.txt").read_text())
        return ""

    def prepare(self, commit=None):
        with mock.patch.object(release, "run", side_effect=self.build):
            return release.prepare(self.store, self.repo, dict(self.manifest, commit=commit or self.a))

    def test_exact_snapshot_offline_dependencies_and_reuse(self):
        with mock.patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://example.invalid",
                                          "PYTHONPATH": "/untrusted"}):
            identity = self.prepare()
        destination = self.store / identity
        self.assertEqual((destination / "source/release.txt").read_text(), "A")
        self.assertEqual(json.loads((destination / "ready.json").read_text()), self.manifest)
        wheels = list((destination / "wheels").iterdir())
        self.assertEqual(len(wheels), 1)
        self.assertEqual(wheels[0].read_bytes(), self.wheel.read_bytes())
        calls = list(self.calls)
        self.assertEqual(self.prepare(), identity)
        self.assertEqual(self.calls, calls)
        self.assertFalse((self.store / "current").exists())

    def test_atomic_pair_and_rollback_preserve_old_environment(self):
        a, b = self.prepare(), self.prepare(self.b)
        release.activate(self.store, a, fenced=True)
        self.assertEqual((self.store / "current/venv/installed-release").read_text(), "A")
        release.activate(self.store, b, fenced=True)
        self.assertEqual((self.store / "current/source/release.txt").read_text(), "B")
        self.assertEqual(os.readlink(self.store / "current"), b)
        self.assertEqual((self.store / "current/venv/installed-release").read_text(), "B")
        release.activate(self.store, b, fenced=True)  # retry keeps the original rollback pair
        self.assertEqual(json.loads((self.store / "switch.json").read_text())["previous"], a)
        calls = list(self.calls)
        release.rollback(self.store, fenced=True)
        release.rollback(self.store, fenced=True)
        self.assertEqual((self.store / "current/source/release.txt").read_text(), "A")
        self.assertEqual((self.store / "current/venv/installed-release").read_text(), "A")
        self.assertEqual(os.readlink(self.store / "current"), a)
        self.assertEqual(self.calls, calls)  # no dependency installation during rollback

    def test_operation_pin_survives_lost_receipt_and_global_previous_changes(self):
        a, b = self.prepare(), self.prepare(self.b)
        release.activate(self.store, a, fenced=True)
        pinned = release.pin_switch(self.store, "release-B", b)
        self.assertEqual(pinned, {"previous": a, "target": b})
        release.activate(self.store, b, fenced=True, operation="release-B")
        self.assertEqual(release.pin_switch(self.store, "release-B", b), pinned)
        # A shared/global previous marker is not this operation's authority.
        (self.store / "switch.json").write_text(json.dumps({"previous": b, "target": b}))
        calls = list(self.calls)
        release.rollback(self.store, fenced=True, operation="release-B")
        release.rollback(self.store, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), a)
        self.assertEqual((self.store / "current/source/release.txt").read_text(), "A")
        self.assertEqual((self.store / "current/venv/installed-release").read_text(), "A")
        self.assertEqual(self.calls, calls)

    def test_operation_switch_recovers_after_pointer_changed_without_reply(self):
        a, b = self.prepare(), self.prepare(self.b)
        release.activate(self.store, a, fenced=True)
        release.pin_switch(self.store, "release-B", b)
        actual_sync = release.sync_directory
        def lost_reply(directory):
            if Path(directory) == self.store:
                raise OSError("lost reply after pointer changed")
            return actual_sync(directory)
        with mock.patch.object(release, "sync_directory", side_effect=lost_reply), self.assertRaises(OSError):
            release.activate(self.store, b, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), b)
        release.activate(self.store, b, fenced=True, operation="release-B")
        release.rollback(self.store, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), a)

    def test_operation_rollback_before_activation_and_conflicting_target(self):
        a, b = self.prepare(), self.prepare(self.b)
        release.activate(self.store, a, fenced=True)
        release.pin_switch(self.store, "release-B", b)
        # Configuration can fail before code activation has written any intent.
        release.rollback(self.store, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), a)
        with self.assertRaisesRegex(release.ReleaseError, "target changed"):
            release.pin_switch(self.store, "release-B", a)
        with self.assertRaisesRegex(release.ReleaseError, "pinned operation"):
            release.activate(self.store, a, fenced=True, operation="release-B")

    def test_operation_cannot_switch_or_undo_unrelated_current_release(self):
        a, b = self.prepare(), self.prepare(self.b)
        c = self.prepare(self.commit("C"))
        release.activate(self.store, a, fenced=True)
        release.pin_switch(self.store, "release-B", b)
        release.activate(self.store, c, fenced=True)
        with self.assertRaisesRegex(release.ReleaseError, "pinned operation"):
            release.activate(self.store, b, fenced=True, operation="release-B")
        with self.assertRaisesRegex(release.ReleaseError, "recorded switch"):
            release.rollback(self.store, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), c)

    def test_operation_pin_requires_prior_and_rejects_unsafe_receipts(self):
        a, b = self.prepare(), self.prepare(self.b)
        with self.assertRaisesRegex(release.ReleaseError, "prior release"):
            release.pin_switch(self.store, "release-B", b)
        release.activate(self.store, a, fenced=True)
        with self.assertRaisesRegex(release.ReleaseError, "invalid switch operation"):
            release.pin_switch(self.store, "../escape", b)
        release.pin_switch(self.store, "release-B", b)
        receipt = self.store / ".operations/release-B.json"
        receipt.chmod(0o644)
        with self.assertRaisesRegex(release.ReleaseError, "unsafe switch receipt"):
            release.activate(self.store, b, fenced=True, operation="release-B")
        receipt.unlink()
        receipt.symlink_to(self.store / "switch.json")
        with self.assertRaises(OSError):
            release.activate(self.store, b, fenced=True, operation="release-B")
        self.assertEqual(os.readlink(self.store / "current"), a)

    def test_failed_import_keeps_previous_pair_and_cleans_incomplete_build(self):
        a = self.prepare()
        release.activate(self.store, a, fenced=True)
        def fail(args, **kw):
            if args[-1] == "import psycopg, ameesh.budget, ameesh.projects":
                raise release.ReleaseError("probe failed")
            return self.build(args, **kw)
        with mock.patch.object(release, "run", side_effect=fail), self.assertRaises(release.ReleaseError):
            release.prepare(self.store, self.repo, dict(self.manifest, commit=self.b))
        self.assertEqual(os.readlink(self.store / "current"), a)
        self.assertEqual([p.name for p in self.store.iterdir() if p.is_dir() and not p.is_symlink()], [a])

    def test_wrong_input_digests_and_missing_fence_refused(self):
        a = self.prepare()
        with self.assertRaises(release.ReleaseError):
            release.activate(self.store, a)
        self.assertFalse((self.store / "current").exists())
        with self.assertRaises(release.ReleaseError):
            release.rollback(self.store)
        for changes in ({"python_sha256": "0" * 64}, {"commit": "HEAD"},
                        {"wheels": [{"path": str(self.wheel), "sha256": "0" * 64}]}):
            with self.subTest(changes=changes), self.assertRaises(release.ReleaseError):
                release.prepare(self.store, self.repo, dict(self.manifest, **changes))

    def test_repository_links_and_tampered_receipts_refused(self):
        (self.repo / "external").symlink_to(self.root)
        linked = self.commit("link")
        with self.assertRaises(release.ReleaseError):
            self.prepare(linked)
        a = self.prepare()
        (self.store / a / "ready.json").write_text("{}")
        with self.assertRaises(release.ReleaseError):
            release.activate(self.store, a, fenced=True)
        self.assertFalse((self.store / "current").exists())

    def test_crash_before_switch_leaves_previous_release_recoverable(self):
        a, b = self.prepare(), self.prepare(self.b)
        release.activate(self.store, a, fenced=True)
        actual_replace = os.replace
        def fail_pointer(source, target):
            if Path(target).name == "current":
                raise OSError("simulated crash")
            return actual_replace(source, target)
        with mock.patch.object(os, "replace", side_effect=fail_pointer), self.assertRaises(OSError):
            release.activate(self.store, b, fenced=True)
        self.assertEqual(os.readlink(self.store / "current"), a)
        release.rollback(self.store, fenced=True)
        self.assertEqual(os.readlink(self.store / "current"), a)
        release.activate(self.store, b, fenced=True)
        self.assertEqual(os.readlink(self.store / "current"), b)

    def test_real_offline_venvs_keep_previous_dependency_on_rollback(self):
        """Real pip/venv, tiny synthetic wheels, no package index or mesh DB."""
        import ensurepip
        import platform
        (self.repo / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n')
        (self.repo / "backend.py").write_text(BACKEND)
        identities = []
        for version in ("1.0", "2.0"):
            commit = self.commit(version)
            path = self.root / ("psycopg-" + version + "-py3-none-any.whl")
            info = "psycopg-" + version + ".dist-info/"
            files = {"psycopg/__init__.py": "__version__ = " + repr(version) + "\n",
                     info + "METADATA": "Metadata-Version: 2.1\nName: psycopg\nVersion: " + version + "\n",
                     info + "WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
            files[info + "RECORD"] = "".join(name + ",,\n" for name in [*files, info + "RECORD"])
            with zipfile.ZipFile(path, "w") as wheel:
                for name, content in files.items():
                    wheel.writestr(name, content)
            manifest = dict(self.manifest, commit=commit, python_version=platform.python_version(),
                            pip_version=ensurepip.version(), wheels=[{"path": str(path), "sha256": release.digest(path)}])
            identity = release.prepare(self.store, self.repo, manifest)
            identities.append(identity)
            release.activate(self.store, identity, fenced=True)
            executable = self.store / "current/venv/bin/python"
            actual = release.run([str(executable), "-I", "-c", "import psycopg; print(psycopg.__version__)"], cwd=self.root)
            self.assertEqual(actual, version)
        release.rollback(self.store, fenced=True)
        self.assertEqual(os.readlink(self.store / "current"), identities[0])
        self.assertEqual((self.store / "current/source/release.txt").read_text(), "1.0")
        self.assertEqual(release.run([str(self.store / "current/venv/bin/python"), "-I", "-c",
                                      "import psycopg; print(psycopg.__version__)"], cwd=self.root), "1.0")


if __name__ == "__main__":
    unittest.main()
