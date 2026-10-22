# SPDX-License-Identifier: AGPL-3.0-only
"""Historique exact et lecture de la porte pour le train (#1467/#2681).

Le train ne tient plus sa propre règle d'avis : il lit la tête gelée avec
`merge_queue.review_gate`, en lui donnant les familles **historiques** (reçus
exacts puis journal des tours). Les auteurs viennent de `review_authors`, la
même lecture que la désignation au gel. Témoins #2602/#879 (blocage d'une tête
antérieure ignoré), #2498 (commit de l'assigné sans reçu) et #2888 (une
correction de relecteur, même sous le seuil, est un auteur de la tête) :
la famille du correcteur est exclue ; tiers non relecteur sans reçu : refusé.
"""
import os
import subprocess
import tempfile
import types
import unittest

from ameesh import (merge_queue, registry, review_authors, review_provenance,
                    storage, train, train_history, work)
from .support import PgTestCase


def _git(root, *args, env=None):
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, env=env)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class HistoryFixture:
    """A real Git repository, real milestones, receipts and turns."""

    def make_repo(self):
        root = tempfile.mkdtemp(prefix="ameesh-history-", dir=self.tmp)
        _git(root, "init", "-q", "-b", "fixture")
        _git(root, "config", "user.name", "fixture")
        _git(root, "config", "user.email", "fixture@example.invalid")
        return root

    def git(self, *args):
        return _git(self.repo, *args)

    def commit(self, text, *, actor="author", path="content"):
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=actor,
                   GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % actor,
                   GIT_COMMITTER_NAME=actor,
                   GIT_COMMITTER_EMAIL="%s@ameesh.invalid" % actor)
        destination = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(destination) or self.repo, exist_ok=True)
        with open(destination, "w", encoding="utf-8") as target:
            target.write(text)
        _git(self.repo, "add", path, env=env)
        _git(self.repo, "commit", "-qm", text, env=env)
        return _git(self.repo, "rev-parse", "HEAD", env=env)

    def receipt_commit(self, sha, actor, harness):
        storage.of(self.db).provenance.add(
            "commit:" + sha, sha, actor, "s-" + harness, "t-" + harness,
            harness, work_item_id=self.lot)

    def receipt_milestone(self, mark, harness):
        storage.of(self.db).provenance.add(
            "milestone:%s" % mark["id"], str(mark["sha"]), str(mark["actor"]),
            "s-" + harness, "t-" + harness, harness, work_item_id=self.lot)

    def turn(self, actor, harness):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, %s, %s, now())", (actor, harness, "s-" + harness))

    def verdict(self, sha, actor, harness, status="ok"):
        mark = self.ops.add_milestone(self.lot, "verdict", sha=sha, actor=actor,
                                      verdict=status, note="")
        self.receipt_milestone(mark, harness)
        return mark

    def freeze(self, sha, actor="author"):
        return self.ops.add_milestone(self.lot, "frozen", sha=sha, actor=actor,
                                      verdict=None, note="")

    def setUp(self):  # noqa: N802 - unittest name, mixed into PgTestCase
        super().setUp()
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM turn_costs")
        self.repo = self.make_repo()
        self.path = self.repo
        self.project = types.SimpleNamespace(name="history-test", repo=self.repo,
                                             target="main")
        self.base = self.commit("base", path="base.txt")
        for name, harness in (("author", "deepseek"), ("r1", "claude"),
                              ("r2", "codex"), ("third", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo, status="idle")
        self.item = work.add(self.db, title="train history", app=self.project.name,
                             assignee="author", branch="lot/history",
                             branch_target="main", actor="fixture")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['r1','r2']"
                        " WHERE id = %s", (self.lot,))
        self.item = work.get(self.db, self.lot)
        self.ops = storage.of(self.db).work
        # #2498 : commit de l'assigné avec reçu, puis tête de l'assigné avec
        # reçu. La correction de r1 (#2888) vit sur une tête latérale : elle ne
        # fait pas partie de la tête gelée de référence.
        self.first = self.commit("author work", actor="author")
        self.receipt_commit(self.first, "author", "deepseek")
        self.head = self.commit("author correction", actor="author")
        self.receipt_commit(self.head, "author", "deepseek")
        self.turn("author", "deepseek")
        self.frozen = self.freeze(self.head)
        self.turn("author", "deepseek")
        self.r1 = self.verdict(self.head, "r1", "claude")
        self.r2 = self.verdict(self.head, "r2", "codex")
        # #2888 : la correction de r1, sans reçu et sous le seuil, est un
        # commit de la tête ; on la garde sur une branche latérale pour ne pas
        # changer le témoin de base ci-dessus.
        self.review_fix = self.commit("r1 small fix\n", actor="r1")
        self.turn("r1", "claude")
        self.git("checkout", "-q", "-B", "fixture", self.head)
        self.history = train_history.Recorded(
            self.db, self.repo, provider=review_provenance,
            review_rules=types.SimpleNamespace(
                families=("claude", "codex", "deepseek")))
        self.entry = {"work_item_id": self.lot, "frozen_id": self.frozen["id"],
                      "frozen_sha": self.head, "generation": 1, "target": "main",
                      "source": "fixture"}
        self.klass = {"class": "sensitive",
                      "files": ({"path": "content", "class": "sensitive",
                                 "rule": None},)}

    def qualify(self, *, required=2, entry=None, klass=None):
        return self.history.qualify_subject(
            entry or self.entry, self.base, required=required,
            klass=klass or self.klass)

    def coauthored(self):
        """A new head carrying a receipted third-party commit (an author)."""
        head = self.commit("third party work", actor="third")
        self.receipt_commit(head, "third", "claude")
        frozen = self.freeze(head)
        return dict(self.entry, frozen_id=frozen["id"], frozen_sha=head)

    def reviewer_fix_head(self, text, path="content"):
        """A new head whose only commit is a designated reviewer's correction."""
        head = self.commit(text, actor="r1", path=path)
        self.turn("r1", "claude")
        frozen = self.freeze(head)
        return dict(self.entry, frozen_id=frozen["id"], frozen_sha=head)

    def dated(self, *marks):
        """#2965 : des jalons à des instants exacts, pour relire la chronologie."""
        for stamp, mark in marks:
            self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                            " WHERE id = %s", (stamp, mark["id"]))


class RecordedHistoryTest(HistoryFixture, PgTestCase):
    def test_every_commit_is_read_on_the_frozen_head_with_the_gate(self):
        proof = self.qualify()
        reviews = proof["reviews"]
        self.assertEqual(reviews["schema"], "ameesh-train-review-gate/1")
        self.assertEqual(reviews["head"], self.head)
        self.assertTrue(reviews["gate"]["ok"], reviews["gate"])
        self.assertEqual(reviews["gate"]["required"], 2)
        self.assertEqual({row["name"] for row in reviews["authors"]},
                         {"author"})

    def test_admission_ignores_a_redundant_ok_after_the_gate_opened(self):
        """B1 (relecture cx1) : le gel effectif, pas le dernier ok répété."""
        extra = self.verdict(self.head, "r1", "claude")
        self.dated((900.0, self.frozen), (1000.0, self.r1),
                   (1100.0, self.r2), (1200.0, extra))
        proof = self.qualify()
        self.assertTrue(proof["reviews"]["gate"]["ok"], proof["reviews"]["gate"])
        # La porte était ouverte à 1100 (r1 claude + r2 codex) ; le second ok
        # de r1 à 1200 ne redate pas l'admission.
        self.assertEqual(proof["reviews"]["admitted_ts"], 1100.0)

    def test_a_lifted_block_dates_an_admission_that_needs_no_review(self):
        """B2 (relecture cx1) : blocage puis levée, même à zéro avis requis."""
        head = self.commit("post freeze fix", actor="author")
        self.receipt_commit(head, "author", "deepseek")
        self.turn("author", "deepseek")
        frozen = self.freeze(head)
        blocked = self.verdict(head, "r1", "claude", status="blocked")
        lifted = self.verdict(head, "r1", "claude", status="ok")
        # Le nouveau gel doit rester le plus récent des gels : on date après
        # le gel du fixture, pas dans un passé qui le rendrait fantôme.
        now = self.db.query(
            "SELECT extract(epoch from clock_timestamp())::float8 AS ts")[0]["ts"]
        self.dated((now + 1, frozen), (now + 2, blocked), (now + 3, lifted))
        entry = dict(self.entry, frozen_id=frozen["id"], frozen_sha=head)
        proof = self.qualify(entry=entry, required=0,
                             klass={"class": "light", "files": ()})
        gate = proof["reviews"]["gate"]
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["required"], 0)
        self.assertEqual(proof["reviews"]["admitted_ts"], now + 3)

    def test_the_compiled_family_flags_drive_the_train_gate(self):
        # #2726 : le train passe `exclude_author_family` et `distinct_families`
        # de son document compilé à la porte, au lieu de laisser le repli.
        head = self.commit("second author work", actor="author")
        self.receipt_commit(head, "author", "deepseek")
        frozen = self.freeze(head)
        entry = dict(self.entry, frozen_id=frozen["id"], frozen_sha=head)
        self.turn("author", "deepseek")
        registry.upsert(self.db, "r5", harness="codex", host=self.cfg.host,
                        cwd=self.repo, status="idle")
        self.verdict(head, "r2", "codex")
        self.verdict(head, "r5", "codex")
        normal = {"class": "normal", "files": ()}
        families = ("claude", "codex", "deepseek")
        loose = train_history.Recorded(
            self.db, self.repo, provider=review_provenance,
            review_rules=types.SimpleNamespace(
                families=families, exclude_author_family=False,
                distinct_families=False))
        proof = loose.qualify_subject(entry, self.base, required=2, klass=normal)
        self.assertTrue(proof["reviews"]["gate"]["ok"], proof["reviews"]["gate"])
        self.assertEqual([row["actor"]
                          for row in proof["reviews"]["gate"]["accepted"]],
                         ["r2", "r5"])
        strict = train_history.Recorded(
            self.db, self.repo, provider=review_provenance,
            review_rules=types.SimpleNamespace(
                families=families, exclude_author_family=False,
                distinct_families=True))
        with self.assertRaisesRegex(train.TrainError, "two independent"):
            strict.qualify_subject(entry, self.base, required=2, klass=normal)

    def test_old_blockage_on_an_earlier_head_is_not_read(self):
        # #2602/#879 : le dernier verdict de r3 sur une tête antérieure est
        # bloquant ; la tête gelée a ses deux ok indépendants.
        other = self.commit("old author work", actor="author", path="old.txt")
        self.receipt_commit(other, "author", "deepseek")
        self.verdict(other, "r3", "claude", status="blocked")
        proof = self.qualify()
        self.assertTrue(proof["reviews"]["gate"]["ok"])
        # Un blocage sur la tête gelée, lui, reste en vigueur.
        self.verdict(self.head, "r3", "claude", status="blocked")
        with self.assertRaisesRegex(train.TrainError, "blocked verdict.*not lifted"):
            self.qualify()

    def test_missing_receipt_of_the_reference_author_is_read_from_the_turn_journal(self):
        # #2498 : un commit restauré par rebase n'a plus de reçu ; l'assigné
        # reste l'auteur, sa famille vient du journal des tours.
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("commit:" + self.first,))
        proof = self.qualify()
        self.assertTrue(proof["reviews"]["gate"]["ok"], proof["reviews"]["gate"])

    def test_missing_receipt_of_a_designated_reviewer_small_fix_is_an_author(self):
        # #2888 : correction de premier tour de r1 (sans reçu) : sa famille
        # (claude) vient du journal des tours et devient une famille d'auteur,
        # pas une provenance inconnue. Son propre avis ne compte pas ; r2
        # (codex) couvre l'unique avis indépendant.
        frozen = self.freeze(self.review_fix)
        entry = dict(self.entry, frozen_id=frozen["id"],
                     frozen_sha=self.review_fix)
        self.verdict(self.review_fix, "r1", "claude")
        self.verdict(self.review_fix, "r2", "codex")
        authors, unknown = review_authors.authors(
            self.db, self.item, frozen,
            [self.first, self.head, self.review_fix], self.repo,
            ("claude", "codex", "deepseek"))
        self.assertEqual(unknown, [])
        self.assertIn({"name": "r1", "family": "claude"}, authors)
        proof = self.qualify(entry=entry, required=1)
        gate = proof["reviews"]["gate"]
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual([row["actor"] for row in gate["accepted"]], ["r2"])

    def test_a_reviewer_fix_family_cannot_cover_the_second_opinion(self):
        # #2888 : auteur DeepSeek, correcteur r1/Claude ; les avis r2/Codex et
        # third/Claude ne laissent que Codex indépendant. Le train refuse deux
        # avis requis, comme la porte et la désignation.
        frozen = self.freeze(self.review_fix)
        entry = dict(self.entry, frozen_id=frozen["id"],
                     frozen_sha=self.review_fix)
        self.verdict(self.review_fix, "r2", "codex")
        self.verdict(self.review_fix, "third", "claude")
        with self.assertRaisesRegex(train.TrainError, "two independent"):
            self.qualify(entry=entry, required=2)

    def test_a_coauthor_cannot_admit_its_own_verdict_with_one_review(self):
        # #2681 B (dev1/cx2) : un tiers avec reçu exact est un auteur de la
        # tête ; son propre `ok` ne compte pas, même pour l'unique avis exigé.
        entry = self.coauthored()
        self.verdict(entry["frozen_sha"], "third", "claude")
        with self.assertRaisesRegex(train.TrainError,
                                    "one verdict from another family"):
            self.qualify(entry=entry, required=1)

    def test_another_agent_of_the_coauthor_family_cannot_admit_with_one_review(self):
        # Même famille que le coauteur : l'avis d'un autre agent ne prouve pas
        # l'indépendance.
        entry = self.coauthored()
        self.verdict(entry["frozen_sha"], "r1", "claude")
        with self.assertRaisesRegex(train.TrainError,
                                    "one verdict from another family"):
            self.qualify(entry=entry, required=1)

    def test_a_coauthor_verdict_does_not_count_towards_two_reviews(self):
        entry = self.coauthored()
        self.verdict(entry["frozen_sha"], "third", "claude")
        self.verdict(entry["frozen_sha"], "r2", "codex")
        with self.assertRaisesRegex(train.TrainError,
                                    "two independent verdicts"):
            self.qualify(entry=entry, required=2)

    def test_another_agent_of_the_coauthor_family_does_not_count_towards_two(self):
        entry = self.coauthored()
        self.verdict(entry["frozen_sha"], "r1", "claude")
        self.verdict(entry["frozen_sha"], "r2", "codex")
        with self.assertRaisesRegex(train.TrainError,
                                    "two independent verdicts"):
            self.qualify(entry=entry, required=2)

    def test_a_reviewer_correction_over_the_threshold_cannot_admit_itself(self):
        # §2 : au-delà de ~50 lignes, le relecteur devient auteur de sa
        # correction ; son propre `ok` ne compte pas.
        entry = self.reviewer_fix_head(
            "r1\n" + "".join("line %02d\n" % index for index in range(60)))
        self.verdict(entry["frozen_sha"], "r1", "claude")
        with self.assertRaisesRegex(train.TrainError,
                                    "one verdict from another family"):
            self.qualify(entry=entry, required=1)

    def test_a_reviewer_design_correction_cannot_admit_itself(self):
        # §2 : une correction de conception n'est jamais un petit correctif.
        entry = self.reviewer_fix_head("study\n", path="docs/design/x.md")
        self.verdict(entry["frozen_sha"], "r1", "claude")
        with self.assertRaisesRegex(train.TrainError,
                                    "one verdict from another family"):
            self.qualify(entry=entry, required=1)

    def test_pilot_waiver_covers_the_frozen_head_in_the_train_too(self):
        # #2681 cas (3) : la dispense `-none` est lue par la même porte que le
        # composeur ; le train ne recopie pas la règle.
        self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                        (work.REVIEWER_NONE, self.lot))
        self.db.execute("DELETE FROM work_item_milestones WHERE work_item_id = %s"
                        " AND kind = 'verdict'", (self.lot,))
        self.receipt_commit(self.review_fix, "r1", "claude")
        proof = self.qualify()
        self.assertTrue(proof["reviews"]["gate"]["ok"], proof["reviews"]["gate"])
        self.assertTrue(proof["reviews"]["gate"]["waived"])

    def test_third_party_without_a_receipt_is_refused_on_both_sides(self):
        third = self.commit("third party work", actor="third")
        frozen = self.freeze(third)
        entry = dict(self.entry, frozen_id=frozen["id"], frozen_sha=third)
        # Train : la provenance inconnue refuse avant la porte des avis.
        with self.assertRaisesRegex(train.TrainError, "unknown historical author"):
            self.qualify(entry=entry)
        # Désignation : la même lecture rend le commit inconnu.
        authors, unknown = review_authors.authors(
            self.db, self.item, frozen,
            [self.first, self.review_fix, self.head, third],
            self.repo, ("claude", "codex", "deepseek"))
        self.assertIn(third[:12], unknown)
        self.assertNotIn("third", {row["name"] for row in authors})

    def test_new_freeze_same_head_invalidates_old_queue_generation(self):
        self.freeze(self.head)
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()

    def test_abbreviated_freeze_is_resolved_but_another_commit_is_refused(self):
        self.db.execute("UPDATE work_item_milestones SET sha = %s WHERE id = %s",
                        (self.head[:10], self.frozen["id"]))
        self.assertTrue(self.qualify()["reviews"]["gate"]["ok"])
        self.db.execute("UPDATE work_item_milestones SET sha = %s WHERE id = %s",
                        (self.first, self.frozen["id"]))
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.qualify()

    def test_shallow_history_is_refused_without_modifying_it(self):
        clone = os.path.join(self.tmp, "shallow")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + self.repo,
                        clone], check=True, capture_output=True)
        history = train_history.Recorded(
            self.db, clone, provider=review_provenance,
            review_rules=types.SimpleNamespace(
                families=("claude", "codex", "deepseek")))
        with self.assertRaisesRegex(train.TrainError, "shallow clone refused"):
            history.qualify(self.entry, self.base, required=2, klass=self.klass)

    def test_already_contained_head_and_unknown_base_supply_no_admission(self):
        for base in (self.head, "0" * 40):
            with self.subTest(base=base), self.assertRaises(train.TrainError):
                self.history.qualify(self.entry, base, required=2, klass=self.klass)

    def test_light_zero_review_still_reads_the_frozen_head(self):
        klass = {"class": "light",
                 "files": ({"path": "content", "class": "light", "rule": None},)}
        proof = self.qualify(required=0, klass=klass)
        self.assertTrue(proof["reviews"]["gate"]["ok"])
        self.assertEqual(proof["reviews"]["gate"]["required"], 0)

    def test_a_blocked_verdict_on_the_frozen_head_refuses_even_a_light_lot(self):
        self.verdict(self.head, "r3", "claude", status="blocked")
        klass = {"class": "light",
                 "files": ({"path": "content", "class": "light", "rule": None},)}
        with self.assertRaisesRegex(train.TrainError, "blocked verdict.*not lifted"):
            self.qualify(required=0, klass=klass)


class ReviewerThresholdTest(HistoryFixture, PgTestCase):
    def authors_for(self, sha):
        return review_authors.authors(
            self.db, self.item, self.frozen, [self.first, self.review_fix, sha],
            self.repo, ("claude", "codex", "deepseek"))

    def test_a_reviewer_small_fix_makes_their_family_an_author(self):
        # #2888 : le seuil du §2 ne protège plus la famille du correcteur.
        authors, unknown = self.authors_for(self.review_fix)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "r1", "family": "claude"}, authors)

    def test_a_reviewer_fix_over_the_threshold_makes_their_family_an_author(self):
        large = self.commit("r1\n" + "".join("line %02d\n" % index
                                             for index in range(60)), actor="r1")
        self.turn("r1", "claude")
        authors, unknown = self.authors_for(large)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "r1", "family": "claude"}, authors)

    def test_a_reviewer_design_change_makes_their_family_an_author(self):
        design = self.commit("study\n", actor="r1", path="docs/design/x.md")
        self.turn("r1", "claude")
        authors, unknown = self.authors_for(design)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "r1", "family": "claude"}, authors)


class CompleteMilestonesTest(PgTestCase):
    def test_explicit_unbounded_read_preserves_blockage_beyond_default_page(self):
        item = work.add(self.db, title="train history", source="fixture", actor="fixture")
        ops = storage.of(self.db).work
        before = len(ops.milestones(item["id"], None))
        self.db.execute("INSERT INTO work_item_milestones(work_item_id,kind,sha,actor,verdict)"
                        " SELECT %s, 'verdict', %s, 'reader',"
                        " CASE WHEN n = 1 THEN 'blocked' ELSE 'ok' END"
                        " FROM generate_series(1, 110) n", (item["id"], "a" * 40))
        self.assertEqual(len(ops.milestones(item["id"], 100)), 100)
        marks = ops.milestones(item["id"], None)
        self.assertEqual(len(marks), before + 110)
        self.assertEqual(sum(m["verdict"] == "blocked" for m in marks), 1)


class AdmittedInstantTest(unittest.TestCase):
    """#2965 : l'instant d'admissibilité, relu par la porte elle-même."""

    SHA = "a" * 40
    FAMILIES = {"author": "human", "r1": "claude", "r2": "codex",
                "r3": "deepseek"}

    def gate(self, verdicts, *, required=2, waived=False, klass=None):
        marks = [dict(kind="frozen", sha=self.SHA, actor="author", verdict=None,
                      at_ts=100.0, id=1)]
        marks.extend(dict(kind="verdict", sha=self.SHA, **mark)
                     for mark in verdicts)
        return merge_queue.review_gate(
            None, marks, self.SHA,
            klass=klass or {"class": "normal", "files": []},
            item=None, required=required,
            authors=[{"name": "author", "family": "human"}],
            authors_complete=True,
            family_of=lambda name: self.FAMILIES.get(name, ""))

    def test_a_redundant_ok_does_not_redate_the_open_gate(self):
        """B1 : r1 ok 110, r2 ok 120, r1 ok 130 -> ouvert à 120, pas 130."""
        gate = self.gate([
            dict(actor="r1", verdict="ok", at_ts=110.0, id=2),
            dict(actor="r2", verdict="ok", at_ts=120.0, id=3),
            dict(actor="r1", verdict="ok", at_ts=130.0, id=4)])
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["completed_ts"], 120.0)
        self.assertEqual(train_history.admitted_ts({"at_ts": 100.0}, gate), 120.0)
        # Le `ok` répété de r1 à 130 ne doit pas effacer son `ok` de 110 :
        # la porte était déjà ouverte à 115 par r1 (claude) + r3 (deepseek).
        gate = self.gate([
            dict(actor="r1", verdict="ok", at_ts=110.0, id=2),
            dict(actor="r3", verdict="ok", at_ts=115.0, id=3),
            dict(actor="r1", verdict="ok", at_ts=130.0, id=4)])
        self.assertEqual(gate["completed_ts"], 115.0)
        self.assertEqual(train_history.admitted_ts({"at_ts": 100.0}, gate), 115.0)

    def test_a_block_delays_admission_even_without_required_review(self):
        """B2 : gel 100, blocage 110, ok 140 ; aucun avis requis."""
        verdicts = [dict(actor="r1", verdict="blocked", at_ts=110.0, id=2),
                    dict(actor="r1", verdict="ok", at_ts=140.0, id=3)]
        gate = self.gate(verdicts, required=0,
                         klass={"class": "light", "files": ()})
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["completed_ts"], 140.0)
        self.assertEqual(train_history.admitted_ts({"at_ts": 100.0}, gate), 140.0)
        # Même chronologie sous dispense du pilote (waived) : le blocage
        # courant ferme la porte avant la dispense, le ok le rouvre à 140.
        self.assertEqual(merge_queue._gate_completed_ts(
            [dict(kind="verdict", sha=self.SHA, **mark) for mark in verdicts], {},
            required=1, waived=True, distinct_families=True), 140.0)

    def test_admission_is_never_before_the_effective_freeze(self):
        gate = self.gate([
            dict(actor="r1", verdict="ok", at_ts=110.0, id=2),
            dict(actor="r2", verdict="ok", at_ts=120.0, id=3)])
        self.assertEqual(train_history.admitted_ts({"at_ts": 500.0}, gate), 500.0)

    def test_an_open_gate_without_a_verdict_keeps_the_freeze(self):
        gate = self.gate([], required=0,
                         klass={"class": "light", "files": ()})
        self.assertTrue(gate["ok"], gate)
        self.assertIsNone(gate["completed_ts"])
        self.assertEqual(train_history.admitted_ts({"at_ts": 100.0}, gate), 100.0)

    def test_a_gate_never_completed_is_refused(self):
        gate = self.gate([dict(actor="r1", verdict="ok", at_ts=110.0, id=2)],
                         required=2)
        self.assertFalse(gate["ok"], gate)
        self.assertIsNone(gate.get("completed_ts"))
        with self.assertRaises(train.TrainError):
            train_history.admitted_ts({"at_ts": 100.0}, gate)
