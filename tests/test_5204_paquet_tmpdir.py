# SPDX-License-Identifier: AGPL-3.0-only
"""#5204 : vrais worktrees hors du /tmp partagé, sans base ni réseau."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_3807_paquet_auto import paquet_auto as auto


class PaquetTmpdirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("-c", "user.name=Banc", "-c", "user.email=banc@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "base")
        self.home = self.root / "home"
        self.home.mkdir()
        patch = mock.patch.object(auto.Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def make(self, configured):
        with mock.patch.dict(os.environ):
            os.environ.pop("TMPDIR", None)
            if configured is not None:
                os.environ["TMPDIR"] = str(configured)
            path = auto.make_worktree(self.repo, self.git("rev-parse", "HEAD"),
                                      "test-5204")
        self.addCleanup(auto.remove_worktree, self.repo, path)
        self.assertTrue((path / ".git").is_file())
        return path

    def test_missing_tmpdir_creates_worktree_in_user_cache(self):
        path = self.make(None)
        self.assertEqual(path.parent, self.home / ".cache/ameesh/paquet-auto")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_shared_tmp_and_descendants_use_user_cache(self):
        for configured in ("/tmp", "/tmp/unused-5204", ""):
            with self.subTest(tmpdir=configured), mock.patch.dict(
                    os.environ, {"TMPDIR": configured}):
                self.assertEqual(auto.temporary_parent(),
                                 self.home / ".cache/ameesh/paquet-auto")

    def test_alias_of_shared_tmp_uses_user_cache(self):
        alias = self.root / "alias"
        alias.symlink_to("/tmp", target_is_directory=True)
        with mock.patch.dict(os.environ, {"TMPDIR": str(alias)}):
            self.assertEqual(auto.temporary_parent(),
                             self.home / ".cache/ameesh/paquet-auto")

    def test_explicit_disk_tmpdir_is_created_and_preserved(self):
        configured = Path("/managed-5204/tmp")
        with mock.patch.dict(os.environ, {"TMPDIR": str(configured)}), \
                mock.patch.object(auto.Path, "mkdir") as mkdir:
            self.assertEqual(auto.temporary_parent(), configured)
        mkdir.assert_called_once_with(parents=True, exist_ok=True, mode=0o700)

    def test_unit_creates_the_configured_cache(self):
        unit = (Path(__file__).resolve().parents[1] / "deploy/systemd/"
                "ameesh-paquet-horaire.service").read_text()
        self.assertIn("CacheDirectory=ameesh/paquet-auto\n", unit)
        self.assertIn("CacheDirectoryMode=0700\n", unit)
        self.assertIn("Environment=TMPDIR=%h/.cache/ameesh/paquet-auto\n", unit)


if __name__ == "__main__":
    unittest.main()
