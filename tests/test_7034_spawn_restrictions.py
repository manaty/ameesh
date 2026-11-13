# SPDX-License-Identifier: AGPL-3.0-only
"""#7034: restricted tools/MCP of a spawned child, applied by the runner.

C1 of the design note #7031 (§4.1): the policy is a subset of the parent's,
stored with the child, and expressed by the harness descriptor. A harness
that does not prove the application is refused, never merely configured.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest

from ameesh import canon_sync, child_reports, spawn, storage
from .support import PgTestCase, REPO

from .test_canon import _CanonDbCase


class SpawnPolicyTest(_CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync()
        self.cwd = tempfile.mkdtemp(prefix="ameesh-7034-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.cwd, True))
        with open(os.path.join(self.cwd, ".mcp.json"), "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": {
                "transport-readonly": {"command": "true"},
                "other": {"command": "true"},
            }}, handle)

    def loaded(self):
        return self.load()

    def test_subset_of_parent_is_stored_with_the_child(self):
        row = canon_sync.spawn(
            self.db, "aide", "orchestre", 3600, canon=self.loaded(),
            tools=["git"], mcp=["transport-readonly"], cwd=self.cwd)
        self.assertEqual(row["tools"], ["git"])
        self.assertEqual(row["mcp"], ["transport-readonly"])
        profile = storage.of(self.db).ephemerals.profile("aide")
        self.assertEqual(profile["parent"], "orchestre")
        self.assertEqual(profile["harness"], self.row("aide")["harness"])
        self.assertEqual(profile["tools"], ["git"])
        self.assertEqual(profile["mcp"], ["transport-readonly"])
        # #7034 × #7107 : les serveurs retenus sont joints au profil, pour un
        # enfant placé sur un hôte qui n'a pas le `.mcp.json` du parent.
        self.assertEqual(profile["mcp_servers"],
                         {"transport-readonly": {"command": "true"}})
        self.assertRegex(profile["config_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(profile["config_digest"],
                         spawn.policy_digest(["git"], ["transport-readonly"]))

    def test_name_outside_parent_is_refused(self):
        for tools, mcp in ((["write"], []), ([], ["other"]), (["mcp:other"], [])):
            with self.subTest(tools=tools, mcp=mcp):
                with self.assertRaises(canon_sync.SpawnError) as ctx:
                    canon_sync.spawn(self.db, "aide", "orchestre", 3600,
                                     canon=self.loaded(), tools=tools, mcp=mcp,
                                     cwd=self.cwd)
                self.assertIn("spawn-tool-not-in-parent", str(ctx.exception))
                self.assertIsNone(storage.of(self.db).ephemerals.profile("aide"))

    def test_grandchild_cannot_widen(self):
        canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                         tools=["git"], cwd=self.cwd)
        child = canon_sync.spawn(self.db, "petit", "aide", 3600, canon=self.loaded(),
                                 tools=["git"], cwd=self.cwd)
        self.assertEqual(child["tools"], ["git"])
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "petit2", "aide", 3600, canon=self.loaded(),
                             tools=["write"], cwd=self.cwd)
        self.assertIn("spawn-tool-not-in-parent", str(ctx.exception))

    def test_a_restricted_parent_never_produces_an_unrestricted_child(self):
        # B 40580 : demander aucun outil n'est pas hériter de tout.
        canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                         tools=["git"], cwd=self.cwd)
        child = canon_sync.spawn(self.db, "petit", "aide", 3600,
                                 canon=self.loaded(), cwd=self.cwd)
        self.assertEqual((child["tools"], child["mcp"]), ([], []))
        profile = storage.of(self.db).ephemerals.profile("petit")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["tools"], [])
        self.assertEqual(profile["mcp"], [])
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "petit2", "aide", 3600,
                             canon=self.loaded(), tools=["write"], cwd=self.cwd)
        self.assertIn("spawn-tool-not-in-parent", str(ctx.exception))

    def test_unsupported_harness_is_refused_before_creation(self):
        self.db.execute("UPDATE agent_registry SET harness = 'codex' WHERE name = 'orchestre'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                             tools=["git"], cwd=self.cwd)
        self.assertIn("spawn-tools-unsupported", str(ctx.exception))
        self.assertIsNone(self.row("aide"))

    def test_mcp_server_must_exist_in_the_project_file(self):
        os.unlink(os.path.join(self.cwd, ".mcp.json"))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                             mcp=["transport-readonly"], cwd=self.cwd)
        self.assertIn("spawn-mcp-not-found", str(ctx.exception))

    def test_context_is_bounded_and_stored_with_the_child(self):
        text = "mission: read the note\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                         context=(text, digest))
        row = storage.of(self.db).ephemerals.context("aide")
        self.assertEqual((row["text"], row["digest"], row["bytes"]),
                         (text, digest, len(text.encode())))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "aide2", "orchestre", 3600, canon=self.loaded(),
                             context=("x" * (spawn.CONTEXT_LIMIT + 1), "0" * 64))
        self.assertIn("spawn-text-too-long", str(ctx.exception))

    def test_context_secret_is_refused(self):
        secret = "sk-" + "a" * 24
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.loaded(),
                             context=(secret, "0" * 64))
        self.assertIn("spawn-context-secret", str(ctx.exception))
        self.assertIsNone(self.row("aide"))


class SpawnApplyTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ameesh-7034-state-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, True))
        self.cwd = tempfile.mkdtemp(prefix="ameesh-7034-cwd-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.cwd, True))
        with open(os.path.join(self.cwd, ".mcp.json"), "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": {"readonly": {"command": "true"}}}, handle)

    def profile(self, **kw):
        value = {"child": "aide", "parent": "chef", "harness": "claude",
                 "tools": ["git"], "mcp": ["readonly"]}
        value.update(kw)
        return value

    def test_private_settings_and_argv(self):
        path, mcp_path = spawn.write_profile(self.state, self.profile(), self.cwd)
        for name in (path, mcp_path):
            self.assertEqual(stat.S_IMODE(os.stat(name).st_mode), 0o600)
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["contract"], spawn.CONTRACT)
        self.assertEqual(document["tools"], ["git"])
        self.assertEqual(document["mcp"], ["readonly"])
        with open(mcp_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["mcpServers"],
                             {"readonly": {"command": "true"}})
        argv = spawn.argv(self.profile(), path)
        self.assertEqual(argv[:2], ["--tools", "git"])
        self.assertIn("--mcp-config", argv)
        self.assertIn(mcp_path, argv)
        self.assertIn("--strict-mcp-config", argv)

    def test_empty_tool_list_disables_all_tools(self):
        path, _ = spawn.write_profile(self.state, self.profile(tools=[], mcp=[]), self.cwd)
        argv = spawn.argv(self.profile(tools=[], mcp=[]), path)
        self.assertEqual(argv[:2], ["--tools", ""])

    def test_harness_without_the_contract_is_refused(self):
        with self.assertRaises(spawn.SpawnToolsError) as ctx:
            spawn.write_profile(self.state, self.profile(harness="codex"), self.cwd)
        self.assertEqual(ctx.exception.code, "spawn-tools-unsupported")

    def test_stored_servers_are_written_without_the_project_file(self):
        profile = self.profile(mcp_servers={"readonly": {"command": "true"}})
        elsewhere = tempfile.mkdtemp(prefix="ameesh-7034-other-host-")
        self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, True))
        path, mcp_path = spawn.write_profile(self.state, profile, elsewhere)
        self.assertTrue(os.path.exists(path))
        with open(mcp_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["mcpServers"],
                             {"readonly": {"command": "true"}})

    def test_the_turn_context_accepts_spawn_alongside_a_conversation(self):
        # B 40580 : les validations #7034 vivaient dans
        # `_conversation_validate` et levaient NameError dès qu'un document
        # portait une conversation portable.
        from ameesh import turn_context
        from .test_l2894_turn_context import _document
        digest = "a" * 64
        document = _document(
            conversation={"move_id": "m1", "from_seq": 1, "to_seq": 2,
                          "digest": digest, "source_host": "h1",
                          "source_session": None, "file": "/tmp/segment"},
            spawn={"path": "/tmp/profile", "parent": "chef",
                   "tools": ["git"], "mcp": [], "digest": digest},
            spawn_context={"path": "/tmp/context", "digest": digest, "bytes": 3})
        self.assertEqual(document["spawn"]["tools"], ["git"])
        turn_context.validate(document)

    def test_a_known_environment_secret_is_refused(self):
        # B 40591 : le filtre couvre les valeurs des variables secrètes, pas
        # seulement les motifs génériques (design #7031 §4.3).
        from unittest import mock
        value = "synthetic-value-7034"
        path = os.path.join(self.cwd, "context.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("mission %s" % value)
        with mock.patch.dict(os.environ, {"REVIEW_7034_API_KEY": value}):
            with self.assertRaises(spawn.SpawnToolsError) as ctx:
                spawn.read_context(path)
        self.assertEqual(ctx.exception.code, "spawn-context-secret")

    def test_context_reads_one_utf8_file_and_refuses_a_directory(self):
        path = os.path.join(self.cwd, "context.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("hello")
        text, digest = spawn.read_context(path)
        self.assertEqual((text, digest), ("hello", hashlib.sha256(b"hello").hexdigest()))
        with self.assertRaises(spawn.SpawnToolsError) as ctx:
            spawn.read_context(self.cwd)
        self.assertEqual(ctx.exception.code, "spawn-context-invalid")


class ChildReportTest(_CanonDbCase):
    """C2: the child → parent channel, its idempotence and the expiry report."""

    def setUp(self) -> None:
        super().setUp()
        # Le schéma est partagé par la classe : chaque test repart d'un
        # registre/conversation propres (les éphémères et leurs rapports).
        for table in ("spawn_contexts", "spawn_profiles", "spawn_reports",
                      "conversation_log", "agent_mailbox"):
            self.db.execute("DELETE FROM %s" % table)
        self.db.execute("DELETE FROM agent_registry WHERE ephemeral")
        self.sync()
        self.cwd = self.make_tmp()
        canon_sync.spawn(self.db, "aide", "orchestre", 3600, canon=self.load(),
                         tools=["git"])

    def messages(self, child="aide", parent="orchestre"):
        return self.db.query(
            "SELECT id, body, payload FROM agent_mailbox"
            " WHERE sender = %s AND recipient = %s ORDER BY id", (child, parent))

    def entries(self, agent="orchestre", event_prefix="child:%"):
        return self.db.query(
            "SELECT seq, kind, author, text, meta, event_id FROM conversation_log"
            " WHERE agent = %s AND event_id LIKE %s ORDER BY seq",
            (agent, event_prefix + "%"))

    def test_progress_and_question_reach_mail_and_journal(self):
        child_reports.report(self.db, self.cfg, "aide", "progress", "step one")
        child_reports.report(self.db, self.cfg, "aide", "question", "which file?")
        rows = self.messages()
        self.assertEqual([r["body"] for r in rows], ["step one", "which file?"])
        self.assertEqual(rows[0]["payload"]["child_report"],
                         {"child": "aide", "kind": "progress", "seq": 1})
        entries = self.entries()
        self.assertEqual([e["author"] for e in entries],
                         ["agent:aide", "agent:aide"])
        self.assertEqual(entries[0]["meta"]["report_kind"], "progress")
        self.assertEqual(entries[0]["meta"]["child"], "aide")
        self.assertEqual(entries[1]["meta"]["report_kind"], "question")

    def test_final_is_idempotent(self):
        first = child_reports.report(self.db, self.cfg, "aide", "final", "done",
                                      status="completed")
        second = child_reports.report(self.db, self.cfg, "aide", "final", "done",
                                       status="completed")
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(len(self.entries()), 1)
        state = storage.of(self.db).ephemerals.report("aide")
        self.assertEqual(state["final_status"], "completed")

    def test_body_secret_is_refused(self):
        with self.assertRaises(spawn.SpawnToolsError) as ctx:
            child_reports.report(self.db, self.cfg, "aide", "progress",
                                 "token = sk-" + "a" * 24)
        self.assertEqual(ctx.exception.code, "spawn-context-secret")
        self.assertEqual(self.messages(), [])

    def test_started_is_written_once(self):
        self.assertIsNotNone(child_reports.started(self.db, self.cfg, "aide"))
        self.assertIsNone(child_reports.started(self.db, self.cfg, "aide"))
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("started", self.messages()[0]["body"])
        self.assertEqual(len(self.entries()), 1)

    def test_turn_end_progress_then_final_on_expiry(self):
        child_reports.turn_end(self.db, self.cfg, "aide", final="answer",
                                status="completed")
        self.assertEqual(self.messages()[-1]["payload"]["child_report"]["kind"],
                         "progress")
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at ="
                        " now() - interval '1 second' WHERE name = 'aide'")
        child_reports.turn_end(self.db, self.cfg, "aide", final="last answer",
                                status="completed")
        self.assertEqual(self.messages()[-1]["payload"]["child_report"],
                         {"child": "aide", "kind": "final", "status": "expired"})

    def test_turn_end_removes_the_private_files_every_turn(self):
        text = "turn context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide5", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        directory = self.cfg.agent_dir("aide5")
        spawn.write_profile(directory, storage.of(self.db).ephemerals.profile("aide5"),
                            self.cwd)
        spawn.write_context(directory, text)
        child_reports.turn_end(self.db, self.cfg, "aide5", final="progress",
                                status="completed")
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            self.assertFalse(os.path.exists(os.path.join(directory, filename)),
                             filename)
        # Les données restent en base : le tour suivant réécrit les fichiers.
        self.assertIsNotNone(storage.of(self.db).ephemerals.profile("aide5"))
        self.assertIsNotNone(storage.of(self.db).ephemerals.context("aide5"))

    def test_a_dead_turns_private_files_are_swept_locally(self):
        # B 40603 : un runner tué entre l'écriture et `turn_end` laisse un
        # marqueur durable sur SON hôte ; un tour ultérieur y exécute le
        # nettoyage, sans dépendre de la base ni du balayage du parent.
        text = "turn context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide6", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        directory = self.cfg.agent_dir("aide6")
        child_reports.begin_turn(self.cfg, "aide6")
        spawn.write_profile(directory, storage.of(self.db).ephemerals.profile("aide6"),
                            self.cwd)
        spawn.write_context(directory, text)
        # Le processus du tour est mort : son identité ne résout plus.
        marker = child_reports.cleanup_path(self.cfg, "aide6")
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump({"child": "aide6", "pid": 0, "boot": "gone", "start": 0.0}, handle)
        self.assertEqual(child_reports.sweep_local(self.cfg), ["aide6"])
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            self.assertFalse(os.path.exists(os.path.join(directory, filename)),
                             filename)
        self.assertFalse(os.path.exists(marker))
        # La base garde le contexte : le tour suivant le réécrirait.

    def test_a_live_turn_keeps_its_private_files(self):
        # Un balayage concurrent ne touche pas un tour en cours (même hôte).
        text = "turn context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide7", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        directory = self.cfg.agent_dir("aide7")
        child_reports.begin_turn(self.cfg, "aide7")
        spawn.write_profile(directory, storage.of(self.db).ephemerals.profile("aide7"),
                            self.cwd)
        spawn.write_context(directory, text)
        self.assertEqual(child_reports.sweep_local(self.cfg), [])
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            self.assertTrue(os.path.exists(os.path.join(directory, filename)),
                            filename)
        child_reports.turn_end(self.db, self.cfg, "aide7", final="progress",
                               status="completed")
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            self.assertFalse(os.path.exists(os.path.join(directory, filename)),
                             filename)
        self.assertFalse(os.path.exists(child_reports.cleanup_path(self.cfg, "aide7")))

    def test_a_failed_cleanup_keeps_the_task_for_retry(self):
        # B 40616 : un unlink transitoire ne doit pas effacer la tâche ; le
        # marqueur est marqué orphelin et le balayage suivant réessaie.
        text = "turn context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide8", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        directory = self.cfg.agent_dir("aide8")
        child_reports.begin_turn(self.cfg, "aide8")
        spawn.write_profile(directory, storage.of(self.db).ephemerals.profile("aide8"),
                            self.cwd)
        spawn.write_context(directory, text)
        marker = child_reports.cleanup_path(self.cfg, "aide8")
        real_unlink = os.unlink

        def refuse_context(path):
            if str(path).endswith(spawn.CONTEXT_FILE):
                raise PermissionError(path)
            return real_unlink(path)

        from unittest import mock
        with mock.patch("ameesh.child_reports.os.unlink", side_effect=refuse_context):
            self.assertFalse(child_reports.remove_state(self.cfg, "aide8"))
        self.assertTrue(os.path.exists(os.path.join(directory, spawn.CONTEXT_FILE)))
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pid"], 0)
        # Rétabli : la tâche conservée s'exécute au balayage suivant.
        self.assertEqual(child_reports.sweep_local(self.cfg), ["aide8"])
        self.assertFalse(os.path.exists(os.path.join(directory, spawn.CONTEXT_FILE)))
        self.assertFalse(os.path.exists(marker))

    def test_a_final_report_purges_the_context_even_before_expiry(self):
        text = "private context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide3", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        self.assertIsNotNone(storage.of(self.db).ephemerals.context("aide3"))
        child_reports.report(self.db, self.cfg, "aide3", "final", "done")
        self.assertIsNone(storage.of(self.db).ephemerals.context("aide3"))

    def test_a_finalized_child_still_returns_to_the_expiry_sweep(self):
        text = "private context 2\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide4", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        storage.of(self.db).ephemerals.set_report("aide4", final="completed")
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at ="
                        " now() - interval '1 second' WHERE name = 'aide4'")
        done = child_reports.sweep_expired(self.db, self.cfg, "orchestre")
        self.assertIn("aide4", [row["child"] for row in done])
        self.assertIsNone(storage.of(self.db).ephemerals.context("aide4"))

    def test_human_copy_only_between_the_same_responsible(self):
        entry = child_reports.copy_human(self.db, self.cfg, "aide",
                                         author="human:alice", body="hello",
                                         source_seq=7)
        self.assertIsNotNone(entry)
        rows = self.entries(event_prefix="child:aide:human:%")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["copied_from"], 7)
        self.assertEqual(rows[0]["author"], "human:alice")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:bob'"
                        " WHERE name = 'aide'")
        self.assertIsNone(child_reports.copy_human(
            self.db, self.cfg, "aide", author="human:alice", body="private",
            source_seq=8))

    def test_expiry_sweep_writes_final_and_purges_context(self):
        text = "mission context\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        canon_sync.spawn(self.db, "aide2", "orchestre", 3600, canon=self.load(),
                         tools=["git"], context=(text, digest))
        directory = self.cfg.agent_dir("aide2")
        spawn.write_context(directory, text)
        spawn.write_profile(directory, storage.of(self.db).ephemerals.profile("aide2"),
                            self.cwd)
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at ="
                        " now() - interval '1 second' WHERE name = 'aide2'")
        done = child_reports.sweep_expired(self.db, self.cfg, "orchestre")
        self.assertEqual([row["child"] for row in done], ["aide2"])
        self.assertIsNone(storage.of(self.db).ephemerals.context("aide2"))
        self.assertFalse(os.path.exists(os.path.join(directory, spawn.CONTEXT_FILE)))
        self.assertFalse(os.path.exists(os.path.join(directory, spawn.PROFILE_FILE)))
        self.assertTrue(any("expired" in row["body"].lower()
                            for row in self.messages(child="aide2")))
        self.assertEqual(child_reports.sweep_expired(self.db, self.cfg, "orchestre"), [])


class SpawnRunnerTest(PgTestCase):
    """C1/C3 end-to-end: the runner applies the policy and the context file."""

    def test_the_turn_receives_the_policy_and_the_context(self):
        from unittest import mock
        from ameesh import mail, registry
        from ameesh.runner import AgentWorker, Runner
        from .support import FAKEBIN

        name, parent = "child-e2e", "parent-e2e"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=cwd)
        self.register(parent, "claude", cwd=cwd)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = %s,"
            " ephemeral_expires_at = now() + interval '1 hour' WHERE name = %s",
            (parent, name))
        text = "mission context for the child\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO spawn_profiles (child, parent, harness, tools, mcp, config_digest)"
            " VALUES (%s, %s, 'claude', ARRAY['git'], ARRAY[]::text[], %s)",
            (name, parent, spawn.policy_digest(["git"], [])))
        self.db.execute(
            "INSERT INTO spawn_contexts (child, parent, digest, bytes, text)"
            " VALUES (%s, %s, %s, %s, %s)", (name, parent, digest, len(text.encode()), text))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        mail.send(self.db, parent, name, "travail", kind="request")
        capture = os.path.join(self.tmp, "turn-context.json")
        import shlex
        import sys
        report_out = os.path.join(self.tmp, "child-report.txt")
        state = self.cfg.agent_dir(name)
        modes_out = os.path.join(self.tmp, "private-modes.txt")
        markers_out = os.path.join(self.tmp, "cleanup-markers.txt")
        cleanup_dir = os.path.join(self.cfg.state_dir, child_reports.CLEANUP_DIR)
        private = " ".join(shlex.quote(os.path.join(state, filename))
                           for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE,
                                            spawn.CONTEXT_FILE))
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_RUN": 'cp "$AMEESH_TURN_CONTEXT" %s; stat -c "%%a %%n" %s'
                                  ' > %s; ls %s > %s 2>/dev/null; %s -m ameesh child'
                                  ' report --kind progress --text "explicit report" > %s 2>&1'
                                  % (shlex.quote(capture), private, shlex.quote(modes_out),
                                     shlex.quote(cleanup_dir), shlex.quote(markers_out),
                                     shlex.quote(sys.executable),
                                     shlex.quote(report_out)),
               "PYTHONPATH": os.path.join(REPO, "src")}
        with mock.patch.dict(os.environ, env, clear=False):
            spec = worker.pick()
            self.assertTrue(worker.run_turn(spec))
        with open(capture, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["spawn"]["tools"], ["git"])
        self.assertEqual(document["spawn"]["parent"], parent)
        self.assertEqual(document["spawn"]["digest"],
                         spawn.policy_digest(["git"], []))
        self.assertEqual(document["spawn_context"]["digest"], digest)
        self.assertEqual(document["spawn_context"]["bytes"], len(text.encode()))
        with open(self.turns_log, encoding="utf-8") as handle:
            turns = [json.loads(line) for line in handle if line.strip()]
        argv = next(row["argv"] for row in turns if "argv" in row)
        self.assertEqual(argv[argv.index("--tools") + 1], "git")
        self.assertIn("--mcp-config", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("SPAWN CONTEXT", argv[-1])
        self.assertIn("spawn-context.md", argv[-1])
        self.assertNotIn("mission context for the child", argv[-1])
        with open(report_out, encoding="utf-8") as handle:
            self.assertIn("report progress child-e2e", handle.read())
        self.assertTrue(self.db.query(
            "SELECT 1 AS x FROM agent_mailbox WHERE sender = %s AND body = %s",
            (name, "explicit report")))
        # Pendant le tour, les fichiers privés existent en 0600 ; le tour
        # fini, ils ne survivent pas (B 40591), le suivant les réécrit.
        with open(modes_out, encoding="utf-8") as handle:
            modes = {line.split(" ", 1)[1].strip(): line.split(" ", 1)[0]
                     for line in handle if line.strip()}
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            path = os.path.join(state, filename)
            self.assertEqual(modes.get(path), "600", path)
            self.assertFalse(os.path.exists(path), path)
        # La tâche de nettoyage durable existait pendant le tour (B 40603) et
        # le tour normal l'a exécutée puis retirée.
        with open(markers_out, encoding="utf-8") as handle:
            markers = [line.strip() for line in handle if line.strip()]
        self.assertEqual(len(markers), 1, markers)
        self.assertTrue(markers[0].endswith(child_reports.CLEANUP_SUFFIX))
        self.assertEqual(os.listdir(cleanup_dir), [])
        self.assertEqual(child_reports.sweep_local(self.cfg), [])
        # The child's final report reaches the parent, and the expiry sweep
        # purges the private context.
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at ="
                        " now() - interval '1 second' WHERE name = %s", (name,))
        child_reports.sweep_expired(self.db, self.cfg, parent)
        self.assertFalse(os.path.exists(os.path.join(state, spawn.CONTEXT_FILE)))
        self.assertTrue(self.db.query(
            "SELECT 1 AS x FROM conversation_log WHERE agent = %s"
            " AND event_id = %s", (parent, "child:%s:final" % name)))


    def test_a_turn_refused_after_writing_the_files_leaves_nothing(self):
        # B 40615 : un refus APRÈS `begin_turn` et l'écriture des fichiers
        # (ici `write_context` échoue) sort sans `turn_end` ; le `finally` du
        # tour exécute le nettoyage, le marqueur ne reste pas « vivant ».
        from unittest import mock
        from ameesh import mail, registry, runner as runner_mod
        from ameesh.runner import AgentWorker, Runner

        name, parent = "child-early", "parent-early"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=cwd)
        self.register(parent, "claude", cwd=cwd)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = %s,"
            " ephemeral_expires_at = now() + interval '1 hour' WHERE name = %s",
            (parent, name))
        text = "mission context for the child\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO spawn_profiles (child, parent, harness, tools, mcp, config_digest)"
            " VALUES (%s, %s, 'claude', ARRAY['git'], ARRAY[]::text[], %s)",
            (name, parent, spawn.policy_digest(["git"], [])))
        self.db.execute(
            "INSERT INTO spawn_contexts (child, parent, digest, bytes, text)"
            " VALUES (%s, %s, %s, %s, %s)", (name, parent, digest, len(text.encode()), text))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        mail.send(self.db, parent, name, "travail", kind="request")
        spec = worker.pick()
        self.assertIsNotNone(spec)
        with mock.patch.object(runner_mod.spawn_policy, "write_context",
                               side_effect=OSError("synthetic write failure")):
            self.assertFalse(worker.run_turn(spec))
        state = self.cfg.agent_dir(name)
        for filename in (spawn.PROFILE_FILE, spawn.MCP_FILE, spawn.CONTEXT_FILE):
            self.assertFalse(os.path.exists(os.path.join(state, filename)), filename)
        self.assertFalse(os.path.exists(child_reports.cleanup_path(self.cfg, name)))
        self.assertEqual(child_reports.sweep_local(self.cfg), [])


class RestrictedNeverResidentTest(PgTestCase):
    """B de la revue 39559 : le chemin résident ne sert jamais un enfant.

    Le processus résident tourne avec `--dangerously-skip-permissions` et sans
    les arguments de la politique : l'y laisser servir un tour élargirait
    l'enfant à tous les outils et tous les MCP. Le refus vaut même si le
    réglage `resident` est à `on` ; `attach` est refusé pour la même raison,
    sans second chemin qui contournerait la liste.
    """

    def child(self, name: str):
        from ameesh import registry
        from ameesh.runner import AgentWorker, Runner
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        runner = Runner(self.cfg, self.db, once=False)
        self.register(name, "claude", cwd=cwd)
        self.register("parent", "claude", cwd=cwd)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = 'parent',"
            " ephemeral_expires_at = now() + interval '1 hour' WHERE name = %s",
            (name,))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        os.makedirs(worker.state_dir, exist_ok=True)
        with open(os.path.join(worker.state_dir, "resident"), "w",
                  encoding="utf-8") as handle:
            handle.write("on")
        return runner, worker

    def test_a_spawned_child_never_uses_a_resident(self):
        _runner, worker = self.child("child-resident")
        self.assertIn("spawned child", worker._resident_refusal(None, False))

    def test_attach_of_a_restricted_child_is_refused(self):
        import contextlib
        import io
        from ameesh import runner as runner_mod
        _runner, worker = self.child("child-attach")
        self.db.execute(
            "INSERT INTO spawn_profiles (child, parent, harness, tools, mcp, config_digest)"
            " VALUES (%s, 'parent', 'claude', ARRAY['git'], ARRAY[]::text[], %s)",
            (worker.name, spawn.policy_digest(["git"], [])))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(runner_mod.run_attach(self.cfg, self.db, worker.name), 1)
        self.assertIn("restricted child", error.getvalue())


class ContextOnlyRunnerTest(PgTestCase):
    """B 40580 : `spawn --context` seul n'écrit pas de profil."""

    def test_context_alone_is_delivered_without_a_profile(self):
        import shlex
        from unittest import mock
        from ameesh import mail, registry
        from ameesh.runner import AgentWorker, Runner
        from .support import FAKEBIN

        self.clear_turns()
        name, parent = "child-ctx", "parent-ctx"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=cwd)
        self.register(parent, "claude", cwd=cwd)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = %s,"
            " ephemeral_expires_at = now() + interval '1 hour' WHERE name = %s",
            (parent, name))
        text = "context without profile\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO spawn_contexts (child, parent, digest, bytes, text)"
            " VALUES (%s, %s, %s, %s, %s)",
            (name, parent, digest, len(text.encode()), text))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        mail.send(self.db, parent, name, "travail", kind="request")
        capture = os.path.join(self.tmp, "turn-context-ctx.json")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_RUN": 'cp "$AMEESH_TURN_CONTEXT" %s'
                                  % shlex.quote(capture),
               "PYTHONPATH": os.path.join(REPO, "src")}
        with mock.patch.dict(os.environ, env, clear=False):
            spec = worker.pick()
            self.assertTrue(worker.run_turn(spec))
        with open(capture, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertNotIn("spawn", document)
        self.assertEqual(document["spawn_context"]["digest"], digest)
        argv = next(row["argv"] for row in self.turns() if "argv" in row)
        self.assertIn("SPAWN CONTEXT", argv[-1])
        self.assertIn("spawn-context.md", argv[-1])
        self.assertNotIn("context without profile", argv[-1])
