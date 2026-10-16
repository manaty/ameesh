# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — le port Postgres de la désignation au gel.

Base réelle : la marque idempotente du journal, le verrou du lot, les
courriers déposés dans la même transaction, le gel périmé refusé sans rien
écrire, le fil externe vidé après le commit seulement, et l'attente
réessayable qui devient une désignation quand la capacité revient. Le moteur
de sélection est celui de `review_designation` (déjà couvert sans base par
`tests/test_1617_review_designation.py`) : ici c'est le raccord qui est
éprouvé.
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd
from ameesh import review_designation_pg as pg
from ameesh import registry, storage, work

from .support import PgTestCase

SHA_A = "a" * 40
SHA_B = "b" * 40
RULES = (rd.Rule(paths=("**/*.sql", "src/ameesh/db.py"), reviews=2),)
ITEM = {"app": "ameesh", "title": "Désigner les relecteurs au gel"}


class Test1617ReviewDesignationPgMark(unittest.TestCase):
    """Le format de la marque : préfixe exact, JSON refusé s'il est illisible."""

    def test_prefix_separates_lots_and_freezes(self):
        self.assertTrue(pg.mark_prefix(3, 12).startswith(pg.MARK_PREFIX))
        self.assertNotEqual(pg.mark_prefix(3, 1), pg.mark_prefix(3, 12))
        self.assertNotEqual(pg.mark_prefix(3, 12), pg.mark_prefix(13, 12))

    def test_a_foreign_note_is_not_a_mark(self):
        self.assertIsNone(pg.designation_of("gel 1617 : tête gelée"))
        self.assertIsNone(pg.designation_of(""))

    def test_an_unreadable_mark_is_refused_not_interpreted(self):
        for note in (pg.MARK_PREFIX + "3:12: pas du json",
                     pg.MARK_PREFIX + "3:12:",
                     pg.MARK_PREFIX + '3:12: {"state": "designated"}'):
            with self.assertRaises(rd.DesignationError):
                pg.designation_of(note)

    def test_a_read_mark_renders_itself(self):
        note = pg._mark_note(3, 12, state=rd.DESIGNATED, required=2,
                             reviewers=[{"name": "r1", "family": "claude"}],
                             missing=0, why="ok",
                             authors=[{"name": "a1", "family": "deepseek"}],
                             policy_ref="canon@deadbeef", sha=SHA_A)
        found = pg.designation_of(note)
        self.assertEqual(found["state"], rd.DESIGNATED)
        self.assertEqual(found["freeze"], {"id": 12, "sha": SHA_A})
        self.assertEqual(found["reviewers"], [{"name": "r1", "family": "claude"}])


class Test1617ReviewDesignationPg(PgTestCase):
    """Le port sur la base : marque, courriers, verrou, transaction."""

    def setUp(self):
        super().setUp()
        self.item = work.add(self.db, title=ITEM["title"], app=ITEM["app"])
        self.lot = int(self.item["id"])
        for name in ("r1", "r2", "pilot"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            status="idle")

    # -- helpers ----------------------------------------------------------
    def freeze(self, sha=SHA_A):
        return storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=sha, actor="mty-amh-impl-02", verdict=None, note="")

    def mailbox(self, db=None):
        return (db or self.db).query(
            "SELECT id, recipient, body, payload FROM agent_mailbox ORDER BY id")

    def marks(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(pg.MARK_PREFIX)]

    def run_apply(self, port, *, candidates, authors=None, unknown=(),
                  policy=RULES, paths=("src/ameesh/db.py",), notify="pilot",
                  risk="sensitive", freeze_sha=None):
        if freeze_sha is None:
            # l'appelant collecte ses entrées sur le dernier gel qu'il voit
            freeze_sha = (pg.PgPort(self.db).latest_freeze(self.lot) or {}).get("sha", "")
        return rd.apply(dict(self.item, state="build"), policy=policy, paths=list(paths),
                        port=port, freeze_sha=freeze_sha,
                        authors=authors or [{"name": "author", "family": "deepseek"}],
                        candidates=candidates, risk=risk, base="c" * 40,
                        policy_ref="canon@deadbeef", notify=notify,
                        unknown_authors=unknown)

    def two_families(self):
        return [rd.Candidate("r1", "claude"), rd.Candidate("r2", "codex")]

    # -- tests ------------------------------------------------------------
    def test_designated_records_and_mails_once(self):
        freeze = self.freeze()
        out = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["required"], 2)
        self.assertEqual(out["freeze"], {"id": int(freeze["id"]), "sha": SHA_A})
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1", "r2"])
        sent = self.mailbox()
        self.assertEqual([m["recipient"] for m in sent], ["r1", "r2"])
        for row in sent:
            self.assertIn(SHA_A, row["body"])
            self.assertIn("frozen milestone #%d" % int(freeze["id"]), row["body"])
        marks = self.marks()
        self.assertEqual(len(marks), 1)
        self.assertEqual(pg.designation_of(marks[0])["freeze"],
                         {"id": int(freeze["id"]), "sha": SHA_A})
        # rejoué : la désignation enregistrée, aucun second courrier
        again = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertTrue(again.get("replayed"))
        self.assertEqual(len(self.mailbox()), 2)
        self.assertEqual(len(self.marks()), 1)

    def test_a_moved_freeze_writes_nothing(self):
        first = self.freeze(SHA_A)
        self.freeze(SHA_B)  # un nouveau gel arrive avant l'écriture
        with self.assertRaises(rd.StaleFreeze):
            pg.PgPort(self.db).commit(
                self.lot, freeze={"id": int(first["id"]), "sha": SHA_A}, required=1,
                state=rd.DESIGNATED, reviewers=[{"name": "r1", "family": "claude"}],
                missing=0, why="", messages=[{"to": "r1", "body": "à relire"}])
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])

    def test_a_write_on_an_open_transaction_without_after_commit_is_refused(self):
        # #1775 N2 : un `with` imbriqué ne commite pas (psql comme psycopg) ;
        # écrire puis vider le fil ici publierait des traces avant le commit
        # de l'appelant. La lecture reste permise, l'écriture est refusée.
        self.freeze()
        with self.db.transaction() as tx:
            last = pg.PgPort(tx).latest_freeze(self.lot)
            with self.assertRaises(rd.DesignationError):
                pg.PgPort(tx).commit(
                    self.lot, freeze=last, required=1, state=rd.DESIGNATED,
                    reviewers=[{"name": "r1", "family": "claude"}], missing=0, why="",
                    messages=[{"to": "r1", "body": "à relire"}])
            self.assertEqual(self.mailbox(tx), [])
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])

    def test_the_journal_read_is_bounded_by_the_freeze(self):
        # #1775 N3 : un marque ne peut pas précéder son gel. Un marque antidaté
        # (impossible en pratique, injecté ici) n'est pas relu : la lecture du
        # journal part du `at_ts` du jalon, pas de l'époque.
        freeze = self.freeze()
        fid = int(freeze["id"])
        stale = pg._mark_note(self.lot, fid, state=rd.DESIGNATED, required=1,
                              reviewers=[{"name": "r1", "family": "claude"}],
                              missing=0, why="", authors=[], policy_ref="", sha=SHA_A)
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, %s, %s, %s, now() - interval '1 hour')",
            (self.lot, "build", stale, "someone"))
        self.assertIsNone(pg.PgPort(self.db).recorded(self.lot, fid))
        # la désignation réelle reste écrite et relue, sans second courrier
        out = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertEqual(out["state"], rd.DESIGNATED)
        found = pg.PgPort(self.db).recorded(self.lot, fid)
        self.assertEqual(found["freeze"], {"id": fid, "sha": SHA_A})
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(len(self.marks()), 2)  # le marque antidaté + le vrai

    def test_a_late_writer_returns_the_recorded_designation_without_mailing(self):
        self.freeze()
        freeze = pg.PgPort(self.db).latest_freeze(self.lot)
        first = pg.PgPort(self.db).commit(
            self.lot, freeze=freeze, required=1, state=rd.DESIGNATED,
            reviewers=[{"name": "r1", "family": "claude"}], missing=0, why="",
            messages=[{"to": "r1", "body": "à relire"}])
        self.assertEqual(first["state"], rd.DESIGNATED)
        self.assertEqual(len(self.mailbox()), 1)
        other = self.connect()
        try:
            # un second écrivain, qui a lu « rien » avant d'attendre le verrou :
            # sous verrou il retrouve la marque du premier et ne remaille pas
            late = pg.PgPort(other).commit(
                self.lot, freeze=freeze, required=1, state=rd.DESIGNATED,
                reviewers=[{"name": "r1", "family": "claude"}], missing=0, why="",
                messages=[{"to": "r1", "body": "à relire"}])
        finally:
            other.close()
        self.assertEqual(late["state"], rd.DESIGNATED)
        self.assertEqual(late["freeze"], freeze)
        self.assertEqual(len(self.mailbox()), 1)  # la marque est celle de la base
        self.assertEqual(len(self.marks()), 1)

    def test_a_moved_freeze_refuses_without_writing(self):
        first = self.freeze(SHA_A)
        port = pg.PgPort(self.db)
        # le gel bouge entre la lecture du moteur et son écriture
        original = port.recorded

        def moved(lot, freeze_id):
            if int(freeze_id) == int(first["id"]):
                self.freeze(SHA_B)
            return original(lot, freeze_id)

        port.recorded = moved
        with self.assertRaises(rd.FreezeMoved):
            self.run_apply(port, candidates=self.two_families())
        self.assertEqual(self.mailbox(), [])   # rien n'est écrit sur l'ancienne tête
        self.assertEqual(self.marks(), [])
        # la nouvelle tête se désigne sur un nouvel appel, avec ses propres entrées
        out = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["freeze"]["sha"], SHA_B)
        self.assertIn(SHA_B, self.mailbox()[0]["body"])
        self.assertNotIn(SHA_A, self.mailbox()[0]["body"])
        self.assertEqual(len(self.marks()), 1)
        self.assertIn(SHA_B, self.marks()[0])

    def test_waiting_notifies_once_then_capacity_returns(self):
        freeze = self.freeze()
        out = self.run_apply(pg.PgPort(self.db),
                             candidates=[rd.Candidate("r1", "deepseek")])
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["missing"], 2)
        sent = self.mailbox()
        self.assertEqual([m["recipient"] for m in sent], ["pilot"])
        self.assertIn("No implicit admission", sent[0]["body"])
        self.assertIn("frozen milestone #%d" % int(freeze["id"]), sent[0]["body"])
        # rejoué sans capacité nouvelle : une seule notification
        again = self.run_apply(pg.PgPort(self.db),
                               candidates=[rd.Candidate("r1", "deepseek")])
        self.assertTrue(again.get("replayed"))
        self.assertEqual(len(self.mailbox()), 1)
        self.assertEqual(len(self.marks()), 1)
        self.assertEqual(pg.designation_of(self.marks()[0])["state"], rd.WAITING)
        # la capacité revient : la désignation remplace l'attente pour le même gel
        out3 = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertEqual(out3["state"], rd.DESIGNATED)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot", "r1", "r2"])
        self.assertEqual(len(self.marks()), 2)
        self.assertEqual(pg.designation_of(self.marks()[-1])["state"], rd.DESIGNATED)

    def test_a_waiting_designation_needs_a_recipient(self):
        self.freeze()
        with self.assertRaises(rd.DesignationError):
            self.run_apply(pg.PgPort(self.db), candidates=[], notify="")
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])

    def test_an_unknown_recipient_writes_nothing(self):
        self.freeze()
        port = pg.PgPort(self.db)
        with self.assertRaises(rd.DesignationError):
            port.commit(self.lot, freeze=port.latest_freeze(self.lot), required=1,
                        state=rd.DESIGNATED,
                        reviewers=[{"name": "ghost", "family": "claude"}],
                        missing=0, why="", messages=[{"to": "ghost", "body": "à relire"}])
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])

    def test_unknown_author_provenance_waits_for_its_evidence(self):
        self.freeze()
        out = self.run_apply(pg.PgPort(self.db), candidates=self.two_families(),
                             unknown=["deadbeef"])
        self.assertEqual(out["state"], rd.WAITING)
        self.assertIn("provenance", out["why"])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        # la preuve arrive : désignation sur le même gel, courriers une seule fois
        out2 = self.run_apply(pg.PgPort(self.db), candidates=self.two_families())
        self.assertEqual(out2["state"], rd.DESIGNATED)
        self.assertEqual([m["recipient"] for m in self.mailbox()],
                         ["pilot", "r1", "r2"])

    def test_a_new_freeze_is_a_new_designation_without_forgetting_the_old(self):
        first = self.freeze(SHA_A)
        light_rule = (rd.Rule(paths=("**/*.sql",), reviews=1),)
        out = self.run_apply(pg.PgPort(self.db),
                             candidates=[rd.Candidate("r1", "claude")],
                             policy=light_rule, paths=("src/x.sql",))
        self.assertEqual(out["freeze"]["sha"], SHA_A)
        second = self.freeze(SHA_B)
        out2 = self.run_apply(pg.PgPort(self.db),
                              candidates=[rd.Candidate("r1", "claude")],
                              policy=light_rule, paths=("src/x.sql",))
        self.assertEqual(out2["freeze"]["sha"], SHA_B)
        self.assertNotEqual(int(first["id"]), int(second["id"]))
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r1"])
        self.assertIn(SHA_B, self.mailbox()[-1]["body"])
        # la désignation du premier gel reste attachée à son gel (portée des verdicts)
        old = pg.PgPort(self.db).recorded(self.lot, int(first["id"]))
        self.assertEqual(old["freeze"]["sha"], SHA_A)
        self.assertEqual(len(self.marks()), 2)

    def test_the_hook_transaction_defers_only_the_thread(self):
        freeze = self.freeze()
        threads: list = []
        with self.db.transaction() as tx:
            out = pg.PgPort(tx, after_commit=threads).commit(
                self.lot, freeze={"id": int(freeze["id"]), "sha": SHA_A}, required=1,
                state=rd.DESIGNATED, reviewers=[{"name": "r1", "family": "claude"}],
                missing=0, why="", messages=[{"to": "r1", "body": "à relire %s" % SHA_A}])
            self.assertEqual(len(self.mailbox(tx)), 1)  # visible dans la transaction
            self.assertEqual(len(threads), 1)           # le fil attend le commit
        self.assertEqual(out["freeze"]["sha"], SHA_A)
        self.assertEqual(len(self.mailbox()), 1)
        self.assertEqual(len(self.marks()), 1)
        for record in threads:  # ce que fait l'appelant après son commit
            record(self.db.cfg, self.db)

    def test_a_rollback_leaves_no_mail_and_no_mark(self):
        self.freeze()
        threads: list = []
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                last = pg.PgPort(tx).latest_freeze(self.lot)
                pg.PgPort(tx, after_commit=threads).commit(
                    self.lot, freeze=last, required=1, state=rd.DESIGNATED,
                    reviewers=[{"name": "r1", "family": "claude"}], missing=0, why="",
                    messages=[{"to": "r1", "body": "à relire"}])
                self.assertEqual(len(self.mailbox(tx)), 1)
                raise RuntimeError("boom")
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])
        self.assertTrue(threads)  # jamais vidé : c'est l'appelant qui décide, après commit


class Test1617FrozenHook(PgTestCase):
    """Le raccord étroit du gel (#1617) : callback dans la transaction du jalon.

    Le raccord est branché mais **inactif** : sans callback, un gel ne désigne
    personne. Avec un callback, la marque et les courriers tiennent dans la
    transaction du jalon `frozen` ; le fil externe reste dans `after_commit`,
    que **l'appelant** vide après le commit qu'il tient (un `with` imbriqué ne
    commite pas), et une exception du callback annule le jalon avec tout ce
    qu'il portait.
    """

    def setUp(self):
        super().setUp()
        self.item = work.add(self.db, title=ITEM["title"], app=ITEM["app"])
        self.lot = int(self.item["id"])
        for name in ("r1", "r2", "pilot"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            status="idle")

    # -- helpers ----------------------------------------------------------
    def mailbox(self, db=None):
        return (db or self.db).query(
            "SELECT id, recipient, body FROM agent_mailbox ORDER BY id")

    def marks(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,)) if str(row["note"]).startswith(pg.MARK_PREFIX)]

    def milestones(self):
        return self.db.query(
            "SELECT id, kind, sha FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen' ORDER BY id", (self.lot,))

    def designated(self, tx, milestone, threads):
        """Le raccord réel : le port écrit dans la transaction du jalon."""
        return rd.apply(
            dict(self.item, state="build"), policy=RULES,
            paths=["src/ameesh/db.py"], port=pg.PgPort(tx, after_commit=threads),
            freeze_sha=milestone["sha"],  # les entrées valent pour CE gel
            authors=[{"name": "author", "family": "deepseek"}],
            candidates=[rd.Candidate("r1", "claude"), rd.Candidate("r2", "codex")],
            risk="sensitive", base="c" * 40, policy_ref="canon@deadbeef",
            notify="pilot")

    # -- tests ------------------------------------------------------------
    def test_without_callback_the_freeze_designates_nobody(self):
        row = work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="me")
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(len(self.milestones()), 1)
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.mailbox(), [])

    def test_the_frozen_callback_designates_in_the_freeze_transaction(self):
        seen = {}

        def hook(tx, milestone, threads):
            seen["kind"] = milestone["kind"]
            seen["id"] = int(milestone["id"])
            seen["inside"] = len(tx.query(
                "SELECT id FROM work_item_milestones WHERE id = %s",
                (int(milestone["id"]),)))
            other = self.connect()
            try:
                # ce que voit un autre écrivain tant que le gel n'est pas commité
                seen["outside"] = len(other.query(
                    "SELECT id FROM work_item_milestones WHERE id = %s",
                    (int(milestone["id"]),)))
                seen["outside_mail"] = len(other.query("SELECT id FROM agent_mailbox"))
            finally:
                other.close()
            seen["out"] = self.designated(tx, milestone, threads)

        threads: list = []
        drained: list = []
        threads.append(lambda cfg, db: drained.append(cfg.schema))
        row = work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="me",
                             on_frozen=hook, after_commit=threads)
        self.assertEqual(seen["kind"], "frozen")
        self.assertEqual(seen["id"], int(row["id"]))
        self.assertEqual(seen["inside"], 1)       # le jalon, dans la transaction
        self.assertEqual(seen["outside"], 0)      # et rien dehors avant le commit
        self.assertEqual(seen["outside_mail"], 0)
        self.assertEqual(seen["out"]["state"], rd.DESIGNATED)
        self.assertEqual(seen["out"]["freeze"], {"id": int(row["id"]), "sha": SHA_A})
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(pg.designation_of(self.marks()[0])["freeze"],
                         {"id": int(row["id"]), "sha": SHA_A})
        # le fil n'est pas publié par le jalon : c'est l'appelant qui le vide
        self.assertTrue(threads)
        self.assertEqual(drained, [])
        for record in threads:
            record(self.db.cfg, self.db)
        self.assertEqual(drained, [self.schema])

    # -- #1634 : une notification impossible n'annule pas le gel ---------
    def waiting_freeze(self, notify):
        """Gel par le hook sans relecteur désignable, notifiant `notify`."""
        def hook(tx, milestone, threads):
            seen["out"] = rd.apply(
                dict(self.item, state="build"), policy=RULES,
                paths=["src/ameesh/db.py"], port=pg.PgPort(tx, after_commit=threads),
                freeze_sha=milestone["sha"],
                authors=[{"name": "author", "family": "deepseek"}], candidates=[],
                risk="sensitive", base="c" * 40, policy_ref="canon@deadbeef",
                notify=notify)

        seen: dict = {}
        threads: list = []
        row = work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="me",
                             on_frozen=hook, after_commit=threads)
        for record in threads:
            record(self.db.cfg, self.db)
        return row, seen["out"]

    def notes(self):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (self.lot,))]

    def assert_freeze_kept_without_mail(self, row, out, recipient):
        self.assertEqual([int(m["id"]) for m in self.milestones()], [int(row["id"])])
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(out["state"], rd.WAITING)
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual(mark["state"], rd.WAITING)
        self.assertEqual(mark["freeze"], {"id": int(row["id"]), "sha": SHA_A})
        self.assertIn("notification undeliverable at freeze", mark["why"])
        lisible = [n for n in self.notes() if n.startswith("désignation au gel")]
        self.assertEqual(len(lisible), 1)
        self.assertIn(recipient, lisible[0])
        self.assertIn("Gel conservé", lisible[0])

    def test_an_empty_notify_keeps_the_freeze(self):
        row, out = self.waiting_freeze("")
        self.assert_freeze_kept_without_mail(row, out, "— (no recipient)")

    def test_an_unknown_notify_keeps_the_freeze(self):
        row, out = self.waiting_freeze("ghost")
        self.assert_freeze_kept_without_mail(row, out, "ghost (not in the registry)")

    def test_a_normal_notify_is_mailed_at_freeze(self):
        row, out = self.waiting_freeze("pilot")
        self.assertEqual(len(self.milestones()), 1)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["pilot"])
        self.assertNotIn("undeliverable", pg.designation_of(self.marks()[0])["why"])
        self.assertFalse([n for n in self.notes() if n.startswith("désignation au gel")])

    def test_an_unknown_reviewer_keeps_the_freeze_and_waits(self):
        def hook(tx, milestone, threads):
            seen["out"] = pg.PgPort(tx, after_commit=threads).commit(
                self.lot, freeze={"id": int(milestone["id"]), "sha": SHA_A},
                required=1, state=rd.DESIGNATED,
                reviewers=[{"name": "ghost", "family": "claude"}], missing=0, why="",
                messages=[{"to": "ghost", "body": "à relire"}])

        seen: dict = {}
        row = work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="me",
                             on_frozen=hook, after_commit=[])
        self.assert_freeze_kept_without_mail(row, seen["out"],
                                             "ghost (not in the registry)")
        self.assertEqual(seen["out"]["reviewers"], [])
        self.assertEqual(seen["out"]["missing"], 1)

    def test_a_raising_callback_rolls_the_freeze_back(self):
        def hook(tx, milestone, threads):
            pg.PgPort(tx, after_commit=threads).commit(
                self.lot, freeze={"id": int(milestone["id"]), "sha": SHA_A},
                required=1, state=rd.DESIGNATED,
                reviewers=[{"name": "r1", "family": "claude"}], missing=0, why="",
                messages=[{"to": "r1", "body": "à relire"}])
            self.assertEqual(len(self.mailbox(tx)), 1)  # écrit, mais pas commité
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            work.milestone(self.db, self.lot, "frozen", sha=SHA_A, actor="me",
                           on_frozen=hook, after_commit=[])
        self.assertEqual(self.milestones(), [])   # le jalon a suivi le rollback
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.mailbox(), [])

    def test_a_nested_freeze_publishes_no_thread_before_the_outer_commit(self):
        threads: list = []
        drained: list = []
        threads.append(lambda cfg, db: drained.append(cfg.schema))
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                work.milestone(tx, self.lot, "frozen", sha=SHA_A, actor="me",
                               on_frozen=self.designated, after_commit=threads)
                self.assertEqual(len(self.mailbox(tx)), 2)  # écrit dans la transaction
                self.assertEqual(drained, [])               # rien de publié
                raise RuntimeError("boom")
        # rollback externe : ni jalon, ni marque, ni courrier, aucune trace de fil
        self.assertEqual(self.milestones(), [])
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.mailbox(), [])
        self.assertTrue(threads)
        self.assertEqual(drained, [])

    def test_a_nested_freeze_publishes_the_thread_after_the_outer_commit(self):
        threads: list = []
        drained: list = []
        threads.append(lambda cfg, db: drained.append(cfg.schema))
        with self.db.transaction() as tx:
            work.milestone(tx, self.lot, "frozen", sha=SHA_A, actor="me",
                           on_frozen=self.designated, after_commit=threads)
            self.assertEqual(drained, [])
        self.assertEqual(drained, [])  # commit externe fait, fil toujours en attente
        for record in threads:
            record(self.db.cfg, self.db)
        self.assertEqual(drained, [self.schema])
        self.assertEqual([m["recipient"] for m in self.mailbox()], ["r1", "r2"])
        self.assertEqual(len(self.marks()), 1)

    def test_the_callback_is_refused_outside_a_freeze(self):
        with self.assertRaises(work.WorkError):
            work.milestone(self.db, self.lot, "verdict", verdict="ok", sha=SHA_A,
                           actor="me", on_frozen=lambda *args: None)
        with self.assertRaises(ValueError):
            storage.of(self.db).work.add_milestone(
                self.lot, "verdict", sha=SHA_A, actor="me", verdict="ok", note="",
                on_frozen=lambda *args: None)
        self.assertEqual(self.milestones(), [])
