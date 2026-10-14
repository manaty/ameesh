# SPDX-License-Identifier: AGPL-3.0-only
"""L79 (#287): `ameesh chat <agent>` — client terminal (rendu, suivi, entrée).

Aucune horloge murale n'est utilisée comme vérité : `_stamp` et l'état
reçoivent leur `now`, l'ascendance est injectée, et les tests sans base
n'ouvrent aucune connexion. Les tests SQL tournent sous PgTestCase (CI).
"""
from __future__ import annotations

import dataclasses
import io
import os
import time
import unittest
from unittest import mock

from ameesh import chat, config as config_mod, conversation as conversation_mod
from ameesh import mail, registry

from .support import PgTestCase

#: variables d'identité retirées de l'environnement des tests (comme L124)
_IDENTITY = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
             "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH", "AMEESH_CHAT", "AMEESH_CHAT_PID")


def clean_chain():
    """Ascendance et environnements injectés : aucun agent au-dessus."""
    return dict(chain=[os.getpid()], environ_of=lambda pid: {})


class _FakeSubscription:
    def __init__(self, waits):
        self.waits = list(waits)
        self.closed = False

    def wait(self, timeout):
        return self.waits.pop(0) if self.waits else None

    def close(self):
        self.closed = True


class _FakeWakeups:
    def __init__(self, subscription=None):
        self.subscription = subscription
        self.channels = None

    def subscribe(self, channels):
        self.channels = list(channels)
        return self.subscription


class _FakeStorage:
    def __init__(self, *, lease=None, lots=(), subscription=None):
        self.leases = mock.Mock()
        self.leases.state.return_value = lease or {}
        self.work = mock.Mock()
        self.work.open_for.return_value = list(lots)
        self.wakeups = _FakeWakeups(subscription)


# ==========================================================================
# rendu
# ==========================================================================

class ViewTest(unittest.TestCase):
    def render(self, entries, **kw):
        out = io.StringIO()
        view = chat.ClientView(out)
        for entry in entries:
            view.entry(entry, **kw)
        return out.getvalue()

    def test_text_slices_share_one_header_and_the_final_is_not_repeated(self):
        text = self.render([
            {"kind": "text", "text": "bonjour la suite", "author": "agent:a", "turn": "t1",
             "ts": 1000},
            {"kind": "text", "text": " encore", "author": "agent:a", "turn": "t1", "ts": 1001},
            {"kind": "turn_end", "text": "bonjour la suite encore", "author": "agent:a",
             "turn": "t1", "ts": 1002,
             "meta": {"status": "completed", "cost_usd": 0.5, "duration_s": 2.0}},
        ], now=1002)
        self.assertEqual(text.count("agent:a"), 1)            # un seul en-tête pour le tour
        self.assertEqual(text.count("bonjour la suite"), 1)   # pas de doublon à la fin
        self.assertIn("── completed · $0.5000 · 2.0s", text)

    def test_final_is_printed_when_nothing_was_streamed(self):
        text = self.render([
            {"kind": "turn_end", "text": "réponse finale", "author": "agent:a", "turn": "t1",
             "ts": 1000, "meta": {"status": "completed"}},
        ], now=1000)
        self.assertIn("réponse finale", text)

    def test_other_turns_and_kinds_are_visible(self):
        text = self.render([
            {"kind": "tool", "text": "read", "author": "agent:a", "turn": "t1", "ts": 1000,
             "meta": {"tool": "read", "status": "started"}},
            {"kind": "state", "text": "sleeping", "author": "ameesh:runner", "ts": 1001},
            {"kind": "human_message", "text": "coucou", "author": "human:alice", "ts": 1002},
            {"kind": "input", "text": "courrier", "author": "ameesh:runner", "ts": 1003},
            {"kind": "text", "text": "tronqué", "author": "agent:a", "turn": "t2", "ts": 1004,
             "truncated": True},
        ], now=1004)
        self.assertIn("· read (started)", text)
        self.assertIn("· sleeping", text)
        self.assertIn("[human]", text)
        self.assertIn("← ameesh:runner", text)
        self.assertIn("tronqué […]", text)

    def test_turn_end_dates_only_other_days(self):
        day = 20 * 3600
        same = self.render([{"kind": "text", "text": "x", "author": "a", "ts": day}],
                           now=day + 60)
        other = self.render([{"kind": "text", "text": "x", "author": "a", "ts": day}],
                            now=day + 3 * 86400)
        self.assertRegex(same, r"\d\d:\d\d:\d\d a\n")
        self.assertRegex(other, r"\d\d-\d\d \d\d:\d\d a\n")


# ==========================================================================
# ligne de commande
# ==========================================================================

class ParserTest(unittest.TestCase):
    def test_agent_positional_selects_the_client(self):
        args = chat.build_parser().parse_args(["dev1", "--once", "--after", "3", "--limit", "5"])
        self.assertEqual((args.agent, args.once, args.after, args.limit), ("dev1", True, 3, 5))
        self.assertIsNone(chat.build_parser().parse_args([]).agent)

    def test_session_options_are_refused_with_an_agent(self):
        args = chat.build_parser().parse_args(["dev1", "--name", "chat-x", "--harness", "codex"])
        self.assertEqual(chat.client_conflicts(args), ["--harness", "--name"])
        self.assertEqual(chat.client_conflicts(chat.build_parser().parse_args(["dev1"])), [])

    def test_once_and_dry_run_are_distinct_flags(self):
        args = chat.build_parser().parse_args(["dev1"])
        self.assertFalse(args.once or args.dry_run)


# ==========================================================================
# client sans base : entrée, identité, suivi
# ==========================================================================

class ClientTest(unittest.TestCase):
    """Le client reçoit une fausse base : aucune connexion, aucun courrier réel."""

    row = {"name": "dev1", "mode": "execute", "harness": "claude", "host": "h1",
           "status": "idle", "status_text": "rien", "last_seen_ts": 1000.0,
           "team": "ameesh", "projects": ["ameesh"]}

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in _IDENTITY}
        env["USER"] = "tester"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dataclasses.replace(config_mod.load(), humans="tester")
        self.storage = _FakeStorage()
        self.patches = [
            mock.patch.object(registry, "get", return_value=dict(self.row)),
            mock.patch("ameesh.storage.of", return_value=self.storage),
            mock.patch.object(conversation_mod, "history",
                              side_effect=lambda db, agent, after=0, limit=100: {
                                  "entries": [], "last_seq": 0, "next_seq": max(after, 0),
                                  "gap": False, "more": False}),
            mock.patch.object(mail, "send", return_value=7),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, **kw):
        clean = clean_chain()
        kw.setdefault("lines", [])
        kw.setdefault("subscribe", lambda: None)
        kw.setdefault("sleep", lambda seconds: time.sleep(0))
        kw.setdefault("chain", clean["chain"])
        kw.setdefault("environ_of", clean["environ_of"])
        return chat.TerminalClient(self.cfg, object(), "dev1", out=io.StringIO(),
                                   err=io.StringIO(), **kw)

    def test_a_line_is_sent_as_the_local_human(self):
        client = self.client()
        self.assertTrue(client.handle("bonjour"))
        mail.send.assert_called_once_with(
            client.db, "tester", "dev1", "bonjour", host=self.cfg.host, kind="notify",
            payload={"human": True})
        self.assertIn("sent to dev1 (mail #7)", client.view.out.getvalue())

    def test_urgent_without_authorisation_falls_back_to_a_normal_message(self):
        client = self.client()
        client.handle("!urgent vite")
        kwargs = mail.send.call_args.kwargs
        self.assertEqual(mail.send.call_args.args[3], "vite")
        self.assertEqual(kwargs["payload"], {"human": True})     # urgent non accordé
        self.assertEqual(kwargs["kind"], "notify")
        self.assertIn("delivered as a normal message", client.view.out.getvalue())

    def test_urgent_and_stop_use_the_interrupt_path_when_authorised(self):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=("tester",))
        client = self.client()
        client.cfg = cfg
        client.handle("!urgent maintenant")
        kwargs = mail.send.call_args.kwargs
        self.assertEqual(kwargs["payload"], {"human": True, "urgent": True})
        self.assertEqual(kwargs["kind"], "event")
        mail.send.reset_mock()
        client.handle("!stop")
        self.assertEqual(mail.send.call_args.args[3], chat.STOP_BODY)
        self.assertEqual(mail.send.call_args.kwargs["payload"],
                         {"human": True, "urgent": True})

    def test_stop_without_authorisation_drops_nothing(self):
        client = self.client()
        client.handle("!stop")
        mail.send.assert_not_called()
        self.assertIn("!stop refused", client.view.out.getvalue())

    def test_read_only_when_launched_from_an_agent_session(self):
        client = self.client(environ_of=lambda pid: {"AGENT_MAIL_NAME": "orch"})
        self.assertIn("agent orch", client.read_only)
        client.handle("bonjour")
        mail.send.assert_not_called()
        self.assertIn("read-only", client.view.out.getvalue())

    def test_quit_help_and_state_do_not_send_anything(self):
        client = self.client()
        self.assertTrue(client.handle("!help"))
        self.assertIn("!urgent", client.view.out.getvalue())
        client.handle("!state")
        self.assertIn("dev1 · execute · claude", client.view.out.getvalue())
        self.assertFalse(client.handle("!quit"))
        mail.send.assert_not_called()

    def test_unverified_human_writes_under_the_chat_identity(self):
        cfg = dataclasses.replace(self.cfg, humans="")
        with mock.patch("ameesh.work.known_human", return_value=None):
            client = self.client()
        client.cfg = cfg
        with mock.patch("ameesh.work.known_human", return_value=None):
            client.handle("bonjour")
        self.assertEqual(mail.send.call_args.args[1], "chat-tester")
        self.assertEqual(mail.send.call_args.kwargs["payload"], None)

    def test_loop_drains_input_then_leaves_on_end_of_input(self):
        client = self.client(lines=["bonjour", "!quit"])
        self.assertEqual(client.run(), 0)
        self.assertEqual(mail.send.call_args.args[3], "bonjour")

    def test_follow_reads_from_the_last_cursor(self):
        calls = []

        def history(db, agent, after=0, limit=100):
            calls.append((after, limit))
            if limit == 1:                     # lecture de tête : dernier seq
                return {"entries": [], "last_seq": 4, "next_seq": 4, "gap": False, "more": False}
            if after == 0:                     # page d'entrée : les derniers 20
                return {"entries": [{"kind": "text", "text": "un", "author": "agent:a",
                                     "turn": "t1", "ts": 1000}],
                        "last_seq": 4, "next_seq": 4, "gap": False, "more": False}
            return {"entries": [{"kind": "text", "text": "deux", "author": "agent:a",
                                 "turn": "t2", "ts": 1001}],
                    "last_seq": 5, "next_seq": 5, "gap": False, "more": False}

        with mock.patch.object(conversation_mod, "history", side_effect=history):
            client = self.client()
            self.assertTrue(client.handle("!state"))
            cursor = client.show_history()
            self.assertEqual(calls[0], (0, 1))
            self.assertEqual(calls[1], (0, client.limit))
            self.assertEqual(cursor, 4)
            self.assertEqual(client.read_new(cursor), 5)
        text = client.view.out.getvalue()
        self.assertIn("un", text)
        self.assertIn("deux", text)


# ==========================================================================
# client sur la base réelle (CI)
# ==========================================================================

class ClientDbTest(PgTestCase):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k not in _IDENTITY}
        env["USER"] = "tester"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = dataclasses.replace(self.cfg, humans="tester")
        self.db.execute("TRUNCATE conversation_log, conversation_heads")
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)
        log = conversation_mod.Log(self.db, self.cfg, agent="dev1")
        log.append("text", "premier", author="agent:dev1", session="s", turn="t1")
        log.append("turn_end", "premier", author="agent:dev1", session="s", turn="t1",
                   meta={"status": "completed"})

    def client(self, argv, **kw):
        args = chat.build_parser().parse_args(argv)
        clean = clean_chain()
        kw.setdefault("lines", [])
        kw.setdefault("subscribe", lambda: None)
        kw.setdefault("sleep", lambda seconds: time.sleep(0))
        return chat.client_run(self.cfg, args, db=self.db, out=kw.pop("out", io.StringIO()),
                               chain=clean["chain"], environ_of=clean["environ_of"], **kw)

    def test_once_shows_the_last_entries(self):
        out = io.StringIO()
        self.assertEqual(self.client(["dev1", "--once"], out=out), 0)
        text = out.getvalue()
        self.assertIn("dev1 · execute · claude", text)
        self.assertIn("premier", text)
        self.assertIn("── completed", text)

    def test_unknown_agent_is_refused(self):
        err = io.StringIO()
        args = chat.build_parser().parse_args(["fantome", "--once"])
        clean = clean_chain()
        code = chat.client_run(self.cfg, args, db=self.db, out=io.StringIO(), err=err,
                               lines=[], chain=clean["chain"],
                               environ_of=clean["environ_of"])
        self.assertEqual(code, 1)
        self.assertIn("unknown agent", err.getvalue())

    def test_input_lands_in_the_mailbox_as_the_human(self):
        client = chat.TerminalClient(self.cfg, self.db, "dev1", out=io.StringIO(),
                                     lines=[], subscribe=lambda: None,
                                     sleep=lambda seconds: time.sleep(0),
                                     **clean_chain())
        client.handle("bonjour")
        rows = mail.unread(self.db, "dev1")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sender"], mail.message_payload(rows[0]).get("human")),
                         ("tester", True))
