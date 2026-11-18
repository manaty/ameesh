# SPDX-License-Identifier: AGPL-3.0-only
"""#7667: an avatar's conversation segment includes the agent mail it received.

The runner journals a mail turn's whole prompt as one `input` row
(`runner.py`, `Recorder.append("input", spec["prompt"])`); only human mail is
also journaled as `human_message` (`_journal_humans`).  A segment used to keep
`human_message`, `text` and `turn_end` only, so after a move the new session
saw its answers without the questions.  `select` now reduces an `input` row to
the delivered messages it contains: each message's id, sender, date and body,
with the runner's wrapper (mail header, authority note, footer, resume
preface) left out, the same secret filter and the same budget.
"""
from __future__ import annotations

import dataclasses
import os
import unittest

from ameesh import adapters, conversation as C, conversation_segment as S, fil, mail

from .support import PgTestCase

SECRET = "sk-test-0123456789abcdef-SECRET"
TS = 1763353394.0


def row(message_id, sender, body, *, ts=TS, kind="mail", payload=None):
    return {"id": message_id, "sender": sender, "body": body, "created_ts": ts,
            "kind": kind, "payload": payload or {}}


def prompt(rows, *, budget=200_000):
    text, kept, remaining = mail.prompt_for(None, list(rows), kind="mail", budget=budget)
    assert len(kept) == len(rows) and remaining == 0, (kept, remaining)
    return text


def turn_rows(text, *, seq=1, answer="réponse", tools=0, agent="avatar"):
    rows = [{"seq": seq, "kind": "input", "author": "ameesh:runner", "ts": TS,
             "text": text, "meta": {}}]
    for index in range(tools):
        rows.append({"seq": seq + 1 + index, "kind": "tool", "author": "agent:" + agent,
                     "ts": TS + index, "text": "Bash",
                     "meta": {"tool": "Bash", "status": "completed"}})
    rows.append({"seq": seq + 1 + tools, "kind": "text", "author": "agent:" + agent,
                 "ts": TS + 2, "text": "je regarde", "meta": {}})
    rows.append({"seq": seq + 2 + tools, "kind": "turn_end", "author": "agent:" + agent,
                 "ts": TS + 3, "text": answer, "meta": {"status": "completed"}})
    return rows


def size(document):
    return len(S.canonical(document).encode("utf-8"))


class DeliveredMessagesTest(unittest.TestCase):
    def test_each_agent_message_keeps_id_sender_date_and_body(self):
        messages = S._delivered_messages(prompt([
            row(223511, "mty-amh-impl-18", "Essai du lot #7635.\nDeuxième ligne.\n\nAprès."),
        ]))
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual((message["id"], message["sender"]), (223511, "mty-amh-impl-18"))
        self.assertEqual(message["ts"], TS)
        self.assertIn("#223511", message["text"])
        self.assertIn("mty-amh-impl-18", message["text"])
        self.assertIn("Essai du lot #7635.", message["text"])
        self.assertIn("Deuxième ligne.\n\nAprès.", message["text"])

    def test_runner_wrapper_and_human_mail_are_left_out(self):
        messages = S._delivered_messages(prompt([
            row(1, "human:alice", "question humaine"),
            row(2, "mty-amh-impl-18", "courrier d'agent"),
        ]))
        self.assertEqual([m["id"] for m in messages], [2])
        text = "\n\n".join(m["text"] for m in messages)
        for wrapper in (adapters.MAIL_HEADER % 2, adapters.AUTHORITY_NOTE,
                        adapters.MAIL_FOOTER, adapters.TMP_NOTE, adapters.HEAVY_NOTE):
            self.assertNotIn(wrapper, text)
        self.assertNotIn("question humaine", text)

    def test_a_quoted_header_in_a_body_does_not_open_a_block(self):
        text = ("— message #7 from agent:a, 2026-01-01T00:00:00+00:00:\n"
                "> — message #8 from agent:b, 2026-01-01T00:00:01+00:00:\n"
                "> corps réel\n")
        messages = S._delivered_messages(text)
        self.assertEqual([m["id"] for m in messages], [7])
        self.assertIn("— message #8 from agent:b", messages[0]["text"])
        self.assertIn("corps réel", messages[0]["text"])

    def test_nature_and_authority_note_stay_in_the_kept_header(self):
        block = mail._block(None, row(9, "ameesh", "SLO breach", kind="event",
                                      payload={"urgent": True}),
                            note="  [signed by an agent: authentic, without the owner's authority]")
        messages = S._delivered_messages(block)
        self.assertEqual(len(messages), 1)
        self.assertIn("(event, urgent)", messages[0]["text"])
        self.assertIn("signed by an agent", messages[0]["text"])


class SelectTest(unittest.TestCase):
    def test_mail_turn_keeps_its_question_without_the_wrapper(self):
        question = "Essai #7635, étape 2.\nRéponds en une ligne."
        rows = turn_rows(prompt([row(223511, "mty-amh-impl-18", question)]))
        doc = S.select(rows, agent="avatar", secrets=[])
        entry = doc["entries"][0]
        self.assertEqual(entry["kind"], "input")
        self.assertEqual(entry["author"], "mty-amh-impl-18")
        self.assertEqual(entry["ts"], TS)
        text = S.canonical(doc)
        for kept in ("223511", "mty-amh-impl-18", fil.iso_local(TS),
                     "Essai #7635, étape 2.", "Réponds en une ligne."):
            self.assertIn(kept, text)
        for wrapper in ("reproduced below", adapters.AUTHORITY_NOTE, adapters.TMP_NOTE,
                        adapters.VENV_NOTE, "conversation continued from host"):
            self.assertNotIn(wrapper, text)
        rendered = S.render(doc)
        self.assertIn("**Courrier de mty-amh-impl-18**", rendered)
        self.assertIn("> Essai #7635, étape 2.", rendered)

    def test_a_multi_message_batch_keeps_every_message_in_order(self):
        rows = turn_rows(prompt([
            row(1, "mty-amh-impl-18", "premier"),
            row(2, "ameesh", "événement", kind="event", payload={"urgent": True}),
            row(3, "human:alice", "question humaine"),
        ]))
        doc = S.select(rows, agent="avatar", secrets=[])
        entry = doc["entries"][0]
        self.assertEqual(entry["author"], "mty-amh-impl-18, ameesh")
        ordered = [entry["text"].index(marker) for marker in
                   ("#1 ", "premier", "#2 ", "événement")]
        self.assertEqual(ordered, sorted(ordered))
        self.assertNotIn("question humaine", S.canonical(doc))

    def test_an_input_without_a_delivered_message_is_dropped(self):
        rows = turn_rows(adapters.SUMMARY_PROMPT)
        doc = S.select(rows, agent="avatar", secrets=[])
        self.assertEqual([e["kind"] for e in doc["entries"]], ["text", "turn_end"])

    def test_mail_text_goes_through_the_same_secret_filter(self):
        question = "mon jeton est %s, garde-le" % SECRET
        rows = turn_rows(prompt([row(1, "mty-amh-impl-18", question)]))
        doc = S.select(rows, agent="avatar", secrets=[SECRET])
        for text in (S.canonical(doc), S.render(doc)):
            self.assertNotIn(SECRET, text)
        self.assertIn(S.REDACTED, S.canonical(doc))
        sealed = S.seal(doc, secrets=[SECRET])
        S.verify(sealed, sealed["digest"])

    def test_oversized_mail_turn_is_reduced_to_the_question_and_answer(self):
        rows = turn_rows(prompt([row(1, "mty-amh-impl-18", "Q" * 30_000)]),
                         answer="R" * 30_000)
        doc = S.select(rows, agent="avatar", secrets=[])
        self.assertLessEqual(size(doc), S.BUDGET_BYTES)
        self.assertEqual([e["kind"] for e in doc["entries"]], ["input", "turn_end"])
        self.assertEqual(doc["entries"][0]["text"][:1], "—")
        self.assertTrue(doc["cut"])

    def test_truncated_input_keeps_its_flag(self):
        rows = turn_rows(prompt([row(1, "mty-amh-impl-18", "corps")]))
        rows[0]["truncated"] = True
        doc = S.select(rows, agent="avatar", secrets=[])
        self.assertTrue(doc["entries"][0]["truncated"])


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def read(self, agent, *, after=0, limit=100):
        return {"entries": [r for r in self.rows if int(r["seq"]) > after][:limit]}


class TurnHeadTest(unittest.TestCase):
    def test_a_short_window_still_finds_the_mail_that_opened_the_turn(self):
        opening = {"seq": 1, "kind": "input", "author": "ameesh:runner", "ts": TS,
                   "text": prompt([row(1, "agent:a", "question")]), "meta": {}}
        rows = [opening]
        rows += [{"seq": n, "kind": "text", "author": "agent:a", "ts": TS, "text": "t",
                  "meta": {}} for n in range(2, 700)]
        rows.append({"seq": 700, "kind": "turn_end", "author": "agent:a", "ts": TS,
                     "text": "fin", "meta": {"status": "completed"}})
        head, whole = S._turn_head(FakeStore(rows), "a", 700)
        self.assertFalse(whole)
        self.assertEqual([entry["seq"] for entry in head], [1])


class BuildTest(PgTestCase):
    """The real journal path: `Recorder` writes `input`, `build` reads it."""

    def setUp(self):
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName),
            project="acme")
        self.log = C.Log(self.db, self.cfg, agent="avatar", project="acme-team")

    def test_build_includes_the_mail_of_the_turn(self):
        text = prompt([row(223511, "mty-amh-impl-18", "question reçue")])
        self.log.append("input", text, author="ameesh:runner", turn="t1")
        self.log.append("text", "je réponds", turn="t1")
        self.log.append("turn_end", "réponse", turn="t1", meta={"status": "completed"},
                        thread_text="réponse", event_id="final:t1")
        self.log.drain_threads()
        before = self.db.query("SELECT count(*) AS n, max(seq) AS m FROM conversation_log")[0]
        doc = S.build(self.db, self.cfg, "avatar", secrets=[])
        after = self.db.query("SELECT count(*) AS n, max(seq) AS m FROM conversation_log")[0]
        self.assertEqual(before, after)
        self.assertEqual([e["kind"] for e in doc["entries"]],
                         ["input", "text", "turn_end"])
        self.assertIn("#223511", S.canonical(doc))
        self.assertIn("question reçue", S.canonical(doc))
        S.verify(doc, doc["digest"])


if __name__ == "__main__":
    unittest.main()
