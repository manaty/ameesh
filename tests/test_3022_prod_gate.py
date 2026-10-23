# SPDX-License-Identifier: AGPL-3.0-only
"""#3022 (canon §5, décision owner #122895) : un lot de porte de production
fusionné sur l'intégration seule reste ouvert « en attente prod » ; il ne se
ferme qu'à la présence de son correctif sur la branche de prod déclarée par le
canon (`projects.<app>.production.branch`). Un lot sans marque n'est pas
affecté."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import mail, mechanics, plan_git, prod_gate, storage, work

from .test_canon import git
from .test_l1616_mechanics import MECHANICS
from .test_plan import _PlanDb

NEXLINK = MECHANICS.replace("projects: {}", """projects:
  nexlink:
    production:
      branch: main""")


class ProductionMechanicsTest(unittest.TestCase):
    def test_la_branche_de_prod_est_compilee_par_projet(self):
        document = mechanics.compile_document(NEXLINK)
        self.assertEqual(document.production_branch("nexlink"), "main")

    def test_sans_declaration_aucune_branche_n_est_supposee(self):
        document = mechanics.compile_document(MECHANICS)
        with self.assertRaisesRegex(mechanics.MechanicsError,
                                    "projects.nexlink.production.branch"):
            document.production_branch("nexlink")

    def test_une_declaration_mal_formee_refuse(self):
        for bad in ("origin/main", "refs/heads/main", "a..b", "main.lock", "main/",
                    "'-main'", "[main]", "{}", "''"):
            with self.subTest(branch=bad):
                with self.assertRaises(mechanics.MechanicsError):
                    mechanics.compile_document(NEXLINK.replace("branch: main",
                                                               "branch: %s" % bad))
        with self.assertRaisesRegex(mechanics.MechanicsError, "unknown key"):
            mechanics.compile_document(NEXLINK.replace(
                "branch: main", "branch: main\n      tag: v1"))


class MarkingTest(unittest.TestCase):
    def test_la_derniere_marque_fait_foi_et_la_levee_l_efface(self):
        events = [{"note": "prod-gate: ne pas déclencher kind=ota", "actor": "orch",
                   "created_ts": 1.0}]
        self.assertEqual(prod_gate.marking(events)["guard"], "ne pas déclencher kind=ota")
        events.append({"note": "prod-gate lifted: mauvaise marque", "actor": "orch"})
        self.assertIsNone(prod_gate.marking(events))
        events.append({"note": "PROD-GATE : porte fermée sur main", "actor": "pilote"})
        self.assertEqual(prod_gate.marking(events)["by"], "pilote")
        self.assertIsNone(prod_gate.marking([{"note": "hold: autre chose"}]))


class ProdGateCloseTest(_PlanDb):
    """Intégration `develop`, prod `main` : le relevé de contenu sur develop."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.make_tmp(), "nexlink")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.commit("README", "nexlink\n", "initial")
        git(self.repo, "branch", "-q", "develop")
        patcher = mock.patch.object(mechanics, "configured",
                                    return_value=mechanics.compile_document(NEXLINK))
        self.configured = patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, path: str, text: str, message: str) -> str:
        with open(os.path.join(self.repo, path), "a", encoding="utf-8") as fh:
            fh.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def on_develop(self, name: str, *, app: str = "nexlink", mark: str | None = None) -> dict:
        """Un lot gelé, fusionné dans develop seul, encore en `qa`."""
        git(self.repo, "checkout", "-q", "-b", "lot/%s" % name, "develop")
        sha = self.commit("%s.txt" % name, "%s\n" % name, name)
        git(self.repo, "checkout", "-q", "develop")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet %s" % name, "lot/%s" % name)
        git(self.repo, "checkout", "-q", "main")
        item = work.add(self.db, title=name, app=app)
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        storage.of(self.db).work.add_milestone(
            item["id"], "frozen", sha=sha, actor="ouvrier", verdict=None,
            note="ameesh-freeze-absent: develop")
        if mark is not None:
            work.note(self.db, item["id"], "prod-gate: %s" % mark, actor="orchestrateur")
        return item

    def release(self) -> None:
        git(self.repo, "merge", "-q", "--no-ff", "-m", "release", "develop")

    def run_sync(self) -> dict:
        report = plan_git.sync_merges(self.db, self.repo, target="develop")
        return {r["work_item"]: r for r in report["results"]}

    def test_un_lot_de_porte_fusionne_sur_develop_reste_en_attente_prod(self):
        item = self.on_develop("ota", mark="ne pas déclencher kind=ota profile=production")
        dry = plan_git.sync_merges(self.db, self.repo, target="develop", dry_run=True)
        [entry] = [r for r in dry["results"] if r["work_item"] == item["id"]]
        self.assertEqual((entry["result"], entry["retention"]["what"]),
                         ("would-withhold", "production"))
        self.assertEqual(mail.unread(self.db, "orchestrateur"), [])
        result = self.run_sync()[item["id"]]
        self.assertEqual((result["result"], result["retention"]["what"]),
                         ("withheld", "production"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")
        notes = [e["note"] or "" for e in work.events(self.db, item["id"])]
        held = [n for n in notes if "en attente prod : main" in n]
        self.assertTrue(held, notes)
        self.assertIn("garde : ne pas déclencher kind=ota profile=production", held[0])
        [row] = mail.unread(self.db, "orchestrateur")
        self.assertIn("production branch", row["body"])
        # le balayage suivant ne double ni la note ni le courrier
        self.run_sync()
        self.assertEqual(len(mail.unread(self.db, "orchestrateur")), 1)
        # la release porte le correctif sur main : le lot se ferme, la note le dit
        self.release()
        self.assertEqual(self.run_sync()[item["id"]]["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
        merged = [m for m in work.milestones(self.db, item["id"]) if m["kind"] == "merged"]
        self.assertIn("porte de production : présent sur main (ancestor",
                      merged[0]["note"])

    def test_un_lot_hors_porte_n_est_pas_affecte(self):
        self.configured.side_effect = AssertionError("canon read for an unmarked lot")
        item = self.on_develop("ordinaire")
        self.assertEqual(self.run_sync()[item["id"]]["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")

    def test_sans_branche_de_prod_declaree_la_fermeture_est_refusee(self):
        item = self.on_develop("autre", app="ameesh", mark="porte du relais")
        self.release()                    # même présent sur main : aucune branche supposée
        result = self.run_sync()[item["id"]]
        self.assertEqual(result["result"], "withheld")
        self.assertIn("branche de prod inconnue", result["detail"])
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")

    def test_une_marque_levee_rend_la_fermeture_ordinaire(self):
        item = self.on_develop("leve", mark="porte OTA")
        self.assertEqual(self.run_sync()[item["id"]]["result"], "withheld")
        work.note(self.db, item["id"], "prod-gate lifted: pas une porte de prod",
                  actor="orchestrateur")
        self.assertEqual(self.run_sync()[item["id"]]["result"], "merged")

    def test_toute_voie_de_constat_est_gardee(self):
        """Une PR fusionnée sur develop (`sync-github`) ne ferme pas non plus."""
        item = self.on_develop("pr", mark="porte OTA")
        head = git(self.repo, "rev-parse", "develop")
        done = work.close_merged(self.db, item["id"], sha=head, actor="sync",
                                 source="sync-github", repo=self.repo)
        self.assertEqual((done["result"], done["retention"]["what"]),
                         ("withheld", "production"))
        self.release()
        done = work.close_merged(self.db, item["id"], sha=head, actor="sync",
                                 source="sync-github", repo=self.repo)
        self.assertEqual(done["result"], "merged")


if __name__ == "__main__":
    unittest.main()
