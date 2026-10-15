# SPDX-License-Identifier: AGPL-3.0-only
"""L83 (#291): an agent's conversation on the mesh API.

`GET /api/v1/agents/{agent}/chat` reads the shared history of L78 with a
sequence cursor; `POST /api/v1/agents/{agent}/chat` is the web input: the
server writes the `human_message` entry and delivers the mail that wakes the
agent (decision 0035 §2/§4, `docs/CONVERSATIONS.md`).

Without a database: the guards, the identity never taken from the body, and
the `thread=False` producer of `Log` (the readable copy is the caller's, so a
chat message has exactly one R12 outbox). On a migrated schema: the entry,
the mail, the single readable thread copy, the cursor and the replay of an
idempotency key.
"""
from __future__ import annotations

import dataclasses
import json
import unittest
import urllib.parse
from unittest import mock

from ameesh import chat, config as config_mod, conversation, fil, mail, registry
from ameesh import serve, serve_actions, storage, work

from .support import PgTestCase
from .test_l87_serve_actions import PostMixin

TEXT = "bonjour depuis la page"


class _Store:
    """A conversation store that records what `Log` asks it to write."""

    def __init__(self):
        self.calls = []

    def append(self, **kwargs):
        self.calls.append(kwargs)
        return dict(kwargs, seq=len(self.calls), ts=1000.0)


class _Storage:
    def __init__(self, store):
        self.conversations = store


class ThreadFlagTest(unittest.TestCase):
    """`Log.append(thread=False)`: the readable R12 copy is the caller's."""

    def setUp(self):
        self.store = _Store()
        patcher = mock.patch.object(storage, "of", return_value=_Storage(self.store))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = conversation.Log(object(), object(), agent="a1", project="demo")

    def test_a_human_message_keeps_its_outbox_by_default(self):
        self.log.append("human_message", "salut", author="human:owner")
        self.assertEqual(self.store.calls[0]["thread_text"], "salut")

    def test_thread_false_writes_no_second_outbox(self):
        self.log.append("human_message", "salut", author="human:owner", thread=False)
        self.assertIsNone(self.store.calls[0]["thread_text"])

    def test_thread_false_refuses_a_thread_text(self):
        with self.assertRaises(ValueError):
            self.log.append("human_message", "salut", thread=False, thread_text="salut")


class PostHumanMessageTest(unittest.TestCase):
    """The single write path: the mail first, then the conversation echo."""

    class _Log:
        instances: list = []

        def __init__(self, db, cfg, *, agent, project=None, lot=None):
            self.kwargs = {"db": db, "cfg": cfg, "agent": agent, "project": project,
                           "lot": lot}
            self.appends = []
            type(self).instances.append(self)

        def append(self, kind, text="", **kwargs):
            self.appends.append((kind, text, kwargs))
            return {"seq": 3, "kind": kind, "author": kwargs.get("author"), "ts": 5.0,
                    "event_id": kwargs.get("event_id")}

    def setUp(self):
        self.cfg = config_mod.load()
        type(self)._Log.instances = []

    def test_mail_first_then_one_echo(self):
        db = object()
        row = {"name": "a1", "team": "demo", "canon_governed": True}
        with mock.patch.object(registry, "get", return_value=row), \
                mock.patch.object(mail, "send", return_value=7) as send, \
                mock.patch.object(conversation, "Log", self._Log):
            out = chat.post_human_message(self.cfg, db, human="human:owner", sender="owner",
                                          agent="a1", text="salut")
        send.assert_called_once_with(db, "owner", "a1", "salut", host=self.cfg.host,
                                     kind="notify", payload={"human": True},
                                     work_item_id=None)
        log = self._Log.instances[0]
        self.assertEqual(log.kwargs["project"], "demo")
        self.assertEqual(log.appends, [("human_message", "salut",
                                        {"author": "human:owner", "event_id": "mail:7",
                                         "thread": False})])
        self.assertEqual(out, {"agent": "a1", "message_id": 7, "lot": None,
                               "entry": {"seq": 3, "kind": "human_message",
                                         "author": "human:owner", "ts": 5.0,
                                         "event_id": "mail:7"}})

    def test_an_invalid_agent_is_refused_before_the_mail(self):
        with mock.patch.object(mail, "send") as send:
            with self.assertRaises(ValueError):
                chat.post_human_message(self.cfg, object(), human="human:owner",
                                        sender="owner", agent="../a1", text="salut")
        send.assert_not_called()


class GuardsTest(PostMixin, unittest.TestCase):
    """No database: refused before any action, nothing written."""

    def setUp(self):
        self.writes = []
        ctx = serve.Context(cfg=object(), human="human:owner")
        self.start(ctx, read=lambda fn: self.fail("no read"),
                   write=lambda fn: self.writes.append(fn) or (200, {}, False))

    def test_the_history_needs_the_token(self):
        status, body = self.get_json("/api/v1/agents/a1/chat", auth=False)
        self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_the_input_needs_a_key(self):
        status, _h, body = self.post("/api/v1/agents/a1/chat", {"text": "x"}, key=None)
        self.assertEqual((status, body["error"]), (400, "idempotency_key"))
        self.assertEqual(self.writes, [])

    def test_the_input_takes_no_identity_from_the_body(self):
        for body in ({"text": "x", "author": "human:other"},
                     {"text": "x", "sender": "other"},
                     {"text": "x", "urgent": True},
                     {"text": "x", "human": True}):
            status, _h, out = self.post("/api/v1/agents/a1/chat", body)
            self.assertEqual((status, out["error"]), (400, "bad_body"), body)
        self.assertEqual(self.writes, [])

    def test_an_empty_text_is_refused(self):
        ctx = serve.Context(cfg=object(), human="human:owner")
        with self.assertRaises(serve_actions.ActionError) as exc:
            serve_actions._chat(ctx, None, {"agent": "a1"}, {"text": "   "})
        self.assertEqual((exc.exception.status, exc.exception.code), (400, "bad_body"))
        with self.assertRaises(serve_actions.ActionError) as exc:
            serve_actions._chat(ctx, None, {"agent": "a1"}, {"text": "x" * 8001})
        self.assertEqual((exc.exception.status, exc.exception.code), (400, "bad_body"))
        self.assertEqual(self.writes, [])


class ChatPgTest(PostMixin, PgTestCase):
    """On a migrated schema: the entry, the mail, the thread and the cursor."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency, agent_mailbox, conversation_log,"
                        " conversation_heads CASCADE")
        registry.upsert(self.db, "a1", chantier="demo", harness="codex", host="h1")
        cfg = dataclasses.replace(self.cfg, read_only=True)
        self.start(serve.Context(cfg=cfg, human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))

    def history(self, agent="a1", **query):
        path = "/api/v1/agents/%s/chat" % agent
        if query:
            path += "?" + urllib.parse.urlencode(query)
        status, data = self.get_json(path)
        self.assertEqual(status, 200, data)
        return data

    def mailbox(self):
        return self.db.query("select sender, recipient, body, payload::text as payload,"
                             " work_item_id from agent_mailbox order by id")

    def thread_texts(self, project="demo"):
        return [entry.text for entry in
                fil.transport_for(self.cfg).read(fil.ThreadRef(project))]

    def test_the_input_writes_one_entry_one_mail_one_thread_copy(self):
        status, headers, out = self.post("/api/v1/agents/a1/chat", {"text": TEXT})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["schema"], out["action"], out["by"], out["for"]),
                         ("ameesh-action/1", "chat", "human:owner", "human:owner"))
        entry = out["result"]["entry"]
        self.assertEqual(entry["kind"], "human_message")
        self.assertEqual(entry["author"], "human:owner")
        self.assertEqual(entry["event_id"], "mail:%d" % out["result"]["message_id"])
        rows = self.mailbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sender"], rows[0]["recipient"]), ("owner", "a1"))
        self.assertTrue(json.loads(rows[0]["payload"])["human"])
        page = self.history()
        self.assertEqual([e["text"] for e in page["entries"]], [TEXT])
        self.assertEqual(page["entries"][0]["kind"], "human_message")
        self.assertEqual(self.thread_texts().count(TEXT), 1)
        # the same key and request: the first answer back, nothing written twice
        again, headers, out2 = self.post("/api/v1/agents/a1/chat", {"text": TEXT})
        self.assertEqual((again, out2, headers.get("Idempotent-Replayed")),
                         (200, out, "true"))
        self.assertEqual(len(self.mailbox()), 1)
        self.assertEqual([e["text"] for e in self.history()["entries"]], [TEXT])
        self.assertEqual(self.thread_texts().count(TEXT), 1)

    def test_a_lot_labels_the_entry_and_the_mail(self):
        item = work.add(self.db, title="lot discute", app="demo", assignee="a1")
        status, _h, out = self.post("/api/v1/agents/a1/chat",
                                    {"text": TEXT, "lot": item["id"]},
                                    key="key-0000000021")
        self.assertEqual(status, 200, out)
        self.assertEqual(out["result"]["lot"], item["id"])
        self.assertEqual(self.mailbox()[0]["work_item_id"], str(item["id"]))
        self.assertEqual(self.history()["entries"][0]["lot"], str(item["id"]))

    def test_the_cursor_pages_the_history(self):
        log = conversation.Log(self.db, self.cfg, agent="a1", project="demo")
        for index in range(3):
            log.append("text", "t%d" % index, author="agent:a1")
        page = self.history(limit=2)
        self.assertEqual(page["schema"], "ameesh-conversation/1")
        self.assertEqual([e["text"] for e in page["entries"]], ["t0", "t1"])
        self.assertTrue(page["more"])
        self.assertEqual(page["next_seq"], page["entries"][-1]["seq"])
        page = self.history(after=page["next_seq"])
        self.assertEqual([e["text"] for e in page["entries"]], ["t2"])
        self.assertFalse(page["more"])
        self.assertFalse(page["gap"])
        for query in ({"limit": 0}, {"after": -1}, {"nope": 1}):
            path = "/api/v1/agents/a1/chat?" + urllib.parse.urlencode(query)
            status, data = self.get_json(path)
            self.assertEqual((status, data["error"]), (400, "bad_query"), query)

    def test_an_unknown_agent_writes_nothing(self):
        status, _h, out = self.post("/api/v1/agents/ghost/chat", {"text": TEXT},
                                    key="key-0000000022")
        self.assertEqual((status, out["error"]), (404, "not_found"))
        self.assertEqual(self.mailbox(), [])
        status, data = self.get_json("/api/v1/agents/ghost/chat")
        self.assertEqual((status, data["error"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
