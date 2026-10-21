# SPDX-License-Identifier: AGPL-3.0-only
"""#2808 — le titulaire du lot n'est auteur que par un commit de la plage gelée.

`review_lot._author_at_freeze` et `review_authors.reference_author` nomment
l'assigné au gel : c'est une **référence d'attribution**, pas une preuve
d'écriture. L'auteur se lit dans la provenance des commits de la plage gelée
(auteur Git, reçus exacts, repli sur le journal des tours, #2721). Un titulaire
réassigné sans commit ne compte donc pas comme auteur : sa famille reste
éligible (#2805). S'il a écrit un commit — y compris comme auteur Git d'un
commit réécrit —, sa famille est exclue ; une provenance illisible garde la
porte fermée (l'indépendance ne se suppose jamais).
"""
from __future__ import annotations

import os
from unittest import TestCase, mock

from ameesh import merge_queue, registry, review_authors, storage, work
from ameesh import review_designation as rd

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
NORMAL = {"id": 1, "assignee": "titular", "branch": "lot/1-review"}


class ReceiptProvider:
    """Un fournisseur de reçus exacts : le commit nomme son acteur et son harnais."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def receipt_for_commit(self, _db, sha):
        return self.rows.get(sha)


class ReadingTest(TestCase):
    """`review_authors.reading` : le titulaire n'entre que par un commit."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(os.environ.get("TMPDIR") or "/tmp",
                                 "ameesh-2808-%d" % id(self))
        self.repo = _Repo(self.root)
        self.repo.write("src/ameesh/chat.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/chat.py", "lot\n")
        self.impl = self.repo.commit("impl: lot", agent="impl")
        self.provider = ReceiptProvider({
            self.impl: {"actor": "impl", "harness": "codex"},
        })

    def reading(self, commits):
        with mock.patch.object(review_authors, "designated_reviewers",
                               return_value=set()):
            return review_authors.reading(
                None, NORMAL, None, commits, self.repo.root, FAMILIES,
                provider=self.provider)

    def test_a_titular_without_a_commit_is_not_an_author(self):
        # #2805 : deepseek2 assigné puis réassigné sans commit ; la seule
        # écriture est d'un autre agent. Le titulaire n'est pas auteur.
        authors, unknown, _correctors = self.reading([self.impl])
        self.assertEqual([row["name"] for row in authors], ["impl"])
        self.assertEqual(unknown, [])

    def test_unreadable_git_authors_cannot_prove_the_titular_wrote_nothing(self):
        # A receipt for the committer does not identify the original Git
        # author preserved by an amend. Failed/incomplete reads must wait.
        for output in (None, "", self.impl, self.impl + "\x1fimpl@ameesh.invalid\nmalformed"):
            with self.subTest(output=output), mock.patch.object(
                    review_authors.plan_git, "_out", return_value=output):
                _authors, unknown, _correctors = self.reading([self.impl])
                self.assertEqual(unknown, [self.impl[:12]])

    def test_the_titular_family_is_eligible_when_they_wrote_nothing(self):
        authors, _unknown, _correctors = self.reading([self.impl])
        out = rd.select(
            required=1, authors=authors, families=FAMILIES,
            candidates=[rd.Candidate("r_deepseek", "deepseek", True, 0),
                        rd.Candidate("r_codex", "codex", True, 0)])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([row["family"] for row in out["reviewers"]],
                         ["deepseek"])

    def test_a_titular_who_wrote_a_commit_is_an_author(self):
        self.repo.write("src/ameesh/chat.py", "lot\nby titular\n")
        by_titular = self.repo.commit("titular: touch", agent="titular")
        self.provider.rows[by_titular] = {"actor": "titular", "harness": "deepseek"}
        authors, unknown, _correctors = self.reading([self.impl, by_titular])
        self.assertEqual(sorted(row["name"] for row in authors),
                         ["impl", "titular"])
        self.assertEqual(unknown, [])
        out = rd.select(
            required=1, authors=authors, families=FAMILIES,
            candidates=[rd.Candidate("r_deepseek", "deepseek", True, 0),
                        rd.Candidate("r_claude", "claude", True, 0)])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([row["family"] for row in out["reviewers"]],
                         ["claude"])

    def test_a_third_party_without_a_receipt_stays_unknown(self):
        # Un tiers sans reçu n'est attribué à personne : la désignation attend
        # (échec fermé), elle ne suppose pas l'indépendance.
        self.repo.write("src/ameesh/chat.py", "lot\nby ghost\n")
        ghost = self.repo.commit("ghost: touch", agent="ghost")
        authors, unknown, _correctors = self.reading([self.impl, ghost])
        self.assertEqual([row["name"] for row in authors], ["impl"])
        self.assertEqual(unknown, [ghost[:12]])


class GateTitularTest(PgTestCase):
    """La porte : un titulaire sans commit ne ferme pas sa famille."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.ops = storage.of(self.db).work
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/chat.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/chat.py", "lot\n")
        self.impl = self.repo.commit("impl: lot", agent="impl")
        self.harnesses: dict[str, str] = {}
        for name, harness in (("titular", "claude"), ("impl", "deepseek"),
                              ("r_claude", "claude"), ("r_codex", "codex"),
                              ("r_deepseek", "deepseek"), ("ghost", "codex")):
            self.harnesses[name] = harness
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        self.item = work.add(self.db, title="titulaire sans commit", app="ameesh",
                             branch="lot/1-titulaire", branch_target=self.base,
                             assignee="titular")
        self.lot = int(self.item["id"])
        self.receipt("impl", "deepseek", [self.impl])

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def freeze(self, sha, actor=""):
        row = self.ops.add_milestone(self.lot, "frozen", sha=sha, actor=actor,
                                     verdict=None, note="")
        return row

    def klass(self, *, base=True):
        out = {"class": "sensitive", "project": "ameesh",
               "files": [{"path": "src/ameesh/chat.py"}]}
        if base:
            out["base_sha"] = self.base
        return out

    def gate(self, head, verdicts, *, klass=None, authors=None,
             authors_complete=False):
        marks = self.ops.milestones(self.lot, None)
        marks = [dict(row, kind=row["kind"], at_ts=float(row["at_ts"]))
                 for row in marks if row["kind"] in ("frozen", "verdict")]
        for actor in verdicts:
            # #2928 : en classe sensible, un verdict ne compte que sur son reçu
            # exact — les témoins écrivent donc le jalon et sa preuve.
            row = self.ops.add_milestone(self.lot, "verdict", sha=head, actor=actor,
                                         verdict="ok", note="")
            harness = self.harnesses[actor]
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("milestone:%d" % int(row["id"]), head, actor, "s-" + harness,
                 "t", harness, self.lot))
            marks.append(dict(row, kind="verdict", at_ts=float(row["at_ts"])))
        return merge_queue.review_gate(
            self.db, marks, head, repo=self.repo.root,
            klass=klass or self.klass(), item=work.get(self.db, self.lot),
            families=FAMILIES, required=1, authors=authors,
            authors_complete=authors_complete)

    def test_a_titular_without_a_commit_does_not_forbid_their_family(self):
        # Le titulaire (claude) n'a rien écrit : un avis claude est indépendant
        # du commit d'impl (deepseek), même si le titulaire reste la référence.
        self.freeze(self.impl)
        out = self.gate(self.impl, ["r_claude"])
        self.assertTrue(out["ok"], out)
        self.assertEqual([row["name"] for row in out["authors"]], ["impl"])
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_claude"])

    def test_a_titular_who_wrote_a_commit_still_forbids_their_family(self):
        self.repo.write("src/ameesh/chat.py", "lot\nby titular\n")
        by_titular = self.repo.commit("titular: touch", agent="titular")
        self.receipt("titular", "claude", [by_titular])
        self.freeze(by_titular, actor="titular")
        out = self.gate(by_titular, ["r_claude"])
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["accepted"], [])
        self.assertEqual(sorted(row["name"] for row in out["authors"]),
                         ["impl", "titular"])

    def test_the_titular_who_authored_an_amended_commit_stays_an_author(self):
        # Un amend conserve l'auteur Git (le titulaire) et change le committer
        # (impl) : les deux ont écrit la plage. La famille du titulaire reste
        # interdite, même si son seul commit a été réécrit par un autre.
        self.repo.write("src/ameesh/chat.py", "lot\nby titular\n")
        self.repo.commit("titular: lot", agent="titular")
        env = dict(self.repo.dry, GIT_AUTHOR_NAME="titular",
                   GIT_AUTHOR_EMAIL="titular@ameesh.invalid",
                   GIT_COMMITTER_NAME="impl",
                   GIT_COMMITTER_EMAIL="impl@ameesh.invalid")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "--amend", "-m", "amend by impl", env=env)
        amended = self.repo.git("rev-parse", "HEAD")
        self.receipt("impl", "deepseek", [amended])
        self.freeze(amended)
        out = self.gate(amended, ["r_claude"])
        self.assertFalse(out["ok"], out)
        self.assertIn({"name": "titular", "family": "claude"}, out["authors"])

    def test_an_unproven_third_party_commit_keeps_the_gate_closed(self):
        # Auditeur #2808 : un titulaire qui n'a rien poussé, avec un commit
        # d'un tiers sans reçu, garde la porte fermée (pas d'indépendance
        # supposée). La provenance est inconnue : la plage n'est pas concluante.
        self.repo.write("src/ameesh/chat.py", "lot\nby ghost\n")
        ghost = self.repo.commit("ghost: touch", agent="ghost")
        self.freeze(ghost)
        out = self.gate(ghost, ["r_claude"])
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["accepted"], [])
        self.assertIn("cannot be read", out["why"])

    def test_the_train_reading_drops_the_titular_without_a_commit(self):
        # Le train lit lui-même la plage (klass sans base) et passe ses
        # auteurs complets : la porte ne doit pas y rajouter le titulaire.
        self.freeze(self.impl)
        authors = [{"name": "impl", "family": "deepseek"}]
        out = self.gate(self.impl, ["r_claude"], klass=self.klass(base=False),
                        authors=authors, authors_complete=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([row["name"] for row in out["authors"]], ["impl"])

    def test_a_partial_caller_list_still_keeps_the_reference_closed(self):
        # Sans la preuve que la liste est complète, la porte garde la
        # référence (direction stricte, #2803) : l'avis claude est refusé.
        self.freeze(self.impl)
        authors = [{"name": "impl", "family": "deepseek"}]
        out = self.gate(self.impl, ["r_claude"], klass=self.klass(base=False),
                        authors=authors)
        self.assertFalse(out["ok"], out)
        self.assertIn({"name": "titular", "family": "claude"}, out["authors"])


class FreezeInstantTest(PgTestCase):
    """`_author_at_freeze` suit le gel, pas la première assignation."""

    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).work
        self.item = work.add(self.db, title="assignation reprise", app="ameesh")
        self.lot = int(self.item["id"])
        self.ops.note(self.lot, "intake", "assigned to titular (before: nobody)",
                      "orchestrateur")
        self.ops.note(self.lot, "intake", "assigned to impl (before: titular)",
                      "orchestrateur")
        self.db.execute("UPDATE work_items SET assignee = 'impl' WHERE id = %s",
                        (self.lot,))

    def test_the_reference_follows_the_freeze_actor_not_the_first_assignee(self):
        self.ops.add_milestone(self.lot, "frozen", sha="a" * 40, actor="impl",
                               verdict=None, note="")
        ref = review_authors.reference_author(
            self.db, work.get(self.db, self.lot), None, FAMILIES)
        self.assertEqual(ref["name"], "impl")

    def test_the_current_assignee_is_the_reference_without_a_named_actor(self):
        self.ops.add_milestone(self.lot, "frozen", sha="a" * 40, actor="",
                               verdict=None, note="")
        ref = review_authors.reference_author(
            self.db, work.get(self.db, self.lot), None, FAMILIES)
        self.assertEqual(ref["name"], "impl")
        self.assertEqual(ref["family"], "")

    def test_an_older_freeze_by_the_first_assignee_does_not_replace_the_latest(self):
        self.ops.add_milestone(self.lot, "frozen", sha="a" * 40, actor="titular",
                               verdict=None, note="")
        self.ops.add_milestone(self.lot, "frozen", sha="b" * 40, actor="impl",
                               verdict=None, note="")
        ref = review_authors.reference_author(
            self.db, work.get(self.db, self.lot), None, FAMILIES)
        self.assertEqual(ref["name"], "impl")
