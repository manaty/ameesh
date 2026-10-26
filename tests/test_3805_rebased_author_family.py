# SPDX-License-Identifier: AGPL-3.0-only
"""#3805 — un commit rebasé garde la famille de son auteur Git.

Un rebase écrit un nouvel objet : le reçu exact reste sur l'ancien commit, et
le nouveau n'en a pas. Le lecteur attribuait alors le commit au seul committer
(la famille de session du journal des tours) : quand un agent B rebase le
commit d'un agent A, la famille de A disparaissait des auteurs, et la
désignation pouvait confier la relecture du contenu de A à un agent de sa
famille.

Le lecteur rend désormais :

* sans reçu exact, l'auteur Git d'agent **différent du committer** et le
  committer sont tous deux auteurs du contenu (le doute conserve, #2924) ;
* une réémission à patch-id verbatim identique d'un commit qui porte un reçu
  exact garde ce reçu : la copie n'est pas attribuée à son committer ;
* un nom Git hors registre ne prouve jamais une famille ; un objet créé hors
  d'un tour d'agent (committer hors registre) n'achète pas le reçu d'une copie
  (#3301).
"""
from __future__ import annotations

import os

from ameesh import merge_queue, registry, review_authors
from ameesh import review_designation as designation

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
AUTHOR = "authora"                     # claude
REBASER = "authorb"                    # deepseek
HEAD = "a" * 40


def _klass():
    return {"class": "sensitive", "doubt": False, "files": []}


def _gate(authors, marks, required=2):
    return merge_queue.review_gate(
        None, marks, HEAD, klass=_klass(), required=required,
        item={"assignee": REBASER},
        family_of=lambda name: {AUTHOR: "claude", REBASER: "deepseek",
                                "r_claude": "claude", "r_codex": "codex",
                                "r_deepseek": "deepseek"}.get(str(name), ""),
        authors=authors, authors_complete=True)


class _RebaseFixture(PgTestCase):
    """Base, commit de A, puis copie rebasée par B sur une base neuve."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        for name, harness in ((AUTHOR, "claude"), (REBASER, "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = {"id": 3805, "assignee": REBASER}
        self.reference = {"name": REBASER, "family": "deepseek"}
        self.reference_families = {REBASER.lower(): {"deepseek"}}
        self.original = self._original()
        self.author_stamp = self.repo.git(
            "log", "-1", "--format=%at", self.original)

    def _original(self):
        """Le commit d'origine, sur `main`, avec son auteur Git A."""
        self.repo.write("src/ameesh/work.py", "lot\n")
        return self.repo.commit("A: contenu", agent=AUTHOR)

    def rebase(self, *, text="lot\n", author=AUTHOR, receipt=False,
               keep_original=False):
        """Rejoue le contenu sur une base neuve, sans reçu sur la copie.

        L'auteur Git et sa date sont conservés (comme un vrai rebase) ; le
        committer est B. `keep_original` garde l'ancien objet atteignable par
        une ref, comme le ferait une branche locale non rebasée."""
        if keep_original:
            self.repo.git("branch", "-f", "keep-original", self.original)
        self.repo.git("checkout", "-q", "-B", "main", self.base)
        self.repo.write("src/ameesh/other.py", "base 2\n")
        new_base = self.repo.commit("base 2")
        self.repo.write("src/ameesh/work.py", text)
        env = dict(self.repo.dry,
                   GIT_AUTHOR_NAME=author,
                   GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % author,
                   GIT_AUTHOR_DATE="%s +0000" % self.author_stamp,
                   GIT_COMMITTER_NAME=REBASER,
                   GIT_COMMITTER_EMAIL="%s@ameesh.invalid" % REBASER)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "B: rebase", env=env)
        self.rebased = self.repo.git("rev-parse", "HEAD")
        self.new_base = new_base
        if receipt:
            self.receipt(self.original, AUTHOR, "claude")
        self.turnos()
        return self.rebased

    def receipt(self, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, None))

    def turnos(self):
        """Le journal des tours à l'instant de l'auteur et du committer."""
        for actor, harness, ref, fmt in (
                (AUTHOR, "claude", self.rebased, "%at"),
                (REBASER, "deepseek", self.rebased, "%ct")):
            stamp = self.repo.git("log", "-1", "--format=" + fmt, ref)
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
                " VALUES (%s, %s, %s, to_timestamp(%s))",
                (actor, harness, "s-" + harness, stamp))

    def reading(self, **kwargs):
        return review_authors.reading(
            self.db, self.item, None, [self.rebased], self.repo.root, FAMILIES,
            reference=self.reference,
            reference_families=self.reference_families,
            families_out=kwargs.pop("families_out", None),
            base=self.new_base, head=self.rebased, **kwargs)


class RebasedWithoutReceiptTest(_RebaseFixture):
    """Le témoin : A (claude) rebasé par B (deepseek) sans reçu."""

    def setUp(self):
        super().setUp()
        self.rebase()

    def test_both_families_are_authors_of_the_content(self):
        families = {}
        authors, unknown, _correctors = self.reading(families_out=families)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {AUTHOR: "claude", REBASER: "deepseek"})
        self.assertEqual(families[AUTHOR.lower()], {"claude"})
        self.assertEqual(families[REBASER.lower()], {"deepseek"})

    def test_designation_excludes_both_families(self):
        authors, _unknown, _correctors = self.reading()
        out = designation.select(
            required=2, authors=authors, families=FAMILIES,
            candidates=[designation.Candidate("r_claude", "claude", True, 0),
                        designation.Candidate("r_codex", "codex", True, 0),
                        designation.Candidate("r_deepseek", "deepseek", True, 0)])
        # Il ne reste que codex : deux avis indépendants sont impossibles.
        self.assertEqual(out["state"], designation.WAITING, out)
        self.assertEqual(out["forbidden"], ["claude", "deepseek"])

    def test_gate_receives_both_families(self):
        authors, _unknown, _correctors = self.reading()
        marks = [dict(id=1, kind="verdict", actor="r_codex", at_ts=1.0,
                      sha=HEAD, verdict="ok"),
                 dict(id=2, kind="verdict", actor="r_claude", at_ts=2.0,
                      sha=HEAD, verdict="ok")]
        out = _gate(authors, marks)
        self.assertFalse(out["ok"], out)
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["r_codex"])


class ReceiptKeptByPatchTest(_RebaseFixture):
    """Une réémission à patch-id identique garde le reçu de l'original."""

    def setUp(self):
        super().setUp()
        self.rebase(receipt=True, keep_original=True)

    def test_the_rebase_keeps_the_authors_receipt(self):
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        # A seul est auteur : le committer n'a pas réécrit le contenu.
        self.assertEqual([row["name"] for row in authors], [AUTHOR])
        self.assertEqual(authors[0]["family"], "claude")

    def test_the_two_independent_families_remain_available(self):
        authors, _unknown, _correctors = self.reading()
        marks = [dict(id=1, kind="verdict", actor="r_deepseek", at_ts=1.0,
                      sha=HEAD, verdict="ok"),
                 dict(id=2, kind="verdict", actor="r_codex", at_ts=2.0,
                      sha=HEAD, verdict="ok")]
        out = _gate(authors, marks)
        self.assertTrue(out["ok"], out)
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["r_deepseek", "r_codex"])

    def test_a_different_patch_is_a_real_rewrite(self):
        # L'audit #3720 : si le rebase adapte le contenu, B a écrit le
        # contenu ; les deux familles comptent.
        self.rebase(text="lot adapté\n", receipt=True, keep_original=True)
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {AUTHOR: "claude", REBASER: "deepseek"})


class UnregisteredGitAuthorTest(_RebaseFixture):
    """Jamais de famille devinée depuis un nom Git hors registre."""

    def setUp(self):
        super().setUp()
        self.rebase(author="ghost")

    def test_the_ghost_author_is_not_read(self):
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in authors], [REBASER])
        self.assertEqual(authors[0]["family"], "deepseek")


class NonAgentCommitterTest(_RebaseFixture):
    """Un objet créé hors tour d'agent n'achète pas le reçu d'une copie (#3301)."""

    def setUp(self):
        super().setUp()
        self.rebase(receipt=True, keep_original=True)
        self.repo.git("checkout", "-q", "-B", "unmanaged", self.base)
        self.repo.write("src/ameesh/work.py", "lot\n")
        env = dict(self.repo.dry,
                   GIT_AUTHOR_NAME=AUTHOR,
                   GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % AUTHOR,
                   GIT_AUTHOR_DATE="%s +0000" % self.author_stamp,
                   GIT_COMMITTER_NAME="operator",
                   GIT_COMMITTER_EMAIL="operator@example.invalid")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "unmanaged copy", env=env)
        self.unmanaged = self.repo.git("rev-parse", "HEAD")

    def test_the_unmanaged_copy_stays_unknown(self):
        authors, unknown, _correctors = review_authors.reading(
            self.db, self.item, None, [self.unmanaged], self.repo.root,
            FAMILIES, reference=self.reference,
            reference_families=self.reference_families,
            base=self.base, head=self.unmanaged)
        self.assertEqual(authors, [])
        self.assertEqual(unknown, [self.unmanaged[:12]])


class _AmbiguousReceiptFixture(_RebaseFixture):
    """Deux réémissions du même patch, reçues sous des harnais différents.

    Le même contenu, le même auteur et la même date d'auteur portent deux reçus
    (claude et codex) ; l'ordre de `git log` est fixé par la date de committer
    des deux originaux. Le lecteur doit garder **les deux** familles, jamais la
    première rencontrée (relecture cx1, #3805).
    """

    #: le reçu codex est-il le plus récent (donc vu en premier par git log) ?
    CODEX_FIRST = True

    def setUp(self):
        super().setUp()
        self.second = self._second_original(newer=self.CODEX_FIRST)
        self.rebase(receipt=False, keep_original=True)
        self.receipt(self.original, AUTHOR, "claude")
        self.receipt(self.second, AUTHOR, "codex")

    def _second_original(self, *, newer):
        first_ct = int(self.repo.git("log", "-1", "--format=%ct",
                                     self.original))
        self.repo.git("checkout", "-q", "-B", "main", self.base)
        self.repo.write("src/ameesh/work.py", "lot\n")
        env = dict(self.repo.dry,
                   GIT_AUTHOR_NAME=AUTHOR,
                   GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % AUTHOR,
                   GIT_AUTHOR_DATE="%s +0000" % self.author_stamp,
                   GIT_COMMITTER_NAME=AUTHOR,
                   GIT_COMMITTER_EMAIL="%s@ameesh.invalid" % AUTHOR,
                   GIT_COMMITTER_DATE="%d +0000"
                   % (first_ct + (100 if newer else -100)))
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "A: contenu (reçu codex)", env=env)
        second = self.repo.git("rev-parse", "HEAD")
        self.repo.git("branch", "-f", "keep-second", second)
        return second

    def test_all_receipt_families_are_kept_whatever_the_log_order(self):
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual(len(authors), 1)
        row = authors[0]
        self.assertEqual(row["name"], AUTHOR)
        # Deux familles prouvées : ambiguïté transportée, pas devinée.
        self.assertEqual(row["family"], "")
        self.assertEqual(row["families"], ["claude", "codex"])


class AmbiguousReceiptCodexFirstTest(_AmbiguousReceiptFixture):
    CODEX_FIRST = True


class AmbiguousReceiptClaudeFirstTest(_AmbiguousReceiptFixture):
    CODEX_FIRST = False
