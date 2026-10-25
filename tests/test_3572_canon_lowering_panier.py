# SPDX-License-Identifier: AGPL-3.0-only
"""#3572 — panier de #3554 : journal isolé, drapeaux, échéance globale.

N1 : dans la transaction du gel, la note `canon-review-lowering:` est écrite
ou son échec est consigné, sans laisser la transaction avortée (psycopg et
psql). N2 : un drapeau `exclude_author_family` / `distinct_families` qui passe
de true à false est une baisse (deux avis, fichier nommé au journal). N3 : la
sonde tient dans une échéance globale et conclut au doute quand elle s'épuise.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import (canon_lowering, db as db_mod, mechanics, registry,
                    review_designation_activation as act, storage, work)
from ameesh.deadline import Deadline

from .support import PgTestCase
from .test_3554_canon_lowering import MECHANICS, _cfg, _federation, _Repo


class FlagsAndDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3572-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.canon = _Repo(os.path.join(self.tmp, "canon"))
        self.canon.write("processus/mecanique.yaml", MECHANICS)
        self.canon.write("federation.yaml", _federation())
        self.base = self.canon.commit("base")
        self.project = _Repo(os.path.join(self.tmp, "ameesh"))
        for path in ("deploy/keep-secret.py", "tools/run.py", "README.md"):
            self.project.write(path, "x\n")
        self.project.commit("project")
        self.cfg = _cfg(self.canon.root, {
            "ameesh": {"repo": self.project.root, "target": "main"},
            "nexlink": {"repo": self.project.root, "target": "main"},
        })

    def _mechanics(self, text, message="head"):
        self.canon.write("processus/mecanique.yaml", text)
        return self.canon.commit(message)

    def _detect(self, head, paths=("processus/mecanique.yaml",), **kwargs):
        return canon_lowering.for_lot(self.cfg, self.canon.root, self.base,
                                      head, list(paths), **kwargs)

    # N2 -------------------------------------------------------------------
    def test_a_flag_going_false_is_a_lowering_named_in_the_journal(self):
        for flag in canon_lowering.FLAGS:
            with self.subTest(flag=flag):
                head = self._mechanics(MECHANICS.replace(
                    "%s: true" % flag, "%s: false" % flag), flag)
                found = self._detect(head)
                self.assertEqual(found.floor, 2)
                self.assertEqual(found.lowered_flags, (flag,))
                self.assertEqual(found.lowered, ())
                self.assertEqual(found.errors, ())
                text = found.journal_text()
                self.assertIn(mechanics.PATH, text)
                self.assertIn("%s true→false" % flag, text)
                self.assertEqual(found.to_dict()["lowered_flags"], [flag])
                self.canon.git("reset", "-q", "--hard", self.base)

    def test_a_flag_going_true_is_a_hardening(self):
        self.base = self._mechanics(MECHANICS.replace(
            "distinct_families: true", "distinct_families: false"), "base")
        head = self._mechanics(MECHANICS)
        found = self._detect(head)
        self.assertEqual(found.floor, 0)
        self.assertEqual(found.lowered_flags, ())

    def test_an_old_journal_dict_without_flags_still_renders(self):
        data = canon_lowering.Detection(
            base="a" * 40, head="b" * 40, errors=("doute",)).to_dict()
        data.pop("lowered_flags")
        self.assertIn("doute", canon_lowering.journal_text(data))

    # N3 -------------------------------------------------------------------
    def test_an_exhausted_deadline_is_a_doubt(self):
        head = self._mechanics(MECHANICS + "# neutre\n")
        found = self._detect(head, deadline=Deadline(0))
        self.assertIsInstance(found, canon_lowering.Detection)
        self.assertEqual(found.floor, 2)
        self.assertIn("échéance", " ".join(found.errors))

    def test_a_slow_project_repository_ends_in_a_bounded_doubt(self):
        head = self._mechanics(MECHANICS + "# neutre\n")
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])
        real = canon_lowering._list_tracked

        def slow(repo, commit, limit):
            # le `ls-tree` d'un gros dépôt consomme tout le budget restant
            now[0] += 11.0
            return real(repo, commit, limit)

        canon_lowering._SCANNED.clear()
        with mock.patch.object(canon_lowering, "_list_tracked", side_effect=slow):
            found = self._detect(head, deadline=deadline)
        self.assertEqual(found.floor, 2)
        self.assertIn("échéance", " ".join(found.errors))
        self.assertEqual(found.scans, ())
        # l'échec n'est pas mémoïsé : une passe avec du temps conclut
        again = self._detect(head)
        self.assertEqual(again.floor, 0, again.errors)
        self.assertTrue(again.scans)

    def test_a_policy_read_cut_by_the_deadline_is_not_unmeasured(self):
        # Une lecture de politique coupée à la base se lirait « politique
        # incomplète à la base » (non mesuré, aucun plancher) : c'est un doute.
        head = self._mechanics(MECHANICS + "# neutre\n")
        now = [0.0]
        deadline = Deadline(10.0, clock=lambda: now[0])

        def cut(*_args, **_kwargs):
            now[0] += 11.0
            raise mechanics.MechanicsError("deadline exhausted while reading")

        canon_lowering._LOADED.clear()
        with mock.patch.object(canon_lowering.mechanics, "load", side_effect=cut):
            found = self._detect(head, deadline=deadline)
        self.assertEqual(found.floor, 2)
        self.assertEqual(found.unmeasured, ())
        self.assertIn("échéance", " ".join(found.errors))

    def test_a_git_deadline_exception_never_escapes(self):
        head = self._mechanics(MECHANICS + "# neutre\n")
        with mock.patch.object(
                canon_lowering, "_flags",
                side_effect=canon_lowering.canon_mod.GitDeadline("épuisée")):
            found = self._detect(head, deadline=Deadline(60.0))
        self.assertEqual(found.floor, 2)


class JournalInTransactionTest(PgTestCase):
    """N1 : la note du gel ne laisse jamais la transaction avortée."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "author", harness="deepseek", host="test",
                        status="idle")
        self.item = work.add(self.db, title="canon", app="ameesh",
                             branch="canon/3572", branch_target="main",
                             assignee="author")
        self.lot = int(self.item["id"])
        self.head = "c" * 40
        found = canon_lowering.Detection(
            base="a" * 40, head=self.head,
            lowered_flags=("exclude_author_family",))
        policy = act.Policy(rules=(), families=(), risk="light", required=2,
                            ref="", commit="", source="",
                            canon_lowering=found.to_dict())
        self.inputs = act.Inputs(
            item=self.item, freeze={"id": 7, "sha": self.head}, repo="",
            base="a" * 40, paths=["processus/mecanique.yaml"], commits=[],
            policy=policy, authors=[], unknown_authors=[], correctors=[],
            candidates=[], notify="", manual=[], waiver=False)

    def _noted(self):
        return storage.of(self.db).work.events_noted(
            canon_lowering.PREFIX, 0.0, ids=[self.lot])

    def _drivers(self):
        # les deux pilotes, nommés : `self.db` est celui du banc (psql sans
        # psycopg installé), pas forcément psycopg
        for name in ("psycopg", "psql"):
            try:
                db = self.connect(driver=name)
            except (db_mod.Unavailable, ImportError) as exc:
                if name == "psql":
                    raise
                print("skip %s: %s" % (name, exc), file=sys.__stderr__)
                continue
            self.addCleanup(getattr(db, "close", lambda: None))
            self.assertEqual(db.name, name)
            yield name, db

    def test_the_note_is_written_once_inside_the_freeze_transaction(self):
        for name, db in self._drivers():
            with self.subTest(driver=name):
                self.db.execute("DELETE FROM work_item_events WHERE note LIKE %s",
                                (canon_lowering.PREFIX + "%",))
                with db.transaction() as tx:
                    for _ in range(2):
                        act._note_canon_lowering(tx, self.item, {"id": 7},
                                                 self.inputs)
                rows = self._noted()
                self.assertEqual(len(rows), 1, rows)
                self.assertIn("exclude_author_family true→false", rows[0]["note"])
                self.assertEqual(rows[0]["actor"], act.SENDER)

    def test_a_failed_note_keeps_the_freeze_transaction_alive(self):
        self.db.execute(
            "CREATE FUNCTION refuse_lowering_note() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF NEW.note LIKE 'canon-review-lowering:"
            "%%' THEN RAISE EXCEPTION 'injected journal failure'; END IF; "
            "RETURN NEW; END $$")
        self.db.execute(
            "CREATE TRIGGER refuse_lowering_note BEFORE INSERT ON work_item_events "
            "FOR EACH ROW EXECUTE FUNCTION refuse_lowering_note()")
        self.addCleanup(self.db.execute, "DROP FUNCTION refuse_lowering_note()")
        self.addCleanup(self.db.execute,
                        "DROP TRIGGER refuse_lowering_note ON work_item_events")
        for name, db in self._drivers():
            with self.subTest(driver=name):
                marker = "freeze write after the failed note (%s)" % name
                with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                    with db.transaction() as tx:
                        act._note_canon_lowering(tx, self.item, {"id": 7},
                                                 self.inputs)
                        # la transaction du gel écrit encore, puis valide
                        storage.of(tx).work.note(self.lot, "intake", marker,
                                                 "author")
                self.assertIn("injected journal failure", err.getvalue())
                self.assertEqual(self._noted(), [])
                notes = [row["note"] for row in work.events(self.db, self.lot)]
                self.assertIn(marker, notes)


if __name__ == "__main__":
    unittest.main()
