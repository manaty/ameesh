# SPDX-License-Identifier: AGPL-3.0-only
"""#7668: the conversation of an avatar (or of an agent whose card declares no
project) is never published to a project feed outside the lot it processes.

The exact defect of #7635: `avatar-essai`, a card without `team`/`projects`,
had `conversation_log.project = 'ameesh'` and its mail and final answers in the
ameesh R12 thread, readable by every agent of the project. The executor's
default project is not a declaration of the card.
"""
from __future__ import annotations

import dataclasses
import os
import types
import unittest
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import chat, conversation, fil, mail, registry, work
from ameesh.mediated_executor.contract import NotSupportedRemotely

from .support import PgTestCase


class HelperTest(unittest.TestCase):
    cfg = types.SimpleNamespace(project="default")

    def test_card_project_is_the_declared_primary_project(self):
        self.assertEqual(fil.card_project({"team": "ameesh", "chantier": "old"}), "ameesh")
        self.assertEqual(fil.card_project({"chantier": "old"}), "old")
        self.assertEqual(fil.card_project({}), "")
        self.assertEqual(fil.card_project(None), "")

    def test_private_conversations(self):
        self.assertFalse(fil.private_conversation({"team": "ameesh"}))
        self.assertTrue(fil.private_conversation({"releve": "preference"}))
        self.assertTrue(fil.private_conversation({"releve": "preference", "team": "ameesh"}))
        self.assertTrue(fil.private_conversation({"team": "", "chantier": ""}))
        self.assertTrue(fil.private_conversation(None))

    def test_avatar_correspondence_is_private_but_a_no_project_agent_keeps_routing(self):
        self.assertTrue(fil.avatar_conversation({"releve": "preference"}))
        self.assertTrue(fil.avatar_conversation({"releve": "preference", "team": "ameesh"}))
        self.assertFalse(fil.avatar_conversation({"team": "ameesh"}))
        self.assertFalse(fil.avatar_conversation({"chantier": "old"}))
        self.assertFalse(fil.avatar_conversation(None))

    def test_no_project_never_falls_back_to_the_executor_default(self):
        self.assertEqual(fil.conversation_project(
            None, self.cfg, {"team": "ameesh", "canon_governed": True}), "ameesh")
        self.assertEqual(fil.conversation_project(
            None, self.cfg, {"releve": "preference"}), "")
        self.assertEqual(fil.conversation_project(None, self.cfg, {}), "")
        self.assertEqual(fil.conversation_project(None, self.cfg, None), "")

    def test_a_mediated_contract_gap_reads_the_avatar_from_the_local_canon(self):
        # Contrat 1.1 : `agents.get` n'est pas servi à une session médiée ; la
        # carte du canon local qualifie encore l'avatar. La carte ordinaire
        # garde le fil historique (#515), sinon toute session médiée perdrait
        # ses copies lisibles.
        db = types.SimpleNamespace(cfg=types.SimpleNamespace(canon="/canon"))
        cards = {
            "avatar": types.SimpleNamespace(title="avatar", team="alice",
                                            releve="preference"),
            "sender": types.SimpleNamespace(title="sender", team="ameesh",
                                            releve="hote"),
        }
        canon = types.SimpleNamespace(agent=cards.get)

        def unsupported(_db, _name):
            raise NotSupportedRemotely("agents.get is not served to a session")

        with mock.patch.object(registry, "get", side_effect=unsupported), \
                mock.patch.object(canon_mod, "configured", return_value=(object(),)), \
                mock.patch.object(canon_mod, "from_config_all", return_value=[canon]):
            self.assertTrue(mail._avatar_correspondence(db, "sender", "avatar"))
            self.assertFalse(mail._avatar_correspondence(db, "sender"))

    def test_a_mediated_contract_gap_without_canon_keeps_the_historical_rule(self):
        db = types.SimpleNamespace(cfg=types.SimpleNamespace(canon=""))

        def unsupported(_db, _name):
            raise NotSupportedRemotely("agents.get is not served to a session")

        with mock.patch.object(registry, "get", side_effect=unsupported):
            self.assertFalse(mail._avatar_correspondence(db, "sender", "avatar"))


class AvatarConversationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads;"
                        " DELETE FROM thread_index")
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, "fils-" + self._testMethodName))
        self.db.cfg = self.cfg
        registry.upsert(self.db, "avatar", harness="claude", host=self.cfg.host,
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET releve = 'preference'"
                        " WHERE name = 'avatar'")
        registry.upsert(self.db, "sender", harness="claude", host=self.cfg.host,
                        cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET chantier = 'project-a',"
                        " workstream = 'project-a' WHERE name = 'sender'")

    def _row(self, name):
        return registry.get(self.db, name)

    def _files_with(self, mark):
        found = []
        for root, _dirs, files in os.walk(self.cfg.threads_root):
            for name in files:
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        body = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if mark in body:
                    found.append(os.path.relpath(path, self.cfg.threads_root))
        return found

    def _log(self, agent, project):
        return conversation.Log(self.db, self.cfg, agent=agent,
                                project=project)

    def test_avatar_without_team_keeps_its_conversation_private(self):
        project = fil.conversation_project(self.db, self.cfg, self._row("avatar"))
        self.assertEqual(project, "")
        log = self._log("avatar", project)
        log.append("human_message", "PRIVATE-QUESTION", author="human:smichea")
        log.append("turn_end", "PRIVATE-ANSWER", thread_text="PRIVATE-ANSWER")
        self.assertEqual(log.drain_threads(), 0)
        rows = self.db.query("SELECT project, thread_text, thread_entry"
                             " FROM conversation_log WHERE agent = 'avatar' ORDER BY seq")
        self.assertEqual([row["project"] for row in rows], [None, None])
        self.assertEqual([row["thread_text"] for row in rows], [None, None])
        self.assertEqual(self._files_with("PRIVATE-QUESTION"), [])
        self.assertEqual(self._files_with("PRIVATE-ANSWER"), [])

    def test_a_declared_project_still_publishes_its_readable_thread(self):
        project = fil.conversation_project(self.db, self.cfg, self._row("sender"))
        self.assertEqual(project, "project-a")
        log = self._log("sender", project)
        log.append("human_message", "PUBLIC-QUESTION", author="human:smichea")
        log.append("turn_end", "PUBLIC-ANSWER", thread_text="PUBLIC-ANSWER")
        self.assertEqual(log.drain_threads(), 2)
        rows = self.db.query("SELECT project, thread_entry FROM conversation_log"
                             " WHERE agent = 'sender' ORDER BY seq")
        self.assertEqual([row["project"] for row in rows], ["project-a", "project-a"])
        self.assertTrue(all(row["thread_entry"] for row in rows))
        self.assertIn("project-a", self._files_with("PUBLIC-QUESTION")[0])
        self.assertIn("project-a", self._files_with("PUBLIC-ANSWER")[0])

    def test_a_pending_copy_of_an_avatar_is_dropped_not_replayed(self):
        # An earlier, project-bound writer may have left an outbox row; a
        # private log acknowledges it without publishing it.
        old = self._log("avatar", "ameesh")
        old.append("human_message", "OLD-PRIVATE", author="human:smichea")
        self.assertIsNotNone(
            self.db.query("SELECT thread_text FROM conversation_log"
                          " WHERE agent = 'avatar'")[0]["thread_text"])
        private = self._log("avatar", None)
        self.assertEqual(private.drain_threads(), 0)
        row = self.db.query("SELECT thread_text, thread_entry FROM conversation_log"
                            " WHERE agent = 'avatar'")[0]
        self.assertIsNone(row["thread_text"])
        self.assertEqual(row["thread_entry"], "")
        self.assertEqual(self._files_with("OLD-PRIVATE"), [])

    def test_a_lot_keeps_the_feed_of_the_lot_it_processes(self):
        lot = work.add(self.db, title="Lot privé", app="project-b")
        row = self._row("avatar")
        self.assertEqual(fil.conversation_project(self.db, self.cfg, row, str(lot["id"])),
                         "project-b")
        log = conversation.Log(self.db, self.cfg, agent="avatar",
                               project="project-b", lot=str(lot["id"]))
        log.append("turn_end", "LOT-ANSWER", thread_text="LOT-ANSWER")
        self.assertEqual(log.drain_threads(), 1)
        self.assertEqual(self._files_with("LOT-ANSWER"),
                         [os.path.join("project-b", "%s.md" % lot["id"])])

    def test_mail_to_an_avatar_does_not_create_a_project_copy(self):
        mail.send(self.db, "sender", "avatar", "PRIVATE-MAIL")
        self.assertEqual(self._files_with("PRIVATE-MAIL"), [])
        self.assertEqual(
            self.db.query("SELECT count(*) AS n FROM thread_index")[0]["n"], 0)

    def test_mail_from_an_avatar_does_not_create_a_project_copy(self):
        mail.send(self.db, "avatar", "human:smichea", "PRIVATE-REPLY")
        self.assertEqual(self._files_with("PRIVATE-REPLY"), [])

    def test_unknown_avatar_qualification_omits_the_project_copy(self):
        # B P2 ameesh-cx2 (#42879): a failing registry must not turn an avatar
        # into a project correspondent. The mail is still deposited; only its
        # readable copy is omitted (privacy default).
        real_get = registry.get

        def failing_get(db, name):
            if name == "avatar":
                raise RuntimeError("registry unavailable")
            return real_get(db, name)

        with mock.patch.object(registry, "get", side_effect=failing_get):
            mail.send(self.db, "sender", "avatar", "PRIVATE-MAIL-UNKNOWN")
        first = self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox"
            " WHERE recipient = 'avatar' AND body = 'PRIVATE-MAIL-UNKNOWN'")
        self.assertEqual(first[0]["n"], 1)
        self.assertEqual(self._files_with("PRIVATE-MAIL-UNKNOWN"), [])

    def test_mail_to_an_avatar_with_a_lot_stays_in_the_lot_feed(self):
        lot = work.add(self.db, title="Lot du message", app="project-b")
        mail.send(self.db, "sender", "avatar", "LOT-MAIL",
                  work_item_id=str(lot["id"]))
        self.assertEqual(self._files_with("LOT-MAIL"),
                         [os.path.join("project-b", "%s.md" % lot["id"])])

    def test_chat_message_to_an_avatar_stays_in_its_journal(self):
        out = chat.post_human_message(self.cfg, self.db, human="human:smichea",
                                      sender="human:smichea", agent="avatar",
                                      text="CHAT-PRIVATE")
        self.assertIsInstance(out.get("message_id"), int)
        row = self.db.query("SELECT project FROM conversation_log"
                            " WHERE agent = 'avatar'")[0]
        self.assertIsNone(row["project"])
        self.assertEqual(self._files_with("CHAT-PRIVATE"), [])
