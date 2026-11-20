# SPDX-License-Identifier: AGPL-3.0-only
"""#7861 : le courrier pendant un `attach` atteint la session, et le
détachement lance tout de suite le tour de courrier restant.

Constat manaty/ameesh#6089 : une session attachée immobile ne recevait rien
(le hook ne remet qu'aux événements du harnais), et le détachement rendait le
bail sans tour de courrier. Ces tests couvrent la notification de bureau de
l'hôte (non passif seulement, une fois par message, rien de marqué lu), le
marqueur de détachement que lit l'exécuteur pour ne pas subir la fenêtre de
regroupement L125, et la remise au prochain prompt sans double remise.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import threading
import time
import unittest
from unittest import mock

from ameesh import attach, attach_mail, mail, registry
from ameesh.runner import AgentWorker, Runner, run_attach

from .support import FAKEBIN, PgTestCase

OWNER = "attach:alice@poste"
NOTIFY_SEND = os.path.join(FAKEBIN, "notify-send")


class NoticeTest(unittest.TestCase):
    """Le contenu d'une notification : expéditeur, numéro, une ligne d'objet."""

    def test_one_line_subject_and_nothing_else(self):
        title, body = attach_mail.notice(
            "dev3", {"id": 42, "sender": "alice",
                     "body": "Objet du message\nla suite reste dans la boîte"})
        self.assertIn("dev3", title)
        self.assertIn("alice", body)
        self.assertIn("#42", body)
        self.assertIn("Objet du message", body)
        self.assertNotIn("la suite", body)

    def test_multiline_and_spaces_are_compacted_and_capped(self):
        _title, body = attach_mail.notice(
            "dev3", {"id": 1, "sender": "  bob\n", "body": "a\n" + "b" * 500})
        self.assertIn("bob", body)
        self.assertLessEqual(len(body), 200)
        self.assertEqual(body.count("\n"), 0)


class NewMailTest(PgTestCase):
    """`new_mail` : le filtre (non passif, arrivé pendant l'attach) et l'unicité."""

    def setUp(self):
        super().setUp()
        self.register("att86", "claude", cwd=self.tmp)

    def test_only_new_non_passive_messages_once(self):
        old = mail.send(self.db, "alice", "att86", "déjà là avant l'attach")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - "
                        "interval '1 hour' WHERE id = %s", (old,))
        current = mail.send(self.db, "alice", "att86", "Bonjour\nSECRET-DU-CORPS")
        passive = mail.send(self.db, "alice", "att86", "une copie",
                            payload={"cc": "att86"})
        calls = []

        def fake(name, row):
            calls.append((name, int(row["id"])))
            return True, ""

        notified: set[int] = set()
        since = time.time() - 60
        self.assertEqual(
            attach_mail.new_mail(self.db, "att86", since=since,
                                 notified=notified, notify=fake), [])
        self.assertEqual(calls, [("att86", current)])
        # une deuxième passe ne re-signale rien (une seule fois par message)
        attach_mail.new_mail(self.db, "att86", since=since,
                             notified=notified, notify=fake)
        self.assertEqual(calls, [("att86", current)])
        # rien n'est marqué lu : le hook ou le tour de détachement les remettront
        self.assertEqual(sorted(int(r["id"]) for r in mail.unread(self.db, "att86")),
                         sorted([old, current, passive]))

    def test_a_failed_notification_is_not_retried_every_check(self):
        message = mail.send(self.db, "alice", "att86", "nouveau")
        calls = []

        def broken(name, row):
            calls.append(int(row["id"]))
            return False, "pas de bus de session"

        notified: set[int] = set()
        failures = attach_mail.new_mail(
            self.db, "att86", since=time.time() - 60,
            notified=notified, notify=broken)
        self.assertEqual([message_id for message_id, _why in failures], [message])
        # le fil d'attach a déjà journalisé l'échec ; pas de nouvelle tentative
        attach_mail.new_mail(self.db, "att86", since=time.time() - 60,
                             notified=notified, notify=broken)
        self.assertEqual(calls, [message])


class DesktopChannelTest(PgTestCase):
    """Le canal notify de l'hôte : le faux `notify-send` du banc le prouve."""

    def setUp(self):
        super().setUp()
        self.register("att86", "claude", cwd=self.tmp)
        self.notify_log = os.path.join(self.tmp, "notify.log")
        patcher = mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_NOTIFY_SEND_BIN": NOTIFY_SEND,
            "AMEESH_OSASCRIPT_BIN": NOTIFY_SEND,
            "AMEESH_FAKE_NOTIFY_LOG": self.notify_log,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lines(self):
        if not os.path.exists(self.notify_log):
            return []
        with open(self.notify_log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_active_mail_is_shown_and_left_unread(self):
        message = mail.send(self.db, "alice", "att86", "Sujet affiché\nSECRET-BODY")
        failures = attach_mail.new_mail(
            self.db, "att86", since=time.time() - 60, notified=set())
        self.assertEqual(failures, [])
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        argv = lines[0]
        text = " ".join(argv)
        self.assertIn("Nouveau courrier pour att86", argv)
        self.assertIn("alice", text)
        self.assertIn("#%d" % message, text)
        self.assertIn("Sujet affiché", text)
        self.assertNotIn("SECRET-BODY", text)
        self.assertEqual([int(r["id"]) for r in mail.unread_active(self.db, "att86")],
                         [message])

    def test_passive_mail_shows_nothing(self):
        mail.send(self.db, "alice", "att86", "une copie",
                  payload={"cc": "att86"})
        attach_mail.new_mail(self.db, "att86", since=time.time() - 60,
                             notified=set())
        self.assertEqual(self._lines(), [])


class AttachNotifyTest(PgTestCase):
    """Bout en bout : une session attachée immobile est prévenue par le bureau."""

    def setUp(self):
        super().setUp()
        self.register("att86", "claude", cwd=self.tmp)
        self.notify_log = os.path.join(self.tmp, "notify.log")
        self.addCleanup(setattr, attach, "INPUT_CHECK", attach.INPUT_CHECK)
        attach.INPUT_CHECK = 0.02

    def _run_attach(self, marker_name: str, mail_body: str | None,
                    payload: dict | None = None):
        marker = os.path.join(self.tmp, marker_name)
        env = {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_NOTIFY_SEND_BIN": NOTIFY_SEND,
            "AMEESH_OSASCRIPT_BIN": NOTIFY_SEND,
            "AMEESH_FAKE_NOTIFY_LOG": self.notify_log,
            "AMEESH_TEST_RUN": "touch %s" % shlex.quote(marker),
            "AMEESH_TEST_HANG_AFTER_RESULT": "2",
        }
        attach_db = self.connect()
        self.addCleanup(attach_db.close)
        result: dict = {}

        def run() -> None:
            with mock.patch.dict(os.environ, env):
                result["code"] = run_attach(
                    self.cfg, attach_db, "att86", ttl=30, max_seconds=10)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(marker) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(marker), "le faux harnais n'a pas démarré")
            if mail_body is not None:
                message = mail.send(self.db, "alice", "att86", mail_body,
                                    payload=payload)
            if payload is None:
                # laisse au fil check_inputs le temps de voir le courrier
                deadline = time.monotonic() + 2
                while (time.monotonic() < deadline
                       and not os.path.exists(self.notify_log)):
                    time.sleep(0.02)
            else:
                # passif : laisser le fil faire plusieurs passes avant de
                # conclure à l'absence de notification
                time.sleep(0.8)
        finally:
            thread.join(20)
        self.assertFalse(thread.is_alive(), "l'attach n'a pas rendu la main")
        return result["code"], message if mail_body is not None else None

    def test_active_mail_during_attach_notifies_once_and_stays_unread(self):
        code, message = self._run_attach("ready-active", "Sujet pendant l'attach\nSECRET")
        self.assertEqual(code, 0)
        with open(self.notify_log, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        text = " ".join(lines[0])
        self.assertIn("alice", text)
        self.assertIn("#%d" % message, text)
        self.assertIn("Sujet pendant l'attach", text)
        self.assertNotIn("SECRET", text)
        self.assertEqual([int(r["id"]) for r in mail.unread_active(self.db, "att86")],
                         [message])

    def test_passive_mail_during_attach_is_not_shown(self):
        code, _message = self._run_attach(
            "ready-passive", "une copie", payload={"cc": "att86"})
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.notify_log))


class DetachMailTest(PgTestCase):
    """Le marqueur de détachement et le tour de courrier immédiat du worker."""

    def setUp(self):
        super().setUp()
        self.register("att86", "claude", cwd=self.tmp)

    def _run_attach(self) -> int:
        env = {"AMEESH_BIN_DIR": FAKEBIN}
        with mock.patch.dict(os.environ, env):
            return run_attach(self.cfg, self.db, "att86", ttl=30, max_seconds=10)

    def _worker(self, lease: dict) -> AgentWorker:
        runner = Runner(dataclasses.replace(self.cfg, mail_batch=90.0),
                        self.db, once=False)
        return AgentWorker(runner, registry.get(self.db, "att86"), lease)

    def test_remaining_unread_mail_is_due_immediately(self):
        message = mail.send(self.db, "alice", "att86", "question restée en boîte")
        self.assertEqual(self._run_attach(), 0)
        self.assertEqual(attach_mail.due_marker(self.cfg, "att86"), message)
        # le bail d'attach est bien rendu
        self.assertIsNone((registry.get(self.db, "att86") or {}).get("lease_owner"))
        rows = mail.unread_active(self.db, "att86")
        self.assertEqual([int(r["id"]) for r in rows], [message])
        lease = registry.claim(self.db, "att86", "runner-7861", 3600)
        self.addCleanup(registry.release, self.db, "att86", "runner-7861",
                        int(lease["lease_epoch"]))
        spec = self._worker(lease)._mail_spec(rows)
        self.assertIsNotNone(spec, "le courrier du détachement doit être dû tout de suite")
        self.assertEqual(spec["ids"], [message])

    def test_without_the_marker_the_same_mail_waits_for_the_l125_window(self):
        message = mail.send(self.db, "alice", "att86", "question ordinaire")
        lease = registry.claim(self.db, "att86", "runner-7861", 3600)
        self.addCleanup(registry.release, self.db, "att86", "runner-7861",
                        int(lease["lease_epoch"]))
        rows = mail.unread_active(self.db, "att86")
        self.assertEqual([int(r["id"]) for r in rows], [message])
        # pas de marqueur : la fenêtre de regroupement de 90 s n'est pas écoulée
        self.assertIsNone(self._worker(lease)._mail_spec(rows))

    def test_passive_mail_at_detach_writes_no_marker(self):
        mail.send(self.db, "alice", "att86", "une copie", payload={"cc": "att86"})
        self.assertEqual(self._run_attach(), 0)
        self.assertIsNone(attach_mail.due_marker(self.cfg, "att86"))


class HookDeliveryTest(PgTestCase):
    """Point 3 : le hook UserPromptSubmit remet le courrier de l'attach une fois."""

    def setUp(self):
        super().setUp()
        self.register("att86", "claude", cwd=self.tmp)
        self.lease = registry.attach_claim(self.db, "att86", OWNER, 300)
        self.addCleanup(self._release)

    def _release(self) -> None:
        try:
            registry.release(self.db, "att86", OWNER, int(self.lease["lease_epoch"]))
        except Exception:  # noqa: BLE001 — le ménage ne juge pas le test
            pass

    def _hook(self):
        env = self.env(AGENT_MAIL_NAME="att86", AMEESH_RUNNER_ID=OWNER,
                       AMEESH_LEASE_EPOCH=str(self.lease["lease_epoch"]),
                       AMEESH_ATTACH="1", AMEESH_HARNESS="claude")
        return self.cli("hook", "claude", env=env, stdin=json.dumps({
            "hook_event_name": "UserPromptSubmit", "session_id": "sess-7861",
            "cwd": self.tmp}))

    def test_delivered_once_and_never_twice(self):
        message = mail.send(self.db, "alice", "att86", "message pendant l'attach")
        proc = self._hook()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = json.loads(proc.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("message pendant l'attach", context)
        self.assertIn("alice", context)
        self.assertIsNotNone(mail.get(self.db, message)["delivered_ts"])
        self.assertEqual(mail.unread_active(self.db, "att86"), [])
        # une seconde remise ne trouve plus rien : pas de doublon
        again = self._hook()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), "")
