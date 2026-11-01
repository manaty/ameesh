# SPDX-License-Identifier: AGPL-3.0-only
"""L53: off-device session backups."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import harnesses, session_backup as sb
from ameesh.runner import AgentWorker

from .support import EMPTY_CODEX_HOME

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

    def test_rollout_symlink_to_account_credentials_ignored(self):
        secret = os.path.join(self.home, "auth.json")
        write_file(secret, "private-account-token")
        rollout = os.path.join(self.home, "sessions/day/rollout-s1.jsonl")
        os.makedirs(os.path.dirname(rollout))
        os.symlink(secret, rollout)
        self.assertEqual(sb.session_paths(self.home, ("sessions/*/rollout-{session}.jsonl",), "s1"), [])
        with self.assertRaises(sb.BackupError):
            sb.archive(self.home, [rollout])

    def test_session_directory_symlink_inside_home_ignored(self):
        os.symlink(os.path.join(self.home, "sessions/--directory--", SESSION),
                   os.path.join(self.home, "sessions/--directory--/alias"))
        self.assertEqual(sb.session_paths(self.home, DSH, "alias"), [])

    def test_archive_omits_nested_credential_links(self):
        secret = os.path.join(self.home, "auth.json")
        write_file(secret, "private-account-token")
        session = os.path.join(self.home, "sessions/--directory--", SESSION)
        os.symlink(secret, os.path.join(session, "credential.json"))
        data = sb.archive(self.home, [session])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            self.assertFalse(any(m.issym() or m.islnk() for m in tar.getmembers()))
            self.assertFalse(any("credential" in m.name for m in tar.getmembers()))
            self.assertNotIn(b"private-account-token", b"".join(
                tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()))

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


class OwnerHomeTest(_Tmp):
    """N de #731 / #782 : le home du propriétaire ne vient jamais de l'appelant."""

    @staticmethod
    def cfg(accounts=None):
        return types.SimpleNamespace(accounts=accounts or {})

    @staticmethod
    def registry(row):
        """Une base minimale : restore_agent ne lit que ces colonnes."""
        class Tx:
            def query(self, sql, params):
                return [dict(row)]
        class Db:
            def transaction(self):
                return contextlib.nullcontext(Tx())
        return Db()

    def test_declared_profile_gives_the_owner_home(self):
        home = os.path.join(self.tmp, "compte-2")
        cfg = self.cfg({"deepseek": [{"name": "compte-2", "type": "config_dir",
                                      "path": home}]})
        row = {"harness": "deepseek", "session_account": "compte-2"}
        self.assertEqual(sb._agent_home(cfg, row, "a"), home)
        self.assertEqual(sb._session_patterns(row), DSH)

    def test_no_profile_never_falls_back_to_the_caller(self):
        os.environ["DSH_HOME"] = self.home          # l'appelant, pas le propriétaire
        self.addCleanup(os.environ.pop, "DSH_HOME", None)
        for account in (None, "inconnu"):
            with self.assertRaises(sb.BackupError) as caught:
                sb._agent_home(self.cfg(), {"harness": "deepseek",
                                            "session_account": account}, "agent-x")
            message = str(caught.exception)
            self.assertIn("agent-x", message)
            self.assertIn("--home", message)
            self.assertNotIn(self.home, message)
            self.assertNotIn("default home", message)

    def test_profile_without_a_path_never_falls_back_to_the_caller(self):
        """B de la relecture #782 : un profil déclaré sans chemin ne prouve rien.

        #5175 : les variables du banc sont rendues par `mock.patch.dict`, pas
        retirées. Les retirer laissait `CODEX_HOME` absent pour les modules
        suivants du même processus ; la garde de rythme Codex lisait alors les
        vrais journaux du compte (`~/.codex/sessions`).
        """
        cfg = self.cfg({
            "deepseek": [{"name": "primaire", "type": "config_dir"}],
            "codex": [{"name": "cle", "type": "api_key_env", "env": "OPENAI_API_KEY",
                       "key_env": "OPENAI_KEY"}],
        })
        avant = {nom: os.environ.get(nom) for nom in ("DSH_HOME", "CODEX_HOME")}
        with mock.patch.dict(os.environ, {"DSH_HOME": self.home,
                                          "CODEX_HOME": self.home}):
            for harness, account in (("deepseek", "primaire"), ("codex", "cle")):
                with self.subTest(harness=harness):
                    with self.assertRaises(sb.BackupError) as caught:
                        sb._agent_home(cfg, {"harness": harness,
                                             "session_account": account}, "agent-x")
                    message = str(caught.exception)
                    self.assertIn("agent-x", message)
                    self.assertIn("no path", message)
                    self.assertIn("--home", message)
                    self.assertNotIn(self.home, message)
        # Témoin de la fuite #5175 : les variables du banc sont rendues, pas
        # retirées ; le dossier Codex vide du banc reste en place.
        self.assertEqual({nom: os.environ.get(nom) for nom in avant}, avant)
        self.assertEqual(os.environ.get("CODEX_HOME"), EMPTY_CODEX_HOME)

    def test_explicit_home_over_an_unprovable_profile_needs_the_flag(self):
        os.environ["DSH_HOME"] = self.home
        self.addCleanup(os.environ.pop, "DSH_HOME", None)
        conf = self.conf()
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.state, now=1000.0)
        key = sb.latest(conf, agent="a", session=SESSION)
        cfg = self.cfg({"deepseek": [{"name": "primaire", "type": "config_dir"}]})
        row = {"harness": "deepseek", "session_id": SESSION, "session_account": "primaire",
               "status": "idle", "leased": False}
        args = types.SimpleNamespace(agent="a", key=key, session=None, home=None,
                                     overwrite=False, allow_other_agent=False)
        with self.assertRaises(sb.BackupError) as caught:
            sb.restore_agent(self.registry(row), cfg, conf, args)
        self.assertIn("--home", str(caught.exception))
        args.home = os.path.join(self.tmp, "explicite")
        # L731 : sans preuve du home du propriétaire, la désignation explicite
        # reste un home étranger — le drapeau est requis avant tout téléchargement.
        with self.assertRaises(sb.BackupError) as caught:
            sb.restore_agent(self.registry(row), cfg, conf, args)
        self.assertIn("--allow-other-agent", str(caught.exception))
        args.allow_other_agent = True
        _key, home, files = sb.restore_agent(self.registry(row), cfg, conf, args)
        self.assertEqual(home, args.home)
        self.assertTrue(files)
        self.assertTrue(os.path.exists(os.path.join(
            home, "sessions/--directory--", SESSION, "session.v4.jsonl.zstd")))

    def test_restore_needs_a_proof_or_a_flagged_explicit_home(self):
        conf = self.conf()
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.state, now=1000.0)
        key = sb.latest(conf, agent="a", session=SESSION)
        row = {"harness": "deepseek", "session_id": SESSION, "session_account": None,
               "status": "idle", "leased": False}
        args = types.SimpleNamespace(agent="a", key=key, session=None, home=None,
                                     overwrite=False, allow_other_agent=False)
        with self.assertRaises(sb.BackupError) as caught:
            sb.restore_agent(self.registry(row), self.cfg(), conf, args)
        self.assertIn("--home", str(caught.exception))
        args.home = os.path.join(self.tmp, "explicite")
        with self.assertRaises(sb.BackupError) as caught:
            sb.restore_agent(self.registry(row), self.cfg(), conf, args)
        self.assertIn("--allow-other-agent", str(caught.exception))
        args.allow_other_agent = True
        _key, home, files = sb.restore_agent(self.registry(row), self.cfg(), conf, args)
        self.assertEqual(home, args.home)
        self.assertTrue(files)
        with open(os.path.join(home, "sessions/--directory--", SESSION,
                               "session.v4.jsonl.zstd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "content")

    def test_backup_cli_needs_the_same_proof(self):
        row = {"harness": "deepseek", "session_id": SESSION, "session_account": None}
        cfg = types.SimpleNamespace(
            session_backup={"target": self.target, "encrypt": False, "min_interval_s": 0},
            accounts={}, host="pc", state_dir=self.state)
        class Db:
            def query(self, sql, params):
                return [dict(row)]
            def close(self):
                pass
        with mock.patch("ameesh.config.load", return_value=cfg), \
                mock.patch("ameesh.db.connect", return_value=Db()), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(sb.main(["backup", "a"]), 1)
            self.assertIn("--home", err.getvalue())
        self.assertEqual(sb.list_keys(self.conf(), "pc/"), [])
        with mock.patch("ameesh.config.load", return_value=cfg), \
                mock.patch("ameesh.db.connect", return_value=Db()):
            self.assertEqual(sb.main(["backup", "a", "--home", self.home]), 0)
        self.assertEqual(len(sb.list_keys(self.conf(), "pc/")), 1)


class EncryptArgvTest(unittest.TestCase):
    """#3320: encryption to public keys must not start a GnuPG agent."""

    def test_encrypt_does_not_autostart_an_agent(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"ciphertext", stderr=b"")

        with mock.patch.object(sb.subprocess, "run", side_effect=run):
            self.assertEqual(sb.encrypt(b"plain", ["key@example"]), b"ciphertext")
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[0], "gpg")
        self.assertIn("--no-autostart", argv)
        self.assertIn("--encrypt", argv)
        self.assertLess(argv.index("--no-autostart"), argv.index("--encrypt"))


@unittest.skipUnless(shutil.which("gpg"), "gpg missing")
class EncryptionTest(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.gnupg = tempfile.mkdtemp(prefix="g")
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

    def test_s3_credentials_use_private_config_for_all_requests(self):
        env = dict(self.env, AMEESH_BACKUP_S3_ENDPOINT="https://objects.example.test")
        configs = []
        def request(argv, **kwargs):
            self.assertNotIn("--user", argv)
            self.assertFalse(any(value in " ".join(argv) for value in ("test-access", "test-secret")))
            self.assertEqual(argv[argv.index("-H") + 1], "x-amz-content-sha256: "
                             + hashlib.sha256(kwargs.get("input") or b"").hexdigest())
            path = argv[argv.index("--config") + 1]
            configs.append(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path) as fh:
                self.assertEqual(fh.read(), 'user = "test-access:test-secret"\n')
            return subprocess.CompletedProcess(argv, 0, b"<ListBucketResult/>", b"")
        with mock.patch.object(sb.subprocess, "run", side_effect=request) as run:
            sb.put(self.cfg, "a/s/1", b"upload", env)
            self.assertEqual(run.call_args.kwargs["input"], b"upload")
            sb.get(self.cfg, "a/s/1", env)
            sb.list_keys(self.cfg, "", env)
        self.assertEqual(len(configs), 3)
        self.assertTrue(all(not os.path.exists(path) for path in configs))

    def test_timeout_and_error_messages_do_not_expose_credentials(self):
        configs = []
        def timeout(argv, **kwargs):
            configs.append(argv[argv.index("--config") + 1])
            raise subprocess.TimeoutExpired(argv, 1, stderr=b"test-access:test-secret")
        with mock.patch.object(sb.subprocess, "run", side_effect=timeout):
            with self.assertRaises(sb.BackupError) as err:
                sb._curl(["https://objects.example.test"], user="test-access:test-secret")
        self.assertEqual(str(err.exception), "S3 storage timed out")
        self.assertFalse(os.path.exists(configs[0]))
        with mock.patch.object(sb.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 22, b"", b"test-access test-secret test-access:test-secret")):
            with self.assertRaises(sb.BackupError) as err:
                sb._curl(["https://objects.example.test"], user="test-access:test-secret")
        self.assertNotIn("test-access", str(err.exception))
        self.assertNotIn("test-secret", str(err.exception))

    def test_credential_controls_rejected(self):
        for user in ("access:secret\nverbose", "access:secret\r", "access:secret\x00"):
            with self.subTest(user=user), self.assertRaises(sb.BackupError):
                sb._curl([], user=user)

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
    def test_incomplete_or_corrupt_gzip_rejected_before_overwrite(self):
        cfg = self.conf()
        data = sb.archive(self.home, sb.session_paths(self.home, DSH, SESSION))
        elsewhere = os.path.join(self.tmp, "restored")
        destination = os.path.join(elsewhere, "sessions/--directory--", SESSION,
                                   "session.v4.jsonl.zstd")
        write_file(destination, "keep-existing-session")
        bad_crc = bytearray(data)
        bad_crc[-8] ^= 1
        for damaged in (data[:-8], data[:-1], bytes(bad_crc)):
            with self.subTest(length=len(damaged)):
                sb.put(cfg, "pc/a/s/bad.tar.gz", damaged)
                with self.assertRaisesRegex(sb.BackupError, "gzip stream"):
                    sb.restore(cfg, "pc/a/s/bad.tar.gz", elsewhere, overwrite=True)
                with open(destination) as fh:
                    self.assertEqual(fh.read(), "keep-existing-session")

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
