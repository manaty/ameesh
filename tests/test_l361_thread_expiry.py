# SPDX-License-Identifier: AGPL-3.0-only
"""L361: shared threads expire after one year (0033 §9), ids stay valid,
no append is lost to the rewrite, the index and the R12 outbox follow."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import conversation as C
from ameesh import fil, runner

from .support import PgTestCase

NOW = 1790000000.0
DAY = 86400.0
YEAR = 365 * DAY
THREAD = fil.ThreadRef("acme", "361")


def entry(text, age_days, author="agent:alpha"):
    return fil.Entry(author=author, recipient="agent:beta", text=text, ts=NOW - age_days * DAY)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="l361-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.transport = fil.FileTransport(self.root)
        self.path = self.transport.location(THREAD)

    def post(self, *entries, transport=None, thread=THREAD):
        return [(transport or self.transport).post(thread, e) for e in entries]

    def expire(self, **kwargs):
        kwargs.setdefault("days", 365)
        return self.transport.expire(THREAD, NOW - YEAR, now=NOW, **kwargs)

    def read_bytes(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_oldest_run_expires_kept_entries_and_ids_unchanged(self):
        ids = self.post(entry("old one", 500), entry("old two", 400),
                        entry("recent", 30), entry("today", 0))
        before = self.read_bytes()
        report = self.expire()
        self.assertEqual((report["dropped"], report["kept"]), (2, 2))
        kept = self.transport.read(THREAD)
        self.assertEqual([e.text for e in kept], ["recent", "today"])
        self.assertEqual([e.id for e in kept], ids[2:])
        # the kept entries are byte-identical, after a readable note
        after = self.read_bytes()
        cut = int(ids[2].rpartition("#")[2])
        self.assertTrue(after.endswith(before[cut:]))
        text = after.decode("utf-8")
        self.assertIn("Entries before %s have expired (retention of 365 days): 2 removed"
                      % fil.iso_local(NOW - YEAR), text)
        self.assertNotIn("old one", text)
        # a cursor on an expired entry reads from the oldest kept one
        self.assertEqual([e.text for e in self.transport.read(THREAD, since=ids[0])],
                         ["recent", "today"])
        self.assertEqual([e.text for e in self.transport.read(THREAD, since=ids[2])],
                         ["today"])
        self.assertEqual(self.transport.read(THREAD, since=ids[3]), [])

    def test_next_ids_continue_as_if_nothing_expired(self):
        twin = fil.FileTransport(os.path.join(self.root, "twin"))
        olds = [entry("old %d" % n, 400 + n, author="agent:a%d" % n) for n in range(3)]
        self.post(*olds)
        self.post(*olds, transport=twin)
        self.assertEqual(self.expire()["dropped"], 3)  # the whole thread
        self.assertEqual(self.transport.read(THREAD), [])
        mine = self.post(entry("after", 0), entry("after two", 0))
        theirs = self.post(entry("after", 0), entry("after two", 0), transport=twin)
        self.assertEqual(mine, theirs)
        # a second expiry adds up and keeps ids
        self.post(entry("late", 0))
        report = self.transport.expire(THREAD, NOW + 1, days=1, now=NOW + 2)
        self.assertEqual(report["dropped"], 3)
        self.assertIn('"entries": 6', self.read_bytes().decode())
        self.assertEqual(self.post(entry("again", -1)),
                         self.post(entry("late", 0), entry("again", -1), transport=twin)[1:])
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["again"])

    def test_only_a_leading_run_and_never_an_unreadable_date(self):
        self.post(entry("recent", 10), entry("late R12 delivery", 500))
        self.assertEqual(self.expire()["dropped"], 0)
        self.assertEqual(len(self.transport.read(THREAD)), 2)
        other = fil.ThreadRef("acme", "hand")
        os.makedirs(os.path.dirname(self.transport.location(other)), exist_ok=True)
        with open(self.transport.location(other), "w") as fh:
            fh.write(fil.header(other) + "### someday — human:x → tous\n\nwritten by hand\n")
        self.post(entry("old", 500), thread=other)
        report = self.transport.expire(other, NOW - YEAR, days=365, now=NOW)
        self.assertEqual(report["dropped"], 0)

    def test_dry_run_and_missing_thread_change_nothing(self):
        self.post(entry("old", 500), entry("new", 1))
        before = self.read_bytes()
        report = self.expire(dry_run=True)
        self.assertEqual((report["dropped"], report["kept"]), (1, 1))
        self.assertEqual(self.read_bytes(), before)
        missing = self.transport.expire(fil.ThreadRef("nowhere", "1"), NOW, days=1)
        self.assertEqual(missing["dropped"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "nowhere")))

    def test_append_racing_the_rewrite_lands_in_the_new_file(self):
        self.post(entry("old", 500), entry("kept", 1))
        real_rename = os.rename
        posted, started = [], threading.Event()

        def writer():
            started.set()
            posted.append(self.transport.post(THREAD, entry("racing", 0)))

        racer = threading.Thread(target=writer)

        def rename(*args, **kwargs):
            racer.start()
            started.wait(2)
            time.sleep(0.3)  # the writer has opened the old file, waits on its lock
            self.assertEqual(posted, [])
            return real_rename(*args, **kwargs)

        with mock.patch.object(fil.os, "rename", side_effect=rename):
            self.assertEqual(self.expire()["dropped"], 1)
        racer.join(5)
        entries = self.transport.read(THREAD)
        self.assertEqual([e.text for e in entries], ["kept", "racing"])
        self.assertEqual(entries[-1].id, posted[0])

    def test_reader_racing_the_rewrite_reads_the_new_file(self):
        self.post(entry("old", 500), entry("kept", 1))
        real_rename, got = os.rename, []
        reader = threading.Thread(target=lambda: got.append(self.transport.read(THREAD)))

        def rename(*args, **kwargs):
            reader.start()
            time.sleep(0.3)
            return real_rename(*args, **kwargs)

        with mock.patch.object(fil.os, "rename", side_effect=rename):
            self.expire()
        reader.join(5)
        self.assertEqual([e.text for e in got[0]], ["kept"])

    def test_planted_temporary_names_are_never_written_through(self):
        """r2 P1: a symlink or a HARD LINK at a temporary name is only unlinked."""
        self.post(entry("old", 500), entry("kept", 1))
        folder = os.path.dirname(self.path)
        outside = os.path.join(self.root, "outside")
        with open(outside, "w") as fh:
            fh.write("untouched")
        os.symlink(outside, os.path.join(folder, ".361.expire.tmp"))
        os.link(self.path, os.path.join(folder, ".361.expire.0123456789abcdef.tmp"))
        before = self.read_bytes()
        # the write fails: the thread must be exactly as before
        with mock.patch.object(fil.os, "fsync", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                self.expire()
        self.assertEqual(self.read_bytes(), before)
        with open(outside) as fh:
            self.assertEqual(fh.read(), "untouched")
        self.assertEqual(sorted(os.listdir(folder)), ["361.md"])
        self.assertEqual(self.expire()["dropped"], 1)
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["kept"])

    def test_unrecognised_heading_between_old_entries_is_never_deleted(self):
        """r2 P1: a heading `_walk` does not recognise (date with a space) is
        text we cannot date: it stays, with everything after it."""
        ids = self.post(entry("old one", 500))
        with open(self.path, "a") as fh:
            fh.write("### 2026-09-20 12:00:00+00:00 — human:x → all\n\nnewer, by hand\n\n")
        self.post(entry("old two", 450), entry("recent", 1))
        report = self.expire()
        self.assertEqual((report["dropped"], report["kept"]), (1, 2))
        text = self.read_bytes().decode()
        self.assertNotIn("old one", text)
        self.assertIn("### 2026-09-20 12:00:00+00:00 — human:x → all\n\nnewer, by hand", text)
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["old two", "recent"])
        report = self.expire()  # nothing past the text, now ahead of every entry
        self.assertEqual((report["dropped"], report["held"]),
                         (0, "unrecognised text before the first entry"))
        self.assertEqual([e.text for e in self.transport.read(THREAD, since=ids[0])],
                         ["old two", "recent"])

    def test_header_width_never_prevents_expiry_and_dry_run_touches_nothing(self):
        """r2 P2: every thread size expires (no shift oscillation); a dry run
        removes no file, even one at an old temporary name."""
        for size in range(0, 700, 7):
            with self.subTest(size=size):
                root = tempfile.mkdtemp(dir=self.root)
                transport = fil.FileTransport(root)
                old, new = transport.post(THREAD, entry("x" * size, 500)), \
                    transport.post(THREAD, entry("recent", 1))
                folder = os.path.dirname(transport.location(THREAD))
                with open(os.path.join(folder, ".361.expire.tmp"), "w") as fh:
                    fh.write("someone else's")
                listing = sorted(os.listdir(folder))
                transport.expire(THREAD, NOW - YEAR, days=365, now=NOW, dry_run=True)
                self.assertEqual(sorted(os.listdir(folder)), listing)
                self.assertEqual(transport.expire(THREAD, NOW - YEAR, days=365,
                                                  now=NOW)["dropped"], 1)
                self.assertEqual([e.id for e in transport.read(THREAD)], [new])
                self.assertEqual(transport.read(THREAD, since=old)[0].id, new)
                # a second expiry over the note keeps ids too
                later = transport.post(THREAD, entry("later", -400))
                self.assertEqual(transport.expire(THREAD, NOW + 1, days=1, now=NOW)["dropped"], 1)
                self.assertEqual([e.id for e in transport.read(THREAD)], [later])

    def test_failed_write_keeps_the_old_file_and_no_leftover(self):
        self.post(entry("old", 500), entry("kept", 1))
        before = self.read_bytes()
        with mock.patch.object(fil.os, "fsync", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                self.expire()
        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["361.md"])

    def test_torn_recent_tail_is_kept_then_repaired(self):
        self.post(entry("old", 500))
        with open(self.path, "ab") as fh:
            fh.write(("### %s — agent:alpha → tous\n\n```\nhalf writ"
                      % fil.iso_local(NOW)).encode())
        self.assertEqual(self.expire()["dropped"], 1)
        self.post(entry("next", 0))
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["half writ", "next"])

    def test_all_threads_and_retention_settings(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        self.post(entry("old elsewhere", 400), thread=fil.ThreadRef("other"))
        self.assertEqual(fil.expire(dataclasses.replace(cfg, thread_retention_days=0),
                                    None, now=NOW), [])
        reports = fil.expire(cfg, None, now=NOW, dry_run=True)
        self.assertEqual(sorted((r["thread"], r["dropped"]) for r in reports),
                         [("acme/361", 1), ("other/_projet", 1)])
        self.assertEqual(fil.expire(cfg, None, days=600, now=NOW)[0]["dropped"], 0)
        self.assertEqual(sum(r["dropped"] for r in fil.expire(cfg, None, now=NOW)), 2)
        with self.assertRaises(ValueError):
            fil.expire(cfg, None, days=-1)
        self.assertEqual(config_mod.Config().thread_retention_days, 365)
        loaded = config_mod.load(env={"AMEESH_THREAD_RETENTION_DAYS": "30",
                                      "AMEESH_STATE": self.root})
        self.assertEqual(loaded.thread_retention_days, 30)
        # r2 P3: never truncated (0.5 would disable expiry), never negative
        for raw in ("-3", "0.5", "1.9", "1e3", "²", "", "seven"):
            loaded = config_mod.load(env={"AMEESH_THREAD_RETENTION_DAYS": raw,
                                          "AMEESH_STATE": self.root})
            self.assertEqual(loaded.thread_retention_days, 365, raw)
        loaded = config_mod.load(env={"AMEESH_THREAD_RETENTION_DAYS": " 0 ",
                                      "AMEESH_STATE": self.root})
        self.assertEqual(loaded.thread_retention_days, 0)

    def test_cli_dry_run_then_expire(self):
        self.post(entry("old", 500), entry("kept", 1))
        env = {"AMEESH_THREADS": self.root, "AMEESH_STATE": self.root,
               "AMEESH_DSN": "postgresql://ameesh:ameesh@127.0.0.1:5599/ameesh"}
        with mock.patch.dict(os.environ, env), mock.patch.object(fil.time, "time",
                                                                   return_value=NOW):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(fil.main(["expire", "--dry-run"]), 0)
            self.assertIn("acme/361: 1 entry would expire before", out.getvalue())
            self.assertEqual(len(self.transport.read(THREAD)), 2)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(fil.main(["expire"]), 0)
            self.assertIn("thread index unavailable", err.getvalue())
            self.assertIn("acme/361: 1 entry expired before", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(fil.main(["expire", "--days", "0"]), 0)
            self.assertIn("nothing expires", out.getvalue())
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["kept"])

    def test_runner_expires_at_most_once_a_day(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        fake = types.SimpleNamespace(cfg=cfg, dry_run=False)
        with mock.patch.object(runner, "log_async") as logged:
            reports = runner.Runner.expire_threads_once(fake, None, now=NOW)
            self.assertEqual(reports[0]["dropped"], 1)
            self.assertIn("1 entry before", logged.call_args.args[0])
            self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW + 3600))
            self.assertIsNotNone(runner.Runner.expire_threads_once(fake, None, now=NOW + DAY))
            with mock.patch.object(fil, "expire", side_effect=RuntimeError("boom")):
                self.assertIsNone(runner.Runner.expire_threads_once(fake, None,
                                                                    now=NOW + 2 * DAY))
            self.assertIn("thread expiry failed", logged.call_args.args[0])


class IndexAndOutboxTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM conversation_log; DELETE FROM conversation_heads;"
                        " DELETE FROM thread_index")
        self.cfg = dataclasses.replace(self.cfg, threads_dir=os.path.join(
            self.tmp, self._testMethodName))

    def test_index_counts_kept_entries_and_forgets_expired_excerpt(self):
        for text, age in (("old secret", 500), ("older secret", 450)):
            fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text=text,
                       ts=NOW - age * DAY, project="acme", lot="9")
        for text, age in (("old", 500), ("fresh", 2)):
            fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text=text,
                       ts=NOW - age * DAY, project="acme", lot="10")
        reports = fil.expire(self.cfg, self.db, now=NOW)
        self.assertEqual(sorted((r["thread"], r["dropped"], r["kept"]) for r in reports),
                         [("acme/10", 1, 1), ("acme/9", 2, 0)])
        rows = {r["lot"]: r for r in self.db.query(
            "SELECT lot, entries, last_excerpt, last_entry_id FROM thread_index")}
        self.assertEqual((rows["9"]["entries"], rows["9"]["last_excerpt"],
                          rows["9"]["last_entry_id"]), (0, "", None))
        self.assertEqual((rows["10"]["entries"], rows["10"]["last_excerpt"]), (1, "fresh"))
        # the index id of the kept entry still reads it
        transport = fil.transport_for(self.cfg)
        self.assertEqual(transport.read(fil.ThreadRef("acme", "10"))[0].id,
                         rows["10"]["last_entry_id"])

    def index_row(self, lot):
        rows = self.db.query("SELECT entries, last_excerpt, last_entry_id, last_mailbox_id"
                             " FROM thread_index WHERE lot = %s", (lot,))
        return rows[0] if rows else None

    def test_indexing_after_expiry_neither_counts_nor_quotes_expired_entries(self):
        """r2 P2: post, pause before indexing, expire, then index."""
        transport = fil.transport_for(self.cfg)
        thread = fil.ThreadRef("acme", "late")
        old = entry("old secret", 500)
        old_id = transport.post(thread, old)  # written, not yet indexed
        self.assertEqual(fil.expire(self.cfg, self.db, now=NOW)[0]["dropped"], 1)
        fil.index(self.db, self.cfg, transport, thread, old, old_id)
        row = self.index_row("late")
        self.assertEqual((row["entries"], row["last_excerpt"], row["last_entry_id"]),
                         (0, "", None))
        # an entry already in the thread when it expired is counted once
        thread = fil.ThreadRef("acme", "inflight")
        fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="gone",
                   ts=NOW - 500 * DAY, project="acme", lot="inflight")
        fresh = entry("fresh", 1)
        fresh.meta = {"ids": [41]}
        fresh_id = transport.post(thread, fresh)
        fil.expire(self.cfg, self.db, now=NOW)
        fil.index(self.db, self.cfg, transport, thread, fresh, fresh_id, [41])
        row = self.index_row("inflight")
        self.assertEqual((row["entries"], row["last_excerpt"], row["last_entry_id"],
                          row["last_mailbox_id"]), (1, "fresh", fresh_id, 41))
        # and one written after the expiry, normally
        fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="next",
                   ts=NOW, project="acme", lot="inflight")
        self.assertEqual((self.index_row("inflight")["entries"],
                          self.index_row("inflight")["last_excerpt"]), (2, "next"))

    def test_expiry_waits_for_an_open_r12_drain_without_deadlock(self):
        """The drain holds the index row through its transaction, then writes
        the file: expiry takes the row first, then the file (same order)."""
        fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="old",
                   ts=NOW - 500 * DAY, project="acme", lot="order")
        fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="new",
                   ts=NOW, project="acme", lot="order")
        done, errors = threading.Event(), []

        def expiry():
            db = __import__("ameesh.db", fromlist=["connect"]).connect(self.cfg)
            try:
                errors.extend(r for r in fil.expire(self.cfg, db, now=NOW) if r.get("error"))
            finally:
                db.close()
                done.set()

        with self.db.transaction() as tx:
            fil.record(self.cfg, tx, sender="alpha", recipients=["beta"], text="drained",
                       ts=NOW, project="acme", lot="order")
            worker = threading.Thread(target=expiry)
            worker.start()
            self.assertFalse(done.wait(0.5))  # waits on the row, not holding the file
            fil.record(self.cfg, tx, sender="alpha", recipients=["beta"], text="drained 2",
                       ts=NOW, project="acme", lot="order")
        self.assertTrue(done.wait(20))
        worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual([e.text for e in fil.transport_for(self.cfg).read(
            fil.ThreadRef("acme", "order"))], ["new", "drained", "drained 2"])
        self.assertEqual(self.index_row("order")["entries"], 3)

    def test_pending_r12_goes_only_past_thread_retention(self):
        log = C.Log(self.db, self.cfg, agent="worker", project="acme", lot="361")
        log.append("human_message", "ancient question", author="human:alice", event_id="a")
        log.append("human_message", "old question", author="human:alice", event_id="b")
        log.append("text", "old detail")
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '400 days'"
                        " WHERE event_id = 'a'")
        self.db.execute("UPDATE conversation_log SET created_at = now() - interval '100 days'"
                        " WHERE event_id IS DISTINCT FROM 'a'")
        self.assertEqual(log.prune(thread_days=0), 1)  # detail only: both pending kept
        self.assertEqual(len(log.store.pending_threads("worker")), 2)
        self.assertEqual(log.prune(), 1)  # the 400-day pending entry would expire on arrival
        self.assertEqual([r["text"] for r in log.store.pending_threads("worker")],
                         ["old question"])
        with self.assertRaises(ValueError):
            log.prune(thread_days=-1)
        self.assertEqual(log.drain_threads(), 1)
        self.assertEqual([e.text for e in fil.transport_for(self.cfg).read(
            fil.ThreadRef("acme", "361"))], ["old question"])


if __name__ == "__main__":
    unittest.main()
