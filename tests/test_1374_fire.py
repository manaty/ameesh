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
et autorité pilote déclarée. #3598 : `give`/`withdraw` attendent la fenêtre
libre du bail `fire:<lot>`, refusent à la borne en nommant le détenteur, et un
passage ne s'arme pas sur un feu retiré pendant l'attente. Tests sans base
(routage, jalons, GitHub simulé), puis sur Postgres (règles de `give`/`withdraw`,
bail, CLI, garde de `land`)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import (fire, land, lot_class, merge_queue, packet_guard, review,
                    review_authors, review_lot, review_provenance, storage, work)

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


def _git_repo(root: str) -> str:
    """Un dépôt git d'un commit vide : rend le SHA complet."""
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, env=env)
    subprocess.run(["git", "-C", root, "commit", "-q", "--allow-empty", "-m", "t"],
                   check=True, env=env)
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


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


def _family(db, name, **kw):
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

    def test_same_commit_by_common_prefix(self):
        # #2349 : gel `c4d85ac37`, tête de PR `c4d85ac37dce` — le même commit
        # sur deux longueurs ne doit pas invalider le feu.
        full = "c4d85ac37dcedb8f11bbdfed89201ee38547ae4a"
        self.assertTrue(fire.same_commit(full, full[:9]))
        self.assertTrue(fire.same_commit(full[:12], full[:9]))
        self.assertTrue(fire.same_commit(full[:9], full[:12]))
        self.assertFalse(fire.same_commit(full[:9], "d4d85ac37"))
        self.assertTrue(fire.same_commit(full, full))

    def test_a_resolved_side_is_compared_by_its_full_id(self):
        # relecture #2349 : le dépôt connaît l'abréviation du feu ; une tête
        # inconnue qui partage seulement ce préfixe n'est pas le même commit.
        known = "c4d85ac37dcedb8f11bbdfed89201ee38547ae4a"
        other = "c4d85ac37000000000000000000000000000000f"
        pins = {known[:9]: known, known: known}
        with mock.patch.object(merge_queue, "pinned",
                               side_effect=lambda _repo, sha: pins.get(sha)):
            self.assertFalse(fire.same_commit(known[:9], other, "/repo"))
            self.assertFalse(fire.same_commit(other, known[:9], "/repo"))
            self.assertTrue(fire.same_commit(known[:9], known, "/repo"))
            # rien de résolu : le préfixe commun reste la règle (#1756)
            self.assertTrue(fire.same_commit("c4d85ac37", other))

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

    def test_state_covers_an_abbreviated_freeze(self):
        # #2370 : un gel en abrégé (352ba651) et un feu à la tête complète.
        item = {"id": 3, "title": "t", "state": "build", "branch": "lot/3-x"}
        marks = _marks(_mark("frozen", A[:8]), _mark("fire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=marks):
            now = fire.state(None, 3)
        self.assertTrue(now["active"])
        self.assertTrue(now["covered"])


class ReconcileTest(unittest.TestCase):
    """#2370 : la réconciliation d'une PR fusionnée résout un gel abrégé."""

    ROW = {"id": 42, "title": "un lot", "assignee": "author", "app": "ameesh"}

    def test_an_abbreviated_freeze_covers_the_full_merged_head(self):
        with tempfile.TemporaryDirectory() as repo:
            full = _git_repo(repo)
            marks = _marks(_mark("frozen", full[:8]), _mark("fire", full[:8]))
            status = _status(state="MERGED", merged=True, head=full, merge_commit=full)
            with mock.patch.object(
                    fire.work, "close_merged",
                    return_value={"result": "merged", "detail": ""}) as close, \
                    mock.patch.object(fire, "_passive"), \
                    mock.patch.object(fire, "_wake") as wake, \
                    mock.patch.object(fire.lot_class, "of_lot",
                                      return_value={"class": "light"}), \
                    mock.patch("ameesh.review_lot.open_after_merge",
                               return_value={"id": None, "created": False}):
                out = fire._reconcile(None, self.ROW, marks, status, slug="manaty/ameesh",
                                      repo=repo, target="main", dry_run=False)
        self.assertTrue(out["covered"])
        wake.assert_not_called()
        self.assertEqual(close.call_args.kwargs.get("extra"), "")


class ReviewGateTest(unittest.TestCase):
    """La porte des avis (#1374, P1-1) : comptés par classe et par domaine
    (SQL/sécurité/prod = deux avis), indépendants, aucun blocage masqué par un
    `ok` plus récent d'un AUTRE relecteur."""

    ITEM = {"assignee": "author"}
    #: un fichier sensible hors SQL/sécurité/prod : un avis d'une autre famille
    PLAIN = {"path": "docs/design/notes.md", "class": "sensitive", "rule": ["docs/**"]}
    #: un fichier SQL : deux avis, familles distinctes
    SQL = {"path": "src/ameesh/migrations/0437_x.sql", "class": "sensitive",
           "rule": ["src/ameesh/migrations/**", "**/*.sql"]}
    #: la porte de fusion est de la sécurité : deux avis
    GATE = {"path": "src/ameesh/merge_queue.py", "class": "sensitive",
            "rule": ["src/ameesh/merge_queue.py"]}

    def gate(self, marks, klass=None, frozen=A, item=None):
        if isinstance(klass, str):
            klass = {"class": klass, "doubt": False}
            if klass["class"] == "sensitive":
                klass["files"] = [self.PLAIN]
        with mock.patch.object(merge_queue, "actor_family", side_effect=_family):
            return merge_queue.review_gate(None, marks, frozen,
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

    def test_grouped_rule_does_not_add_unchanged_critical_files(self):
        # #2218 : le canon regroupe runner et porte de fusion dans une règle.
        policy, problems = review.parse({"default": "light", "rules": [{
            "paths": ["src/ameesh/runner.py", "src/ameesh/merge_queue.py",
                      "src/ameesh/land.py"], "class": "sensitive"}]})
        self.assertEqual(problems, [])
        klass = review.classify(policy, ["src/ameesh/runner.py", "tests/test_runner.py"])
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
        self.assertEqual(one["required"], 1)
        self.assertTrue(one["ok"], one["why"])
        for actor in ("author", "other", "ghost"):
            with self.subTest(actor=actor):
                self.assertFalse(self.gate(
                    _marks(_mark("frozen", A), _verdict(actor)), klass=klass)["ok"])

    def test_changed_critical_file_keeps_two_reviews_in_a_grouped_rule(self):
        for path in ("src/ameesh/merge_queue.py", "src/ameesh/land.py",
                     "src/ameesh/migrations/0460_overview_runner_seen.sql",
                     "src/ameesh/authority.py", "src/ameesh/approve_check.py",
                     "src/ameesh/approve_client.py", "src/ameesh/config.py",
                     "deploy/mise-a-jour/poste.sh"):
            with self.subTest(path=path):
                policy, _ = review.parse({"default": "light", "rules": [{
                    "paths": ["src/ameesh/runner.py", path], "class": "sensitive"}]})
                klass = review.classify(policy, ["src/ameesh/runner.py", path])
                one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
                self.assertEqual(one["required"], 2)
                self.assertFalse(one["ok"])
                two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"),
                                       _verdict("rev2")), klass=klass)
                self.assertTrue(two["ok"], two["why"])

    def test_sensitive_without_changed_paths_keeps_one_review(self):
        # #2394 : le canon réserve les deux avis aux catégories critiques
        # (SQL, secrets, paiement, prod réelle, porte de fusion) ; une liste de
        # chemins vide ne prouve aucune de ces catégories. La classe reste
        # sensible : un avis d'une autre famille, et rien de plus.
        for files in ([], [{"class": "sensitive", "rule": ["src/ameesh/runner.py"]}]):
            with self.subTest(files=files):
                klass = {"class": "sensitive", "doubt": False, "files": files}
                one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
                self.assertEqual(one["required"], 1)
                self.assertTrue(one["ok"], one["why"])

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

    def test_class_and_off_api_merge_need_two_families(self):
        # #2249 : la classe du lot, son classement et la fusion hors API sont
        # la porte de fusion, comme merge_queue et land.
        for path in ("src/ameesh/merge_pr.py", "src/ameesh/lot_class.py",
                     "src/ameesh/review.py"):
            with self.subTest(path=path):
                klass = {"class": "sensitive", "files": [
                    {"path": path, "class": "sensitive", "rule": [path]}]}
                one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
                self.assertEqual(one["required"], 2)
                self.assertFalse(one["ok"])
        near = {"class": "sensitive", "files": [
            {"path": "src/ameesh/preview.py", "class": "sensitive",
             "rule": ["src/ameesh/preview.py"]}]}
        self.assertEqual(merge_queue.required_verdicts(near), 1, "motif ancré au nom")

    #: le canon synthétique du témoin de bout en bout (#2264) : les trois
    #: chemins que la porte de fusion doit voir (#1629), puis fire.py, qui
    #: tombe dans la portée critique « prod » (deux avis).
    GATE_CANON_PATHS = ("src/ameesh/merge_pr.py", "src/ameesh/lot_class.py",
                        "src/ameesh/review.py")
    CRITICAL_CANON_PATH = "src/ameesh/fire.py"

    def canon(self, paths=(), default="light", root="canon"):
        """Un canon minimal, de la même forme que celui de `test_review`."""
        rules = [{"paths": [path], "class": "sensitive"} for path in paths]
        return types.SimpleNamespace(
            root=root, sources=[],
            federation={"review_policies": {"risk_classes": {
                "projects": {"ameesh": {"default": default, "rules": rules}}}}})

    def test_the_assignee_historical_family_beats_the_current_registry(self):
        # R9 B de claude2 : la porte reprenait le registre courant pour
        # l'assigné, même quand `authors` portait sa famille historique
        # prouvée ; un relecteur de cette famille comptait alors à tort.
        def current(_db, name, **_kw):
            return {"author": "deepseek", "rev2": "codex",
                    "rev3": "claude"}.get(str(name or "").strip(), "")

        with mock.patch.object(merge_queue, "actor_family", side_effect=current):
            out = merge_queue.review_gate(
                None, _marks(_mark("frozen", A), _verdict("rev2"), _verdict("rev3")),
                A, klass={"class": "sensitive", "files": [self.SQL]},
                item={"assignee": "author"}, required=2,
                authors=[{"name": "author", "family": "codex"}])
        self.assertEqual(out["author_family"], "codex")
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]], ["rev3"])

    def test_the_canon_chain_drives_the_required_verdicts(self):
        """#2264 : le témoin de #2249 forçait `klass` — il serait resté vert si
        le canon ne classait plus ces chemins. Ici la classe est lue par
        `lot_class.class_of_paths` sur un canon synthétique, puis la porte en
        déduit les avis ; le contrôle négatif (même canon sans règle) retombe
        à `light`, tout en gardant la ceinture critique de #2265."""
        canon = self.canon(self.GATE_CANON_PATHS + (self.CRITICAL_CANON_PATH,))
        for path in self.GATE_CANON_PATHS:
            klass = lot_class.class_of_paths([canon], "ameesh", [path])
            self.assertEqual(klass["class"], "sensitive", (path, klass))
            self.assertFalse(klass["doubt"], (path, klass))
            # #2374 : ces trois chemins comptent pour la porte, même quand le
            # canon les classe par une règle groupée (le motif de secours les
            # reconnaît et exige les deux familles).
            self.assertEqual(merge_queue.required_verdicts(klass), 2, (path, klass))
        critical = lot_class.class_of_paths(
            [canon], "ameesh", [self.CRITICAL_CANON_PATH])
        self.assertEqual(critical["class"], "sensitive", critical)
        self.assertEqual(merge_queue.required_verdicts(critical), 2, critical)
        together = lot_class.class_of_paths(
            [canon], "ameesh", self.GATE_CANON_PATHS + (self.CRITICAL_CANON_PATH,))
        self.assertEqual(merge_queue.required_verdicts(together), 2, together)
        two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                        klass=together)
        self.assertTrue(two["ok"], two["why"])
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=together)
        self.assertFalse(one["ok"], "la classe lue exige bien deux avis")
        # Sans règle, la classe devient légère ; #2265 garde deux avis sur
        # les chemins critiques oubliés du canon, indépendamment de la classe.
        bare = self.canon()
        light = lot_class.class_of_paths(
            [bare], "ameesh", self.GATE_CANON_PATHS + (self.CRITICAL_CANON_PATH,))
        self.assertEqual(light["class"], "light", light)
        self.assertEqual(merge_queue.required_verdicts(light), 2, light)

    def test_author_family_never_counts(self):
        klass = {"class": "sensitive", "files": [self.SQL]}
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("other")),
                        klass=klass)
        self.assertFalse(out["ok"], "l'avis de la famille de l'auteur ne compte pas")
        self.assertEqual([a["actor"] for a in out["accepted"]], ["rev1"])

    def test_a_doubt_never_supposes_a_family_and_needs_one(self):
        # #2394 : le doute de classe reste sensible (canon §8) : un avis d'une
        # autre famille suffit (owner 15/10), mais l'indépendance ne s'y
        # suppose jamais — famille du relecteur connue et autre que l'auteur.
        klass = {"class": "sensitive", "doubt": True, "files": []}
        out = self.gate(_marks(_mark("frozen", A)), klass=klass)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
        self.assertTrue(one["ok"], one["why"])
        ghost = self.gate(_marks(_mark("frozen", A), _verdict("ghost")), klass=klass)
        self.assertFalse(ghost["ok"], "une famille inconnue ne prouve pas l'indépendance")
        same = self.gate(_marks(_mark("frozen", A), _verdict("other")), klass=klass)
        self.assertFalse(same["ok"], "la famille de l'auteur ne compte pas")
        out = self.gate(_marks(_mark("frozen", A), _verdict("author")), klass=klass)
        self.assertFalse(out["ok"], "l'auteur ne relit pas son propre travail")

    def test_an_unknown_author_family_never_proves_independence(self):
        # B/P1 cx1 : quand la famille de l'auteur n'est pas connue, l'ancien
        # code sautait la comparaison et acceptait n'importe quelle famille.
        klass = {"class": "sensitive", "files": [self.SQL]}
        item = {"assignee": "inconnu"}       # famille absente du registre simulé
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
        item = {"assignee": "human:alice"}
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
                        klass="normal", item={"assignee": "inconnu"})
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

    def test_pilot_waiver_lifts_the_verdicts_but_never_a_block(self):
        # #2281 : `work reviewer --no-verdict` (#1979) marque la dispense de
        # re-verdict (`reviewer = -none`) ; la porte partagée la respecte,
        # sans changer la classe ni lever un verdict bloquant courant.
        for item in ({"assignee": "author", "reviewer": work.REVIEWER_NONE},
                     {"assignee": "author", "reviewers": [work.REVIEWER_NONE]}):
            out = self.gate(_marks(_mark("frozen", A)),
                            klass={"class": "sensitive", "files": [self.SQL]}, item=item)
            self.assertTrue(out["ok"], out["why"])
            self.assertTrue(out["waived"])
            self.assertEqual(out["required"], 2, "la dispense ne change pas la classe")
        named = self.gate(_marks(_mark("frozen", A)),
                          klass={"class": "sensitive", "files": [self.SQL]},
                          item={"assignee": "author", "reviewer": work.REVIEWER_NONE,
                                "events": [{"note": "verdict not required: no re-verdict "
                                                    "due (#1979)", "actor": "pilote-x"}]})
        self.assertEqual(named["waived_by"], "pilote-x")
        blocked = self.gate(_marks(_mark("frozen", A),
                                   _verdict("rev1", verdict="blocked")),
                            klass={"class": "sensitive", "files": [self.SQL]},
                            item={"assignee": "author", "reviewer": work.REVIEWER_NONE})
        self.assertFalse(blocked["ok"], "une dispense ne lève jamais une correction due")
        self.assertIn("blocked verdict", blocked["why"])

    def test_a_stale_waiver_row_does_not_cover_a_new_freeze(self):
        # #2281/B1 cx1 : la ligne passée à la porte peut être antérieure au
        # gel ; le stockage a périmé la dispense en base mais la copie la
        # porte encore. La porte relit la marque après les jalons : un gel B
        # sans avis reste refusé, même avec une ligne A en main.
        stale = {"id": 9, "assignee": "author", "reviewer": work.REVIEWER_NONE}
        fresh = {"id": 9, "assignee": "author", "reviewer": "", "reviewers": []}
        with mock.patch.object(merge_queue.work, "get", return_value=fresh):
            out = self.gate(_marks(_mark("frozen", B)),
                            klass={"class": "sensitive", "files": [self.SQL]},
                            frozen=B, item=stale)
        self.assertFalse(out["ok"])
        self.assertFalse(out["waived"])
        self.assertIn("no review verdict ok", out["why"])

    def test_the_waiver_actor_comes_from_the_lot_journal(self):
        # #2281/N1 cx1 : `work.get` ne rend pas les événements ; en usage réel
        # la note de file doit citer le pilote, lu dans la table du journal.
        fresh = {"id": 9, "assignee": "author", "reviewer": work.REVIEWER_NONE}
        ops = mock.Mock()
        ops.work.events_noted.return_value = [
            {"note": "verdict not required: no re-verdict due (#1979)",
             "actor": "pilote-x"}]
        with mock.patch.object(merge_queue.work, "get", return_value=fresh), \
                mock.patch.object(merge_queue.storage, "of", return_value=ops):
            out = self.gate(_marks(_mark("frozen", A)),
                            klass={"class": "sensitive", "files": [self.SQL]},
                            item={"id": 9, "assignee": "author",
                                  "reviewer": work.REVIEWER_NONE})
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["waived_by"], "pilote-x")


class ReadyTest(unittest.TestCase):
    """Les portes du feu : verdicts d'abord (sans PR), puis PR/CI."""

    ROW = {"id": 42, "title": "t", "branch": "lot/42-x", "assignee": "author"}

    def _verdicts(self, marks, klass, *, repo=None, project=None, target="main",
                  authors=None):
        # #1515 : les auteurs complets sont lus par la porte du feu ; ici on
        # simule cette lecture, la provenance réelle est éprouvée à part.
        # La liste est **complète** (`authors_complete=True`, #1515/cx2) : elle
        # doit nommer la référence, sinon la porte conclut à juste titre qu'elle
        # n'a rien écrit. Par défaut : l'assigné, comme en production.
        if authors is None:
            authors = [{"name": self.ROW["assignee"],
                        "family": FAM.get(self.ROW["assignee"], "")}]
        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass), \
                mock.patch.object(merge_queue, "actor_family", side_effect=_family), \
                mock.patch.object(merge_queue, "_complete_authors",
                                  return_value=(list(authors), ())):
            return fire.ready_verdicts(None, None, self.ROW, marks, repo=repo,
                                       project=project, target=target)

    def test_light_passes_without_a_pr(self):
        ok, why, klass = self._verdicts(_marks(_mark("frozen", A)), {"class": "light"})
        self.assertTrue(ok, why)
        self.assertEqual(klass["class"], "light")

    def test_the_resolved_target_base_drives_the_class_and_the_gate(self):
        # #3091 : la cible déjà résolue par l'appelant (baseRefOid de la PR)
        # est la révision de classe et la preuve passée à la porte, pas la ref
        # locale du checkout qui peut être en retard.
        klass = {"class": "light"}
        base = "4" * 40
        seen = {}

        def gate(db, marks, frozen, **kw):
            seen.update(kw)
            return {"ok": True}

        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass) as of_lot, \
                mock.patch.object(merge_queue, "_class_base", return_value="origin/main"), \
                mock.patch.object(merge_queue, "pinned",
                                  side_effect=lambda repo, rev: rev), \
                mock.patch.object(merge_queue, "review_gate", side_effect=gate):
            ok, why, _ = fire.ready_verdicts(
                None, None, self.ROW, _marks(_mark("frozen", A)),
                repo="/repo", project=None, target="main", base_sha=base)
        self.assertTrue(ok, why)
        self.assertEqual(of_lot.call_args.kwargs["base"], "main")
        self.assertEqual(of_lot.call_args.kwargs["base_sha"], base)
        self.assertEqual(seen["target"], base)

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

    def test_a_vehicle_does_not_pre_read_the_whole_packet_range(self):
        # B cx1 (#2803/#2766) : sur un véhicule, la lecture préalable de toute
        # la plage du paquet agrège les familles des membres déjà relus et peut
        # lever « ambiguïté » avant que la porte ne borne au delta propre. La
        # prélecture est donc réservée aux lots simples ; la porte lit le
        # véhicule elle-même.
        klass = {"class": "sensitive", "doubt": False, "files": [
            {"path": "docs/x.md", "class": "sensitive", "rule": ["docs/**"]}]}
        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass), \
                mock.patch.object(merge_queue, "actor_family", side_effect=_family), \
                mock.patch("ameesh.packet_reviews.is_vehicle", return_value=True), \
                mock.patch("ameesh.review_lot.compiled_families",
                           return_value=("claude", "codex", "deepseek")), \
                mock.patch.object(merge_queue, "_complete_authors",
                                  side_effect=AssertionError(
                                      "the packet range must not be pre-read")):
            ok, why, _ = fire.ready_verdicts(None, None, self.ROW,
                                            _marks(_mark("frozen", A)),
                                            repo=None, project=None, target="main")
        self.assertFalse(ok)
        self.assertIn("independent review", why)

    def test_a_corrector_author_does_not_review_his_own_work(self):
        # #1515 N1 : les auteurs du delta sont lus avant la porte ; le
        # relecteur qui a corrigé ne relit pas sa propre correction.
        klass = {"class": "sensitive", "doubt": False, "files": [
            {"path": "docs/x.md", "class": "sensitive", "rule": ["docs/**"]}]}
        authors = [{"name": "rev1", "family": "codex"}]
        ok, why, _ = self._verdicts(
            _marks(_mark("frozen", A), _verdict("rev1")), klass, authors=authors)
        self.assertFalse(ok, "le correcteur relit sa propre correction")
        self.assertIn("independent review", why)
        ok, why, _ = self._verdicts(
            _marks(_mark("frozen", A), _verdict("rev2")), klass, authors=authors)
        self.assertTrue(ok, why)

    def test_gate_participants_are_critical_scope(self):
        # #1515 N2 : radar et work peuvent calculer la classe ou le correcteur
        # de la porte ; un lot qui ne les touche que là demande deux avis.
        # R12 (B1 de mesh-design) : les lecteurs d'auteurs et de train que la
        # porte traverse, le plan de git et les résolutions comptent aussi.
        for path in ("src/ameesh/radar.py", "src/ameesh/work.py",
                     "src/ameesh/merge_queue.py", "src/ameesh/review_lot.py",
                     "src/ameesh/review_authors.py",
                     "src/ameesh/review_designation_activation.py",
                     "src/ameesh/train.py", "src/ameesh/plan_git.py"):
            self.assertTrue(any(rx.search(path) for rx in merge_queue.CRITICAL_SCOPE),
                            path)

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
        # #2271 : la protection de la cible est lue à la forge ; ce témoin
        # isole les autres portes de la PR avec une exigence vide.
        for ci in ("pending", "red"):
            ok, why = fire.ready_pr(_status(ci=ci), target="main", project=None,
                                    frozen=A, required=())
            self.assertFalse(ok)
            self.assertIn(ci, why)
        ok, why = fire.ready_pr(_status(draft=True), target="main", project=None,
                                frozen=A, required=())
        self.assertFalse(ok)
        self.assertIn("draft", why)
        ok, why = fire.ready_pr(_status(base="develop"), target="main", project=None,
                                frozen=A, required=())
        self.assertFalse(ok)
        self.assertIn("targets", why)
        ok, why = fire.ready_pr(_status(), target="main", project=None,
                                frozen=A, required=())
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


class LeaseWaitTest(unittest.TestCase):
    """#3598 : l'attente du bail `fire:<lot>` par `give`/`withdraw`."""

    def test_wait_lock_retries_with_backoff_until_released(self):
        handles = [None, None, ("ops", "owner", 7, "fire:42")]
        calls, slept = [], []

        def lock(db, lot):
            calls.append(lot)
            return handles.pop(0)

        with mock.patch.object(fire, "_lock", side_effect=lock):
            handle = fire._wait_lock(None, 42, wait=10.0, sleep=slept.append,
                                     clock=iter([0.0, 0.0, 1.0]).__next__)
        self.assertEqual(handle[3], "fire:42")
        self.assertEqual(calls, [42, 42, 42])
        self.assertEqual(slept, [fire.LOCK_WAIT_FIRST_S, fire.LOCK_WAIT_FIRST_S * 2])

    def test_wait_lock_refuses_at_the_bound_with_the_holder(self):
        holder = {"owner": "pass:r5:4242", "expires_at": "2026-10-25T19:00:00Z",
                  "left_s": 3.0}
        with mock.patch.object(fire, "_lock", return_value=None), \
                mock.patch.object(fire, "_land_holder", return_value=holder):
            with self.assertRaises(fire.FireError) as caught:
                fire._wait_lock(None, 42, wait=10.0, sleep=lambda _s: None,
                                clock=iter([0.0, 0.0, 10.0]).__next__)
        message = str(caught.exception)
        self.assertIn("another fire operation holds lot #42", message)
        self.assertIn("pass:r5:4242", message)
        self.assertIn("2026-10-25T19:00:00Z", message)
        self.assertIn("3s left", message)
        self.assertIn("waited 10s", message)


class ActTest(unittest.TestCase):
    """Le routage d'un passage : verdicts avant PR/file/CI, fusion au geste
    effectif, jamais d'auto-fusion laissée armée, jamais deux gestes
    concurrents."""

    ROW = {"id": 42, "title": "un lot", "branch": "lot/42-x", "assignee": "author",
           "app": "ameesh", "branch_target": "main", "state": "build"}

    def _act(self, marks, status, *, gate=(True, "", {"class": "light"}), pr_gate=(True, ""),
             info=None, project=None, dry_run=False, opened=None, fresh=None, renew=True,
             statuses=None, merge=(True, "PR merged"), target_blocked=False,
             packet_refusal=None):
        names = ("_repo_of", "_slug", "_project", "_pr_status", "_repo_info", "_target",
                 "ready_verdicts", "ready_pr", "_merge_pr", "_disarm_auto", "_reconcile",
                 "_invalidate", "_disarm", "_open_pr", "_queue", "_passive", "_wake",
                 "_leave_queue", "_fresh", "_renew", "_target_lock", "_unlock",
                 "landing_path")
        packet_patch = (
            mock.patch.object(fire.packet_guard, "check_merge",
                              side_effect=packet_guard.PacketGuardError(packet_refusal))
            if packet_refusal else
            mock.patch.object(fire.packet_guard, "check_merge",
                              return_value={"allowed": True}))
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched, \
                packet_patch:
            patched["_repo_of"].return_value = None
            patched["_slug"].return_value = "manaty/ameesh"
            patched["_project"].return_value = project
            # #2256 : le routage est testé ici ; la décision de voie a son
            # propre témoin (tests/test_2256_fire_landing_path.py).
            land = getattr(project, "land", "off") if project is not None else "off"
            patched["landing_path"].return_value = (
                "queue" if land != "off" else "direct", "test")
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

    def test_fire_withdrawn_before_the_gesture_stops_the_pass(self):
        """P1-3 (#1374) : le passage relit le feu sous son bail ; un unfire
        arrivé entre-temps l'arrête avant la fusion (#3598, témoin 3)."""
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), _status(),
            fresh=(self.ROW, _marks(_mark("frozen", A), _mark("fire", A),
                                    _mark("unfire", A))))
        self.assertEqual(out["result"], "withdrawn")
        self.assertIn("withdrawn before the merge", out["detail"])
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

    def test_packet_guard_refuses_before_any_pr_or_queue(self):
        """#2333 : un feu de lot hors paquet est refusé avant la PR et la file."""
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), _status(),
            packet_refusal="direct merge of lot/42-x into main refused: canon §11 bis")
        self.assertEqual(out["result"], "not-ready")
        self.assertIn("§11 bis", out["detail"])
        calls["_open_pr"].assert_not_called()
        calls["_queue"].assert_not_called()
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

        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A, "branch": "integration/p",
                                             "target": "main"},
                                        project=types.SimpleNamespace(repo="/r"), eject=eject)
        with mock.patch.object(fire, "land_guard", return_value=(False, "fire withdrawn")):
            out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("fire withdrawn", ejected["reason"])
        self.assertIn("queue remove", ejected["todo"])

    def test_land_continues_without_fire_markers(self):
        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A, "branch": "integration/p",
                                             "target": "main"},
                                        project=types.SimpleNamespace(repo="/r"),
                                        eject=lambda *a, **k: {"result": "ejected"})
        with mock.patch.object(fire, "land_guard", return_value=(True, "")):
            self.assertIsNone(land._fire_guard(landing))


class FireDbTest(PgTestCase):
    """Le feu en base : `give`/`withdraw`, bail, passage, CLI, garde de land."""

    def _frozen_lot(self, sha=A):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        work.milestone(self.db, lot, "frozen", sha=sha, actor="pilote")
        # #2333 : ces témoins-ci portent sur le mécanisme du feu ; l'exception
        # incident déclarée au journal les autorise hors paquet. Les refus de
        # la garde ont leurs propres témoins ci-dessous.
        packet_guard.declare(self.db, lot, "incident", "test: fire fixture", sha,
                             actor="pilote")
        return lot

    def _bare_frozen_lot(self, sha=A, branch="lot/1-un-lot"):
        """Un gel sans exception : la garde du paquet doit refuser le feu."""
        lot = work.add(self.db, title="un lot", branch=branch)["id"]
        work.milestone(self.db, lot, "frozen", sha=sha, actor="pilote")
        return lot

    def test_packet_guard_refuses_a_direct_fire_on_the_target(self):
        """#2333 : hors paquet, le feu d'une branche de lot est refusé, sans jalon."""
        lot = self._bare_frozen_lot()
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("§11 bis", str(caught.exception))
        self.assertFalse(fire.state(self.db, lot)["active"])
        self.assertEqual([e for e in work.events(self.db, lot)
                          if (e.get("note") or "").startswith("packet-exception")], [])

    def test_packet_guard_exception_is_journaled_on_the_fire(self):
        """#2333 : l'exception motivée passe et laisse sa marque au journal."""
        lot = self._bare_frozen_lot()
        row = fire.give(self.db, lot, sha=A, actor="pilote",
                        exception="incident", reason="cible rouge 18/10")
        self.assertEqual(row["kind"], "fire")
        notes = [e["note"] for e in work.events(self.db, lot)
                 if (e.get("note") or "").startswith("packet-exception")]
        self.assertEqual(notes, ["packet-exception incident sha=%s reason=cible rouge 18/10"
                                 % A])

    def test_packet_guard_exception_requires_a_reason(self):
        """#2333 : une exception sans motif n'est pas une déclaration."""
        lot = self._bare_frozen_lot()
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote", exception="incident")
        self.assertIn("requires a reason", str(caught.exception))
        self.assertFalse(fire.state(self.db, lot)["active"])

    def test_packet_head_passes_without_an_exception(self):
        """#2333 : la tête d'un paquet (`integration/**`) a le droit de fusionner."""
        lot = self._bare_frozen_lot(branch="integration/paquet-2333")
        row = fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertEqual(row["kind"], "fire")

    def _landing(self, lot, *, branch="lot/1-un-lot", target="main", sha=A):
        """Le dernier geste de `land` : une ligne de file et son éjection."""
        ejected: dict = {}

        def eject(reason, todo, **extra):
            ejected.update(reason=reason, todo=todo)
            return {"lot": lot, "result": "ejected", "reason": reason, "todo": todo}

        landing = types.SimpleNamespace(
            db=self.db, lot=lot,
            row={"branch": branch, "target": target, "frozen_sha": sha},
            project=types.SimpleNamespace(repo=".", name="ameesh"), eject=eject)
        return landing, ejected

    def test_land_guard_refuses_a_queued_lot_head_without_a_packet(self):
        """#2333 : une ligne de file déjà posée ne contourne pas le paquet, sans feu."""
        lot = self._bare_frozen_lot()
        landing, ejected = self._landing(lot)
        out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("§11 bis", ejected["reason"])
        self.assertIn("queue remove", ejected["todo"])

    def test_land_guard_passes_a_declared_exception_on_that_head(self):
        """#2333 : l'exception déclarée au journal vaut au dernier geste de land."""
        lot = self._bare_frozen_lot()
        packet_guard.declare(self.db, lot, "incident", "cible rouge 18/10", A)
        landing, _ejected = self._landing(lot)
        self.assertIsNone(land._fire_guard(landing))

    def test_land_guard_refuses_a_declaration_on_another_head(self):
        """#2333 : une déclaration d'une autre tête ne couvre pas ce gel."""
        lot = self._bare_frozen_lot()
        packet_guard.declare(self.db, lot, "incident", "ancienne tête", B)
        landing, ejected = self._landing(lot)
        out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("§11 bis", ejected["reason"])

    def test_land_guard_passes_an_integration_packet(self):
        """#2333 : une ligne de file dont la tête est integration/** passe."""
        lot = self._bare_frozen_lot(branch="integration/paquet-2333")
        landing, _ejected = self._landing(lot, branch="integration/paquet-2333")
        self.assertIsNone(land._fire_guard(landing))

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
                fire.give(self.db, lot, sha=A, actor="pilote", wait=0.05)
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)
        fire.give(self.db, lot, sha=A, actor="pilote")
        epoch = ops.land_lock(key, "another-host", 60.0)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.withdraw(self.db, lot, actor="pilote", wait=0.05)
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)

    def test_give_waits_for_the_lease_to_be_released(self):
        """#3598 : le bail rendu pendant l'attente, le feu se donne."""
        lot = self._frozen_lot(A)
        key = "fire:%d" % lot
        other = self.connect()
        self.addCleanup(other.close)
        their_ops = storage.of(other).work
        epoch = their_ops.land_lock(key, "another-host", 60.0)
        self.assertIsNotNone(epoch)
        self.addCleanup(their_ops.land_unlock, key, "another-host", epoch)
        timer = threading.Timer(
            0.3, lambda: their_ops.land_unlock(key, "another-host", epoch))
        timer.start()
        self.addCleanup(timer.cancel)
        row = fire.give(self.db, lot, sha=A, actor="pilote", wait=5.0)
        self.assertEqual(row["kind"], "fire")
        self.assertTrue(fire.state(self.db, lot)["active"])

    def test_withdraw_waits_for_the_lease_to_be_released(self):
        """#3598 : le retrait attend la fenêtre libre ; le passage l'ayant
        rendue, le feu se retire."""
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        key = "fire:%d" % lot
        other = self.connect()
        self.addCleanup(other.close)
        their_ops = storage.of(other).work
        epoch = their_ops.land_lock(key, "pass:r5:4242", 60.0)
        self.assertIsNotNone(epoch)
        self.addCleanup(their_ops.land_unlock, key, "pass:r5:4242", epoch)
        timer = threading.Timer(
            0.3, lambda: their_ops.land_unlock(key, "pass:r5:4242", epoch))
        timer.start()
        self.addCleanup(timer.cancel)
        row = fire.withdraw(self.db, lot, actor="pilote", wait=5.0)
        self.assertEqual(row["kind"], "unfire")
        self.assertFalse(fire.state(self.db, lot)["active"])

    def test_give_refusal_names_the_holder_and_its_expiry(self):
        """#3598 : à la borne, le refus dit qui tient le bail et jusqu'à quand."""
        lot = self._frozen_lot(A)
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "pass:r5:4242", 60.0)
        self.assertIsNotNone(epoch)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.give(self.db, lot, sha=A, actor="pilote", wait=0.05)
        finally:
            ops.land_unlock(key, "pass:r5:4242", epoch)
        message = str(caught.exception)
        self.assertIn("another fire operation holds lot #%d" % lot, message)
        self.assertIn("pass:r5:4242", message)
        self.assertIn("until", message)

    def test_withdraw_refusal_names_the_holder_and_its_expiry(self):
        """#3598 : le retrait refusé à la borne nomme le détenteur et son
        échéance, comme `give`."""
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "pass:r5:4242", 60.0)
        self.assertIsNotNone(epoch)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.withdraw(self.db, lot, actor="pilote", wait=0.05)
        finally:
            ops.land_unlock(key, "pass:r5:4242", epoch)
        message = str(caught.exception)
        self.assertIn("another fire operation holds lot #%d" % lot, message)
        self.assertIn("pass:r5:4242", message)
        self.assertIn("until", message)

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


class StackedBaseMergeTest(unittest.TestCase):
    """#2603 : le feu ne fusionne pas sur une base de travail déjà fusionnée
    dans la cible par défaut (le contenu atterrirait sur une branche morte)."""

    def _status(self, base="canon/2452"):
        return {"number": 112, "base": base}

    def test_a_base_merged_into_the_default_refuses_before_merging(self):
        calls = []

        def gh(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '{"status": "ahead"}', "")

        ok, detail = fire._merge_pr("acme/home", self._status(), A, "merge", gh=gh,
                                    target="canon/2452", default="main")
        self.assertFalse(ok)
        self.assertIn("already merged into main", detail)
        self.assertIn("gh pr edit --base main 112", detail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "api")

    def test_a_work_base_not_yet_merged_proceeds(self):
        calls = []

        def gh(args, **kw):
            calls.append(list(args))
            if args[0] == "api":
                return subprocess.CompletedProcess(args, 0, '{"status": "diverged"}', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        ok, detail = fire._merge_pr("acme/home", self._status("canon/2453"), A, "merge",
                                    gh=gh, target="canon/2453", default="main")
        self.assertTrue(ok, detail)
        self.assertEqual([c[0] for c in calls], ["api", "pr"])

    def test_a_comparison_without_a_known_status_refuses(self):
        for payload in ('{}', '{"status": "unknown"}'):
            with self.subTest(payload=payload):
                gh = mock.Mock(return_value=subprocess.CompletedProcess(
                    ["gh"], 0, payload, ""))
                ok, detail = fire._merge_pr("acme/home", self._status(), A, "merge",
                    gh=gh, target="canon/2452", default="main")
                self.assertFalse(ok)
                self.assertIn("unreadable", detail)
                self.assertEqual(gh.call_count, 1)

    def test_the_default_base_needs_no_comparison(self):
        calls = []

        def gh(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        ok, detail = fire._merge_pr("acme/home", self._status("main"), A, "merge", gh=gh,
                                    target="main", default="main")
        self.assertTrue(ok, detail)
        self.assertEqual([c[0] for c in calls], ["pr"])

    def test_an_unreadable_comparison_refuses_before_merging(self):
        calls = []

        def gh(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, "", "HTTP 403")

        ok, detail = fire._merge_pr("acme/home", self._status(), A, "merge", gh=gh,
                                    target="canon/2452", default="main")
        self.assertFalse(ok)
        self.assertIn("cannot be read", detail)
        self.assertEqual([c[0] for c in calls], ["api"])


class DeltaBaseTest(unittest.TestCase):
    """#1515 B2 : la base du delta se lit sur la cible effective — ref privée
    de la file d'abord, puis ref distante pour une cible non qualifiée, la
    branche locale seulement ensuite (réécriture de la préférence 04e1d85e)."""

    @staticmethod
    def _git(tmp):
        def git(*args, name="author", committer=None, email_domain="example.invalid"):
            committer = committer or name
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
            env.update(GIT_AUTHOR_NAME=name,
                       GIT_AUTHOR_EMAIL=name + "@" + email_domain,
                       GIT_COMMITTER_NAME=committer,
                       GIT_COMMITTER_EMAIL=committer + "@" + email_domain)
            proc = subprocess.run(["git", "-C", tmp, *args], check=True,
                                  capture_output=True, text=True, env=env)
            return proc.stdout.strip()
        return git

    @staticmethod
    def _write(tmp, name, text):
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _base(self, tmp, git):
        git("init", "-q", "-b", "main")
        self._write(tmp, "a", "a\n")
        git("add", "a")
        git("commit", "-qm", "a")

    def test_the_remote_ref_wins_for_an_unqualified_target(self):
        # Une branche locale en avance du gel non publié ne masque pas la
        # cible distante : c'est elle que le feu a contrôlée.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            base = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/develop", base)
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "correction\n")
            git("commit", "-qam", "correction", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("branch", "-f", "develop", frozen)
            commit, ref, _place = merge_queue._delta_base(tmp, {}, "develop")
            self.assertEqual(ref, "refs/remotes/origin/develop")
            self.assertEqual(commit, base)

    def test_an_explicit_local_ref_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "correction\n")
            git("commit", "-qam", "correction", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("branch", "-f", "develop", frozen)
            commit, ref, _place = merge_queue._delta_base(tmp, {}, "refs/heads/develop")
            self.assertEqual(ref, "refs/heads/develop")
            self.assertEqual(commit, frozen)

    def test_the_private_queue_ref_is_honoured_in_the_queue_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            base = git("rev-parse", "HEAD")
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "correction\n")
            git("commit", "-qam", "correction", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("branch", "-f", "develop", frozen)
            git("update-ref", "refs/ameesh/queue/develop", base)
            commit, ref, _place = merge_queue._delta_base(
                tmp, {}, "develop", queue=True)
            self.assertEqual(ref, "refs/ameesh/queue/develop")
            self.assertEqual(commit, base)

    def test_a_local_queue_ref_is_ignored_in_an_agent_checkout(self):
        # R6 B2 : dans le checkout de l'assigné, `refs/ameesh/queue/<cible>` est
        # une ref locale que l'agent peut déplacer jusqu'à la tête gelée pour
        # vider le delta. Seul le clone de la file (`queue=True`) la lit ; le
        # checkout lit `origin/<cible>`.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            base = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/develop", base)
            git("checkout", "-qb", "lot")
            self._write(tmp, "a", "correction\n")
            git("commit", "-qam", "correction", name="rev1")
            frozen = git("rev-parse", "HEAD")
            git("update-ref", "refs/ameesh/queue/develop", frozen)
            commit, ref, _place = merge_queue._delta_base(tmp, {}, "develop")
            self.assertEqual(ref, "refs/remotes/origin/develop")
            self.assertEqual(commit, base)
            commit, ref, _place = merge_queue._delta_base(
                tmp, {}, "develop", queue=True)
            self.assertEqual(ref, "refs/ameesh/queue/develop")
            self.assertEqual(commit, frozen)

    def test_an_unreadable_target_raises(self):
        # Une cible introuvable n'est pas « aucun auteur » (#1515 B1/B2).
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            self._base(tmp, git)
            with self.assertRaises(merge_queue._ProvenanceError):
                merge_queue._delta_base(tmp, {}, "nope")


class _EmptyHistoryDb:
    """Magasin factice de ces bancs : aucun jalon, donc aucun gel antérieur.

    `_complete_authors` lit désormais l'historique des gels (#2924) ; ces
    témoins-ci n'en ont pas et ne doivent pas toucher la base de production.
    """

    def query(self, *_args, **_kwargs):
        return []


class CompleteAuthorsTest(unittest.TestCase):
    """#1515 N1/#2808 : la porte lit les auteurs du delta gelé ; un détenteur
    du journal n'est auteur que par un commit, la référence au gel n'est pas
    semée, et un outil hors registre est omis sans bloquer."""

    @staticmethod
    def _git(tmp):
        return DeltaBaseTest._git(tmp)

    def test_a_holder_who_only_wrote_a_merge_cannot_review_it(self):
        # La lecture complète voit la fusion de rev1 ; la lecture partagée
        # des commits sans fusion ne voit que author. Elle ne doit pas
        # effacer rev1 des auteurs ni compter son propre avis.
        authors = [{"name": "author", "family": "deepseek"},
                   {"name": "rev1", "family": "codex"}]
        marks = _marks(_verdict("rev1"), _verdict("rev2"))
        with mock.patch.object(merge_queue, "_attribution_on_freeze",
                               return_value=([authors[0]], [])):
            gate = merge_queue.review_gate(
                None, marks, A, item={"assignee": "rev1"},
                klass={"class": "sensitive"}, required=2,
                authors=authors, authors_complete=True, family_of=FAM.get)
        self.assertFalse(gate["ok"], gate["why"])
        self.assertEqual({entry["name"] for entry in gate["authors"]},
                         {"author", "rev1"})
        self.assertEqual([entry["actor"] for entry in gate["accepted"]], ["rev2"])

    def _repo(self, tmp, git, *commits):
        DeltaBaseTest._write(tmp, "a", "a\n")
        git("init", "-q", "-b", "main")
        git("add", "a")
        git("commit", "-qm", "a", name="author")
        base = git("rev-parse", "HEAD")
        git("checkout", "-qb", "lot")
        for index, name in enumerate(commits):
            DeltaBaseTest._write(tmp, "a", "v%d\n" % index)
            git("commit", "-qam", "v%d" % index, name=name)
        return base, git("rev-parse", "HEAD")

    def _authors(self, tmp, frozen, family=None):
        item = {"id": 7, "assignee": "author", "branch_target": "main"}
        with mock.patch.object(merge_queue, "actor_family",
                               side_effect=family or _family), \
                mock.patch.object(review_lot, "compiled_families", return_value=()), \
                mock.patch.object(review_authors, "_commit_receipt", return_value=None), \
                mock.patch.object(review_authors, "reference_author",
                                  return_value={"name": "author",
                                                "family": "deepseek"}):
            return merge_queue._complete_authors(_EmptyHistoryDb(), item, frozen, tmp, "main")

    def test_a_holder_without_a_commit_is_not_an_author(self):
        # #2808 : un détenteur du journal n'est auteur que par un commit de la
        # plage gelée. `author` n'a signé que la base, `rev2` n'a rien écrit :
        # seul `rev1` est auteur, et une famille inconnue ne refuse plus.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            _base, frozen = self._repo(tmp, git, "rev1")
            authors, _families = self._authors(tmp, frozen)
        self.assertEqual({entry["name"]: entry["family"] for entry in authors},
                         {"rev1": "codex"})

    def test_a_former_holder_counts_by_their_commit_not_by_the_journal(self):
        # #2808 : le détenteur d'origine reste auteur s'il a écrit dans la
        # plage ; le journal ne le nomme pas auteur à lui seul.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            _base, frozen = self._repo(tmp, git, "rev2")
            authors, _families = self._authors(tmp, frozen)
        got = {entry["name"]: entry["family"] for entry in authors}
        self.assertEqual(got.get("rev2"), "claude")
        self.assertNotIn("author", got)

    def test_an_out_of_registry_commit_author_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            _base, frozen = self._repo(tmp, git, "rev1", "tool")
            authors, _families = self._authors(tmp, frozen)
        self.assertEqual([entry["name"] for entry in authors], ["rev1"])

    def test_a_small_reviewer_correction_is_an_author(self):
        # #2888 : l'auteur d'un petit correctif de relecteur est un auteur de
        # la tête, exactement comme au-delà du seuil ; sa famille est exclue.
        # Le lecteur partagé le reconnaît par son reçu exact, pas par le nom
        # Git. #2808 : la référence n'est pas semée, elle n'a signé que la base.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, frozen = self._repo(tmp, git, "rev1")
            receipt = {"subject": "commit:" + frozen, "commit": frozen,
                       "actor": "rev1", "session": "s", "turn": "t",
                       "harness": "codex", "record": "r"}
            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            with mock.patch.object(merge_queue, "actor_family",
                                      side_effect=_family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("claude", "codex", "deepseek")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      return_value=receipt), \
                    mock.patch.object(review_authors, "designated_reviewers",
                                      return_value={"rev1"}):
                authors, _families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, frozen, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
        self.assertEqual(authors, [{"name": "rev1", "family": "codex"}])

    def test_a_big_reviewer_fix_is_an_author_despite_the_preserved_git_author(self):
        # #1515 R7 (B ameesh-cx2) : le reçu exact nomme rev1/Codex, mais le
        # commit de correction a gardé l'auteur Git du lot (cas cherry-pick).
        # À 61 lignes, le §2 en fait un auteur : un simple `git log --format=
        # %an` l'oubliait et rev1 pouvait relire son propre travail.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, _head = self._repo(tmp, git)
            DeltaBaseTest._write(tmp, "a", "a\n" + "".join(
                "extra %02d\n" % index for index in range(60)))
            git("commit", "-qam", "r1 big correction", name="author",
                committer="rev1", email_domain="ameesh.invalid")
            frozen = git("rev-parse", "HEAD")
            receipt = {"subject": "commit:" + frozen, "commit": frozen,
                       "actor": "rev1", "session": "s", "turn": "t",
                       "harness": "codex", "record": "r"}
            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            def current_family(_db, name, **_kw):
                # Le registre courant a changé de famille : la lecture
                # partagée garde la famille **historique** du reçu.
                return {"author": "deepseek", "rev1": "claude"}.get(name, "")

            with mock.patch.object(merge_queue, "actor_family",
                                      side_effect=current_family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("deepseek", "claude", "codex")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      return_value=receipt), \
                    mock.patch.object(review_authors, "reference_author",
                                      return_value={"name": "author",
                                                    "family": "deepseek"}), \
                    mock.patch.object(review_authors, "designated_reviewers",
                                      return_value={"rev1"}):
                authors, families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, frozen, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
            got = {entry["name"]: entry["family"] for entry in authors}
            self.assertEqual(got.get("author"), "deepseek")
            self.assertEqual(got.get("rev1"), "codex",
                             "le correcteur au-delà du seuil est auteur de la tête")
            # La porte ne compte plus l'avis du correcteur devenu auteur.
            marks = _marks(_mark("frozen", frozen), _verdict("rev1", sha=frozen),
                           _verdict("rev2", sha=frozen))
            with mock.patch.object(merge_queue, "actor_family", side_effect=_family):
                gate = merge_queue.review_gate(
                    None, marks, frozen, repo=tmp, klass={"class": "sensitive"},
                    item=item, families=families, required=2, authors=authors)
        self.assertFalse(gate["ok"], gate["why"])
        self.assertEqual([entry["actor"] for entry in gate["accepted"]], ["rev2"])

    def test_a_local_queue_ref_cannot_empty_the_frozen_delta(self):
        # #1515 R6 B2 (claude2) : dans le checkout de l'assigné, un
        # `git update-ref refs/ameesh/queue/main <tête gelée>` local vidait le
        # delta et rev1 relisait son propre travail. La base du checkout est
        # `origin/main`, jamais la ref de file locale.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, _head = self._repo(tmp, git)
            DeltaBaseTest._write(tmp, "a", "v1\n")
            git("commit", "-qam", "rev1 content", name="rev1",
                committer="rev1", email_domain="ameesh.invalid")
            frozen = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", base)
            git("update-ref", "refs/ameesh/queue/main", frozen)
            receipt = {"subject": "commit:" + frozen, "commit": frozen,
                       "actor": "rev1", "session": "s", "turn": "t",
                       "harness": "codex", "record": "r"}
            item = {"id": 7, "assignee": "author", "branch_target": "main"}

            def family(_db, name, **_kw):
                return {"author": "deepseek", "rev1": "codex"}.get(name, "")

            with mock.patch.object(merge_queue, "actor_family", side_effect=family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("deepseek", "claude", "codex")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      return_value=receipt), \
                    mock.patch.object(review_authors, "reference_author",
                                      return_value={"name": "author",
                                                    "family": "deepseek"}):
                authors, _families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, frozen, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
        got = {entry["name"]: entry["family"] for entry in authors}
        self.assertEqual(got.get("rev1"), "codex",
                         "la ref de file locale ne vide pas le delta du checkout")

    def test_a_merge_author_is_an_author(self):
        # #1515 B3 : une fusion peut ajouter du contenu ; son auteur ne peut
        # pas compter comme relecteur indépendant. La lecture partagée écarte
        # les fusions, `_complete_authors` doit les reprendre.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, _head = self._repo(tmp, git)
            git("checkout", "-qb", "side", base)
            DeltaBaseTest._write(tmp, "b", "side\n")
            git("add", "b")
            git("commit", "-qm", "side commit", name="rev2")
            git("checkout", "-q", "lot")
            git("merge", "--no-ff", "-m", "merge side", "side", name="merger",
                email_domain="ameesh.invalid")
            frozen = git("rev-parse", "HEAD")
            item = {"id": 7, "assignee": "author", "branch_target": "main"}

            def family(_db, name, **_kw):
                return {"author": "deepseek", "rev2": "claude",
                        "merger": "codex"}.get(name, "")

            with mock.patch.object(merge_queue, "actor_family", side_effect=family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("deepseek", "claude", "codex")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      return_value=None), \
                    mock.patch.object(review_authors, "reference_author",
                                      return_value={"name": "author",
                                                    "family": "deepseek"}):
                authors, _families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, frozen, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
        got = {entry["name"]: entry["family"] for entry in authors}
        self.assertNotIn("author", got,
                         "la référence au gel n'est pas semée (#2808)")
        self.assertEqual(got.get("rev2"), "claude")
        self.assertEqual(got.get("merger"), "codex",
                         "l'auteur d'une fusion ajoute du contenu au lot")

    def test_two_historical_families_for_a_coauthor_are_excluded_not_refused(self):
        # #3584 (décision du 26/10, notée au lot) : un ensemble de familles
        # prouvées n'est pas une famille inconnue. L'ancien refus de #2681 R8
        # (une famille vide pouvait cacher deux preuves) est remplacé par
        # l'exclusion de **chaque** famille : ni codex ni claude ne peuvent
        # compter pour rev1, et rev1 lui-même reste exclu par son nom. C'est au
        # moins aussi strict pour l'indépendance, sans bloquer la relecture
        # quand une famille hors de l'ensemble existe. Sans famille restante,
        # la porte se ferme (aucune admission implicite).
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, _head = self._repo(tmp, git)
            DeltaBaseTest._write(tmp, "a", "v1\n")
            git("commit", "-qam", "r1 codex", name="author", committer="rev1",
                email_domain="ameesh.invalid")
            first = git("rev-parse", "HEAD")
            DeltaBaseTest._write(tmp, "a", "v2\n")
            git("commit", "-qam", "r1 claude", name="author", committer="rev1",
                email_domain="ameesh.invalid")
            second = git("rev-parse", "HEAD")

            def receipt(sha, harness):
                return {"subject": "commit:" + sha, "commit": sha,
                        "actor": "rev1", "session": "s", "turn": "t",
                        "harness": harness, "record": "r"}

            receipts = {first: receipt(first, "codex"),
                        second: receipt(second, "claude")}

            def providers(_provider, _db, sha, **_kw):
                return receipts.get(sha)

            def family(_db, name, **_kw):
                return {"author": "deepseek", "rev1": "deepseek"}.get(name, "")

            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            with mock.patch.object(merge_queue, "actor_family", side_effect=family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("claude", "codex", "deepseek")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      side_effect=providers):
                authors, _families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, second, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
        got = {entry["name"]: (entry["family"], entry.get("families") or ())
               for entry in authors}
        self.assertEqual(got.get("author"), ("deepseek", ()))
        self.assertEqual(got.get("rev1"), ("", ["claude", "codex"]))
        # Sans famille restante hors de l'ensemble, la porte se ferme : ni
        # rev1 (co-auteur, et exclu par son nom) ni rev2 (claude) ne comptent.
        marks = _marks(_mark("frozen", second), _verdict("rev1", sha=second),
                       _verdict("rev2", sha=second))
        with mock.patch.object(merge_queue, "actor_family", side_effect=family):
            gate = merge_queue.review_gate(
                None, marks, second, item=item, families=FAM.values(),
                klass={"class": "sensitive"}, required=1, authors=authors,
                authors_complete=True)
        self.assertFalse(gate["ok"], gate["why"])
        self.assertEqual(gate["accepted"], [])

    def test_a_malformed_receipt_refuses_instead_of_the_git_fallback(self):
        # R8 B de cx2 : strict=True fait d'un reçu contradictoire une erreur,
        # jamais un commit inconnu reconstruit par son auteur Git.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, frozen = self._repo(tmp, git, "rev1")
            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            with mock.patch.object(merge_queue, "actor_family",
                                      side_effect=_family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("claude", "codex", "deepseek")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      side_effect=review_provenance.ProvenanceError(
                                          "contradictory provenance")):
                with self.assertRaises(merge_queue._ProvenanceError):
                    merge_queue._complete_authors(
                        _EmptyHistoryDb(), item, frozen, tmp, "main",
                        klass={"class": "sensitive", "base_sha": base}, marks=[])

    def test_two_historical_families_for_the_reference_are_excluded_not_refused(self):
        # #3584 (décision du 26/10, notée au lot) : la référence elle-même
        # peut avoir deux familles historiques. L'ancien refus de #2681 R9
        # est remplacé par l'exclusion des deux : un avis claude ou codex ne
        # compte pas, un avis d'une famille hors de l'ensemble (ici deepseek)
        # reste recevable. L'avis du relecteur de la famille de l'auteur tombe
        # donc, sans admettre implicitement le lot.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, _head = self._repo(tmp, git)
            DeltaBaseTest._write(tmp, "a", "v1\n")
            git("commit", "-qam", "author codex", name="author",
                committer="author", email_domain="ameesh.invalid")
            first = git("rev-parse", "HEAD")
            DeltaBaseTest._write(tmp, "a", "v2\n")
            git("commit", "-qam", "author claude", name="author",
                committer="author", email_domain="ameesh.invalid")
            second = git("rev-parse", "HEAD")

            def receipt(sha, harness):
                return {"subject": "commit:" + sha, "commit": sha,
                        "actor": "author", "session": "s", "turn": "t",
                        "harness": harness, "record": "r"}

            receipts = {first: receipt(first, "codex"),
                        second: receipt(second, "claude")}

            def providers(_provider, _db, sha, **_kw):
                return receipts.get(sha)

            def family(_db, name, **_kw):
                return {"author": "", "rev2": "claude", "dev1": "deepseek"}.get(name, "")

            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            with mock.patch.object(merge_queue, "actor_family", side_effect=family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("claude", "codex", "deepseek")), \
                    mock.patch.object(review_authors, "_commit_receipt",
                                      side_effect=providers):
                authors, _families = merge_queue._complete_authors(
                    _EmptyHistoryDb(), item, second, tmp, "main",
                    klass={"class": "sensitive", "base_sha": base}, marks=[])
        self.assertEqual(authors, [{"name": "author", "family": "",
                                    "families": ["claude", "codex"]}])
        # La porte exclut claude et codex : seul dev1 (deepseek) compte.
        marks = _marks(_mark("frozen", second), _verdict("rev2", sha=second),
                       _verdict("dev1", sha=second))
        with mock.patch.object(merge_queue, "actor_family", side_effect=family):
            gate = merge_queue.review_gate(
                None, marks, second, item=item, families=FAM.values(),
                klass={"class": "sensitive"}, required=1, authors=authors,
                authors_complete=True)
        self.assertTrue(gate["ok"], gate)
        self.assertEqual([row["actor"] for row in gate["accepted"]], ["dev1"])

    def test_an_unreadable_author_range_refuses(self):
        # Une plage que le lecteur partagé ne peut pas lire ne laisse pas
        # supposer l'absence d'auteur : la porte refuse.
        with tempfile.TemporaryDirectory() as tmp:
            git = self._git(tmp)
            base, frozen = self._repo(tmp, git, "rev1")
            item = {"id": 7, "assignee": "author", "branch_target": "main"}
            with mock.patch.object(merge_queue, "actor_family",
                                      side_effect=_family), \
                    mock.patch.object(review_lot, "compiled_families",
                                      return_value=("claude", "codex", "deepseek")), \
                    mock.patch.object(review_authors, "reading",
                                      side_effect=RuntimeError("range down")):
                with self.assertRaises(merge_queue._ProvenanceError):
                    merge_queue._complete_authors(
                        _EmptyHistoryDb(), item, frozen, tmp, "main",
                        klass={"class": "sensitive", "base_sha": base}, marks=[])


if __name__ == "__main__":
    unittest.main()
