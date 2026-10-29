# SPDX-License-Identifier: AGPL-3.0-only
"""#1104 — une fusion enregistrée **hors file** ouvre le lot de relecture L539.

Constat de l'orchestrateur (13/10) : un seul `review-after-merge:#…` en base
pour ~1019 lots, parce que le lot de relecture n'était ouvert que par
l'atterrissage de la file de fusion. Témoin : une fusion constatée par le
relevé des branches (L118) ou déclarée (`ameesh work merged`) l'ouvre aussi,
une seule fois ; une fusion dont la classe ne peut pas être lue (pas de gel,
dépôt illisible) n'ouvre rien.

Le canon est un **vrai** canon minimal (`default: light`) et la fusion est
constatée par git : la classe est calculée, pas doublée.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import os
import subprocess
import threading
import unittest
import uuid
from unittest import mock

from ameesh import mail, plan_git, process_registry, registry, review_lot, storage, work

from .support import PgTestCase


def _git(repo, *args, env=None):
    base = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    base.update(env or {})
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=base)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class OpenForMergeRuleTest(unittest.TestCase):
    """La règle d'entrée : déjà ouvert (aucune relecture de classe), un doute
    n'ouvre rien, une lecture impossible ne lève jamais."""

    def opened(self):
        return [{"id": 7, "state": "merged"}]

    def test_un_lot_deja_ouvert_ne_relit_pas_la_classe(self):
        ops = mock.Mock()
        ops.by_issue_ref.return_value = self.opened()
        with mock.patch.object(review_lot.storage, "of", return_value=mock.Mock(work=ops)), \
                mock.patch.object(review_lot.work, "get", return_value={"id": 1}), \
                mock.patch.object(review_lot, "class_at_merge") as klass:
            out = review_lot.open_for_merge(None, 1)
        self.assertFalse(out["created"])
        self.assertEqual(out["id"], 7)
        klass.assert_not_called()

    def test_un_doute_n_ouvre_rien(self):
        ops = mock.Mock()
        ops.by_issue_ref.return_value = []
        ops.open_by_ref.return_value = []
        ops.events.return_value = []
        with mock.patch.object(review_lot.storage, "of", return_value=mock.Mock(work=ops)), \
                mock.patch.object(review_lot.work, "get",
                                  return_value={"id": 1, "assignee": "a"}), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "sensitive", "why": "pas de gel"}), \
                mock.patch.object(review_lot.work, "add") as add:
            out = review_lot.open_for_merge(None, 1)
        self.assertFalse(out["created"])
        self.assertEqual(out["class"], "sensitive")
        add.assert_not_called()

    def test_un_avis_ne_rouvre_pas_la_relecture_d_un_avis(self):
        """R codex3 #1280 B2 : `open_for_merge` sur un avis (même déclaré
        `merged`) ne crée pas la relecture de cet avis, et ne lit même pas sa
        classe — un avis n'a pas de code à relire."""
        ops = mock.Mock()
        ops.by_issue_ref.return_value = []
        ops.open_by_ref.return_value = []
        ops.events.return_value = []
        review = {"id": 5, "title": "REVUE #9 : relecture après fusion",
                  "source": "review-after-merge:#9",
                  "issue_ref": "review-after-merge:#9", "state": "merged"}
        with mock.patch.object(review_lot.storage, "of",
                               return_value=mock.Mock(work=ops)), \
                mock.patch.object(review_lot.work, "get", return_value=review), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "light"}) as klass, \
                mock.patch.object(review_lot.work, "add") as add:
            out = review_lot.open_for_merge(None, 5)
        self.assertFalse(out["created"])
        self.assertIn("avis", out["why"])
        klass.assert_not_called()
        add.assert_not_called()

    def test_une_lecture_impossible_ne_leve_pas(self):
        with mock.patch.object(review_lot.storage, "of",
                               side_effect=RuntimeError("base injoignable")):
            out = review_lot.open_for_merge(None, 3)
        self.assertFalse(out["created"])
        self.assertIn("RuntimeError", out["why"])


class ClassAtMergeRuleTest(unittest.TestCase):
    """R1 #1104 P2-1 : sans intervalle de fusion **prouvé**, `of_lot` n'est pas
    appelé — son repli gel/cible relirait `cible..gel`, vide dès que le gel est
    ancêtre de la cible, et classerait « light » un lot SQL fusionné."""

    ITEM = {"id": 1, "assignee": "a", "app": "ameesh"}
    FREEZE = [{"kind": "frozen", "sha": "f" * 40, "id": 1, "at_ts": 1}]

    def call(self, diff):
        with mock.patch.object(review_lot, "_repo_of_agent", return_value="/depot"), \
                mock.patch.object(review_lot.work, "milestones", return_value=self.FREEZE), \
                mock.patch("ameesh.plan_git.latest_freeze",
                           return_value={"kind": "frozen", "sha": "f" * 40}), \
                mock.patch("ameesh.lot_measures.diff_range", **diff), \
                mock.patch("ameesh.lot_measures.diff_paths",
                           return_value=["docs/preuve.md"]), \
                mock.patch.object(review_lot.lot_class, "of_lot") as of_lot:
            out = review_lot.class_at_merge(None, 1, item=dict(self.ITEM), sha="s" * 40)
        return out, of_lot

    def test_une_panne_de_lecture_du_diff_ne_classe_pas_light(self):
        out, of_lot = self.call({"side_effect": RuntimeError("panne de lecture")})
        self.assertFalse(of_lot.called, "of_lot ne classe pas un doute")
        self.assertEqual(out["class"], "sensitive")
        self.assertTrue(out["doubt"])
        self.assertIn("lecture du diff impossible", out["why"])

    def test_un_intervalle_introuvable_ne_classe_pas_light(self):
        """Gel lisible et ancêtre de la cible, commit de fusion absent du
        dépôt : le repli serait un diff vide, donc « light »."""
        out, of_lot = self.call({"return_value": None})
        self.assertFalse(of_lot.called, "of_lot ne classe pas un doute")
        self.assertEqual(out["class"], "sensitive")
        self.assertTrue(out["doubt"])
        self.assertIn("ne contient pas le commit", out["why"])

    def test_un_intervalle_prouve_est_transmis_a_of_lot(self):
        out, of_lot = self.call({"return_value": ("base", "head")})
        of_lot.assert_called_once()
        self.assertEqual(of_lot.call_args.kwargs["base"], "base")
        self.assertEqual(of_lot.call_args.kwargs["head"], "head")
        self.assertIs(out, of_lot.return_value)


class _Witness(PgTestCase):
    """Un dépôt réel, un canon minimal (`light` par défaut, `**/*.sql`
    sensible), un agent placé dessus : de quoi constater une vraie fusion et
    lire une vraie classe."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "canon-test")
        os.makedirs(self.root)
        self.write(os.path.join(self.root, "index.md"),
                   "---\ntype: Index\ntitle: canon de test\n---\n")
        self.write(os.path.join(self.root, "federation.yaml"),
                   "id: test\nmembers: []\nreview_policies:\n  risk_classes:\n"
                   "    default: light\n    rules:\n"
                   "      - paths: ['**/*.sql']\n        class: sensitive\n")
        patcher = mock.patch.object(
            self.db, "cfg",
            dataclasses.replace(self.db.cfg, canon=self.root, canon_untrusted=True,
                                require_responsible=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        # #1286 : la preuve git de la fusion déclarée (dépôt du lot, ascendance
        # dans la cible, message citant le lot) a son propre témoin
        # (`test_1286_declared_merge_proof`) ; ces classes portent sur
        # l'ouverture de la relecture après fusion, pas sur git — la preuve est
        # neutralisée comme dans `test_l317` et `test_l56`.
        proof = mock.patch.object(work, "_declared_merge_commit",
                                  side_effect=lambda item, sha, **kw: sha)
        proof.start()
        self.proof_patcher = proof     # #3201 : un témoin lit la preuve réelle
        self.addCleanup(proof.stop)
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        registry.upsert(self.db, "claude1", harness="deepseek", host=self.cfg.host,
                        cwd=self.repo)

    def write(self, path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, name, text=None):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.write(path, text if text is not None else name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def lot(self, title="un lot leger"):
        return work.add(self.db, title=title, app="ameesh", assignee="claude1",
                        actor="orch")["id"]

    def frozen_on_branch(self, lot, branch, name="docs/preuve.md"):
        """Une branche portant un changement `light`, gelée sur sa pointe."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(name)
        sha = _git(self.repo, "rev-parse", "HEAD")
        # Repository discovery is not this witness: unassigned imported lots
        # otherwise search the real host's neighbouring worktrees for the SHA.
        from ameesh import review_designation_activation
        with mock.patch.object(review_designation_activation, "_repository", return_value=self.repo):
            work.milestone(self.db, lot, "frozen", sha=sha, actor="claude1", repo=self.repo)
        return sha

    def review_lots(self, lot):
        return storage.of(self.db).work.by_issue_ref(review_lot.ref_of(lot))

    def merged_lot(self, title="lot fusionne", *, promoted=False, provided=False,
                   sha="abcdef1234"):
        """Un lot fusionné (`merged`, ou `promoted`), pourvu ou non de sa
        relecture. La classe n'est pas l'objet du témoin : le lot est fusionné
        par la voie déclarée, sans git."""
        lot = self.lot(title)
        done = work.merged(self.db, lot, sha=sha, actor="claude1")
        self.assertEqual(done["result"], "merged", done)
        if promoted:
            work.move(self.db, lot, "promoted", note="témoin", actor="claude1")
        if provided:
            review_lot.open_after_merge(self.db, lot=lot, klass={"class": "light"},
                                        merged_sha=sha)
        return lot


class MissingRefSelectionTest(_Witness):
    """R2 #1104 P2-2 : `items_missing_ref` sélectionne les manquants **avant**
    la borne — les pourvus ne consomment plus la limite — et `merged` et
    `promoted` forment un seul ensemble ordonné par récence."""

    def rows(self, limit):
        return storage.of(self.db).work.items_missing_ref(
            states=work.MERGED_STATES, ref_prefix=review_lot.PREFIX + "#", limit=limit)

    def test_la_borne_porte_sur_les_manquants_pas_sur_les_pourvus(self):
        old = self.merged_lot("manquant ancien")
        for i in range(5):
            self.merged_lot("fourni %d" % i, provided=True)
        self.assertEqual([int(r["id"]) for r in self.rows(1)], [old],
                         "un manquant ancien caché par des pourvus plus récents")
        recent = self.merged_lot("manquant recent")
        self.assertEqual([int(r["id"]) for r in self.rows(1)], [recent])
        self.assertEqual(sorted(int(r["id"]) for r in self.rows(10)), sorted([old, recent]))

    def test_un_promoted_manquant_recent_passe_avant_un_merged_manquant_ancien(self):
        old = self.merged_lot("manquant fusionne")
        promoted = self.merged_lot("manquant promu", promoted=True)
        self.assertEqual([int(r["id"]) for r in self.rows(1)], [promoted],
                         "le promoted manquant est affamé par le merged")
        self.assertEqual(sorted(int(r["id"]) for r in self.rows(10)), sorted([old, promoted]))


class SweepWitnessTest(_Witness):
    """Le relevé des branches (L118), constat d'une fusion manuelle locale."""

    def test_une_fusion_locale_ouvre_le_lot_de_relecture(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-preuve")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-preuve")
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        item = work.get(self.db, rows[0]["id"])
        self.assertEqual((item["state"], item["assignee"], item["type"], item["app"]),
                         ("intake", None, "evolution", "ameesh"))
        self.assertIn("Classe : light", item["body"])
        self.assertNotIn("no file changed", item["body"])   # classe du diff fusionné
        self.assertIn("23:59 Europe/Paris", item["body"])
        self.assertIn("codex", item["body"])       # famille opposée à deepseek

    def test_une_fusion_light_sur_la_porte_de_fusion_ouvre_deux_avis(self):
        """#2374 : `merge_pr.py` est classé `light` par le canon, mais il est
        la porte de fusion (`CRITICAL_SCOPE`) : la relecture après fusion doit
        être sensible et demander deux familles, pas un avis léger."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/2374-porte", name="src/ameesh/merge_pr.py")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/2374-porte")
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        body = work.get(self.db, rows[0]["id"])["body"]
        self.assertIn("Classe : sensitive", body)
        self.assertIn("**porte de fusion** : deux avis indépendants", body)
        self.assertIn("deux familles distinctes", body)
        self.assertIn("Avis requis : **2**", body)
        self.assertNotIn("§2, lot léger", body)

    def test_une_fusion_sensible_n_ouvre_rien(self):
        """Le piège du gel devenu ancêtre : `cible..gel` est vide après la
        fusion et se lirait « light ». La classe est lue sur le diff que la
        fusion a fait entrer — `**/*.sql` est sensible, rien ne s'ouvre."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-sensible", name="db/x.sql")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-sensible")
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_une_fusion_sans_delta_ne_se_classe_pas_light_sur_un_diff_vide(self):
        """#2374, second cas (#2340) : l'arbre du merge égale son premier
        parent (le delta du lot est déjà porté par un autre commit de la
        cible). Le diff de fusion est vide ; la classe se relit sur le delta
        du lot — `db/x.sql` est sensible, donc rien ne s'ouvre — au lieu de
        conclure « light » sur le vide."""
        lot = self.lot()
        sha = self.frozen_on_branch(lot, "lot/2374-vide", name="db/x.sql")
        _git(self.repo, "checkout", "-q", "develop")
        # le contenu entre d'abord par cherry-pick : le merge qui suit n'ajoute
        # aucun fichier (arbre du merge = arbre du premier parent)
        # Une cible divergente empêche le cherry-pick de réutiliser le SHA du
        # gel si les deux commits sont créés dans la même seconde.
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "cible divergente")
        _git(self.repo, "cherry-pick", sha)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/2374-vide")
        merged = _git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD^2"), sha)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD^{tree}"),
                         _git(self.repo, "rev-parse", "HEAD^1^{tree}"))
        klass = review_lot.class_at_merge(self.db, lot, repo=self.repo,
                                         target="develop", sha=merged)
        self.assertEqual(klass["class"], "sensitive", klass)
        self.assertFalse(klass.get("doubt"), klass)
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_la_relecture_ne_s_ouvre_qu_une_fois(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-une-fois")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-une-fois")
        self.assertEqual(plan_git.sync_branches(self.db, host=self.cfg.host)["merged"], 1)
        self.assertEqual(plan_git.sync_branches(self.db, host=self.cfg.host)["merged"], 0)
        self.assertEqual(len(self.review_lots(lot)), 1)


class CatchUpWitnessTest(_Witness):
    """R1 #1104 P2-2 : une ouverture ratée au moment de la fusion est reprise,
    durablement et sans duplication — la fusion reste acquise."""

    def merge_lot(self, lot, branch="lot/1104-reprise"):
        self.frozen_on_branch(lot, branch)
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot, branch)
        return _git(self.repo, "rev-parse", "HEAD")

    def test_le_releve_suivant_retrouve_le_parent_fusionne(self):
        lot = self.lot()
        self.merge_lot(lot)
        with mock.patch.object(review_lot, "open_for_merge",
                               side_effect=RuntimeError("panne d'écriture")):
            first = plan_git.sync_branches(self.db, host=self.cfg.host)
        self.assertEqual(first["merged"], 1)
        self.assertEqual(self.review_lots(lot), [], "la panne a bien perdu la relecture")
        # panne passée : le relevé récurrent (qui ne sélectionne plus un lot
        # fusionné) rattrape la relecture manquante. La tentative en panne a
        # été marquée (binôme 14/10) : le relevé immédiat l'exclut le temps du
        # délai — on recule la marque pour éprouver la reprise après le délai.
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - interval '2 hours'"
            " WHERE work_item_id = %s AND split_part(note, ' ', 1) = %s",
            (lot, review_lot.ATTEMPT_PREFIX + str(lot)))
        second = plan_git.sync_branches(self.db, host=self.cfg.host)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(second["review_catch_up"]["opened"], 1)
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        # reprise sans duplication
        third = plan_git.sync_branches(self.db, host=self.cfg.host)
        self.assertEqual(third["review_catch_up"]["opened"], 0)
        self.assertEqual(len(self.review_lots(lot)), 1)

    def test_un_light_critique_non_ouvert_reste_reessayable(self):
        """#2374 : l'échec d'ouverture d'une relecture sensible (chemin
        critique classé light) ne doit pas devenir terminal — la classe du
        diff fusionné reste `light`, la relecture à deux avis reste due."""
        lot = self.merged_lot("porte de fusion non ouverte")
        klass = {"class": "light", "why": "policy",
                 "files": [{"path": "src/ameesh/merge_pr.py", "class": "light"}]}
        with mock.patch.object(review_lot, "class_at_merge", return_value=klass), \
                mock.patch.object(review_lot.work, "add",
                                  side_effect=work.WorkError("refusé")):
            first = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((first["examined"], first["missing"], first["attempted"],
                          first["terminal"]), (1, 1, 1, 0), first)
        self.assertEqual(self.review_lots(lot), [])
        with mock.patch.object(review_lot, "class_at_merge", return_value=klass):
            second = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((second["examined"], second["opened"]), (1, 1), second)
        body = work.get(self.db, self.review_lots(lot)[0]["id"])["body"]
        self.assertIn("Classe : sensitive", body)
        self.assertIn("Avis requis : **2**", body)

    def test_un_lot_deja_fusionne_reprend_l_ouverture(self):
        lot = self.lot()
        sha = self.merge_lot(lot, "lot/1104-deja")
        with mock.patch.object(review_lot, "open_for_merge",
                               side_effect=RuntimeError("panne d'écriture")):
            done = work.close_merged(self.db, lot, sha=sha, actor="claude1", source="test")
        self.assertEqual(done["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])
        again = work.close_merged(self.db, lot, sha=sha, actor="claude1", source="test")
        self.assertEqual(again["result"], "already", again)
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        third = work.close_merged(self.db, lot, sha=sha, actor="claude1", source="test")
        self.assertEqual(third["result"], "already")
        self.assertEqual(len(self.review_lots(lot)), 1)

    def test_un_lot_fusionne_avant_l_activation_ne_reprend_pas(self):
        """B1 de la R1 : `close_merged` rappelé (`already`, comme le fait
        `sync-github`) sur un lot fusionné **avant** l'activation ne relance pas
        l'ouverture — sinon chaque passage rendrait une relecture due le jour
        même aux ~1019 fusions historiques. Le lot reste au rattrapage borné,
        qui l'écarte aussi ; le prédicat partagé le dit."""
        lot = self.lot()
        sha = self.merge_lot(lot, "lot/1104-historique")
        with mock.patch.object(review_lot, "open_for_merge",
                               side_effect=RuntimeError("panne d'écriture")):
            done = work.close_merged(self.db, lot, sha=sha, actor="claude1", source="test")
        self.assertEqual(done["result"], "merged", done)
        self.assertEqual(self.review_lots(lot), [])
        self.assertTrue(review_lot.catch_up_eligible(self.db, lot),
                        "fusionné à l'instant : le rattrapage le reprendrait")
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - interval '1 day'"
            " WHERE work_item_id = %s AND kind = 'merged'", (lot,))
        self.assertFalse(review_lot.catch_up_eligible(self.db, lot),
                         "fusion antérieure à la borne : rien n'est dû")
        # une classe lisible ouvrirait la relecture : seule la borne l'empêche
        # (une classe en panne serait avalée par `open_for_merge`, best effort,
        # et ne distinguerait pas le correctif)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}) as klass:
            again = work.close_merged(self.db, lot, sha=sha, actor="claude1", source="test")
        self.assertEqual(again["result"], "already", again)
        klass.assert_not_called()
        self.assertEqual(self.review_lots(lot), [], "relecture historique non due")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            caught = review_lot.catch_up(self.db)
        self.assertEqual((caught["examined"], caught["opened"]), (0, 0), caught)

    def test_saturation_vingt_pourvus_ne_cachent_pas_un_manquant_ancien(self):
        """R2 #1104 P2-2, témoin de saturation 1 : vingt parents **pourvus**
        plus récents ne cachent plus un parent ancien dont l'ouverture a
        échoué. L'ancien code consommait sa limite sur les vingt pourvus
        (`examined=20, missing=0, opened=0` à chaque passage)."""
        old = self.merged_lot("manquant ancien")
        for i in range(review_lot.CATCH_UP_LIMIT):
            self.merged_lot("fourni %d" % i, provided=True)
        self.assertEqual(self.review_lots(old), [])
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            first = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((first["examined"], first["missing"], first["opened"]), (1, 1, 1),
                         first)
        rows = self.review_lots(old)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(work.get(self.db, old)["state"], "delivered", "la fusion reste acquise")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            second = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((second["examined"], second["opened"]), (0, 0), second)
        self.assertEqual(len(self.review_lots(old)), 1, "reprise sans duplication")

    def test_saturation_vingt_pourvus_ne_cachent_pas_un_promoted_manquant(self):
        """R2 #1104 P2-2, témoin de saturation 2 : un `promoted` manquant plus
        récent n'est plus affamé par vingt `merged` pourvus — les états sont
        un seul ensemble ordonné, la sélection précède la borne."""
        for i in range(review_lot.CATCH_UP_LIMIT):
            self.merged_lot("fourni %d" % i, provided=True)
        promoted = self.merged_lot("promu manquant", promoted=True)
        self.assertEqual(self.review_lots(promoted), [])
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            first = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((first["examined"], first["missing"], first["opened"]), (1, 1, 1),
                         first)
        self.assertEqual(len(self.review_lots(promoted)), 1)
        self.assertEqual(work.get(self.db, promoted)["state"], "delivered")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            second = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((second["examined"], second["opened"]), (0, 0), second)
        self.assertEqual(len(self.review_lots(promoted)), 1)


    def test_vingt_sensibles_confirmes_ne_cachent_pas_un_light_ancien(self):
        """Contre-épreuve du binôme (14/10), resserrée par l'arbitrage du 15/10 :
        vingt candidats SQL **prouvés** non-light (sensitive, doute=False) plus
        récents ne consomment plus chaque passage — leur classe confirmée est
        **terminale** (une seule lecture, aucune tentative horaire perpétuelle) ;
        le light ancien est atteint au passage suivant."""
        old = self.merged_lot("light ancien")
        for i in range(review_lot.CATCH_UP_LIMIT):
            self.merged_lot("sensible %d" % i)

        def klass(_db, lot, **_kw):
            if int(lot) == old:
                return {"class": "light", "why": "témoin light"}
            return {"class": "sensitive", "why": "témoin SQL", "doubt": False}

        with mock.patch.object(review_lot, "class_at_merge", side_effect=klass):
            first = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((first["examined"], first["missing"], first["opened"]),
                         (review_lot.CATCH_UP_LIMIT, review_lot.CATCH_UP_LIMIT, 0), first)
        self.assertEqual(first["attempted"], 0, first)
        self.assertEqual(first["terminal"], review_lot.CATCH_UP_LIMIT, first)
        self.assertEqual(self.review_lots(old), [])
        with mock.patch.object(review_lot, "class_at_merge", side_effect=klass):
            second = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((second["examined"], second["missing"], second["opened"]), (1, 1, 1),
                         second)
        self.assertEqual(len(self.review_lots(old)), 1)
        with mock.patch.object(review_lot, "class_at_merge", side_effect=klass):
            third = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((third["examined"], third["opened"]), (0, 0), third)

    def test_vingt_doutes_ne_cachent_pas_un_light_ancien(self):
        """Même contre-épreuve avec vingt classes en **doute** (intervalle de
        fusion illisible) : le doute est marqué puis réessayé après le délai,
        et le light ancien passe au passage suivant."""
        old = self.merged_lot("light ancien")
        for i in range(review_lot.CATCH_UP_LIMIT):
            self.merged_lot("doute %d" % i)

        def klass(_db, lot, **_kw):
            if int(lot) == old:
                return {"class": "light", "why": "témoin light"}
            return {"class": "sensitive", "why": "gel illisible", "doubt": True}

        with mock.patch.object(review_lot, "class_at_merge", side_effect=klass):
            first = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((first["examined"], first["missing"], first["opened"]),
                         (review_lot.CATCH_UP_LIMIT, review_lot.CATCH_UP_LIMIT, 0), first)
        self.assertEqual(first["attempted"], review_lot.CATCH_UP_LIMIT, first)
        with mock.patch.object(review_lot, "class_at_merge", side_effect=klass):
            second = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((second["examined"], second["missing"], second["opened"]), (1, 1, 1),
                         second)
        self.assertEqual(len(self.review_lots(old)), 1)

    def test_une_erreur_est_retentee_apres_le_delai(self):
        """Reprise des erreurs : une tentative en panne est marquée puis exclue
        le temps du délai ; le délai écoulé (`retry_s=0`), elle est réessayée
        et ouvre — l'échec n'est ni perdu ni rejoué en boucle."""
        lot = self.merged_lot("erreur de rattrapage")
        with mock.patch.object(review_lot, "open_for_merge",
                               side_effect=RuntimeError("panne d'écriture")), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "light", "why": "témoin"}):
            first = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((first["examined"], first["failed"]), (1, 1), first)
        self.assertEqual(self.review_lots(lot), [])
        second = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT)
        self.assertEqual((second["examined"], second["failed"]), (0, 0), second)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            third = review_lot.catch_up(self.db, limit=review_lot.CATCH_UP_LIMIT,
                                        retry_s=0)
        self.assertEqual((third["examined"], third["missing"], third["opened"]), (1, 1, 1),
                         third)
        self.assertEqual(len(self.review_lots(lot)), 1)

    # -- arbitrage pilote du 15/10 : borne d'activation, terminal, borné ------

    def test_la_borne_d_activation_ecarte_l_historique(self):
        """Un lot déjà fusionné **avant** l'activation (la borne écrite par la
        migration 0438) n'est pas rattrapé ; un lot fusionné après l'est. La
        borne est écrite à l'activation, pas au premier passage : un lot
        fusionné avant la première exécution du rattrapage ne serait pas
        repris autrement (le P2 de la R2 #1104)."""
        before = self.merged_lot("fusionne avant l'activation")
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - interval '1 day'"
            " WHERE work_item_id = %s AND kind = 'merged'", (before,))
        after = self.merged_lot("fusionne apres l'activation")
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            out = review_lot.catch_up(self.db)
        self.assertEqual((out["examined"], out["missing"], out["opened"]), (1, 1, 1), out)
        self.assertTrue(out["bound"], out)
        self.assertEqual(self.review_lots(before), [])
        self.assertEqual(len(self.review_lots(after)), 1)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            again = review_lot.catch_up(self.db)
        self.assertEqual((again["examined"], again["opened"]), (0, 0), again)

    def test_la_borne_est_commune_et_conservee(self):
        """La borne vit dans la base partagée : un second exécuteur (autre
        connexion, comme après un redémarrage) lit la même valeur, et le
        rattrapage la rend telle quelle — aucune réactivation au passage."""
        bound = storage.of(self.db).work.review_catch_up_bound()
        self.assertIsNotNone(bound, "la migration 0438 doit écrire la borne")
        other = self.connect()
        self.addCleanup(other.close)
        self.assertEqual(storage.of(other).work.review_catch_up_bound(), bound,
                         "la borne n'est pas commune aux exécuteurs")
        out = review_lot.catch_up(self.db)
        self.assertEqual(out["bound"], bound, out)
        self.assertEqual(storage.of(self.db).work.review_catch_up_bound(), bound)

    def test_un_non_light_confirme_est_terminal_une_seule_fois(self):
        """Une classe non-light **confirmée** (ici un `.sql` prouvé par le diff
        fusionné) est terminale : une seule lecture, aucune tentative horaire
        perpétuelle, et jamais de lot de relecture (canon §2)."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-sql-terminal", name="db/x.sql")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-sql-terminal")
        # N1 de la R1 : le relevé porte lui-même un rattrapage borné
        # (`sync_branches`) qui marquerait la classe terminale avant nous ;
        # on l'isole pour que la valeur attendue dise qui marque — ici,
        # c'est bien le `catch_up` explicite, une seule fois.
        with mock.patch.object(review_lot, "catch_up", return_value={}):
            plan_git.sync_branches(self.db, host=self.cfg.host)   # le relevé fusionne
        first = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((first["examined"], first["terminal"]), (1, 1), first)
        second = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual(second["examined"], 0, second)
        self.assertEqual(self.review_lots(lot), [])
        events = storage.of(self.db).work.events(lot, 50)
        terminal = [e for e in events
                    if str(e.get("note") or "").startswith(
                        review_lot.TERMINAL_PREFIX + str(lot))]
        self.assertEqual(len(terminal), 1, terminal)
        self.assertIn("non-light", str(terminal[0].get("note") or ""))

    def test_un_doute_est_retente_borne_puis_terminal(self):
        """Un candidat **douteux** (ici une fusion déclarée dont le commit est
        absent du dépôt) est réessayé, mais un nombre borné de fois : au plus
        `CATCH_UP_MAX_ATTEMPTS` lectures, puis terminal — jamais une tentative
        horaire perpétuelle."""
        lot = self.merged_lot("doute borne")
        for _ in range(review_lot.CATCH_UP_MAX_ATTEMPTS - 1):
            out = review_lot.catch_up(self.db, retry_s=0)
            self.assertEqual((out["examined"], out["attempted"], out["terminal"]),
                             (1, 1, 0), out)
        last = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((last["examined"], last["attempted"], last["terminal"]),
                         (1, 0, 1), last)
        after = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual(after["examined"], 0, after)
        self.assertEqual(self.review_lots(lot), [])
        events = storage.of(self.db).work.events(lot, 50)
        terminals = [e for e in events
                     if str(e.get("note") or "").startswith(
                         review_lot.TERMINAL_PREFIX + str(lot))]
        self.assertEqual(len(terminals), 1, terminals)
        self.assertIn("doute", str(terminals[0].get("note") or ""))

    def test_les_tentatives_anciennes_comptent_malgre_200_evenements(self):
        """#1448 N2 : `attempts()` ne lisait que les 200 derniers événements ;
        un lot très bavard remettait son compteur de tentatives à zéro et
        retentait sans fin au lieu de devenir terminal. Le compteur porte sur
        les seules notes de tentative du lot, aussi anciennes soient-elles."""
        lot = self.merged_lot("doute bavard")
        for _ in range(review_lot.CATCH_UP_MAX_ATTEMPTS - 1):
            out = review_lot.catch_up(self.db, retry_s=0)
            self.assertEqual((out["examined"], out["attempted"], out["terminal"]),
                             (1, 1, 0), out)
        # Bruit postérieur : sans la correction, les marques de tentative
        # sortent de la fenêtre des 200 derniers événements.
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " SELECT %s, 'merged', 'bruit ' || g, 'témoin'"
            "   FROM generate_series(1, 250) g", (lot,))
        last = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((last["examined"], last["attempted"], last["terminal"]),
                         (1, 0, 1), last)
        after = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual(after["examined"], 0, after)
        self.assertEqual(self.review_lots(lot), [])

    def test_deux_executeurs_ne_doublent_pas_la_relecture(self):
        """Concurrence entre exécuteurs : l'autre exécuteur ouvre la relecture
        entre la sélection et le geste ; le second ne double pas le lot, ne
        marque pas de tentative et le compte `already` le dit."""
        lot = self.merged_lot("course entre executeurs")
        other = self.connect()
        self.addCleanup(other.close)
        real = review_lot.open_for_merge
        calls = {"n": 0}

        def racy(db, lot_id, **kw):
            if calls["n"] == 0:
                calls["n"] += 1
                review_lot.catch_up(other)      # l'autre exécuteur gagne la course
            return real(db, lot_id, **kw)

        with mock.patch.object(review_lot, "open_for_merge", side_effect=racy), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "light", "why": "témoin"}):
            out = review_lot.catch_up(self.db)
        self.assertEqual((out["opened"], out["already"]), (0, 1), out)
        self.assertEqual(len(self.review_lots(lot)), 1, "relecture doublée")
        events = storage.of(self.db).work.events(lot, 50)
        attempts = [e for e in events
                    if str(e.get("note") or "").startswith(
                        review_lot.ATTEMPT_PREFIX + str(lot))]
        self.assertEqual(attempts, [], attempts)

    def test_un_hote_sans_depot_ne_tente_ni_n_avertit_pas(self):
        """#3426 : l'hôte sans clone ne journalise aucune tentative et
        n'envoie aucun courrier — la relecture reste à l'hôte qui porte le
        dépôt (le faux « aucun dépôt lisible » d'ameesh-agents-1)."""
        lot = self.merged_lot("hôte sans dépôt")
        registry.upsert(self.db, "pilote", harness="claude", host=self.cfg.host,
                        cwd=self.repo)
        warn = mock.Mock()
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "pilote"}), \
                mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None), \
                mock.patch.object(review_lot, "_warn_unopened", warn):
            first = review_lot.catch_up(self.db, host="ameesh-agents-1")
        self.assertEqual((first["examined"], first["missing"], first["deferred"],
                          first["attempted"], first["unopened"]),
                         (1, 1, 1, 0, []), first)
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "pilote"}), \
                mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None), \
                mock.patch.object(review_lot, "_warn_unopened", warn):
            second = review_lot.catch_up(self.db, retry_s=0, host="ameesh-agents-1")
        self.assertEqual((second["deferred"], second["attempted"]), (1, 0), second)
        warn.assert_not_called()
        self.assertEqual(mail.unread_active(self.db, "pilote"), [])
        events = storage.of(self.db).work.events(lot, 50)
        attempts = [e for e in events
                    if str(e.get("note") or "").startswith(
                        review_lot.ATTEMPT_PREFIX + str(lot))]
        self.assertEqual(attempts, [], "un hôte sans dépôt ne marque rien")

    def test_un_depot_sans_le_commit_avertit_le_pilote(self):
        """#3201/#3384/#3426 : sur un hôte muni d'un dépôt, une relecture qui
        reste sans classement n'est pas seulement journalisée. La deuxième
        tentative envoie un courrier aux pilotes déclarés, qui nomme le lot et
        le geste de reprise ; les tentatives suivantes ne le renvoient pas."""
        lot = self.merged_lot("dépôt sans le commit")
        registry.upsert(self.db, "pilote", harness="claude", host=self.cfg.host,
                        cwd=self.repo)
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "pilote"}):
            out = review_lot.catch_up(self.db, host="ameesh-agents-2")
            self.assertEqual(out["attempted"], 1, out)
            self.assertEqual(out["unopened"], [], out)
            self.assertEqual(mail.unread_active(self.db, "pilote"), [])
            out = review_lot.catch_up(self.db, retry_s=0, host="ameesh-agents-2")
            self.assertEqual([entry["lot"] for entry in out["unopened"]], [lot], out)
            self.assertIn("avertissement envoyé", out["warning"], out)
            messages = mail.unread_active(self.db, "pilote")
            self.assertEqual(len(messages), 1, messages)
            body = messages[0]["body"]
            self.assertIn("#%d" % lot, body)
            self.assertIn("ameesh work merged", body)
            again = review_lot.catch_up(self.db, retry_s=0, host="ameesh-agents-2")
            self.assertEqual(again["unopened"], [], again)
            self.assertEqual(len(mail.unread_active(self.db, "pilote")), 1,
                             "l'avertissement est renvoyé à chaque tentative")

    def test_l_avertissement_ne_part_qu_aux_pilotes_du_projet(self):
        """#3453 : chaque pilote ne reçoit que les lots de son projet — un lot
        ameesh ne part plus à l'orchestrateur nexlink, ni l'inverse."""
        ours = self.merged_lot("lot ameesh")
        theirs = work.add(self.db, title="lot nexlink", app="nexlink",
                          assignee="claude1", actor="orch")["id"]
        self.assertEqual(work.merged(self.db, theirs, sha="abcdef1234",
                                     actor="claude1")["result"], "merged")
        for name, project in (("pilote-a", "ameesh"), ("pilote-n", "nexlink")):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                            cwd=self.repo, chantier=project)
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "pilote-a,pilote-n"}):
            review_lot.catch_up(self.db, host="ameesh-agents-2")
            out = review_lot.catch_up(self.db, retry_s=0, host="ameesh-agents-2")
        self.assertEqual(sorted(e["lot"] for e in out["unopened"]),
                         sorted([ours, theirs]), out)
        for name, mine, other in (("pilote-a", ours, theirs),
                                  ("pilote-n", theirs, ours)):
            messages = mail.unread_active(self.db, name)
            self.assertEqual(len(messages), 1, (name, messages))
            self.assertIn("#%d " % mine, messages[0]["body"])
            self.assertNotIn("#%d " % other, messages[0]["body"])

    def test_le_geste_conseille_relance_la_classification(self):
        """#3413 : l'avertissement conseille `work merged --repo` ; lancé juste
        après la tentative qui l'a motivé, ce geste relance la classification
        avec le clone retrouvé au lieu de répondre « nothing to do » (le délai
        de reprise d'une heure et le plafond du balayage ne valent pas pour un
        geste explicite)."""
        lot = self.merged_lot("dépôt retrouvé")
        registry.upsert(self.db, "pilote", harness="claude", host=self.cfg.host,
                        cwd=self.repo)
        # #3426 : un hôte sans dépôt s'abstient désormais ; le doute qui
        # motive l'avertissement vient d'un dépôt candidat qui ne contient
        # pas le commit (le dossier de l'assigné, ici le dépôt du témoin).
        with mock.patch.dict(os.environ,
                             {"AMEESH_ALERT_ORCHESTRATORS": "pilote"}):
            first = review_lot.catch_up(self.db)
            second = review_lot.catch_up(self.db, retry_s=0)
        self.assertEqual((first["attempted"], second["attempted"]), (1, 1),
                         (first, second))
        self.assertEqual(len(mail.unread_active(self.db, "pilote")), 1)
        with mock.patch.object(review_lot, "class_at_merge",
                               return_value={"class": "light", "why": "témoin"}):
            done = work.merged(self.db, lot, sha="abcdef1234", repo=self.repo,
                               actor="claude1")
        self.assertEqual(done["result"], "already", done)
        self.assertIn("relecture", done["detail"], done)
        self.assertEqual(len(self.review_lots(lot)), 1, done)
        # le lot de relecture est désormais ouvert : un second geste le rend
        # tel quel, sans relire la classe (idempotence d'`open_for_merge`).
        with mock.patch.object(review_lot, "class_at_merge") as klass:
            again = work.merged(self.db, lot, sha="abcdef1234", repo=self.repo,
                                actor="claude1")
        klass.assert_not_called()
        self.assertIn("déjà ouverte", again["detail"], again)

    def test_le_geste_explicite_ne_relit_pas_l_historique(self):
        """#3413 : la reprise explicite reste tenue par la borne d'activation
        (B1 #1104) — un lot fusionné avant l'activation n'est pas relu
        rétroactivement, même avec un clone."""
        lot = self.merged_lot("fusion ancienne")
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - interval '1 day'"
            " WHERE work_item_id = %s AND kind = 'merged'", (lot,))
        with mock.patch.object(review_lot, "class_at_merge") as klass:
            done = work.merged(self.db, lot, sha="abcdef1234", repo=self.repo,
                               actor="claude1")
        self.assertEqual(done["result"], "already", done)
        klass.assert_not_called()
        self.assertEqual(self.review_lots(lot), [])
        self.assertIn("hors bornes", done["detail"], done)

    def test_depot_lisible_a_la_deuxieme_passe_sans_courrier(self):
        """#3384 : doute transitoire puis classe sensible confirmée — la
        deuxième passe classe sans courrier."""
        lot = self.merged_lot("clone pas encore lisible")
        with mock.patch.object(review_lot, "_warn_unopened") as warn:
            first = review_lot.catch_up(self.db)
            self.assertEqual(first["attempted"], 1, first)
            with mock.patch.object(review_lot, "class_at_merge",
                                   return_value={"class": "sensitive", "doubt": False}):
                second = review_lot.catch_up(self.db, retry_s=0)
            self.assertEqual(second["terminal"], 1, second)
            self.assertEqual(second["unopened"], [], second)
            warn.assert_not_called()
        self.assertEqual(self.review_lots(lot), [])

    def test_deux_sessions_psql_ne_doublent_pas_l_avertissement(self):
        """#3426 (B1 de la relecture cx1) : sur le pilote psql, deux sessions
        distinctes sélectionnent le lot au même instant. Le verrou du lot doit
        être pris sur la session de la transaction (`storage.of(tx)`), pas sur
        la connexion extérieure : sinon les deux lisent le même compteur,
        écrivent chacune la deuxième tentative et envoient deux courriers."""
        lot = self.merged_lot("course psql")
        storage.of(self.db).work.log_review_attempt(
            lot, "merged",
            "%s%d amorce" % (review_lot.ATTEMPT_PREFIX, lot), "témoin")
        barrier = threading.Barrier(2, timeout=10)

        def racy(_db, _lot, **_kw):
            barrier.wait()
            return {"class": "sensitive", "why": "clone sans le commit",
                    "doubt": True}

        warn = mock.Mock()
        dbs = [self.connect(driver="psql") for _ in range(2)]
        for db in dbs:
            self.addCleanup(db.close)
        with mock.patch.object(review_lot, "open_for_merge", side_effect=racy), \
                mock.patch.object(review_lot, "_warn_unopened", warn):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outs = list(pool.map(
                    lambda db: review_lot.catch_up(
                        db, retry_s=0, host="ameesh-agents-2"), dbs))
        self.assertEqual(warn.call_count, 1, outs)
        self.assertEqual(sum(len(out["unopened"]) for out in outs), 1, outs)
        self.assertEqual(sum(out["attempted"] for out in outs), 1, outs)
        notes = [e for e in storage.of(self.db).work.events(lot, 50)
                 if str(e.get("note") or "").startswith(
                     review_lot.ATTEMPT_PREFIX + str(lot))]
        self.assertEqual(len(notes), 2,
                         "une amorce + une seule deuxième tentative : %s" % notes)

    def test_deux_hotes_une_fusion_une_classification_zero_avertissement(self):
        """#3426, témoin du pilote : deux hôtes dont un sans dépôt, une fusion
        — l'hôte sans dépôt s'abstient (zéro courrier), l'autre classe une
        seule fois."""
        lot = self.lot()
        sha = self.merge_lot(lot, "lot/3426-deux-hotes")
        with mock.patch.object(review_lot, "open_for_merge",
                               side_effect=RuntimeError("ouverture perdue")):
            done = work.close_merged(self.db, lot, sha=sha, actor="claude1",
                                     source="témoin #3426")
        self.assertEqual(done["result"], "merged", done)
        self.assertEqual(self.review_lots(lot), [])
        with mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None), \
                mock.patch.object(review_lot, "_warn_unopened") as warn:
            without = review_lot.catch_up(self.db, host="ameesh-agents-1")
        self.assertEqual((without["deferred"], without["attempted"],
                          without["opened"]), (1, 0, 0), without)
        self.assertEqual(self.review_lots(lot), [])
        warn.assert_not_called()
        with mock.patch.object(review_lot, "_warn_unopened") as warn:
            with_repo = review_lot.catch_up(self.db, host="ameesh-agents-2")
        self.assertEqual((with_repo["opened"], with_repo["deferred"],
                          with_repo["attempted"]), (1, 0, 0), with_repo)
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Classe : light", work.get(self.db, rows[0]["id"])["body"])
        warn.assert_not_called()


class DeclaredMergeWitnessTest(_Witness):
    """`ameesh work merged` : une fusion déclarée, sans PR ni file."""

    def test_work_merged_ouvre_le_lot_de_relecture(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-declare")
        done = work.merged(self.db, lot, sha="abcdef1234", note="fusion locale",
                           actor="claude1")
        self.assertEqual(done["result"], "merged")
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Classe : light", work.get(self.db, rows[0]["id"])["body"])

    def test_sans_gel_la_classe_est_un_doute_et_rien_ne_s_ouvre(self):
        """La classe se lit sur le diff de la fusion : sans gel, elle est
        inconnue, et un doute n'ouvre pas de lot de relecture (même règle que
        la file)."""
        lot = self.lot()
        self.assertEqual(work.merged(self.db, lot, sha="abcdef1234",
                                     actor="claude1")["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_un_lot_sensible_n_ouvre_rien(self):
        """Une classe supérieure au `light` n'ouvre rien : la relecture
        d'après fusion est celle des lots légers (canon §2)."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-sensible", name="db/x.sql")
        self.assertEqual(work.merged(self.db, lot, sha="abcdef1234",
                                     actor="claude1")["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_work_merged_transmet_le_depot_prouve_a_la_relecture(self):
        """#3201 : la déclaration transmet à `close_merged` le clone où elle a
        prouvé la fusion. Sur un hôte qui ne connaît ni le dossier de
        l'assigné ni le projet radar — le rattrapage d'un exécuteur —, la
        classe restait « dépôt inconnu » et la relecture §2 du lot léger ne
        s'ouvrait jamais (#3029, #3011, #3105)."""
        _git(self.repo, "remote", "add", "origin", "git@github.com:manaty/ameesh.git")
        lot = work.add(self.db, title="lot 3201 dépôt prouvé", app="ameesh",
                       issue_ref="manaty/ameesh#7", actor="orch")["id"]
        self.frozen_on_branch(lot, "lot/3201-depot")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/3201-depot")
        sha = _git(self.repo, "rev-parse", "HEAD")
        self.proof_patcher.stop()      # la preuve git réelle, pas la doublure
        self.addCleanup(self.proof_patcher.start)
        with mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None):
            done = work.merged(self.db, lot, sha=sha, repo=self.repo, actor="claude1")
        self.assertEqual(done["result"], "merged", done)
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        body = work.get(self.db, rows[0]["id"])["body"]
        self.assertIn("Classe : light", body)
        self.assertIn(sha[:12], body)

    def test_un_lot_deja_fusionne_rejoue_sa_relecture_avec_le_depot(self):
        """#3201 : la voie idempotente de `merged` rejoue une relecture
        manquante sur un lot déjà fusionné quand l'appelant donne le clone —
        le relevé réel des lots à rejouer (#3029 et suivants)."""
        _git(self.repo, "remote", "add", "origin", "git@github.com:manaty/ameesh.git")
        lot = work.add(self.db, title="lot 3201 rejeu", app="ameesh",
                       issue_ref="manaty/ameesh#8", actor="orch")["id"]
        self.frozen_on_branch(lot, "lot/3201-rejeu")
        first = work.merged(self.db, lot, sha="abcdef1234", actor="claude1")
        self.assertEqual(first["result"], "merged", first)
        self.assertEqual(self.review_lots(lot), [], "classe inconnue sans dépôt")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/3201-rejeu")
        sha = _git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(review_lot.lot_class, "repository", return_value=None):
            done = work.merged(self.db, lot, sha=sha, repo=self.repo,
                               actor="claude1")
        self.assertEqual(done["result"], "already", done)
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Classe : light", work.get(self.db, rows[0]["id"])["body"])

    def test_le_cli_dit_pourquoi_la_relecture_n_est_pas_ouverte(self):
        """#3413 : « already merged: nothing to do » cachait la relecture
        manquante et son motif ; le geste explicite doit dire ce qu'il a fait
        ou pourquoi il n'a rien pu faire."""
        lot = self.merged_lot("hors bornes")
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - interval '1 day'"
            " WHERE work_item_id = %s AND kind = 'merged'", (lot,))
        proc = self.mesh("work", "merged", str(lot), "--sha", "abcdef1234",
                         "--repo", self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d already delivered:" % lot, proc.stdout)
        self.assertIn("relecture non ouverte", proc.stdout)
        self.assertNotIn("nothing to do", proc.stdout)


class CloseGuardWitnessTest(_Witness):
    """#2374 R1 : la clôture d'une relecture après fusion n'est pas
    déclarative — `work.close --delivered` compte les avis dus sur le sha
    fusionné, relus de la marque structurée du lot de relecture."""

    def setUp(self):
        super().setUp()
        for name, harness in (("codex1", "codex"), ("codex2", "codex"),
                              ("claude2", "claude"), ("deepseek2", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo)

    def critical_review(self, sha):
        lot = self.merged_lot("porte de fusion", sha=sha)
        with mock.patch.object(review_lot, "_author_at_freeze",
                               return_value={"name": "claude1", "harness": "deepseek"}):
            out = review_lot.open_after_merge(
                self.db, lot=lot,
                klass={"class": "light", "why": "policy",
                       "files": [{"path": "src/ameesh/merge_pr.py", "class": "light"}]},
                merged_sha=sha)
        self.assertTrue(out["created"], out)
        return lot, out["id"]

    def test_deux_avis_sans_famille_auteur_connue_ne_suffisent_pas(self):
        sha = "8" * 40
        with mock.patch.object(review_lot, "_author_at_freeze",
                               return_value={"name": "claude1", "harness": ""}):
            lot = self.merged_lot("auteur non trace", sha=sha)
            out = review_lot.open_after_merge(
                self.db, lot=lot,
                klass={"class": "light", "files": [
                    {"path": "src/ameesh/merge_pr.py", "class": "light"}]},
                merged_sha=sha)
        # #2384 : le corps annonce la route de sortie avant les verdicts
        body = work.get(self.db, out["id"])["body"]
        self.assertIn("Famille de l'auteur non tracée", body)
        self.assertIn("--abandoned", body)
        self.verdict(lot, sha, "codex1")
        self.verdict(lot, sha, "claude2")
        with self.assertRaises(work.WorkError) as caught:
            work.close(self.db, out["id"], delivered=True, sha=sha, actor="codex1")
        text = str(caught.exception)
        self.assertIn("famille de l'auteur inconnue", text)
        self.assertIn("pilote", text)
        self.assertIn("abandon/remplacement", text)

    def verdict(self, lot, sha, actor, verdict="ok"):
        # Témoin de lecture de la porte des avis : le verdict est posé au
        # stockage (les writes synthétiques de la porte #2962 sont ailleurs).
        return storage.of(self.db).work.add_milestone(
            lot, "verdict", sha=sha, actor=actor, verdict=verdict, note="")

    def test_la_marque_porte_le_nombre_d_avis_et_le_sha(self):
        sha = "b" * 40
        lot, review = self.critical_review(sha)
        found = review_lot.requirement_of(self.db, work.get(self.db, review))
        self.assertEqual((found["source"], found["required"], found["sha"]),
                         (lot, 2, sha), found)
        self.assertEqual(found["author"], "claude1", found)

    def test_la_famille_de_l_auteur_ne_compte_pas(self):
        """Deux verdicts dont un de la famille de l'auteur (deepseek) ne font
        pas les deux avis : la seconde famille doit être indépendante."""
        sha = "9" * 40
        lot = self.merged_lot("porte auteur deepseek", sha=sha)
        with mock.patch.object(review_lot, "_author_at_freeze",
                               return_value={"name": "claude1",
                                             "harness": "deepseek"}):
            out = review_lot.open_after_merge(
                self.db, lot=lot,
                klass={"class": "light", "why": "policy",
                       "files": [{"path": "src/ameesh/merge_pr.py",
                                  "class": "light"}]},
                merged_sha=sha)
        self.assertTrue(out["created"], out)
        found = review_lot.requirement_of(self.db, work.get(self.db, out["id"]))
        self.assertEqual(found["author_family"], "deepseek", found)
        self.verdict(lot, sha, "codex1")
        self.verdict(lot, sha, "deepseek2")
        with self.assertRaises(work.WorkError):
            work.close(self.db, out["id"], delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "claude2")
        row = work.close(self.db, out["id"], delivered=True, sha=sha, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_un_seul_avis_ne_clot_pas_une_relecture_sensible(self):
        sha = "b" * 40
        lot, review = self.critical_review(sha)
        with self.assertRaises(work.WorkError) as ctx:
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.assertIn("2 avis", str(ctx.exception))
        self.verdict(lot, sha, "codex1")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "claude2")
        row = work.close(self.db, review, delivered=True, sha=sha, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_deux_avis_de_la_meme_famille_ne_suffisent_pas(self):
        sha = "c" * 40
        lot, review = self.critical_review(sha)
        self.verdict(lot, sha, "codex1")
        self.verdict(lot, sha, "codex2")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "claude2")
        row = work.close(self.db, review, delivered=True, sha=sha, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_un_ok_ancien_ne_survit_pas_a_un_blocage_plus_recent(self):
        """Le dernier verdict de chaque relecteur compte, comme à la porte des
        avis : codex1 `ok` puis `blocked` ne vaut pas un avis favorable."""
        sha = "f" * 40
        lot, review = self.critical_review(sha)
        self.verdict(lot, sha, "codex1")
        self.verdict(lot, sha, "codex1", verdict="blocked")
        self.verdict(lot, sha, "claude2")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="claude2")
        self.verdict(lot, sha, "codex1")      # plus récent : le blocage est levé
        row = work.close(self.db, review, delivered=True, sha=sha, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_un_blocage_n_est_pas_masque_par_l_avis_d_un_autre(self):
        """R2 dev1 : le `blocked` courant de codex1 refuse la clôture même
        quand un autre relecteur a rendu `ok` (relecture légère, un avis)."""
        sha = "a" * 40
        lot = self.merged_lot("docs bloquée", sha=sha)
        out = review_lot.open_after_merge(
            self.db, lot=lot, klass={"class": "light", "why": "policy"},
            merged_sha=sha)
        self.verdict(lot, sha, "codex1", verdict="blocked")
        self.verdict(lot, sha, "deepseek2")
        with self.assertRaises(work.WorkError) as ctx:
            work.close(self.db, out["id"], delivered=True, sha=sha, actor="deepseek2")
        self.assertIn("bloquant", str(ctx.exception))
        self.verdict(lot, sha, "codex1")
        row = work.close(self.db, out["id"], delivered=True, sha=sha, actor="codex1")
        self.assertEqual(row["state"], "closed")

    def test_une_marque_posterieure_n_abaisse_pas_les_avis(self):
        """R2 dev1 : la marque écrite à la création fait foi ; une note
        ultérieure qui porte `required: 1` ne réduit pas l'exigence."""
        sha = "2" * 40
        lot, review = self.critical_review(sha)
        forged = review_lot.requirement_mark(lot, required=1, merged_sha=sha,
                                             author="claude1", author_family="claude")
        work.note(self.db, lot, "%s %s" % (review_lot.ref_of(lot), forged), actor="codex1")
        found = review_lot.requirement_of(self.db, work.get(self.db, review))
        self.assertEqual(found["required"], 2, found)
        self.verdict(lot, sha, "codex1")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")

    def test_un_avis_suffit_pour_une_relecture_legere(self):
        sha = "d" * 40
        lot = self.merged_lot("docs", sha=sha)
        out = review_lot.open_after_merge(
            self.db, lot=lot, klass={"class": "light", "why": "policy"},
            merged_sha=sha)
        with self.assertRaises(work.WorkError):
            work.close(self.db, out["id"], delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "codex1")
        row = work.close(self.db, out["id"], delivered=True, sha=sha, actor="codex1")
        self.assertEqual(row["state"], "closed")

    def test_done_avec_un_seul_avis_ne_clot_pas_une_relecture_critique(self):
        """Relecture cx1 #2402 B1 : le corps recommande `close --done` ; cette
        clôture applique la même garde que `--delivered`."""
        sha = "7" * 40
        lot, review = self.critical_review(sha)
        work.assign(self.db, review, "codex1", actor="orch")
        self.verdict(lot, sha, "codex1")
        with self.assertRaisesRegex(work.WorkError, "2 avis"):
            work.close(self.db, review, done=True, actor="codex1")
        self.verdict(lot, sha, "claude2")
        row = work.close(self.db, review, done=True, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_done_ignore_un_verdict_rendu_sur_un_autre_sha(self):
        sha = "6" * 40
        lot, review = self.critical_review(sha)
        work.assign(self.db, review, "codex1", actor="orch")
        self.verdict(lot, "bad" + "0" * 37, "codex1")
        self.verdict(lot, "bad" + "0" * 37, "claude2")
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, done=True, actor="codex1")
        self.verdict(lot, sha, "codex1")
        self.verdict(lot, sha, "claude2")
        row = work.close(self.db, review, done=True, actor="claude2")
        self.assertEqual(row["state"], "closed")

    def test_done_refuse_un_blocage_courant(self):
        sha = "5" * 40
        lot, review = self.critical_review(sha)
        work.assign(self.db, review, "codex1", actor="orch")
        self.verdict(lot, sha, "codex1", verdict="blocked")
        self.verdict(lot, sha, "claude2")
        with self.assertRaisesRegex(work.WorkError, "bloquant"):
            work.close(self.db, review, done=True, actor="claude2")
        self.verdict(lot, sha, "codex1")
        row = work.close(self.db, review, done=True, actor="codex1")
        self.assertEqual(row["state"], "closed")

    def test_done_d_une_relecture_legere_exige_l_avis_au_sha(self):
        sha = "4" * 40
        lot = self.merged_lot("docs done", sha=sha)
        out = review_lot.open_after_merge(
            self.db, lot=lot, klass={"class": "light", "why": "policy"},
            merged_sha=sha)
        work.assign(self.db, out["id"], "codex1", actor="orch")
        self.verdict(lot, "bad" + "0" * 37, "codex1")
        with self.assertRaises(work.WorkError):
            work.close(self.db, out["id"], done=True, actor="codex1")
        self.verdict(lot, sha, "codex1")
        row = work.close(self.db, out["id"], done=True, actor="codex1")
        self.assertEqual(row["state"], "closed")

    def test_done_d_une_mission_manuelle_accepte_un_blocked(self):
        """Hors `review-after-merge:#N`, un verdict bloqué reste une relecture
        livrée : la garde structurée ne s'applique pas."""
        parent = self.merged_lot("parent manuel", sha="3" * 40)
        review = work.add(self.db, title="R1 Codex #%d : avis" % int(parent),
                          source="mail", issue_ref="manaty/ameesh#941",
                          app="ameesh", assignee="codex1")["id"]
        self.verdict(parent, "3" * 40, "codex1", verdict="blocked")
        row = work.close(self.db, review, done=True, actor="codex1")
        self.assertEqual(row["state"], "closed")

    def test_un_lot_ordinaire_se_clot_sans_avis(self):
        lot = self.lot("rapport ordinaire")
        row = work.close(self.db, lot, delivered=True, sha="e" * 40, actor="claude1")
        self.assertEqual(row["state"], "closed")

    def test_sans_marque_un_lot_ancien_garde_un_avis(self):
        sha = "1" * 40
        lot = self.merged_lot("relecture ancienne", sha=sha)
        ref = review_lot.ref_of(lot)
        review = work.add(self.db, title="REVUE ancienne", source=ref, issue_ref=ref,
                          app="ameesh", actor="orch")["id"]
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "codex1")
        row = work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.assertEqual(row["state"], "closed")

    # #2385 : la même garde quand le lot de relecture est pinné sur un
    # workflow — `work.close` le refuse, la clôture passe par le registre.

    def pin(self, review):
        self.db.execute(
            "UPDATE work_items SET workflow_ref = %s, workflow_stage = 'draft',"
            " workflow_version = 1 WHERE id = %s", (_PinnedSpec.workflow_ref, review))

    def registry_close(self, review, sha, actor):
        version = int(self.db.query(
            "SELECT coalesce(max(id), 0) AS v FROM work_item_events"
            " WHERE work_item_id = %s", (review,))[0]["v"])
        command = process_registry.Command(
            work_item_id=review, command_id=str(uuid.uuid4()),
            expected_version=version,
            authority=process_registry.Authority(actor=actor, source="explicit"),
            workflow_ref=_PinnedSpec.workflow_ref,
            workflow_version=_PinnedSpec.workflow_version,
            action="advance", from_stage="draft", to_stage="stopped",
            guard="stop_work", evidence={"close_reason": "delivered", "sha": sha})
        return process_registry._apply(
            self.db, command, spec=_PinnedSpec(),
            verifiers={"stop_work": lambda context: process_registry.Verification(
                ok=True, reason="témoin")})

    def test_une_relecture_pinnee_ne_se_clot_pas_sans_ses_avis(self):
        sha = "3" * 40
        lot, review = self.critical_review(sha)
        self.pin(review)
        with self.assertRaises(work.WorkError):
            work.close(self.db, review, delivered=True, sha=sha, actor="codex1")
        self.verdict(lot, sha, "codex1")
        with self.assertRaises(process_registry.RegistryRefusal) as ctx:
            self.registry_close(review, sha, "codex1")
        self.assertEqual(ctx.exception.code, "review-undelivered", str(ctx.exception))
        self.assertIn("2 avis", str(ctx.exception))
        self.assertEqual(work.get(self.db, review)["state"], "intake")
        self.verdict(lot, sha, "claude2")
        receipt = self.registry_close(review, sha, "claude2")
        self.assertEqual(receipt.state, "closed")
        self.assertEqual(work.get(self.db, review)["close_reason"], "delivered")

    def test_un_lot_pinne_ordinaire_se_clot_sans_avis(self):
        review = self.lot("rapport pinné")
        self.pin(review)
        receipt = self.registry_close(review, "f" * 40, "claude2")
        self.assertEqual(receipt.state, "closed")


class _PinnedSpec:
    """Artefact minimal du protocole `WorkflowSpec` : brouillon → arrêté."""

    workflow_ref = "acme/process-workflow@2385"
    workflow_version = 1
    initial = "draft"

    def statuses(self):
        return ("draft", "stopped")

    def core_of(self, status):
        return {"draft": "intake", "stopped": "closed"}[status]

    def transitions_from(self, source):
        return (("stopped", "stop_work"),) if source == "draft" else ()
