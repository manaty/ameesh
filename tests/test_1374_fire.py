# SPDX-License-Identifier: AGPL-3.0-only
"""#1374 : le feu du pilote au gel — atterrissage automatique à la CI verte.

Le feu se donne sur la tête exacte du gel (`ameesh work fire <lot> --sha S`),
se retire (`--withdraw`), et ne vaut jamais pour un autre gel. Le passage
(`fire.run_pass`) franchit **d'abord la porte des verdicts** (avis comptés par
classe, indépendants, sans blocage non levé), puis ouvre la PR ou entre en file,
puis fusionne au geste effectif sous `--match-head-commit` — jamais en laissant
une auto-fusion GitHub armée entre deux passages.

Témoins demandés par le lot : lot sans feu, mauvaise tête, feu retiré, CI
incomplète ou rouge, événement de fusion terminal ; témoins du gel corrigé :
verdicts avant PR/file/CI, avis indépendants et blocages non masqués, sortie de
file et garde de `land` après retrait, jalons relus sous bail, bail renouvelé,
et autorité pilote déclarée. Tests sans base (routage, jalons, GitHub simulé),
puis sur Postgres (règles de `give`/`withdraw`, bail, CLI, garde de `land`)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import fire, land, merge_queue, storage, work

from .support import PgTestCase

A = "a" * 40
B = "b" * 40

#: familles simulées pour la porte des avis (le registre est lu en vrai, ou
#: remplacé par ce dictionnaire dans les tests sans base).
FAM = {"author": "deepseek", "rev1": "codex", "rev2": "claude", "rev3": "codex",
       "ghost": "", "other": "deepseek"}


def _mark(kind, sha="", **extra):
    row = {"kind": kind, "sha": sha, "actor": "pilote", "at_ts": 0.0, "id": 0}
    row.update(extra)
    return row


def _marks(*rows):
    return [dict(row, at_ts=float(i), id=i) for i, row in enumerate(rows, start=1)]


def _verdict(actor, sha=A, verdict="ok", **extra):
    return _mark("verdict", sha, verdict=verdict, actor=actor, **extra)


def _status(**extra):
    row = {"number": 7, "state": "OPEN", "head": A, "base": "main", "draft": False,
           "ci": "green", "merged": False, "merge_commit": "", "armed": False,
           "url": "https://forge/pr/7"}
    row.update(extra)
    return row


def _info(**extra):
    row = {"slug": "manaty/ameesh", "allow_auto_merge": True, "default_branch": "main",
           "private": True, "merge": True, "squash": True, "rebase": True}
    row.update(extra)
    return row


def _family(db, name):
    return FAM.get(str(name or "").strip(), "")


class MarkersTest(unittest.TestCase):
    """Les jalons du feu : le dernier `fire` non suivi d'un `unfire`."""

    def test_active_and_given(self):
        rows = _marks(_mark("frozen", A), _mark("fire", A, actor="pilote"))
        self.assertEqual(fire.active(rows)["sha"], A)
        self.assertEqual(fire.given(rows)["actor"], "pilote")
        withdrawn = _marks(_mark("frozen", A), _mark("fire", A), _mark("unfire", A))
        self.assertIsNone(fire.active(withdrawn))
        self.assertEqual(fire.given(withdrawn)["sha"], A)
        again = _marks(_mark("frozen", B), _mark("fire", A), _mark("unfire", A),
                       _mark("fire", B))
        self.assertEqual(fire.active(again)["sha"], B)

    def test_same_commit_without_repository(self):
        self.assertTrue(fire.same_commit(A, A.upper()))
        self.assertFalse(fire.same_commit(A, B))
        self.assertFalse(fire.same_commit("", A))

    def test_state_covered_and_withdrawn(self):
        item = {"id": 3, "title": "t", "state": "build", "branch": "lot/3-x"}
        covered = _marks(_mark("frozen", A), _mark("fire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=covered):
            now = fire.state(None, 3)
        self.assertTrue(now["active"])
        self.assertTrue(now["covered"])
        moved = _marks(_mark("frozen", B), _mark("fire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=moved):
            now = fire.state(None, 3)
        self.assertTrue(now["active"])
        self.assertFalse(now["covered"])
        gone = _marks(_mark("frozen", A), _mark("fire", A), _mark("unfire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=gone):
            now = fire.state(None, 3)
        self.assertFalse(now["active"])
        self.assertTrue(now["withdrawn"])


class ReviewGateTest(unittest.TestCase):
    """La porte des avis (#1374, P1-1) : comptés par classe et par domaine
    (SQL/sécurité/prod = deux avis), indépendants, aucun blocage masqué par un
    `ok` plus récent d'un AUTRE relecteur."""

    ITEM = {"assignee": "author", "id": 42}
    #: un fichier sensible hors SQL/sécurité/prod : un avis d'une autre famille
    PLAIN = {"path": "docs/design/notes.md", "class": "sensitive", "rule": ["docs/**"]}
    #: un fichier SQL : deux avis, familles distinctes
    SQL = {"path": "src/ameesh/migrations/0437_x.sql", "class": "sensitive",
           "rule": ["src/ameesh/migrations/**", "**/*.sql"]}
    #: la porte de fusion est de la sécurité : deux avis
    GATE = {"path": "src/ameesh/merge_queue.py", "class": "sensitive",
            "rule": ["src/ameesh/merge_queue.py"]}

    def gate(self, marks, klass=None, frozen=A, item=None, db=None, repo="/fake/repo",
             events=None, delta=()):
        """La porte avec ses sources simulées : un journal vide, aucun auteur
        de delta, un dépôt factice (la provenance réelle est testée à part).

        `events` et `delta` remplacent les lectures correspondantes : le
        helper patche `work.events` et `_delta_authors`, donc un test qui les
        remplace le fait ici, pas par un patch extérieur (qui serait écrasé).
        """
        if isinstance(klass, str):
            klass = {"class": klass, "doubt": False}
            if klass["class"] == "sensitive":
                klass["files"] = [self.PLAIN]

        def resolve(_repo, sha):
            sha = str(sha or "").strip().lower()
            return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None

        with mock.patch.object(merge_queue, "actor_family", side_effect=_family), \
                mock.patch.object(merge_queue, "pinned", side_effect=resolve), \
                mock.patch.object(merge_queue.work, "events",
                                  return_value=list(events or [])), \
                mock.patch.object(merge_queue, "_delta_authors",
                                  return_value=list(delta)):
            return merge_queue.review_gate(db if db is not None else object(), marks, frozen,
                                           repo=repo,
                                           klass=klass or {"class": "sensitive",
                                                           "doubt": False,
                                                           "files": [self.PLAIN]},
                                           item=item or self.ITEM)

    def test_light_needs_no_verdict(self):
        out = self.gate(_marks(_mark("frozen", A)), klass="light")
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["required"], 0)

    def test_blocked_blocks_even_a_light_lot(self):
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1", verdict="blocked")),
                        klass="light")
        self.assertFalse(out["ok"])
        self.assertIn("blocked verdict", out["why"])
        self.assertIn("rev1", out["why"])

    def test_normal_needs_one_reviewer_that_is_not_the_author(self):
        out = self.gate(_marks(_mark("frozen", A)), klass="normal")
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        out = self.gate(_marks(_mark("frozen", A), _verdict("author")), klass="normal")
        self.assertFalse(out["ok"], "l'auteur ne relit pas son propre travail")
        out = self.gate(_marks(_mark("frozen", A), _verdict("ghost")), klass="normal")
        self.assertTrue(out["ok"], out["why"])

    def test_sensitive_outside_critical_domains_needs_one_other_family(self):
        out = self.gate(_marks(_mark("frozen", A)))
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1")))
        self.assertTrue(out["ok"], out["why"])
        unknown = self.gate(_marks(_mark("frozen", A), _verdict("ghost")))
        self.assertFalse(unknown["ok"], "une famille inconnue ne prouve pas l'indépendance")
        same = self.gate(_marks(_mark("frozen", A), _verdict("other")))
        self.assertFalse(same["ok"], "la famille de l'auteur ne compte pas")

    def test_sql_and_merge_gate_need_two_distinct_families(self):
        for entry in (self.SQL, self.GATE):
            klass = {"class": "sensitive", "files": [entry]}
            one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
            self.assertFalse(one["ok"], entry["path"])
            self.assertEqual(one["required"], 2, entry["path"])
            two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                            klass=klass)
            self.assertTrue(two["ok"], two["why"])
            same = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev3")),
                             klass=klass)
            self.assertFalse(same["ok"], "deux avis de la même famille ne comptent qu'une fois")
            unknown = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("ghost")),
                                klass=klass)
            self.assertFalse(unknown["ok"], "une famille inconnue ne prouve pas l'indépendance")

    def test_author_family_never_counts(self):
        klass = {"class": "sensitive", "files": [self.SQL]}
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("other")),
                        klass=klass)
        self.assertFalse(out["ok"], "l'avis de la famille de l'auteur ne compte pas")
        self.assertEqual([a["actor"] for a in out["accepted"]], ["rev1"])

    def test_a_reassigned_lot_excludes_its_previous_author(self):
        # #1515 N1 : le journal dit les assignés précédents ; un lot réassigné
        # ne peut pas compter l'avis de l'auteur d'origine (rev2, famille
        # claude : sans la lecture du journal, son avis passerait en normal).
        item = {"assignee": "author", "id": 42}
        events = [{"note": "assigned to author (before: rev2)"}]
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev2")),
                        klass="normal", item=item, events=events)
        self.assertFalse(out["ok"], "un ancien assigné reste auteur")
        self.assertEqual(out["authors"], ["author", "rev2"])
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1")),
                        klass="normal", item=item, events=events)
        self.assertTrue(out["ok"], out["why"])

    def test_delegation_notes_name_the_delegate_and_the_previous_holder(self):
        # #1515 B1 : une délégation est un changement de détenteur comme une
        # assignation ; le délégataire et l'ancien détenteur sont auteurs.
        events = [{"note": "delegated to rev1 by author, due in 30 min (before: rev2)"}]
        with mock.patch.object(merge_queue.work, "events", return_value=events):
            self.assertEqual(merge_queue._assignees(object(), {"assignee": "author", "id": 7}),
                             ["author", "rev1", "rev2"])
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev2")),
                        klass="normal", item={"assignee": "author", "id": 7},
                        events=events)
        self.assertFalse(out["ok"], "l'ancien détenteur délégué ne relit pas le travail")
        self.assertIn("rev1", out["authors"])
        self.assertIn("rev2", out["authors"])
        ok = self.gate(_marks(_mark("frozen", A), _verdict("rev3")),
                       klass="normal", item={"assignee": "author", "id": 7},
                       events=events)
        self.assertTrue(ok["ok"], ok["why"])

    def test_journal_is_read_whole_beyond_the_first_page(self):
        # #1515 B1 : un ancien détenteur au-delà de la première page ne doit
        # pas disparaître ; la lecture élargit tant que la page est pleine.
        filler = [{"note": "note sans rapport"} for _ in range(merge_queue.JOURNAL_PAGE)]
        old = {"note": "assigned to rev2 (before: nobody)"}
        seen = []

        def events(_db, _item, limit):
            seen.append(limit)
            return filler + [old] if limit > merge_queue.JOURNAL_PAGE else filler

        with mock.patch.object(merge_queue.work, "events", side_effect=events):
            self.assertEqual(merge_queue._assignees(object(), {"assignee": "author", "id": 7}),
                             ["author", "rev2"])
        self.assertGreater(len(seen), 1, "la première page pleine doit être élargie")
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev2")),
                        klass="normal", item={"assignee": "author", "id": 7},
                        events=filler + [old])
        self.assertFalse(out["ok"], "un détenteur de la deuxième page reste auteur")

    def test_assignees_reads_the_whole_journal(self):
        events = [{"note": "assigned to b (before: a)"},
                  {"note": "assigned to a (before: nobody)"},
                  {"note": "note sans rapport"}]
        with mock.patch.object(merge_queue.work, "events", return_value=events):
            self.assertEqual(merge_queue._assignees(object(), {"assignee": "c", "id": 7}),
                             ["c", "b", "a"])

    def test_unreadable_journal_refuses_the_gate(self):
        # #1515 B1 : un journal illisible ne vaut pas « aucun ancien
        # détenteur » : la provenance est incomplète, la porte refuse.
        with mock.patch.object(merge_queue.work, "events",
                               side_effect=RuntimeError("journal down")):
            out = merge_queue.review_gate(
                object(), _marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                A, repo="/fake/repo",
                klass={"class": "sensitive", "files": [self.SQL]},
                item={"assignee": "author", "id": 42}, target="main")
        self.assertFalse(out["ok"])
        self.assertIn("complete authors", out["why"])
        self.assertIn("unreadable", out["why"])

    def test_a_missing_repository_refuses_a_verdict_class(self):
        # #1515 B1 : sans dépôt, les auteurs des commits du delta ne sont pas
        # établis ; l'indépendance ne se suppose pas.
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass={"class": "sensitive", "files": [self.SQL]}, repo=None)
        self.assertFalse(out["ok"])
        self.assertIn("complete authors", out["why"])
        self.assertIn("no repository", out["why"])

    def test_an_unreadable_frozen_head_refuses_the_gate(self):
        # #1515 B1 : un gel absent du dépôt n'est pas un delta vide.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", tmp, "init", "-q", "-b", "main"], check=True)
            with mock.patch.object(merge_queue.work, "events", return_value=[]):
                out = merge_queue.review_gate(
                    object(), _marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                    A, repo=tmp,
                    klass={"class": "sensitive", "files": [self.SQL]},
                    item={"assignee": "author", "id": 42}, target="main")
        self.assertFalse(out["ok"])
        self.assertIn("complete authors", out["why"])
        self.assertIn("frozen head", out["why"])

    def test_an_unreadable_git_log_refuses_the_gate(self):
        # #1515 B1 : `git log` en échec ne rend pas une liste d'auteurs vide.
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args, name="author"):
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
                env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=name + "@example.invalid",
                           GIT_COMMITTER_NAME=name,
                           GIT_COMMITTER_EMAIL=name + "@example.invalid")
                return subprocess.run(["git", "-C", tmp, *args], check=True,
                                      capture_output=True, text=True, env=env).stdout.strip()

            git("init", "-q", "-b", "main")
            with open(os.path.join(tmp, "a"), "w", encoding="utf-8") as fh:
                fh.write("a\n")
            git("add", "a")
            git("commit", "-qm", "a")
            git("checkout", "-qb", "lot")
            with open(os.path.join(tmp, "a"), "w", encoding="utf-8") as fh:
                fh.write("b\n")
            git("commit", "-qam", "b", name="rev1")
            frozen = git("rev-parse", "HEAD")
            real = merge_queue.plan_git._git

            def failing(repo, *args, **kwargs):
                if args and args[0] == "log":
                    return subprocess.CompletedProcess(["git", "log"], 1, b"", b"log broke")
                return real(repo, *args, **kwargs)

            with mock.patch.object(merge_queue.plan_git, "_git", side_effect=failing), \
                    mock.patch.object(merge_queue.work, "events", return_value=[]):
                out = merge_queue.review_gate(
                    object(), _marks(_mark("frozen", frozen), _verdict("rev1"),
                                     _verdict("rev2")),
                    frozen, repo=tmp,
                    klass={"class": "sensitive", "files": [self.SQL]},
                    item={"assignee": "author", "id": 42}, target="main")
        self.assertFalse(out["ok"])
        self.assertIn("complete authors", out["why"])
        self.assertIn("unreadable", out["why"])

    def test_a_corrector_is_an_author_not_a_reviewer(self):
        # #1515 N1 : le relecteur qui corrige lui-même (§2) devient auteur du
        # delta ; son avis ne relit pas sa propre correction.
        item = {"assignee": "author", "id": 43}
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1")),
                        klass="normal", item=item, delta=["rev1"])
        self.assertFalse(out["ok"], "le correcteur relit sa propre correction")
        self.assertEqual(out["authors"], ["author", "rev1"])
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass="normal", item=item, delta=["rev1"])
        self.assertTrue(out["ok"], out["why"])

    def test_a_human_reviewer_has_no_model_family_to_count(self):
        # #1515 (dev4) : une famille humaine n'existe pas. En classe sensible,
        # l'avis d'un humain ne prouve pas une famille de modèle indépendante ;
        # il ne remplace pas l'un des deux avis requis (canon §2).
        out = self.gate(_marks(_mark("frozen", A), _verdict("human:alice")),
                        klass="sensitive", item={"assignee": "author", "id": 46})
        self.assertFalse(out["ok"])
        self.assertEqual(out["accepted"], [])

    def test_an_unknown_commit_author_does_not_block_the_gate(self):
        # Un outil ou un humain peut signer un commit du delta : son nom est
        # exclu des relecteurs, mais il n'a pas de famille à comparer et ne
        # doit pas refuser la porte (contrairement à un assigné hors registre).
        item = {"assignee": "author", "id": 44}
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1")),
                        klass="sensitive", item=item, delta=["ameesh"])
        self.assertTrue(out["ok"], out["why"])

    def test_the_gate_participants_need_two_reviews(self):
        # #1515 N2 : lot_class/review_lot/radar/work peuvent affaiblir la
        # porte (classe, relecteurs, état CI, correcteur) : ils sont critiques.
        for path in ("src/ameesh/lot_class.py", "src/ameesh/review_lot.py",
                     "src/ameesh/radar.py", "src/ameesh/work.py"):
            klass = {"class": "sensitive", "files": [
                {"path": path, "class": "sensitive", "rule": [path]}]}
            one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
            self.assertFalse(one["ok"], path)
            self.assertEqual(one["required"], 2, path)

    def test_a_doubt_never_supposes_a_family_and_needs_two(self):
        # B/P1 cx1 (57812f9b..979f2e2b) : un doute (politique ou diff illisible)
        # peut cacher SQL/la sécurité/la prod ; il garde donc les deux avis des
        # domaines critiques et l'indépendance ne s'y suppose jamais.
        klass = {"class": "sensitive", "doubt": True, "files": []}
        out = self.gate(_marks(_mark("frozen", A)), klass=klass)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
        self.assertFalse(one["ok"], "un avis ne suffit pas sur un doute")
        ghost = self.gate(_marks(_mark("frozen", A), _verdict("ghost"), _verdict("rev1")),
                          klass=klass)
        self.assertFalse(ghost["ok"], "une famille inconnue ne prouve pas l'indépendance")
        two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass=klass)
        self.assertTrue(two["ok"], two["why"])
        out = self.gate(_marks(_mark("frozen", A), _verdict("author")), klass=klass)
        self.assertFalse(out["ok"], "l'auteur ne relit pas son propre travail")

    def test_an_unknown_author_family_never_proves_independence(self):
        # B/P1 cx1 : quand la famille de l'auteur n'est pas connue, l'ancien
        # code sautait la comparaison et acceptait n'importe quelle famille.
        klass = {"class": "sensitive", "files": [self.SQL]}
        item = {"assignee": "inconnu", "id": 45}       # famille absente du registre simulé
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass=klass, item=item)
        self.assertFalse(out["ok"], "l'indépendance ne se suppose jamais")
        self.assertIn("no known model family", out["why"])
        self.assertEqual(out["required"], 2)
        plain = {"class": "sensitive", "doubt": False, "files": [self.PLAIN]}
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")),
                        klass=plain, item=item)
        self.assertFalse(one["ok"], "même un avis unique exige une famille d'auteur connue")

    def test_a_human_author_has_no_model_family_to_compare(self):
        # un auteur humain n'a pas de famille de modèle : il n'y a rien à
        # comparer, seules les familles des relecteurs comptent (l'exemption
        # ne vaut que pour `human:`, jamais pour un agent au registre muet).
        klass = {"class": "sensitive", "files": [self.SQL]}
        item = {"assignee": "human:alice", "id": 47}
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass, item=item)
        self.assertFalse(one["ok"], "deux avis restent requis pour SQL")
        two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass=klass, item=item)
        self.assertTrue(two["ok"], two["why"])
        same = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev3")),
                         klass=klass, item=item)
        self.assertFalse(same["ok"], "deux avis de la même famille ne comptent qu'une fois")

    def test_normal_lot_keeps_a_verdict_without_a_family(self):
        # hors classe sensible, un lot normal garde un avis d'un autre agent,
        # même de famille inconnue : la famille n'y est pas exigée.
        out = self.gate(_marks(_mark("frozen", A), _verdict("ghost")),
                        klass="normal", item={"assignee": "inconnu", "id": 48})
        self.assertTrue(out["ok"], out["why"])

    def test_a_newer_ok_from_another_reviewer_does_not_mask_a_block(self):
        klass = {"class": "sensitive", "files": [self.SQL]}
        marks = _marks(_mark("frozen", A), _verdict("rev1", verdict="blocked"),
                       _verdict("rev2", verdict="ok"))
        out = self.gate(marks, klass=klass)
        self.assertFalse(out["ok"])
        self.assertIn("rev1", out["why"])
        lifted = _marks(_mark("frozen", A), _verdict("rev1", verdict="blocked"),
                        _verdict("rev2"), _verdict("rev1", verdict="ok"))
        out = self.gate(lifted, klass=klass)
        self.assertTrue(out["ok"], out["why"])

    def test_verdict_on_another_freeze_is_ignored(self):
        out = self.gate(_marks(_mark("frozen", B), _verdict("rev1", sha=B),
                               _verdict("rev2", sha=B)))
        self.assertFalse(out["ok"], "un avis sur un autre gel n'autorise rien")

    def test_release_keeps_the_verdict_in_the_queue_gate(self):
        # la file garde un avis pour une release (jamais légère, L160e) ; c'est
        # le **feu** qui refuse une release (test_release_is_refused, ReadyTest).
        out = self.gate(_marks(_mark("frozen", A)), klass="release")
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        self.assertIn("class: release", out["why"])
        ok = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass="release")
        self.assertTrue(ok["ok"], ok["why"])


class DeltaAuthorsTest(unittest.TestCase):
    """#1515 N1/B2/B3 : les auteurs du delta gelé, lus depuis la cible
    effective (refs privées comprises), fusions qui ajoutent du contenu
    incluses ; un correcteur qui pousse sur la branche devient auteur."""

    @staticmethod
    def _git(tmp):
        def git(*args, name="author"):
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
            env.update(GIT_AUTHOR_NAME=name,
                       GIT_AUTHOR_EMAIL=name + "@example.invalid",
                       GIT_COMMITTER_NAME=name,
                       GIT_COMMITTER_EMAIL=name + "@example.invalid")
            proc = subprocess.run(["git", "-C", tmp, *args], check=True,
                                  capture_output=True, text=True, env=env)
            return proc.stdout.strip()
        return git

    @staticmethod
    def _write(tmp, name: str, text: str) -> None:
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _base(self, tmp, git):
        git("init", "-q", "-b", "main")
        self._write(tmp, "a", "a\n")
        git("add", "a")
        git("commit", "-qm", "a", name="author")

    def test_a_corrector_commit_is_an_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "b\n")
            git("commit", "-qam", "b", name="rev1")
            frozen = git("rev-parse", "HEAD")
            self.assertEqual(
                merge_queue._delta_authors(tmp, {"branch_target": "main"}, frozen),
                ["rev1"])

    def test_a_merge_commit_that_adds_content_is_an_author(self):
        # #1515 B3 : le correcteur qui résout un conflit dans le commit de
        # fusion ajoute du contenu ; il est auteur du delta et ne peut pas
        # compter son propre avis.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "lot\n")
            git("commit", "-qam", "lot", name="author")
            git("checkout", "-q", "main")
            self._write(tmp, "a", "main\n")
            git("commit", "-qam", "main", name="other")
            git("checkout", "-q", "lot")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
            env.update(GIT_AUTHOR_NAME="rev1", GIT_AUTHOR_EMAIL="rev1@example.invalid",
                       GIT_COMMITTER_NAME="rev1",
                       GIT_COMMITTER_EMAIL="rev1@example.invalid")
            subprocess.run(["git", "-C", tmp, "merge", "main"], capture_output=True,
                           text=True, env=env, check=False)
            self._write(tmp, "a", "resolved\n")
            git("add", "a")
            git("commit", "-qm", "merge main", name="rev1")
            frozen = git("rev-parse", "HEAD")
            authors = merge_queue._delta_authors(tmp, {"branch_target": "main"}, frozen)
            self.assertIn("rev1", authors, "l'auteur de la résolution est un auteur")
            self.assertIn("author", authors)

    def test_the_effective_target_is_the_base_not_origin_main(self):
        # #1515 B2 : projet develop, lot sans cible ; main porte déjà le gel.
        # La cible effective develop doit rendre rev1 auteur, main ne le fait
        # pas — c'est la cible du feu/readiness qui compte, jamais origin/main.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "develop")
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "b\n")
            git("commit", "-qam", "b", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("checkout", "-q", "main")
            git("merge", "--ff-only", "-q", "lot")
            self.assertEqual(
                merge_queue._delta_authors(tmp, {"branch_target": "main"}, frozen,
                                           target="develop"),
                ["rev1"])
            self.assertEqual(
                merge_queue._delta_authors(tmp, {"branch_target": "main"}, frozen),
                [], "main porte déjà le gel : le delta y est vide")
            marks = _marks(_mark("frozen", frozen), _verdict("rev1", sha=frozen))
            item = {"assignee": "author", "id": 42, "branch_target": "main"}
            klass = {"class": "sensitive", "files": [
                {"path": "docs/x.md", "class": "sensitive", "rule": ["docs/**"]}]}
            with mock.patch.object(merge_queue.work, "events", return_value=[]), \
                    mock.patch.object(merge_queue, "actor_family", side_effect=_family):
                wrong = merge_queue.review_gate(object(), marks, frozen, repo=tmp,
                                                klass=klass, item=item)
                right = merge_queue.review_gate(object(), marks, frozen, repo=tmp,
                                                klass=klass, item=item, target="develop")
            self.assertTrue(wrong["ok"], wrong["why"])
            self.assertFalse(right["ok"], "sur develop, rev1 est l'auteur du delta")
            self.assertIn("rev1", right["authors"])

    def test_a_local_target_ahead_of_origin_does_not_hide_a_corrector(self):
        # Le feu passe une cible sans préfixe. Un montage local non publié
        # ne prouve pas que la correction est déjà dans la cible distante.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "develop")
            git("update-ref", "refs/remotes/origin/develop", "HEAD")
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "correction\n")
            git("commit", "-qam", "correction", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("branch", "-f", "develop", frozen)
            self.assertEqual(
                merge_queue._delta_authors(tmp, {}, frozen, target="develop"),
                ["rev1"], "la branche locale masque le correcteur")
            self.assertEqual(
                merge_queue._delta_authors(tmp, {}, frozen, target="refs/heads/develop"),
                [], "une cible locale explicite reste respectée")

    def test_the_private_queue_ref_is_honoured(self):
        # #1515 B2 : la ref privée de la file est fraîche ; la branche locale
        # peut être en avance (ou en retard), c'est la ref privée qui fait foi.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "develop")
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "b\n")
            git("commit", "-qam", "b", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("checkout", "-q", "develop")
            base = git("rev-parse", "HEAD")
            git("checkout", "-q", "main")
            git("branch", "-f", "develop", frozen)
            git("update-ref", "refs/ameesh/queue/develop", base)
            self.assertEqual(
                merge_queue._delta_authors(tmp, {}, frozen, target="develop"),
                ["rev1"])

    def test_an_unreadable_target_is_an_error_not_an_empty_delta(self):
        # #1515 B1/B2 : une cible introuvable n'est pas « aucun auteur ».
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            frozen = git("rev-parse", "HEAD")
            with self.assertRaises(merge_queue._ProvenanceError):
                merge_queue._delta_authors(tmp, {}, frozen, target="nope")


class ReadyTest(unittest.TestCase):
    """Les portes du feu : verdicts d'abord (sans PR), puis PR/CI."""

    ROW = {"id": 42, "title": "t", "branch": "lot/42-x", "assignee": "author"}

    def _verdicts(self, marks, klass, *, repo="/fake/repo", project=None, target="main"):
        # Le dépôt factice et les sources simulées suffisent : la provenance
        # réelle (journal, delta, cible) est éprouvée dans ReviewGateTest et
        # DeltaAuthorsTest ; ici on teste le routage des portes du feu.
        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass), \
                mock.patch.object(merge_queue, "actor_family", side_effect=_family), \
                mock.patch.object(merge_queue, "pinned", return_value=A), \
                mock.patch.object(merge_queue.work, "events", return_value=[]), \
                mock.patch.object(merge_queue, "_delta_authors", return_value=[]):
            return fire.ready_verdicts(None, object(), self.ROW, marks, repo=repo,
                                       project=project, target=target)

    def test_the_verdict_gate_receives_the_effective_target(self):
        # #1515 B2 : la cible résolue par le feu (project develop, par
        # exemple) doit atteindre la porte, pas retomber sur origin/main.
        with mock.patch.object(fire.lot_class, "of_lot", return_value={"class": "light"}), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True, "why": "", "required": 0}) as gate:
            ok, why, _ = fire.ready_verdicts(None, object(), self.ROW, [],
                                             repo="/fake/repo", project=None,
                                             target="develop")
        self.assertTrue(ok, why)
        self.assertEqual(gate.call_args.kwargs.get("target"), "develop")

    def test_light_passes_without_a_pr(self):
        ok, why, klass = self._verdicts(_marks(_mark("frozen", A)), {"class": "light"})
        self.assertTrue(ok, why)
        self.assertEqual(klass["class"], "light")

    def test_sensitive_requires_a_family_before_any_pr(self):
        marks = _marks(_mark("frozen", A))
        klass = {"class": "sensitive", "doubt": False, "files": [
            {"path": "docs/x.md", "class": "sensitive", "rule": ["docs/**"]}]}
        ok, why, _ = self._verdicts(marks, klass)
        self.assertFalse(ok)
        self.assertIn("independent review", why)
        good = _marks(_mark("frozen", A), _verdict("rev1"))
        ok, why, _ = self._verdicts(good, klass)
        self.assertTrue(ok, why)

    def test_sql_needs_two_families_before_any_pr(self):
        klass = {"class": "sensitive", "files": [
            {"path": "src/ameesh/migrations/0437_x.sql", "class": "sensitive",
             "rule": ["**/*.sql"]}]}
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1")), klass)
        self.assertFalse(ok)
        self.assertIn("2 independent review", why)
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1"),
                                           _verdict("rev2")), klass)
        self.assertTrue(ok, why)

    def test_release_is_refused(self):
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1")),
                                    {"class": "release"})
        self.assertFalse(ok)
        self.assertIn("release", why)

    def test_pr_gates_need_a_green_ci_on_the_frozen_head(self):
        for ci in ("pending", "red"):
            ok, why = fire.ready_pr(_status(ci=ci), target="main", project=None, frozen=A)
            self.assertFalse(ok)
            self.assertIn(ci, why)
        ok, why = fire.ready_pr(_status(draft=True), target="main", project=None, frozen=A)
        self.assertFalse(ok)
        self.assertIn("draft", why)
        ok, why = fire.ready_pr(_status(base="develop"), target="main", project=None, frozen=A)
        self.assertFalse(ok)
        self.assertIn("targets", why)
        ok, why = fire.ready_pr(_status(), target="main", project=None, frozen=A)
        self.assertTrue(ok, why)


class TargetKeyTest(unittest.TestCase):
    def test_branch_aliases_share_the_target_lease(self):
        project = types.SimpleNamespace(name="ameesh")
        for target in ("main", "origin/main", "refs/heads/main", "refs/remotes/origin/main"):
            with self.subTest(target=target):
                self.assertEqual(land.lock_key(project, target), "ameesh:main")

    def test_target_lease_takes_the_train_ttl(self):
        """Le bail de cible se prend sous `land.LOCK_TTL_S` (le TTL du train),
        pas sous le TTL court du bail de feu."""
        seen = []

        class Ops:
            def land_lock(self, key, owner, ttl, *, epoch=None):
                seen.append((key, ttl, epoch))
                return 7

        ops = Ops()
        project = types.SimpleNamespace(name="ameesh")
        with mock.patch.object(fire.storage, "of", return_value=mock.Mock(work=ops)):
            handle, blocked = fire._target_lock(mock.Mock(), project, "main")
        self.assertFalse(blocked)
        self.assertEqual(seen[0], ("ameesh:main", land.LOCK_TTL_S, None))
        self.assertTrue(fire._renew(handle, land.LOCK_TTL_S))
        self.assertEqual(seen[1], ("ameesh:main", land.LOCK_TTL_S, 7))
        if land.LOCK_TTL_S != fire.LOCK_TTL_S:
            self.assertNotEqual(seen[0][1], fire.LOCK_TTL_S)


class ActTest(unittest.TestCase):
    """Le routage d'un passage : verdicts avant PR/file/CI, fusion au geste
    effectif, jamais d'auto-fusion laissée armée, jamais deux gestes
    concurrents."""

    ROW = {"id": 42, "title": "un lot", "branch": "lot/42-x", "assignee": "author",
           "app": "ameesh", "branch_target": "main", "state": "build"}

    def _act(self, marks, status, *, gate=(True, "", {"class": "light"}), pr_gate=(True, ""),
             info=None, project=None, dry_run=False, opened=None, fresh=None, renew=True,
             statuses=None, merge=(True, "PR merged"), target_blocked=False):
        names = ("_repo_of", "_slug", "_project", "_pr_status", "_repo_info", "_target",
                 "ready_verdicts", "ready_pr", "_merge_pr", "_disarm_auto", "_reconcile",
                 "_invalidate", "_disarm", "_open_pr", "_queue", "_passive", "_wake",
                 "_leave_queue", "_fresh", "_renew", "_target_lock", "_unlock")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched:
            patched["_repo_of"].return_value = None
            patched["_slug"].return_value = "manaty/ameesh"
            patched["_project"].return_value = project
            if statuses is not None:
                patched["_pr_status"].side_effect = list(statuses)
            else:
                patched["_pr_status"].return_value = status
            patched["_repo_info"].return_value = info if info is not None else _info()
            patched["_target"].return_value = "main"
            patched["ready_verdicts"].return_value = gate
            patched["ready_pr"].return_value = pr_gate
            patched["_merge_pr"].return_value = merge
            patched["_disarm_auto"].return_value = (True, "auto-merge disarmed")
            patched["_reconcile"].return_value = {"lot": 42, "result": "merged"}
            patched["_invalidate"].return_value = {"lot": 42, "result": "invalidated"}
            patched["_disarm"].return_value = {"lot": 42, "result": "disarmed"}
            patched["_open_pr"].return_value = opened
            patched["_queue"].return_value = {"lot": 42, "result": "queued"}
            patched["_leave_queue"].return_value = False
            patched["_fresh"].return_value = fresh if fresh is not None else (self.ROW, marks)
            patched["_renew"].return_value = renew
            patched["_target_lock"].return_value = ((None, True) if target_blocked
                                                    else (mock.sentinel.cible, False))
            out = fire._act(None, None, self.ROW, marks, gh=lambda *a, **k: None,
                            dry_run=dry_run, repo=None, cache={})
        return out, patched

    def test_lot_without_fire_is_idle(self):
        out, calls = self._act([], None)
        self.assertEqual(out["result"], "idle")
        calls["_merge_pr"].assert_not_called()
        calls["_disarm"].assert_not_called()
        calls["_reconcile"].assert_not_called()

    def test_withdrawn_fire_disarms_and_leaves_the_queue(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A),
                                      _mark("unfire", A)), _status(armed=True))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        self.assertIn("no active fire", calls["_disarm"].call_args.args[3])
        calls["_leave_queue"].assert_called_once()
        calls["_merge_pr"].assert_not_called()

    def test_merged_pr_is_terminal(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="MERGED", merged=True, merge_commit=B))
        self.assertEqual(out["result"], "merged")
        calls["_reconcile"].assert_called_once()
        calls["_merge_pr"].assert_not_called()
        calls["_invalidate"].assert_not_called()

    def test_wrong_freeze_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", B), _mark("fire", A)), _status())
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the freeze is", calls["_invalidate"].call_args.args[4])
        calls["_merge_pr"].assert_not_called()

    def test_moved_head_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(head=B))
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the PR head moved", calls["_invalidate"].call_args.args[4])
        calls["_merge_pr"].assert_not_called()

    def test_verdicts_gate_before_opening_the_pr(self):
        """L'ordre du gel corrigé (B) : sans avis, aucune PR, donc aucune CI."""
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), None,
            gate=(False, "2 independent review verdict(s) ok required", {"class": "sensitive"}))
        self.assertEqual(out["result"], "not-ready")
        self.assertIn("independent review", out["detail"])
        calls["_open_pr"].assert_not_called()
        calls["_merge_pr"].assert_not_called()
        calls["_queue"].assert_not_called()

    def test_verdicts_gate_before_the_merge_queue(self):
        project = types.SimpleNamespace(name="nexlink", land="queue", merge_method="merge",
                                        ci_required=False)
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), _status(), project=project,
            gate=(False, "blocked verdict(s) on the freeze a not lifted: rev1",
                  {"class": "sensitive"}))
        self.assertEqual(out["result"], "not-ready")
        calls["_queue"].assert_not_called()
        calls["_merge_pr"].assert_not_called()

    def test_gate_failure_disarms_an_armed_pr(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(armed=True),
                               gate=(False, "blocked verdict(s)", {"class": "sensitive"}))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        calls["_merge_pr"].assert_not_called()
        calls["_open_pr"].assert_not_called()

    def test_ready_merges_now_on_the_exact_head(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, _status())
        self.assertEqual(out["result"], "merged")
        calls["_merge_pr"].assert_called_once()
        self.assertEqual(calls["_merge_pr"].call_args.args[2], A)
        calls["_open_pr"].assert_not_called()

    def test_direct_merge_yields_to_the_target_lease(self):
        """#1467 : le train tient la cible sous le même bail -> aucun geste."""
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, _status(), target_blocked=True)
        self.assertEqual(out["result"], "busy")
        calls["_merge_pr"].assert_not_called()
        calls["_disarm"].assert_not_called()
        calls["_reconcile"].assert_not_called()

    def test_direct_merge_renews_the_target_lease_at_the_train_ttl(self):
        """Le bail de cible, renouvelé juste avant le geste, l'est au TTL du
        train (`land.LOCK_TTL_S`) ; le TTL court du feu ne vaut que pour
        `fire:<lot>`."""
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, _status())
        self.assertEqual(out["result"], "merged")
        self.assertEqual(calls["_renew"].call_args_list[-1].args,
                         (mock.sentinel.cible, land.LOCK_TTL_S))
        calls["_unlock"].assert_called_once_with(mock.sentinel.cible)

    def test_stale_auto_merge_is_disarmed_before_merging(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None,
                               statuses=[_status(armed=True), _status(), _status()])
        self.assertEqual(out["result"], "merged")
        calls["_disarm"].assert_called_once()
        calls["_merge_pr"].assert_called_once()
        self.assertIn("outside the fire", calls["_disarm"].call_args.args[3])

    def test_ci_pending_does_not_merge(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               pr_gate=(False, "the CI of PR #7 is pending on the freeze a"))
        self.assertEqual(out["result"], "not-ready")
        calls["_merge_pr"].assert_not_called()

    def test_dry_run_touches_nothing(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               dry_run=True)
        self.assertEqual(out["result"], "would-open-pr")
        calls["_open_pr"].assert_not_called()
        calls["_merge_pr"].assert_not_called()
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               dry_run=True)
        self.assertEqual(out["result"], "would-merge")
        calls["_merge_pr"].assert_not_called()

    def test_land_project_enters_the_queue_after_the_gate(self):
        project = types.SimpleNamespace(name="nexlink", land="queue", merge_method="merge",
                                        ci_required=False)
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               project=project)
        self.assertEqual(out["result"], "queued")
        calls["_queue"].assert_called_once()
        calls["_merge_pr"].assert_not_called()

    def test_lost_lease_before_the_merge_is_busy(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               renew=False)
        self.assertEqual(out["result"], "busy")
        calls["_merge_pr"].assert_not_called()

    def test_merge_refused_and_head_moved_invalidates(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None, statuses=[_status(), _status(head=B)],
                               merge=(False, "head moved"))
        self.assertEqual(out["result"], "invalidated")
        calls["_merge_pr"].assert_called_once()
        calls["_invalidate"].assert_called_once()

    def test_missing_pr_is_opened_only_after_the_gate_then_merged(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None, opened=(_status(), "opened"))
        self.assertEqual(out["result"], "merged")
        calls["_open_pr"].assert_called_once()
        calls["_merge_pr"].assert_called_once()

    def test_unpublishable_pr_is_not_opened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               opened=(None, "title not publishable (secret)"))
        self.assertEqual(out["result"], "no-pr")
        calls["_merge_pr"].assert_not_called()

    def test_closed_pr_is_not_reopened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="CLOSED"))
        self.assertEqual(out["result"], "pr-closed")
        calls["_merge_pr"].assert_not_called()


class LandGuardCallTest(unittest.TestCase):
    """La garde de `land` au dernier geste (#1374, P1-2)."""

    def test_land_refuses_when_the_fire_is_gone(self):
        ejected = {}

        def eject(reason, todo, **extra):
            ejected.update(reason=reason, todo=todo)
            return {"lot": 7, "result": "ejected"}

        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A},
                                        project=types.SimpleNamespace(repo="/r"), eject=eject)
        with mock.patch.object(fire, "land_guard", return_value=(False, "fire withdrawn")):
            out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("fire withdrawn", ejected["reason"])
        self.assertIn("queue remove", ejected["todo"])

    def test_land_continues_without_fire_markers(self):
        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A},
                                        project=types.SimpleNamespace(repo="/r"),
                                        eject=lambda *a, **k: {"result": "ejected"})
        with mock.patch.object(fire, "land_guard", return_value=(True, "")):
            self.assertIsNone(land._fire_guard(landing))


class FireDbTest(PgTestCase):
    """Le feu en base : `give`/`withdraw`, bail, passage, CLI, garde de land."""

    def _frozen_lot(self, sha=A):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        work.milestone(self.db, lot, "frozen", sha=sha, actor="pilote")
        return lot

    def test_give_refuses_without_a_freeze(self):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("not frozen", str(caught.exception))

    def test_give_refuses_a_head_other_than_the_freeze(self):
        lot = self._frozen_lot(A)
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=B, actor="pilote")
        self.assertIn("the fire names", str(caught.exception))
        with self.assertRaises(fire.FireError):
            fire.give(self.db, lot, sha="not-a-commit", actor="pilote")

    def test_give_then_withdraw(self):
        lot = self._frozen_lot(A)
        row = fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertEqual(row["kind"], "fire")
        now = fire.state(self.db, lot)
        self.assertTrue(now["active"])
        self.assertTrue(now["covered"])
        gone = fire.withdraw(self.db, lot, actor="pilote")
        self.assertEqual(gone["kind"], "unfire")
        now = fire.state(self.db, lot)
        self.assertFalse(now["active"])
        self.assertTrue(now["withdrawn"])
        with self.assertRaises(fire.FireError) as caught:
            fire.withdraw(self.db, lot, actor="pilote")
        self.assertIn("no active fire", str(caught.exception))

    def test_give_refuses_a_closed_lot(self):
        lot = self._frozen_lot(A)
        work.close(self.db, lot, abandoned=True, actor="pilote")
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("no fire to give", str(caught.exception))

    def test_give_and_withdraw_take_the_fire_lease(self):
        lot = self._frozen_lot(A)
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "another-host", 60.0)
        self.assertIsNotNone(epoch)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.give(self.db, lot, sha=A, actor="pilote")
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)
        fire.give(self.db, lot, sha=A, actor="pilote")
        epoch = ops.land_lock(key, "another-host", 60.0)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.withdraw(self.db, lot, actor="pilote")
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)

    def test_pass_skips_a_lot_without_fire(self):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        self.assertEqual(fire.run_pass(self.cfg, self.db, only_lot=lot), [])

    def test_pass_reports_an_unknown_repository(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        out = fire.run_pass(self.cfg, self.db, only_lot=lot, dry_run=True)
        self.assertEqual([e["result"] for e in out], ["no-repository"])

    def test_pass_yields_to_another_host_holding_the_lease(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "another-host", 60.0)
        self.assertIsNotNone(epoch)
        try:
            out = fire.run_pass(self.cfg, self.db, only_lot=lot)
            self.assertEqual(out, [{"lot": lot, "result": "busy"}])
        finally:
            ops.land_unlock(key, "another-host", epoch)

    def test_target_lease_is_shared_with_the_merge_train(self):
        """#1467 : la fusion directe prend le bail de cible du train, ou cede."""
        project = types.SimpleNamespace(name="ameesh")
        ops = storage.of(self.db).work
        key = land.lock_key(project, "origin/main")
        epoch = ops.land_lock(key, "train:runner", 60.0)
        self.assertIsNotNone(epoch)
        try:
            self.assertEqual(fire._target_lock(self.db, project, "main"), (None, True))
        finally:
            ops.land_unlock(key, "train:runner", epoch)
        handle, blocked = fire._target_lock(self.db, project, "main")
        self.assertFalse(blocked)
        self.assertIsNotNone(handle)
        fire._unlock(handle)

    def test_one_rereads_the_milestones_under_the_lease(self):
        """P1-3 : les jalons lus avant le bail ne servent pas au geste."""
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        stale = []          # ce que le relevé avait lu avant de prendre le bail
        seen = {}

        def act(cfg, db, row, marks, **kwargs):
            seen["marks"] = marks
            return {"lot": lot, "result": "idle"}

        with mock.patch.object(fire, "_act", side_effect=act):
            out = fire._one(self.cfg, self.db, work.get(self.db, lot), stale,
                            gh=lambda *a, **k: None, dry_run=False, repo=None, cache={},
                            lock=True)
        self.assertEqual(out["result"], "idle")
        self.assertTrue(fire.active(seen["marks"]),
                        "le passage doit relire le feu sous le bail")
        self.assertTrue(any(m["kind"] == "frozen" for m in seen["marks"]))

    def test_land_guard_without_fire_is_open(self):
        lot = self._frozen_lot(A)
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)

    def test_land_guard_requires_an_active_fire_covering_the_freeze(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)
        ok, why = fire.land_guard(self.db, lot, B)
        self.assertFalse(ok)
        self.assertIn("covers", why)
        fire.withdraw(self.db, lot, actor="pilote")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertFalse(ok)
        self.assertIn("withdrawn or invalidated", why)

    def test_land_guard_refuses_an_unlifted_block(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        work.milestone(self.db, lot, "verdict", sha=A, actor="rev1", verdict="blocked")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertFalse(ok)
        self.assertIn("blocked verdict", why)
        work.milestone(self.db, lot, "verdict", sha=A, actor="rev1", verdict="ok")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)

    def test_cli_state_and_fire_flags(self):
        lot = self._frozen_lot(A)
        shown = self.mesh("work", "fire", str(lot))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("no active fire", shown.stdout)
        refused = self.mesh("work", "milestone", str(lot), "fire", "--sha", A)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("invalid choice", refused.stderr)
        both = self.mesh("work", "fire", str(lot), "--sha", A, "--withdraw")
        self.assertEqual(both.returncode, 2)
        self.assertIn("exclusive", both.stderr)

    def test_cli_gives_and_withdraws(self):
        lot = self._frozen_lot(A)
        given = self.mesh("work", "fire", str(lot), "--sha", A, "--actor", "human:pilote")
        self.assertEqual(given.returncode, 0, given.stderr)
        self.assertIn("fire given", given.stdout)
        self.assertTrue(fire.state(self.db, lot)["active"])
        withdrawn = self.mesh("work", "fire", str(lot), "--withdraw",
                              "--actor", "human:pilote")
        self.assertEqual(withdrawn.returncode, 0, withdrawn.stderr)
        self.assertIn("fire withdrawn", withdrawn.stdout)
        self.assertFalse(fire.state(self.db, lot)["active"])

    def test_cli_refuses_an_executor_whose_only_authority_is_history(self):
        """P1-5 : avoir confié des lots récemment ne donne pas le feu."""
        lot = self._frozen_lot(A)
        operations = storage.of(self.db).operations
        with mock.patch.object(operations.__class__, "assigners", return_value=["impl1"]):
            self.assertIn("impl1", operations.assigners(since_s=0))
            refused = self.mesh("work", "fire", str(lot), "--sha", A,
                                env=self.env(AGENT_MAIL_NAME="impl1",
                                             AMEESH_ALERT_ORCHESTRATORS=""))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("pilot's gesture", refused.stderr)
        self.assertFalse(fire.state(self.db, lot)["active"])

    def test_cli_authority_accepts_a_declared_orchestrator(self):
        """Le même exécutant, déclaré orchestrateur, donne le feu."""
        lot = self._frozen_lot(A)
        given = self.mesh("work", "fire", str(lot), "--sha", A,
                          env=self.env(AGENT_MAIL_NAME="impl1",
                                       AMEESH_ALERT_ORCHESTRATORS="impl1"))
        self.assertEqual(given.returncode, 0, given.stderr)
        self.assertTrue(fire.state(self.db, lot)["active"])


if __name__ == "__main__":
    unittest.main()
