# SPDX-License-Identifier: AGPL-3.0-only
"""L361: shared threads expire after one year (0033 §9), ids stay valid,
no append is lost to the rewrite, the index and the R12 outbox follow."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import threading
import time
import types
import tracemalloc
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

    def test_latest_of_entries_written_in_the_same_second_stays_the_latest(self):
        """r3 P2: a timestamp tie keeps the later entry, as indexing does."""
        first, second = entry("first", 0), entry("second", 0)
        ids = self.post(entry("old", 500), first, second)
        self.assertEqual(first.ts, second.ts)
        last = self.expire()["last"]
        self.assertEqual((last.id, last.text), (ids[2], "second"))

    def test_expiry_memory_does_not_materialize_all_retained_bodies(self):
        # A project's daily expiry runs in the long-lived runner, even when
        # nothing expires. Its temporary allocations must not multiply the
        # size of a large retained thread (#4809).
        self.post(*(entry("x" * 65536, 1) for _ in range(64)))
        size = os.path.getsize(self.path)
        tracemalloc.start()
        try:
            report = self.expire()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((report["dropped"], report["kept"]), (0, 64))
        self.assertLess(peak, 3 * size)

    def census_complet(self):
        """Le tuple du census complet — (entrées gardées, ligne d'index de la
        plus récente, fin logique) — la référence du census du post (#1764 N6 :
        comparer le tuple entier, pas seulement le compte ou l'extrait)."""
        entries, whole_end = self.transport.census(THREAD)
        newest = max(reversed(entries), key=lambda e: e.ts) if entries else None
        return (len(entries), fil._index_row(newest), whole_end)

    def assert_census_after_post(self, expected_last=None):
        """Le census retenu par le post est exactement celui d'une relecture
        complète."""
        kept, last, end = self.transport.census_after_post(THREAD)
        self.assertEqual((kept, last, end), self.census_complet())
        if expected_last is not None:
            self.assertEqual(last["excerpt"], expected_last)

    def test_census_after_post_matches_a_full_read(self):
        """#1670 : le census déduit par `post` de sa propre marche (réparation
        d'une écriture interrompue) est celui d'une lecture complète — la plus
        récente à date égale, une entrée en retard comprise — et il est écarté
        dès qu'un autre écrivain ou une expiration touche le fichier."""
        self.post(entry("old", 400), entry("first", 0))
        self.post(entry("second", 0))
        self.assert_census_after_post("second")
        self.post(entry("late", 3))  # date ancienne, écrite en dernier
        self.assert_census_after_post("second")
        # un autre écrivain (autre instance) : le census retenu est écarté
        fil.FileTransport(self.root).post(THREAD, entry("other", 0))
        self.assertIsNone(self.transport.census_after_post(THREAD))
        self.post(entry("after", 0))
        self.assert_census_after_post("after")
        # une expiration réécrit le fil : le décalage du nouvel en-tête est pris
        self.assertEqual(self.expire()["dropped"], 1)  # « old », en tête
        self.post(entry("post-expiry", 0))
        self.assert_census_after_post("post-expiry")

    def test_the_census_is_the_full_read_even_with_an_expiry_shift(self):
        """#1764 N6 : après une expiration (décalage d'en-tête), le census du
        post reste celui d'une relecture complète — identité logique de
        l'entrée comprise (`base`), et une entrée datée ancienne écrite après
        l'expiration ne prend pas la place de la plus récente."""
        self.post(entry("old", 400), entry("kept", 1))
        self.assertEqual(self.expire()["dropped"], 1)
        self.post(entry("late", 3))     # date ancienne, écrite en dernier
        self.assert_census_after_post("kept")

    def test_a_same_size_rewrite_forces_a_full_census(self):
        """#1764 N1 : l'identité du census n'est pas que (périphérique, inode,
        taille) — une réécriture de même taille change mtime/ctime_ns et le
        census retenu est écarté."""
        self.post(entry("old", 500))
        self.transport.post(THREAD, entry("mine", 0))
        self.assertIsNotNone(self.transport.census_after_post(THREAD))

        raw = self.read_bytes()
        self.assertIn(b"old", raw)
        rewritten = raw.replace(b"old", b"OLD")     # même longueur
        self.assertEqual(len(rewritten), len(raw))
        with open(self.path, "r+b") as fh:
            fh.write(rewritten)
            fh.flush()
            os.fsync(fh.fileno())
        # mtime à coup sûr différent, même sur un système à granularité
        # grossière (ctime, lui, a bougé avec l'écriture)
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns - 1_000_000_000))
        self.assertEqual(os.stat(self.path).st_size, st.st_size)
        self.assertIsNone(self.transport.census_after_post(THREAD))

    def test_a_census_error_after_the_write_does_not_fail_the_post(self):
        """#1764 N2 : le fil est écrit ; un census qui échoue est écarté, le
        post rend son id et l'appelant relira le fil entier."""
        with mock.patch.object(self.transport, "_census_after",
                               side_effect=RuntimeError("census boom")):
            external_id = self.transport.post(THREAD, entry("mine", 0))
        self.assertIsNotNone(external_id)
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["mine"])
        self.assertIsNone(self.transport.census_after_post(THREAD))

    def test_a_post_without_census_does_not_read_the_latest_entry(self):
        """#1764 N3 : une indexation locale ne demande pas de census — `post`
        ne relit pas la plus récente entrée pour rien, et n'en retient aucun."""
        self.post(entry("old", 500))
        with mock.patch.object(self.transport, "_census_after",
                               side_effect=AssertionError("census computed")):
            external_id = self.transport.post(THREAD, entry("mine", 0),
                                              with_census=False)
        self.assertIsNotNone(external_id)
        self.assertEqual([e.text for e in self.transport.read(THREAD)],
                         ["old", "mine"])
        self.assertIsNone(self.transport.census_after_post(THREAD))

    def test_the_census_cache_is_bounded(self):
        """#1764 N5 : `_posted` ne garde pas un census par fil sans borne —
        le plus ancien est oublié au-delà de la borne."""
        with mock.patch.object(fil.FileTransport, "_POSTED_MAX", 2):
            for n in range(5):
                self.post(entry("e%d" % n, 1),
                          thread=fil.ThreadRef("acme", "t%d" % n))
            self.assertEqual(list(self.transport._posted),
                             [fil.ThreadRef("acme", "t3").key,
                              fil.ThreadRef("acme", "t4").key])

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

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_expires_at_most_once_a_day(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        fake = types.SimpleNamespace(cfg=cfg, dry_run=False, db=None)
        with mock.patch.object(runner, "log_async") as logged:
            reports = runner.Runner.expire_threads_once(fake, None, now=NOW, clock=0.0)
            self.assertEqual(reports[0]["dropped"], 1)
            self.assertIn("1 entry before", logged.call_args.args[0])
            self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW + 3600,
                                                                clock=3600.0))
            # r8: the day is counted on the monotonic clock, not the wall one
            self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW + 2 * DAY,
                                                                clock=7200.0))
            self.assertIsNotNone(runner.Runner.expire_threads_once(fake, None, now=NOW + DAY,
                                                                   clock=DAY))
            with mock.patch.object(fil, "expire", side_effect=RuntimeError("boom")):
                self.assertIsNone(runner.Runner.expire_threads_once(fake, None,
                                                                    now=NOW + 2 * DAY,
                                                                    clock=2 * DAY))
            self.assertIn("thread expiry failed", logged.call_args.args[0])


class MediatedHostTest(TransportTest):
    """L361b (#499) : un hôte médié n'a ni transaction ni verrou d'une requête
    à l'autre ; il expire ses fils locaux et met l'index à jour par UNE
    opération serveur (`threads.recount` : verrou de la ligne, recomptage,
    expiration dans la même transaction). Sans connexion, rien n'expire."""

    def remote_db(self):
        from ameesh.storage.remote.client import RemoteDb
        db = object.__new__(RemoteDb)
        db.cfg = self.mediated_cfg()
        db.storage = mock.Mock()
        db.close = mock.Mock()
        return db

    def mediated_cfg(self):
        return dataclasses.replace(config_mod.Config(), threads_dir=self.root,
                                   backend="mediated", exec_url="http://127.0.0.1:1")

    def test_mediated_index_is_an_absolute_census_not_a_count(self):
        """L361c : l'indexation médiée porte le census du fichier (portée H),
        comme l'expiration ; la trace de la boîte reste écrite, sans compter."""
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        e = entry("new", 0)
        external_id = self.transport.post(THREAD, e)
        fil.index(db, cfg, self.transport, THREAD, e, external_id, mailbox_ids=[9])
        threads = db.storage.threads
        threads.lock.assert_not_called()
        threads.expired.assert_not_called()
        # la trace de la boîte : la même écriture, mais qui ne compte pas
        threads.index.assert_called_once()
        kwargs = threads.index.call_args.kwargs
        self.assertIs(kwargs["count"], False)
        self.assertEqual((kwargs["entry_id"], kwargs["mailbox_ids"], kwargs["location"]),
                         (external_id, [9], self.path))
        # le compte : le census du fichier, par l'opération de l'expiration
        threads.recount.assert_called_once()
        kwargs = threads.recount.call_args.kwargs
        entries, end = self.transport.census(THREAD)
        self.assertEqual((kwargs["kept"], kwargs["end"]), (len(entries), end))
        self.assertEqual(kwargs["last"]["entry_id"], external_id)
        self.assertEqual((kwargs["project"], kwargs["lot"], kwargs["transport"],
                          kwargs["host"], kwargs["location"]),
                         ("acme", "361", "file", cfg.host, self.path))
        self.assertEqual([c[0] for c in threads.mock_calls], ["index", "recount"])

    def test_mediated_index_without_a_mailbox_message_is_one_operation(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500))
        db = self.remote_db()
        e = entry("new", 0)
        external_id = self.transport.post(THREAD, e)
        fil.index(db, cfg, self.transport, THREAD, e, external_id)
        threads = db.storage.threads
        threads.index.assert_not_called()
        threads.recount.assert_called_once()
        self.assertEqual(threads.recount.call_args.kwargs["kept"], 2)

    def test_mediated_index_reuses_the_census_of_the_post(self):
        """#1670 : le census vient de la marche faite par `post` (réparation
        d'une écriture interrompue) ; l'indexation médiée ne relit pas le fil,
        et le census porté est celui d'une lecture complète."""
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        e = entry("new", 0)
        external_id = self.transport.post(THREAD, e)
        read = self.transport.census
        with mock.patch.object(self.transport, "census", side_effect=read) as spy:
            fil.index(db, cfg, self.transport, THREAD, e, external_id, mailbox_ids=[9])
        spy.assert_not_called()
        kwargs = db.storage.threads.recount.call_args.kwargs
        entries, end = self.transport.census(THREAD)
        self.assertEqual((kwargs["kept"], kwargs["end"]), (len(entries), end))
        self.assertEqual(kwargs["last"]["entry_id"], external_id)
        self.assertEqual(self.transport.census_after_post(THREAD) is not None, True)

    def test_a_writer_between_post_and_index_forces_a_full_census(self):
        """#1670 : dès que le fichier a bougé après le `post` (ici un second
        écrivain), le census retenu est écarté et le fil est relu."""
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500))
        db = self.remote_db()
        e = entry("mine", 0)
        external_id = self.transport.post(THREAD, e)
        fil.FileTransport(self.root).post(THREAD, entry("other", 0, author="agent:gamma"))
        read = self.transport.census
        with mock.patch.object(self.transport, "census", side_effect=read) as spy:
            fil.index(db, cfg, self.transport, THREAD, e, external_id, mailbox_ids=[9])
        spy.assert_called_once()
        kwargs = db.storage.threads.recount.call_args.kwargs
        entries, end = self.transport.census(THREAD)
        self.assertEqual((kwargs["kept"], kwargs["end"]), (len(entries), end))
        self.assertEqual(kwargs["kept"], 3)
        # même date, écrite après : c'est la dernière du census complet
        self.assertEqual(kwargs["last"]["author"], "agent:gamma")

    def test_an_expiry_between_post_and_index_forces_a_full_census(self):
        """#1670 : l'expiration réécrit le fil (renommage) ; le census du
        `post` est caduc et l'index relit le nouveau fichier."""
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        e = entry("new", 0)
        external_id = self.transport.post(THREAD, e)
        self.assertEqual(self.expire()["dropped"], 1)
        read = self.transport.census
        with mock.patch.object(self.transport, "census", side_effect=read) as spy:
            fil.index(db, cfg, self.transport, THREAD, e, external_id, mailbox_ids=[9])
        spy.assert_called_once()
        kwargs = db.storage.threads.recount.call_args.kwargs
        entries, end = self.transport.census(THREAD)
        self.assertEqual((kwargs["kept"], kwargs["end"]), (len(entries), end))
        self.assertEqual(kwargs["kept"], 2)

    def test_a_torn_tail_is_repaired_and_not_reused_as_a_census(self):
        """#1670 : quand `post` a dû réparer une écriture interrompue, le fil
        est relu — la marche de réparation ne vaut pas census."""
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("kept", 1))
        raw = self.read_bytes()
        with open(self.path, "wb") as fh:  # écriture interrompue en pleine entrée
            fh.write(raw[:-4])
        db = self.remote_db()
        e = entry("new", 0)
        external_id = self.transport.post(THREAD, e)
        self.assertIsNone(self.transport.census_after_post(THREAD))
        read = self.transport.census
        with mock.patch.object(self.transport, "census", side_effect=read) as spy:
            fil.index(db, cfg, self.transport, THREAD, e, external_id, mailbox_ids=[9])
        spy.assert_called_once()
        kwargs = db.storage.threads.recount.call_args.kwargs
        entries, end = self.transport.census(THREAD)
        self.assertEqual((kwargs["kept"], kwargs["end"]), (len(entries), end))
        self.assertEqual([x.text for x in entries][-1], "new")

    def test_expiry_is_one_server_side_recount_of_the_rewritten_file(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        seen = {}

        def recount(**kwargs):
            seen.update(kwargs)
            seen["entries"] = [e.text for e in self.transport.read(THREAD)]
            return kwargs["end"]

        db.storage.threads.recount.side_effect = recount
        reports = fil.expire(cfg, db, now=NOW)
        self.assertEqual([(r["thread"], r["dropped"], r["kept"]) for r in reports],
                         [(THREAD.key, 1, 1)])
        # the file is rewritten first, and the census the op carries is the new one
        self.assertEqual(seen["entries"], ["kept"])
        self.assertEqual(seen["kept"], 1)
        self.assertEqual(seen["last"]["entry_id"], self.transport.read(THREAD)[0].id)
        self.assertIsInstance(seen["end"], int)
        self.assertEqual((seen["project"], seen["lot"], seen["transport"], seen["host"],
                          seen["location"]), ("acme", "361", "file", cfg.host, self.path))
        # one operation, never the refused lock/expired/index
        self.assertEqual([c[0] for c in db.storage.threads.mock_calls], ["recount"])

    def test_a_lost_recount_is_redone_by_the_next_expiry(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        db.storage.threads.recount.side_effect = RuntimeError("server lost")
        reports = fil.expire(cfg, db, now=NOW)
        self.assertIn("error", reports[0])
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["kept"])
        # nothing left to drop, but the file's expiry note is behind the index
        db.storage.threads.recount.side_effect = None
        db.storage.threads.recount.reset_mock()
        reports = fil.expire(cfg, db, now=NOW)
        self.assertEqual(reports[0]["dropped"], 0)
        self.assertEqual(db.storage.threads.recount.call_args.kwargs["kept"], 1)

    def test_a_thread_that_never_expired_is_left_to_the_index_of_before_l361(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("recent", 1))
        db = self.remote_db()
        reports = fil.expire(cfg, db, now=NOW)
        self.assertEqual(reports[0]["dropped"], 0)
        db.storage.threads.recount.assert_not_called()

    def test_without_a_connection_expiry_is_refused_and_touches_nothing(self):
        cfg = self.mediated_cfg()
        self.post(entry("old", 500), entry("kept", 1))
        before = self.read_bytes()
        with self.assertRaises(fil.ExpiryUnavailable):
            fil.expire(cfg, None, now=NOW)
        self.assertEqual(self.read_bytes(), before)
        # a dry run reads the file only: still answered
        self.assertEqual(fil.expire(cfg, None, now=NOW, dry_run=True)[0]["dropped"], 1)
        self.assertEqual(self.read_bytes(), before)

    def test_runner_expires_through_the_connection_it_holds(self):
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root)
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        fake = types.SimpleNamespace(cfg=cfg, dry_run=False, db=db)
        with mock.patch.object(runner, "log_async") as logged:
            reports = runner.Runner.expire_threads_once(fake, None, now=NOW, clock=0.0)
        self.assertEqual(reports[0]["dropped"], 1)
        self.assertEqual([c[0] for c in db.storage.threads.mock_calls], ["recount"])
        logged.assert_called_once()
        self.assertIn("1 entr", logged.call_args.args[0])
        # #1482-N3 : la connexion tenue par l'exécuteur n'est pas fermée par l'expiration
        db.close.assert_not_called()
        # r8/l361b: le throttle quotidien se compte sur l'horloge monotone ; sans
        # `clock` explicite le temoin dependait de la duree de vie de la machine
        # (monotonic > 86400 s -> la deuxieme passe n'etait plus retenue).
        self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW + 3600,
                                                            clock=3600.0))

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_closes_the_connection_it_opened_itself(self):
        # #1482-N3 : la connexion ouverte pour l'expiration quotidienne est
        # fermée ; celle que l'exécuteur tient déjà ne l'est pas.
        cfg = self.mediated_cfg()
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        from ameesh import db as db_mod
        fake = types.SimpleNamespace(cfg=cfg, dry_run=False, db=None)
        with mock.patch.object(db_mod, "connect", return_value=db) as connect, \
                mock.patch.object(runner, "log_async"):
            reports = runner.Runner.expire_threads_once(fake, None, now=NOW)
        connect.assert_called_once()
        db.close.assert_called_once()
        self.assertEqual([r["dropped"] for r in reports], [1])

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_says_the_missing_index_once_per_pass(self):
        cfg = self.mediated_cfg()
        self.post(entry("old", 500), entry("kept", 1), thread=fil.ThreadRef("other"))
        self.post(entry("old", 500), entry("kept", 1))
        before = self.read_bytes()
        from ameesh import db as db_mod
        fake = types.SimpleNamespace(cfg=cfg, dry_run=False, db=None)
        with mock.patch.object(db_mod, "connect",
                               side_effect=db_mod.DbError("no token")) as connect, \
                mock.patch.object(runner, "log_async") as logged:
            self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW))
            self.assertEqual(logged.call_count, 1)
            self.assertIn("thread expiry skipped", logged.call_args.args[0])
            self.assertIn("no token", logged.call_args.args[0])
            # the day is counted anyway: no new attempt before tomorrow
            self.assertIsNone(runner.Runner.expire_threads_once(fake, None, now=NOW + 3600))
        connect.assert_called_once()
        self.assertEqual(self.read_bytes(), before)

    def test_host_mediated_without_a_connection_is_refused_too(self):
        # r8: `host_mediated` keeps its threads local whatever the backend;
        # without the index, `fil expire` refuses, and the connection is tried
        cfg = dataclasses.replace(config_mod.Config(), threads_dir=self.root,
                                  host_mediated=True)
        self.assertNotEqual(cfg.backend, "mediated")
        self.post(entry("old", 500), entry("kept", 1))
        before = self.read_bytes()
        with self.assertRaises(fil.ExpiryUnavailable):
            fil.expire(cfg, None, now=NOW)
        from ameesh import db as db_mod
        with mock.patch.object(fil, "transport_for", return_value=self.transport), \
                mock.patch.object(db_mod, "connect",
                                  side_effect=db_mod.DbError("no credentials")) as connect, \
                self.assertRaises(fil.ExpiryUnavailable):
            fil.cmd_expire(cfg, types.SimpleNamespace(days=None, dry_run=False))
        connect.assert_called_once()
        self.assertEqual(self.read_bytes(), before)

    def test_notice_thread_survives_an_error(self):
        # r8: an exception of a notice is logged and the loop goes on
        cfg = self.mediated_cfg()
        calls = []
        stop = threading.Event()
        twice = threading.Event()

        def once(db):
            calls.append(db)
            if len(calls) == 2:
                twice.set()
            raise RuntimeError("boom")
        fake = types.SimpleNamespace(cfg=cfg, stop=stop, expire_threads_once=once)
        # the loop's wait made short: the second notice comes at once
        stop.wait = lambda timeout: stop.is_set()
        with mock.patch.object(runner, "log_async") as logged:
            runner.Runner._start_thread_expiry_notice(fake)
            self.assertTrue(twice.wait(5))
            stop.set()
        self.assertIn("thread expiry notice failed (RuntimeError: boom)",
                      logged.call_args_list[0].args[0])

    def test_mediated_cli_expires_through_the_server_op(self):
        cfg = self.mediated_cfg()
        self.post(entry("old", 500), entry("kept", 1))
        db = self.remote_db()
        from ameesh import db as db_mod
        out = io.StringIO()
        with mock.patch.object(fil, "transport_for", return_value=self.transport), \
                mock.patch.object(db_mod, "connect", return_value=db), \
                contextlib.redirect_stdout(out):
            code = fil.cmd_expire(cfg, types.SimpleNamespace(days=None, dry_run=False))
        self.assertEqual(code, 0)
        self.assertIn("1 entry expired before", out.getvalue())
        db.storage.threads.recount.assert_called_once()
        db.close.assert_called_once()
        self.assertEqual([e.text for e in self.transport.read(THREAD)], ["kept"])

    def test_mediated_dry_run_reports_without_the_index(self):
        cfg = self.mediated_cfg()
        self.post(entry("old", 500), entry("kept", 1))
        before = self.read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fil, "transport_for", return_value=self.transport), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fil.cmd_expire(cfg, types.SimpleNamespace(days=None, dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn("1 entry would expire", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.read_bytes(), before)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_says_it_without_housekeeping(self):
        # r7: a mediated runner has no housekeeping pass; the notice has its
        # own daily schedule, whatever the housekeeping interval
        for interval in (0, 600):
            cfg = dataclasses.replace(self.mediated_cfg(), housekeeping_interval=interval)
            fake = types.SimpleNamespace(cfg=cfg, dry_run=False, once=False, mediated=True,
                                         stop=threading.Event(), db=None)
            fake.expire_threads_once = lambda db, now=None, f=fake: \
                runner.Runner.expire_threads_once(f, db, now=now)
            fake._start_thread_expiry_notice = lambda f=fake: \
                runner.Runner._start_thread_expiry_notice(f)
            said = threading.Event()
            with mock.patch.object(runner, "log_async",
                                   side_effect=lambda msg: said.set()) as logged:
                runner.Runner.start_housekeeping_poll(fake)
                self.assertTrue(said.wait(5), interval)
                fake.stop.set()
            self.assertEqual(logged.call_count, 1)
            self.assertIn("thread expiry skipped", logged.call_args.args[0])
            self.assertIn("index unavailable", logged.call_args.args[0])
        # in --once, nothing scheduled
        fake = types.SimpleNamespace(cfg=cfg, once=True, mediated=True,
                                     _start_thread_expiry_notice=mock.Mock(), db=None)
        runner.Runner.start_housekeeping_poll(fake)
        fake._start_thread_expiry_notice.assert_not_called()


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

    def test_a_local_index_asks_no_census_of_the_post(self):
        """#1764 N3 : sur une base locale, `record` ne demande pas de census au
        transport — l'indexation locale n'en lit aucun (le census ne sert qu'à
        l'exécuteur médié)."""
        calls = []
        real_post = fil.FileTransport.post

        def spy(self, thread, entry, **kw):
            calls.append(kw)
            return real_post(self, thread, entry, **kw)

        with mock.patch.object(fil.FileTransport, "post", spy):
            fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"],
                       text="local", project="acme", lot="n3")
        self.assertEqual([kw.get("with_census") for kw in calls], [False])

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

    def test_expiry_without_a_committed_recount_counts_later_indexing(self):
        """r3 P2: the file is no proof the index was recounted."""
        transport = fil.transport_for(self.cfg)

        def post(lot):
            thread = fil.ThreadRef("acme", lot)
            transport.post(thread, entry("old", 500))
            fresh = [entry("fresh %d" % n, 1) for n in (1, 2)]
            return thread, [(e, transport.post(thread, e)) for e in fresh]

        # expiry without a database (the CLI fallback), then delayed indexing
        thread, fresh = post("nodb")
        self.assertEqual(fil.expire(self.cfg, None, now=NOW)[0]["dropped"], 1)
        for e, entry_id in fresh:
            fil.index(self.db, self.cfg, transport, thread, e, entry_id)
        self.assertEqual(self.index_row("nodb")["entries"], 2)
        # a recount that fails after the rename is rolled back: same thing
        thread, fresh = post("failed")
        from ameesh.storage.postgres import threads as pg_threads
        with mock.patch.object(pg_threads.Threads, "expired",
                               side_effect=RuntimeError("recount lost")):
            report = fil.expire(self.cfg, self.db, now=NOW)
        self.assertIn("error", [r for r in report if r["thread"] == "acme/failed"][0])
        self.assertEqual(len(transport.read(thread)), 2)  # the file did expire
        for e, entry_id in fresh:
            fil.index(self.db, self.cfg, transport, thread, e, entry_id)
        self.assertEqual(self.index_row("failed")["entries"], 2)

    def test_recount_lost_after_the_rename_is_redone_from_the_file(self):
        """r4 P2: the old entry was indexed; the rename happened, the recount
        did not commit. The next indexing, or the next expiry, recounts."""
        transport = fil.transport_for(self.cfg)
        from ameesh.storage.postgres import threads as pg_threads

        def lose_recount(lot):
            fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="old",
                       ts=NOW - 500 * DAY, project="acme", lot=lot)
            thread = fil.ThreadRef("acme", lot)
            fresh = [entry("fresh %d" % n, 1) for n in (1, 2)]
            fresh = [(e, transport.post(thread, e)) for e in fresh]  # not indexed yet
            with mock.patch.object(pg_threads.Threads, "expired",
                                   side_effect=RuntimeError("crash before commit")):
                fil.expire(self.cfg, self.db, now=NOW)
            self.assertEqual(len(transport.read(thread)), 2)  # renamed
            self.assertEqual(self.index_row(lot)["entries"], 1)  # rolled back
            return thread, fresh

        # delayed indexing recounts from the file first
        thread, fresh = lose_recount("index")
        for e, entry_id in fresh:
            fil.index(self.db, self.cfg, transport, thread, e, entry_id)
        row = self.index_row("index")
        self.assertEqual((row["entries"], row["last_excerpt"], row["last_entry_id"]),
                         (2, "fresh 2", fresh[1][1]))
        # a retried expiry drops nothing but recounts; indexing then adds nothing
        thread, fresh = lose_recount("retry")
        report = {r["thread"]: r for r in fil.expire(self.cfg, self.db, now=NOW)}
        self.assertEqual(report["acme/retry"]["dropped"], 0)
        row = self.index_row("retry")
        self.assertEqual((row["entries"], row["last_excerpt"]), (2, "fresh 2"))
        for e, entry_id in fresh:
            fil.index(self.db, self.cfg, transport, thread, e, entry_id)
        self.assertEqual(self.index_row("retry")["entries"], 2)
        # one written after the repair counts normally
        fil.record(self.cfg, self.db, sender="alpha", recipients=["beta"], text="next",
                   ts=NOW, project="acme", lot="retry")
        self.assertEqual(self.index_row("retry")["entries"], 3)

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


class RecountTest(PgTestCase):
    """L361b : l'opération serveur d'un hôte médié — verrou de la ligne,
    recomptage absolu depuis le census du fichier et expiration en une seule
    transaction ; un recomptage plus récent n'est jamais écrasé."""

    PROJ, LOT, HOST = "acme", "361b", "host-1"
    #: la plus récente d'un census d'indexation (L361c)
    LAST = {"entry_id": "acme/361b#30", "author": "agent:a", "excerpt": "trois",
            "ts": 2.0, "mailbox_id": None}

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM thread_index")
        from ameesh import storage
        self.threads = storage.of(self.db).threads
        self.key = dict(project=self.PROJ, lot=self.LOT, transport="file", host=self.HOST)

    def row(self):
        rows = self.db.query(
            "SELECT location, entries, last_entry_id, last_excerpt, counted_until"
            "  FROM thread_index"
            " WHERE project = %s AND lot = %s AND transport = 'file' AND host = %s",
            (self.PROJ, self.LOT, self.HOST))
        return rows[0] if rows else None

    def test_recount_locks_counts_and_expires_in_one_operation(self):
        self.threads.index(**self.key, location="/threads/acme/361b.md",
                           entry_id="acme/361b#10", mailbox_ids=[], author="agent:a",
                           excerpt="x", ts=1.0, trace={})
        self.assertIsNone(self.row()["counted_until"])
        value = self.threads.recount(**self.key, location="/threads/acme/361b.md",
                                     kept=0, last=None, end=42)
        self.assertEqual(value, 42)
        row = self.row()
        self.assertEqual((row["entries"], row["last_entry_id"], row["counted_until"]),
                         (0, None, 42))

    def test_a_newer_recount_is_not_overwritten(self):
        self.threads.recount(**self.key, location="x", kept=3,
                             last={"entry_id": "acme/361b#30", "author": "agent:a",
                                   "excerpt": "trois", "ts": 2.0, "mailbox_id": None},
                             end=90)
        value = self.threads.recount(**self.key, location="x", kept=1, last=None, end=42)
        self.assertEqual(value, 90)
        row = self.row()
        self.assertEqual((row["entries"], row["last_entry_id"], row["counted_until"]),
                         (3, "acme/361b#30", 90))

    def test_at_equal_end_the_expiry_is_applied(self):
        # à fin logique constante, seule une expiration change le fil
        self.threads.recount(**self.key, location="x", kept=5, last=None, end=42)
        value = self.threads.recount(**self.key, location="x", kept=2, last=None, end=42)
        self.assertEqual(value, 42)
        self.assertEqual(self.row()["entries"], 2)

    def test_recount_creates_the_row_of_an_unknown_thread(self):
        value = self.threads.recount(**self.key, location="x", kept=0, last=None, end=7)
        self.assertEqual(value, 7)
        self.assertEqual(self.row()["counted_until"], 7)

    def test_a_stale_census_never_erases_a_newer_index(self):
        """L361c : l'indexation médiée est un census absolu (portée H). La
        trace ne compte pas ; un recomptage d'expiration plus court, arrivé
        après le census de l'indexation, ne recule pas et ne double rien."""
        self.threads.recount(**self.key, location="x", kept=2, last=None, end=100)
        # la trace de l'indexation : écrite, mais `+1` local désactivé
        self.threads.index(**self.key, location="x", entry_id="acme/361b#30",
                           mailbox_ids=[], author="agent:a", excerpt="x", ts=2.0,
                           trace={}, count=False)
        self.assertEqual(self.row()["entries"], 2)
        # le census de l'indexation, plus récent que le recomptage d'expiration
        self.threads.recount(**self.key, location="x", kept=3,
                             last={"entry_id": "acme/361b#30", "author": "agent:a",
                                   "excerpt": "trois", "ts": 2.0, "mailbox_id": None},
                             end=120)
        # un recomptage périmé (census d'avant l'indexation) ne l'écrase pas
        self.assertEqual(self.threads.recount(**self.key, location="x", kept=2,
                                              last=None, end=100), 120)
        row = self.row()
        self.assertEqual((row["entries"], row["counted_until"]), (3, 120))

    def test_an_index_census_at_equal_end_is_stale_after_the_expiry(self):
        """L361c, témoin inverse : l'expiration préserve la fin logique
        (`end`), donc un census d'indexation lu avant elle a le même `end`.
        Arrivé après le recount de l'expiration, il est périmé : il ne
        ressuscite ni les entrées ni `last_*`."""
        self.threads.recount(**self.key, location="x", kept=3, last=dict(self.LAST), end=120)
        # l'expiration, de même fin logique : le fil ne garde qu'une entrée
        self.assertEqual(self.threads.recount(**self.key, location="x", kept=1,
                                              last=None, end=120), 120)
        row = self.row()
        self.assertEqual((row["entries"], row["last_entry_id"], row["last_excerpt"]),
                         (1, None, ""))
        # le census d'indexation, lu avant l'expiration, arrive après elle
        self.assertEqual(self.threads.recount(**self.key, location="x", kept=3,
                                              last=dict(self.LAST), end=120), 120)
        row = self.row()
        self.assertEqual((row["entries"], row["last_entry_id"], row["last_excerpt"],
                          row["counted_until"]), (1, None, "", 120))

    def test_a_trace_write_counts_nothing_and_quotes_nothing(self):
        """L361c : la trace de la boîte (`count` faux) écrit `meta` et
        `location`, jamais `entries` ni `last_*` : une entrée retirée par une
        expiration passée avant l'écriture de trace ne reste pas citée."""
        msg = self.db.query("INSERT INTO agent_mailbox (sender, recipient, body)"
                            " VALUES ('agent:a', 'agent:b', 'x') RETURNING id")[0]["id"]
        self.threads.recount(**self.key, location="x", kept=1,
                             last={"entry_id": "acme/361b#20", "author": "agent:a",
                                   "excerpt": "deux", "ts": 1.0, "mailbox_id": None},
                             end=120)
        before = self.row()
        self.threads.index(**self.key, location="y", entry_id="acme/361b#30",
                           mailbox_ids=[msg], author="agent:a", excerpt="trois",
                           ts=3.0, trace={"fil": {"id": "acme/361b#30"}}, count=False)
        row = self.row()
        self.assertEqual((row["entries"], row["last_entry_id"], row["last_excerpt"],
                          row["counted_until"]),
                         (before["entries"], before["last_entry_id"],
                          before["last_excerpt"], before["counted_until"]))
        self.assertEqual(row["location"], "y")  # la trace rafraîchit `location`
        meta = self.db.query("SELECT meta FROM agent_mailbox WHERE id = %s", (msg,))[0]["meta"]
        meta = meta if isinstance(meta, dict) else json.loads(meta)
        self.assertEqual(meta.get("fil"), {"id": "acme/361b#30"})


if __name__ == "__main__":
    unittest.main()
