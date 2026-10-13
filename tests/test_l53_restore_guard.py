# SPDX-License-Identifier: AGPL-3.0-only
"""Restoration refuses active agents and excludes concurrent runner claims."""
from __future__ import annotations

import io
import os
import types
from unittest import mock

from ameesh import db as db_mod, registry, storage, session_backup as sb
from .support import PgTestCase


class RestoreGuardTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "restore-a", harness="codex", host="pc", mode="execute")
        registry.upsert(self.db, "restore-b", harness="codex", host="pc", mode="execute")
        self.home = os.path.join(self.tmp, "source")
        os.makedirs(self.home)
        with open(os.path.join(self.home, "s1.jsonl"), "w") as fh:
            fh.write("saved transcript")
        self.conf = sb.parse({"target": os.path.join(self.tmp, "backups"), "encrypt": False})
        self.key = "pc/restore-a/s1/1.tar.gz"
        sb.put(self.conf, self.key, sb.archive(self.home, [os.path.join(self.home, "s1.jsonl")]))
        self.args = types.SimpleNamespace(agent="restore-a", session="s1", key=self.key,
                                          home=os.path.join(self.tmp, "restored"), overwrite=True)
        os.makedirs(self.args.home)
        self.destination = os.path.join(self.args.home, "s1.jsonl")
        with open(self.destination, "w") as fh:
            fh.write("live transcript")

    def test_active_status_and_live_or_unknown_lease_refuse_before_download(self):
        for status, owner, expires in (("running", None, None),
                                      ("idle", "runner", "5 minutes"),
                                      ("idle", "runner", None)):
            with self.subTest(status=status, owner=owner, expires=expires):
                self.db.execute(
                    "UPDATE agent_registry SET status = %s, lease_owner = %s, "
                    "lease_expires_at = CASE WHEN %s::text IS NULL THEN NULL "
                    "ELSE now() + %s::interval END WHERE name = 'restore-a'",
                    (status, owner, expires, expires))
                with mock.patch.object(sb, "get") as get:
                    with self.assertRaisesRegex(sb.BackupError, "active agent"):
                        sb.restore_agent(self.db, self.cfg, self.conf, self.args)
                get.assert_not_called()
                with open(self.destination) as fh:
                    self.assertEqual(fh.read(), "live transcript")

    def test_cross_agent_key_refused_by_default_before_download(self):
        self.args.agent = "restore-b"
        with mock.patch.object(sb, "get") as get:
            with self.assertRaisesRegex(sb.BackupError, "belongs to restore-a"):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        get.assert_not_called()
        with open(self.destination) as fh:
            self.assertEqual(fh.read(), "live transcript")

    def test_explicit_home_outside_the_key_owners_home_needs_the_flag(self):
        """L731: --home must not overwrite a foreign home without the flag."""
        foreign = os.path.join(self.tmp, "other-agent-home")
        os.makedirs(foreign)
        self.args.home = foreign
        destination = os.path.join(foreign, "s1.jsonl")
        with open(destination, "w") as fh:
            fh.write("other agent live transcript")
        with mock.patch.object(sb, "get") as get:
            with self.assertRaisesRegex(sb.BackupError, "not the home of restore-a"):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        get.assert_not_called()
        with open(destination) as fh:
            self.assertEqual(fh.read(), "other agent live transcript")

    def test_unprovable_owner_home_is_refused_without_the_flag(self):
        """L731: any doubt about the owner's home refuses, never guesses."""
        with mock.patch.object(sb, "_agent_home",
                               side_effect=sb.BackupError("boom")), \
                mock.patch.object(sb, "get") as get:
            with self.assertRaisesRegex(sb.BackupError, "cannot identify the home of restore-a"):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        get.assert_not_called()

    def test_explicit_home_of_the_key_owner_is_allowed(self):
        own = os.path.join(self.tmp, "own-home")
        os.makedirs(own)
        with mock.patch.dict(os.environ, {"CODEX_HOME": own}):
            self.args.home = own
            restored = sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        self.assertEqual(restored[2], ["s1.jsonl"])
        with open(os.path.join(own, "s1.jsonl")) as fh:
            self.assertEqual(fh.read(), "saved transcript")

    def test_explicit_home_reached_through_a_symlink_is_allowed(self):
        own = os.path.join(self.tmp, "own-home")
        os.makedirs(own)
        link = os.path.join(self.tmp, "own-home-link")
        os.symlink(own, link)
        with mock.patch.dict(os.environ, {"CODEX_HOME": own}):
            self.args.home = link
            restored = sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        self.assertEqual(restored[2], ["s1.jsonl"])
        with open(os.path.join(own, "s1.jsonl")) as fh:
            self.assertEqual(fh.read(), "saved transcript")

    def test_explicit_home_with_the_flag_still_allowed(self):
        foreign = os.path.join(self.tmp, "other-agent-home")
        os.makedirs(foreign)
        self.args.home = foreign
        self.args.allow_other_agent = True
        restored = sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        self.assertEqual(restored[2], ["s1.jsonl"])
        with open(os.path.join(foreign, "s1.jsonl")) as fh:
            self.assertEqual(fh.read(), "saved transcript")

    def test_explicit_cross_agent_restore_still_refuses_active_key_owner(self):
        self.args.agent = "restore-b"
        self.args.allow_other_agent = True
        for status, owner, expires in (("running", None, None),
                                      ("idle", "runner", "5 minutes"),
                                      ("idle", "runner", None)):
            with self.subTest(status=status, owner=owner, expires=expires):
                self.db.execute(
                    "UPDATE agent_registry SET status = %s, lease_owner = %s, "
                    "lease_expires_at = CASE WHEN %s::text IS NULL THEN NULL "
                    "ELSE now() + %s::interval END WHERE name = 'restore-a'",
                    (status, owner, expires, expires))
                with mock.patch.object(sb, "get") as get:
                    with self.assertRaisesRegex(sb.BackupError, "stop restore-a"):
                        sb.restore_agent(self.db, self.cfg, self.conf, self.args)
                get.assert_not_called()
                with open(self.destination) as fh:
                    self.assertEqual(fh.read(), "live transcript")

    def test_explicit_cross_agent_restore_locks_both_agents_through_extraction(self):
        self.args.agent = "restore-b"
        self.args.allow_other_agent = True
        contender = db_mod.connect(self.cfg)
        original = sb.restore
        def blocked_claim(*args, **kwargs):
            for agent in ("restore-a", "restore-b"):
                with self.subTest(agent=agent), self.assertRaises(db_mod.DbError):
                    with contender.transaction() as tx:
                        tx.execute("SET LOCAL lock_timeout = '100ms'")
                        storage.of(tx).leases.claim(agent, "contender", 60,
                                                    require_responsible=False)
            return original(*args, **kwargs)
        try:
            with mock.patch.object(sb, "restore", side_effect=blocked_claim):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
            for agent in ("restore-a", "restore-b"):
                self.assertIsNotNone(storage.of(contender).leases.claim(
                    agent, "contender", 60, require_responsible=False))
            with open(self.destination) as fh:
                self.assertEqual(fh.read(), "saved transcript")
        finally:
            contender.close()

    def test_malformed_key_refused_before_download(self):
        for key in ("pc/restore-a/s1/extra/1.tar.gz", "pc/../s1/1.tar.gz",
                    "/pc/restore-a/s1/1.tar.gz", "restore-a/1.tar.gz",
                    "pc/restore-a/s1/1.tar.gz\n"):
            with self.subTest(key=key), mock.patch.object(sb, "get") as get:
                self.args.key = key
                with self.assertRaisesRegex(sb.BackupError, "invalid backup key"):
                    sb.restore_agent(self.db, self.cfg, self.conf, self.args)
                get.assert_not_called()

    def test_unknown_agent_refuses_before_download(self):
        self.args.agent = "unknown"
        self.args.key = "pc/unknown/s1/1.tar.gz"
        with mock.patch.object(sb, "get") as get:
            with self.assertRaisesRegex(sb.BackupError, "unknown agent"):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
        get.assert_not_called()

    def test_expired_idle_agent_can_restore(self):
        self.db.execute("UPDATE agent_registry SET status = 'idle', lease_owner = 'old', "
                        "lease_expires_at = now() - interval '1 second' WHERE name = 'restore-a'")
        self.args.allow_other_agent = True  # L731: --home is not the agent's own home
        self.assertEqual(sb.restore_agent(self.db, self.cfg, self.conf, self.args)[2], ["s1.jsonl"])
        with open(self.destination) as fh:
            self.assertEqual(fh.read(), "saved transcript")

    def test_registry_lock_excludes_runner_claim_until_extraction_completes(self):
        contender = db_mod.connect(self.cfg)
        original = sb.restore
        self.args.allow_other_agent = True  # L731: --home is not the agent's own home
        def blocked_claim(*args, **kwargs):
            with self.assertRaises(db_mod.DbError):
                with contender.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout = '100ms'")
                    storage.of(tx).leases.claim("restore-a", "contender", 60,
                                                require_responsible=False)
            return original(*args, **kwargs)
        try:
            with mock.patch.object(sb, "restore", side_effect=blocked_claim):
                sb.restore_agent(self.db, self.cfg, self.conf, self.args)
            self.assertIsNotNone(storage.of(contender).leases.claim(
                "restore-a", "contender", 60, require_responsible=False))
            with open(self.destination) as fh:
                self.assertEqual(fh.read(), "saved transcript")
        finally:
            contender.close()

    def test_cli_restore_uses_guard(self):
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'restore-a'")
        with mock.patch('ameesh.config.load', return_value=types.SimpleNamespace(
                session_backup={"target": self.conf.target, "encrypt": False})), \
                mock.patch.object(db_mod, "connect", return_value=self.db), \
                mock.patch.object(self.db, "close"), \
                mock.patch.object(sb, "get") as get, \
                mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(sb.main(["restore", "restore-a", "--key", self.key,
                                      "--home", self.args.home, "--overwrite"]), 1)
        self.assertIn("active agent", err.getvalue())
        get.assert_not_called()

    def test_cli_explicit_home_outside_the_key_owner_needs_the_flag(self):
        foreign = os.path.join(self.tmp, "other-agent-home")
        os.makedirs(foreign)
        with mock.patch('ameesh.config.load', return_value=types.SimpleNamespace(
                session_backup={"target": self.conf.target, "encrypt": False})), \
                mock.patch.object(db_mod, "connect", return_value=self.db), \
                mock.patch.object(self.db, "close"), \
                mock.patch.object(sb, "get") as get, \
                mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(sb.main(["restore", "restore-a", "--key", self.key,
                                      "--home", foreign, "--overwrite"]), 1)
        self.assertIn("not the home of restore-a", err.getvalue())
        get.assert_not_called()

    def test_cli_shared_home_cross_key_never_truncates_running_owner(self):
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'restore-a'")
        for extra, error in (([], "belongs to restore-a"),
                             (["--allow-other-agent"], "stop restore-a")):
            with self.subTest(extra=extra), \
                    mock.patch('ameesh.config.load', return_value=types.SimpleNamespace(
                        session_backup={"target": self.conf.target, "encrypt": False})), \
                    mock.patch.object(db_mod, "connect", return_value=self.db), \
                    mock.patch.object(self.db, "close"), \
                    mock.patch.object(sb, "_agent_home", return_value=(self.args.home, ())), \
                    mock.patch.object(sb, "get") as get, \
                    mock.patch('sys.stderr', io.StringIO()) as err:
                self.assertEqual(sb.main(["restore", "restore-b", "--key", self.key,
                                          "--overwrite", *extra]), 1)
                self.assertIn(error, err.getvalue())
                get.assert_not_called()
            with open(self.destination) as fh:
                self.assertEqual(fh.read(), "live transcript")
