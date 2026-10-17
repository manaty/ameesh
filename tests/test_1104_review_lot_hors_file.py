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

import dataclasses
import os
import subprocess
import unittest
from unittest import mock

from ameesh import plan_git, registry, review_lot, storage, work

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
                mock.patch.object(review_lot.lot_class, "of_lot") as of_lot:
            out = review_lot.class_at_merge(None, 1, item=dict(self.ITEM), sha="s" * 40)
        return out, of_lot

    def test_une_panne_de_lecture_du_diff_ne_classe_pas_light(self):
        out, of_lot = self.call({"side_effect": RuntimeError("panne de lecture")})
        of_lot.assert_not_called()
        self.assertEqual(out["class"], "sensitive")
        self.assertTrue(out["doubt"])
        self.assertIn("lecture du diff impossible", out["why"])

    def test_un_intervalle_introuvable_ne_classe_pas_light(self):
        """Gel lisible et ancêtre de la cible, commit de fusion absent du
        dépôt : le repli serait un diff vide, donc « light »."""
        out, of_lot = self.call({"return_value": None})
        of_lot.assert_not_called()
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
        work.milestone(self.db, lot, "frozen", sha=sha, actor="claude1")
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
        self.assertEqual(work.get(self.db, old)["state"], "merged", "la fusion reste acquise")
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
        self.assertEqual(work.get(self.db, promoted)["state"], "promoted")
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
