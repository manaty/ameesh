# SPDX-License-Identifier: AGPL-3.0-only
"""L53: off-device session backups."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest

from ameesh import harnesses, session_backup as sb
from ameesh.runner import AgentWorker

SESSION = "session-bae71c17-331f-421b-b645-d3a590b676a3"
DSH = ("sessions/*/{session}",)
CLAUDE = ("projects/*/{session}.jsonl", "projects/*/{session}")


def write_file(path: str, text: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-l53-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.target = os.path.join(self.tmp, "target")
        self.state = os.path.join(self.tmp, "state")
        write_file(os.path.join(self.home, "sessions/--directory--", SESSION, "session.v4.jsonl.zstd"),
               "content")
        write_file(os.path.join(self.home, "sessions/--directory--", SESSION, "session.lock"), "")
        write_file(os.path.join(self.home, "sessions/--directory--/other/session.v4.jsonl.zstd"), "no")

    def conf(self, **extra):
        raw = {"target": self.target, "encrypt": False, "min_interval_s": 0}
        raw.update(extra)
        return sb.parse(raw)


class ConfigurationTest(unittest.TestCase):
    def test_absent(self):
        self.assertIsNone(sb.parse({}))

    def test_encryption_without_recipient_rejected(self):
        with self.assertRaises(sb.BackupError):
            sb.parse({"target": "/srv/copies"})

    def test_relative_target_rejected(self):
        with self.assertRaises(sb.BackupError):
            sb.parse({"target": "copies", "encrypt": False})

    def test_s3(self):
        conf = sb.parse({"target": "s3://bucket/mesh/", "recipients": ["ABCD"]})
        self.assertTrue(conf.is_s3)
        self.assertEqual(conf.target, "s3://bucket/mesh")


class SessionFilesTest(_Tmp):
    def test_only_session_directory(self):
        paths = sb.session_paths(self.home, DSH, SESSION)
        self.assertEqual([os.path.relpath(p, self.home) for p in paths],
                         ["sessions/--directory--/" + SESSION])

    def test_claude_file_and_directory(self):
        write_file(os.path.join(self.home, "projects/-a/s1.jsonl"))
        write_file(os.path.join(self.home, "projects/-a/s1/subagents/x.jsonl"))
        paths = sb.session_paths(self.home, CLAUDE, "s1")
        self.assertEqual(sorted(os.path.relpath(p, self.home) for p in paths),
                         ["projects/-a/s1", "projects/-a/s1.jsonl"])

    def test_unsafe_identifier_rejected(self):
        for invalid in ("*", "../x", "a/b", "", "s[1]"):
            with self.assertRaises(sb.BackupError):
                sb.session_paths(self.home, DSH, invalid)

    def test_link_outside_home_ignored(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.home, "sessions/--directory--/link"))
        self.assertEqual(sb.session_paths(self.home, DSH, "link"), [])


class RoundTripTest(_Tmp):
    def test_unchanged_then_changed_backup(self):
        conf = self.conf()
        r = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                      session=SESSION, state_dir=self.state, now=1000.0)
        self.assertEqual(r["status"], "copied")
        self.assertTrue(r["key"].startswith("pc/deepseek2/%s/" % SESSION))
        self.assertTrue(r["key"].endswith(".tar.gz"))
        r2 = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                       session=SESSION, state_dir=self.state, now=1001.0)
        self.assertEqual(r2["status"], "unchanged")
        write_file(os.path.join(self.home, "sessions/--directory--", SESSION, "session.v4.jsonl.zstd"),
               "longer content")
        r3 = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                       session=SESSION, state_dir=self.state, now=1002.0)
        self.assertEqual(r3["status"], "copied")
        self.assertEqual(len(sb.list_keys(conf, "pc/")), 2)

    def test_minimum_interval(self):
        conf = self.conf(min_interval_s=600)
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.state, now=1000.0)
        write_file(os.path.join(self.home, "sessions/--directory--", SESSION, "n"), "new")
        r = sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH,
                      session=SESSION, state_dir=self.state, now=1100.0)
        self.assertEqual(r["status"], "too_soon")

    def test_restore_on_another_host(self):
        conf = self.conf()
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.state, now=1000.0)
        elsewhere = os.path.join(self.tmp, "server")
        key = sb.latest(conf, agent="a", session=SESSION)
        files = sb.restore(conf, key, elsewhere)
        restored = os.path.join(elsewhere, "sessions/--directory--", SESSION, "session.v4.jsonl.zstd")
        with open(restored, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "content")
        self.assertNotIn("session.lock", " ".join(files))
        with self.assertRaises(sb.BackupError):
            sb.restore(conf, key, elsewhere)
        sb.restore(conf, key, elsewhere, overwrite=True)

    def test_escaping_archive_rejected(self):
        conf = self.conf()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evasion")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        sb.put(conf, "pc/a/s/1.tar.gz", buf.getvalue())
        with self.assertRaises(sb.BackupError):
            sb.restore(conf, "pc/a/s/1.tar.gz", os.path.join(self.tmp, "server"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evasion")))


@unittest.skipUnless(shutil.which("gpg"), "gpg missing")
class EncryptionTest(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.gnupg = tempfile.mkdtemp(prefix="g", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.gnupg, True)
        os.chmod(self.gnupg, 0o700)
        previous = os.environ.get("GNUPGHOME")
        os.environ["GNUPGHOME"] = self.gnupg
        self.addCleanup(lambda: os.environ.pop("GNUPGHOME") if previous is None
                        else os.environ.__setitem__("GNUPGHOME", previous))
        subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-gen-key",
                        "test-l53@ameesh.invalid", "default", "default", "never"],
                       check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["gpgconf", "--kill", "gpg-agent"], capture_output=True)

    def test_encrypted_round_trip(self):
        conf = sb.parse({"target": self.target, "recipients": ["test-l53@ameesh.invalid"],
                         "min_interval_s": 0})
        r = sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                      state_dir=self.state)
        self.assertTrue(r["key"].endswith(".tar.gz.gpg"))
        with open(os.path.join(self.target, r["key"]), "rb") as fh:
            self.assertNotIn(b"content", fh.read())
        elsewhere = os.path.join(self.tmp, "server")
        sb.restore(conf, r["key"], elsewhere)
        with open(os.path.join(elsewhere, "sessions/--directory--", SESSION,
                               "session.v4.jsonl.zstd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "content")


class DescriptorsTest(unittest.TestCase):
    def test_all_harnesses_declare_session_files(self):
        for h in ("claude", "codex", "deepseek"):
            d = harnesses.get(h)
            self.assertTrue(d.session_files, h)
            self.assertTrue(all("{session}" in f for f in d.session_files), h)

    def test_invalid_pattern_rejected(self):
        for invalid in (["sessions/*"], ["/abs/{session}"], ["../{session}"], []):
            findings = harnesses.validate({"id": "x", "name": "x", "version": "1",
                                           "schema_version": "1", "description": "x",
                                           "ameesh": {"protocol": "cli", "binary": "x",
                                                      "backup": {"session_files": invalid}}})
            self.assertIn("harness-backup-invalid", {f.code for f in findings}, invalid)


class ExecutorTest(_Tmp):
    def test_backup_at_turn_end(self):
        logs = []
        worker = types.SimpleNamespace(
            name="deepseek2", agent={"harness": "deepseek"},
            runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(session_backup={"target": self.target, "encrypt": False,
                                                       "min_interval_s": 0},
                                      host="pc", state_dir=self.state))
        adapter = types.SimpleNamespace(descriptor=types.SimpleNamespace(session_files=DSH))
        os.environ["DSH_HOME"] = self.home
        self.addCleanup(os.environ.pop, "DSH_HOME", None)
        import ameesh.runner as runner_mod
        previous_log = runner_mod.log
        previous_async_log = runner_mod.log_async
        runner_mod.log_async = logs.append
        self.addCleanup(setattr, runner_mod, "log_async", previous_async_log)
        runner_mod.log = logs.append
        self.addCleanup(setattr, runner_mod, "log", previous_log)
        AgentWorker._backup_session(worker, adapter, None, SESSION)
        for t in threading.enumerate():
            if t.name == "deepseek2-session-backup":
                t.join(10)
        self.assertTrue(any("backed up" in line for line in logs), logs)
        self.assertEqual(len(sb.list_keys(sb.parse(worker.cfg.session_backup), "pc/")), 1)

    def test_invalid_configuration_logged_once(self):
        logs = []
        worker = types.SimpleNamespace(
            name="a", agent={"harness": "deepseek"}, runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(session_backup={"target": "/srv"}, host="pc",
                                      state_dir=self.state))
        import ameesh.runner as runner_mod
        previous_log = runner_mod.log
        previous_async_log = runner_mod.log_async
        runner_mod.log_async = logs.append
        self.addCleanup(setattr, runner_mod, "log_async", previous_async_log)
        runner_mod.log = logs.append
        self.addCleanup(setattr, runner_mod, "log", previous_log)
        AgentWorker._backup_session(worker, None, None, SESSION)
        AgentWorker._backup_session(worker, None, None, SESSION)
        self.assertEqual(len([j for j in logs if "disabled" in j]), 1)


if __name__ == "__main__":
    unittest.main()


class S3EndpointTest(unittest.TestCase):
    def setUp(self):
        self.cfg = sb.parse({"target": "s3://bucket/backups", "encrypt": False})
        self.env = {"AMEESH_BACKUP_S3_ACCESS_KEY": "test-access",
                    "AMEESH_BACKUP_S3_SECRET_KEY": "test-secret",
                    "AMEESH_BACKUP_S3_REGION": "test-region"}

    def test_no_provider_default(self):
        with self.assertRaisesRegex(sb.BackupError, "AMEESH_BACKUP_S3_ENDPOINT"):
            sb._s3(self.cfg, self.env)
        self.env["AMEESH_BACKUP_S3_ENDPOINT"] = "https://objects.example.test"
        base, prefix, _, region = sb._s3(self.cfg, self.env)
        self.assertEqual((base, prefix, region),
                         ("https://objects.example.test/bucket", "backups", "test-region"))
        del self.env["AMEESH_BACKUP_S3_REGION"]
        with self.assertRaisesRegex(sb.BackupError, "AMEESH_BACKUP_S3_REGION"):
            sb._s3(self.cfg, self.env)

    def test_endpoint_rejects_plaintext_and_embedded_credentials(self):
        for endpoint in ("http://objects.example.test", "https://user:secret@objects.example.test",
                         "https://objects.example.test?secret=x", "https://objects.example.test#x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(sb.BackupError):
                sb._s3(self.cfg, dict(self.env, AMEESH_BACKUP_S3_ENDPOINT=endpoint))

    def test_listing_decodes_xml_and_encodes_continuation_token(self):
        from unittest.mock import patch
        env = dict(self.env, AMEESH_BACKUP_S3_ENDPOINT="https://objects.example.test")
        replies = [b'<ListBucketResult xmlns="urn:test"><Contents><Key>backups/a&amp;b</Key>'
                   b'</Contents><NextContinuationToken>a+b/=</NextContinuationToken></ListBucketResult>',
                   b'<ListBucketResult><Contents><Key>backups/second</Key></Contents></ListBucketResult>']
        with patch.object(sb, "_curl", side_effect=replies) as curl:
            self.assertEqual(sb.list_keys(self.cfg, "", env), ["a&b", "second"])
            self.assertIn("continuation-token=a%2Bb%2F%3D", curl.call_args_list[1].args[0][-1])


class RestoreSafetyTest(_Tmp):
    def test_rejects_special_files_and_links(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                    member = tarfile.TarInfo("session/unsafe")
                    member.type = kind
                    member.linkname = "../outside"
                    tar.addfile(member)
                cfg = self.conf()
                sb.put(cfg, "pc/a/s/unsafe.tar.gz", buf.getvalue())
                with self.assertRaises(sb.BackupError):
                    sb.restore(cfg, "pc/a/s/unsafe.tar.gz", os.path.join(self.tmp, "restored"))

    def test_local_key_cannot_escape_storage_target(self):
        cfg = self.conf()
        for key in ("../outside", os.path.join(self.tmp, "outside")):
            with self.subTest(key=key):
                with self.assertRaises(sb.BackupError):
                    sb.put(cfg, key, b"test")
                with self.assertRaises(sb.BackupError):
                    sb.get(cfg, key)
