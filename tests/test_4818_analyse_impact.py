# SPDX-License-Identifier: AGPL-3.0-only
"""#4818 (manaty/ameesh#3643) : analyse d'impact avant dispatch.

Constat (TPK, nuit du 29 au 30/10) : 14 PR en parallèle sur les mêmes fichiers
chauds, 31 blocages QA en 6 h. La garde d'empreinte (#3185) existait, mais
l'empreinte d'intake ne lisait que les chemins cités : un lot sans chemin
cité ne retenait rien.

Attendu :
1. une analyse d'impact en lecture seule, bornée, avec confiance par chemin,
   une ligne de journal par estimation ; `--set` du pilote prioritaire ;
2. empreinte inconnue prudente par politique de dépôt (marqueur `.`) ;
3. refresh depuis le diff au premier push (passe de l'exécuteur) ;
4. chemins partagés ignorés pour le chevauchement ;
5. sélection d'un ensemble disjoint deux à deux et plafond de lots en vol,
   raisons visibles dans le plan et le débit ;
6. un occupant immobile qui retient des lots est signalé avec le geste dû.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import unittest

from ameesh import (debit, exploitation, impact_analysis as ia, project_plan, slo,
                    storage, work, work_footprints as fp)
from ameesh.storage.postgres.work import WorkItems

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo
from .test_4173_empreinte import _Empreintes


def _proc(out: str, code: int = 0):
    return subprocess.CompletedProcess([], code, out.encode(), b"")


class _FakeGit:
    """Un dépôt simulé : `files` (chemin → contenu) à la ref `REF`."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, repo, *args, timeout=None):
        self.calls.append(args)
        if args[0] == "rev-parse":
            path = args[-1].split(":", 1)[1]
            return _proc("x\n" if path in self.files else "", 0 if path in self.files else 1)
        if args[0] == "ls-tree":
            return _proc("\n".join(sorted(self.files)))
        if args[0] == "grep":
            pattern = args[args.index("-e") + 1]
            if "-E" in args:
                import re
                rx = re.compile(pattern.replace("[[:space:]]", r"\s"))
                hits = [p for p, text in self.files.items() if rx.search(text)]
            else:
                hits = [p for p, text in self.files.items() if pattern in text]
            return _proc("".join("REF:%s\n" % p for p in sorted(hits)), 0 if hits else 1)
        raise AssertionError(args)


class AnalyseTest(unittest.TestCase):
    FILES = {
        "src/ameesh/work_footprints.py": "def unassigned_lots():\n    pass\n",
        "src/ameesh/storage/postgres/work.py": "x = unassigned_lots()\n",
        "tests/test_x.py": "unassigned_lots()\n",
        "src/a.py": "def run_pass(): pass\n",
        "src/b.py": "run_pass()\n", "src/c.py": "run_pass()\n", "src/d.py": "run_pass()\n",
        "docs/WORK-FOOTPRINTS.md": "doc\n",
    }

    def test_identifiants_cites(self):
        text = ("Corriger `storage.unassigned_lots` et `WorkItems` ; la commande "
                "`ameesh work sync-branches` ; voir footprint_blockers dans le texte. "
                "`src/x.py` est un chemin, `work` un mot")
        self.assertEqual(ia.identifiers(text),
                         ["unassigned_lots", "WorkItems", "sync-branches",
                          "footprint_blockers"])

    def test_confiances_haute_moyenne_et_devinette_non_retenue(self):
        git = _FakeGit(self.FILES)
        analysis = ia.analyze(
            "Footprints : `unassigned_lots` et docs/WORK-FOOTPRINTS.md",
            repo="/r", ref="REF", title="Footprints prudentes", git=git)
        self.assertEqual(analysis["status"], "known")
        by_path = {p["path"]: p["confidence"] for p in analysis["paths"]}
        # le test qui cite l'identifiant n'est pas une surface
        self.assertEqual(by_path, {"docs/WORK-FOOTPRINTS.md": "high",
                                   "src/ameesh/storage/postgres/work.py": "medium",
                                   "src/ameesh/work_footprints.py": "medium"})
        # « footprints » (titre) nomme un fichier : devinette journalisée,
        # jamais dans l'empreinte
        self.assertEqual([w["path"] for w in analysis["weak"]], [])
        self.assertLessEqual(analysis["git_calls"], ia.MAX_GIT_CALLS)

    def test_identifiant_ambigu_se_lit_a_sa_definition(self):
        analysis = ia.analyze("Rendre `run_pass` idempotent", repo="/r", ref="REF",
                              git=_FakeGit(self.FILES))
        self.assertEqual([p["path"] for p in analysis["paths"]], ["src/a.py"])

    def test_devinette_du_titre(self):
        files = {"src/ameesh/debit_report.py": "x\n"}
        analysis = ia.analyze("Le rapport", repo="/r", ref="REF", title="Débit report",
                              git=_FakeGit(files))
        self.assertEqual(analysis["status"], "unknown")
        self.assertEqual([w["path"] for w in analysis["weak"]], ["src/ameesh/debit_report.py"])
        self.assertIn("weak (not held): src/ameesh/debit_report.py",
                      ia.note(dict(analysis, policy="open")))

    def test_budget_borne_rend_une_analyse_inconnue(self):
        git = _FakeGit(self.FILES)
        analysis = ia.analyze("`unassigned_lots` `run_pass`", repo="/r", ref="REF",
                              git=git, max_git_calls=0)
        self.assertEqual(analysis["status"], "unknown")
        self.assertIn("budget exhausted", analysis["reason"])
        self.assertEqual(git.calls, [])

    def test_sans_clone_seuls_les_chemins_cites(self):
        analysis = ia.analyze("voir src/x.py et `unassigned_lots`", repo=None)
        self.assertEqual(analysis["paths"],
                         [{"path": "src/x.py", "confidence": "high", "why": "cited"}])
        self.assertEqual(ia.analyze("rien", repo=None)["reason"],
                         "no local clone of the repository and no cited path")

    def test_ligne_de_journal(self):
        known = {"status": "known", "paths": [
            {"path": "a.py", "confidence": "high"}, {"path": "b.py", "confidence": "medium"}],
            "elapsed_ms": 12, "git_calls": 3}
        self.assertEqual(ia.note(known), "Impact analysis: 2 path(s) (high: a.py; "
                                         "medium: b.py) in 12 ms, 3 git call(s)")
        unknown = {"status": "unknown", "reason": "no cited path", "paths": [],
                   "policy": "repository", "elapsed_ms": 1, "git_calls": 0}
        self.assertIn("held as the whole repository", ia.note(unknown))


class RegleTest(unittest.TestCase):
    def test_le_marqueur_chevauche_tout_et_les_partages_sont_ignores(self):
        self.assertTrue(fp.overlaps(".", "src/a.py"))
        self.assertTrue(fp.overlaps("docs", "."))
        self.assertEqual(fp.held_pairs(["CHANGELOG.md", "src/a.py"],
                                       ["CHANGELOG.md", "src/b.py"], ["CHANGELOG.md"]), [])
        self.assertEqual(fp.held_pairs(["locales/fr.ts"], ["locales"], ["locales"]), [])
        # le marqueur n'est jamais partagé
        self.assertEqual(fp.held_pairs(["."], ["CHANGELOG.md", "src"], ["CHANGELOG.md"]),
                         [(".", "src")])

    def _lot(self, ident, state="intake", assignee=None, branch=None):
        return {"id": ident, "app": "tpk", "state": state, "assignee": assignee,
                "branch": branch}

    def test_bloqueurs_slo_plafond_et_vehicule(self):
        waiting = self._lot(1)
        lots = [waiting, self._lot(2, "build", "a"), self._lot(3, "qa", "b"),
                self._lot(4, "build", "c", branch="integration/paquet-1")]
        paths = {1: ["src/x.py"], 2: ["src/y.py"], 3: ["CHANGELOG.md"], 4: ["src"]}
        # sans politique : le véhicule chevauche (comme la garde SQL)
        self.assertEqual([b["id"] for b in slo.footprint_blockers(waiting, lots, paths)], [4])
        paths[4] = ["other"]
        policies = {"tpk": {"max_in_flight": 2, "shared_paths": []}}
        held = slo.footprint_blockers(waiting, lots, paths, policies)
        self.assertEqual([(b["id"], b["reason"]) for b in held], [(2, "cap"), (3, "cap")])
        policies["tpk"]["max_in_flight"] = 3
        self.assertEqual(slo.footprint_blockers(waiting, lots, paths, policies), [])

    def test_plan_choisit_un_ensemble_disjoint(self):
        def row(ident, paths, priority=2, in_flight=0):
            return {"id": ident, "state": "intake", "type": "evolution", "app": "tpk",
                    "lot_team": "tpk", "priority": priority, "created_ts": ident,
                    "footprint_paths": paths, "repository_in_flight": in_flight}

        rows = [row(1, ["src/a.py"], 1), row(2, ["src"]), row(3, ["docs"]),
                row(4, ["CHANGELOG.md"]), row(5, ["CHANGELOG.md", "lib"])]
        plan = project_plan.evaluate(rows, project="tpk", policies={
            "tpk": {"shared_paths": ["CHANGELOG.md"], "max_in_flight": None}})
        self.assertEqual(plan["ready"], [1, 3, 4, 5])
        waiting = {r["id"]: r["waiting"] for r in plan["lots"]}
        self.assertEqual(waiting[2], ["footprint overlaps #1, dispatched first"])

        rows = [row(1, ["a"], in_flight=1), row(2, ["b"], in_flight=1), row(3, ["c"], in_flight=1)]
        plan = project_plan.evaluate(rows, project="tpk",
                                     policies={"tpk": {"max_in_flight": 2}})
        self.assertEqual(plan["ready"], [1])
        self.assertIn("repository in-flight cap 2 reached with #1",
                      {r["id"]: r["waiting"] for r in plan["lots"]}[2])

    def test_geste_du_sur_un_occupant_immobile(self):
        text = exploitation.footprint_hold_action(5, [{"lot": 7, "reason": "overlap"}])
        self.assertIn("it holds #7 by its footprint (never released silently)", text)
        self.assertIn("ameesh work footprint 5 --set", text)
        self.assertIn("ameesh work return 5", text)
        cap = exploitation.footprint_hold_action(5, [{"lot": 7, "reason": "cap"}])
        self.assertIn("in-flight cap", cap)
        self.assertNotIn("--set", cap)

    def test_debit_dit_le_plafond(self):
        line = debit._footprint_gesture([{"lot": 9, "state": "intake", "reason": "cap",
                                          "blockers": [{"id": 2, "state": "build"}]}])
        self.assertIn("#9 (intake) attend le plafond de lots en vol du dépôt (#2 (build))",
                      line)


class GardeTest(_Empreintes):
    def policy(self, app="nexlink", **kw):
        return storage.of(self.db).work_footprints.set_policy(app, actor="human:proprio", **kw)

    def test_un_occupant_inconnu_retient_tout_le_depot(self):
        inconnu = self.lot("inconnu")
        autre = self.lot("autre")
        ailleurs = self.lot("ailleurs", app="ameesh")
        storage.of(self.db).work_footprints.replace(
            int(inconnu["id"]), "intake", ["."], actor="test")
        self.footprint(autre, "docs/x.md")
        self.footprint(ailleurs, "docs/x.md")
        self.give(inconnu)
        free = {i["id"] for i in storage.of(self.db).work.unassigned_lots(10)}
        self.assertNotIn(autre["id"], free)
        self.assertIn(ailleurs["id"], free)
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, autre["id"], "human:autre", actor="test")
        self.assertIn("(unknown footprint: whole repository)", str(ctx.exception))

    def test_chemins_partages_ignores_par_la_garde(self):
        premier = self.lot("premier")
        second = self.lot("second")
        self.footprint(premier, "CHANGELOG.md", "src/a.py")
        self.footprint(second, "CHANGELOG.md", "src/b.py")
        self.give(premier)
        self.assertTrue(storage.of(self.db).work_footprints.overlapping(second["id"]))
        self.policy(shared_paths=["CHANGELOG.md"])
        self.assertEqual(storage.of(self.db).work_footprints.overlapping(second["id"]), [])
        work.assign(self.db, second["id"], "human:autre", actor="test")

    def test_plafond_de_lots_en_vol(self):
        lots = [self.lot("l%d" % i) for i in range(3)]
        vehicule = self.lot("paquet", branch="integration/paquet-h-1")
        for i, lot in enumerate(lots):
            self.footprint(lot, "src/f%d.py" % i)
        self.policy(max_in_flight=1)
        self.give(lots[0])
        self.give(vehicule)  # un véhicule ne compte pas
        free = {i["id"] for i in storage.of(self.db).work.unassigned_lots(10)}
        self.assertEqual(free & {l["id"] for l in lots}, set())
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, lots[1]["id"], "human:autre", actor="test")
        self.assertIn("in-flight cap (1/1", str(ctx.exception))
        self.assertIsNone(WorkItems(self.db).assign(
            lots[1]["id"], "claude1", current=None, note="t", actor="t", ready_only=True))
        # création attribuée au plafond : refusée, rien n'est écrit
        with self.assertRaises(storage.RepositoryCapReached):
            work.add(self.db, title="nouveau", app="nexlink", assignee="human:x",
                     footprint=["src/neuf.py"])
        # le plan et le débit disent la raison
        plan = project_plan.report(self.db, "nexlink", cfg=self.cfg)
        reasons = {r["id"]: r["waiting"] for r in plan["lots"]}
        self.assertIn("repository in-flight cap reached (1 lots in progress)",
                      reasons[lots[1]["id"]])
        self.policy(clear_cap=True)
        work.assign(self.db, lots[1]["id"], "human:autre", actor="test")

    def test_politique_par_defaut_et_journal(self):
        self.assertEqual(fp.policy_of(self.db, "nexlink")["unknown_policy"], "open")
        result = self.policy(unknown_policy="repository", max_in_flight=4)
        self.assertTrue(result["changed"])
        again = self.policy(unknown_policy="repository")
        self.assertFalse(again["changed"])
        self.assertEqual(again["after"]["max_in_flight"], 4)


class CreationTest(_Empreintes):
    def test_inconnue_prudente_ou_ouverte_selon_la_politique(self):
        ouvert = work.add(self.db, title="sans chemin", app="nexlink", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, ouvert["id"]), [])
        store = storage.of(self.db).work_footprints
        self.assertEqual(store.analyses([ouvert["id"]])[ouvert["id"]]["status"], "unknown")
        store.set_policy("nexlink", actor="human:proprio", unknown_policy="repository")
        prudent = work.add(self.db, title="toujours sans chemin", app="nexlink",
                           cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, prudent["id"]), ["."])
        notes = [e["note"] for e in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (prudent["id"],))]
        self.assertTrue(any(n.startswith("Impact analysis: ") and "whole repository" in n
                            for n in notes), notes)
        # le pilote prime : --set remplace le marqueur, --estimate ne réécrit rien
        fp.set_paths(self.db, prudent["id"], ["src/a.py"], actor="human:proprio")
        self.assertEqual(fp.paths_of(self.db, prudent["id"]), ["src/a.py"])
        self.assertIsNone(fp.record_estimate(self.db, self.cfg, work.get(
            self.db, prudent["id"]), actor="test"))
        # un chemin cité : connue, haute confiance
        cite = work.add(self.db, title="corriger src/b.py", app="nexlink", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, cite["id"]), ["src/b.py"])
        analysis = store.analyses([cite["id"]])[cite["id"]]
        self.assertEqual(analysis["paths"][0]["confidence"], "high")


class PassePushTest(PgTestCase):
    def setUp(self):
        super().setUp()
        root = os.path.join(self.tmp, self._testMethodName)
        os.makedirs(root, exist_ok=True)
        self.repo = _Repo(os.path.join(root, "tpk"))
        self.repo.write("README.md", "base\n")
        self.repo.write("src/hot.py", "def hot_path():\n    pass\n")
        self.repo.commit("base")
        self.repo.git("branch", "-M", "main")
        origin = os.path.join(root, "origin.git")
        self.repo.git("init", "--bare", "-q", origin)
        self.repo.git("remote", "add", "origin", origin)
        self.repo.git("push", "-q", "origin", "main")
        self.cfg = dataclasses.replace(self.cfg, radar={"projects": {
            "tpk": {"repo": self.repo.root, "target": "main"}}})
        storage.of(self.db).work_footprints.set_policy(
            "tpk", actor="human:proprio", unknown_policy="repository")

    def test_premier_push_remplace_le_marqueur_par_le_diff(self):
        item = work.add(self.db, title="quelque chose de vague", app="tpk",
                        branch="lot/1-vague", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["."])
        self.db.execute("UPDATE work_items SET assignee = 'agent1', state = 'build'"
                        " WHERE id = %s", (item["id"],))
        self.repo.git("checkout", "-qb", "lot/1-vague")
        self.repo.write("src/feature.py", "x\n")
        self.repo.commit("lot")
        # pas encore poussée : rien
        self.assertEqual(fp.run_pass(self.cfg, self.db, actor="runner")["refreshed"], [])
        self.repo.git("push", "-q", "origin", "lot/1-vague")
        report = fp.run_pass(self.cfg, self.db, actor="runner", agents={"agent1"})
        self.assertEqual([r["lot"] for r in report["refreshed"]], [item["id"]], report)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/feature.py"])
        # même tête : la passe suivante ne relit rien
        self.assertEqual(fp.run_pass(self.cfg, self.db, actor="runner")["refreshed"], [])
        # un autre hôte (agents sans l'assigné) ne le touche pas
        self.repo.write("src/more.py", "y\n")
        self.repo.commit("more")
        self.repo.git("push", "-q", "origin", "lot/1-vague")
        self.assertEqual(fp.run_pass(self.cfg, self.db, actor="runner",
                                     agents={"autre"})["refreshed"], [])
        fp.run_pass(self.cfg, self.db, actor="runner", agents={"agent1"})
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/feature.py", "src/more.py"])

    def test_analyse_avec_clone_et_reprise_des_inconnues(self):
        known = work.add(self.db, title="Accélérer `hot_path`", app="tpk", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, known["id"]), ["src/hot.py"])
        vague = work.add(self.db, title="vague", app="tpk", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, vague["id"]), ["."])
        # le corps est précisé : la reprise (après le délai) trouve la surface
        self.db.execute("UPDATE work_items SET body = 'voir src/hot.py' WHERE id = %s",
                        (vague["id"],))
        self.assertEqual(fp.run_pass(self.cfg, self.db, actor="runner")["analyzed"], [])
        self.db.execute("UPDATE work_item_impact_analyses SET analyzed_at = analyzed_at"
                        " - interval '1 hour' WHERE work_item_id = %s", (vague["id"],))
        report = fp.run_pass(self.cfg, self.db, actor="runner")
        self.assertEqual(report["analyzed"], [{"lot": vague["id"], "status": "known",
                                               "footprint": ["src/hot.py"]}], report)
        self.assertEqual(fp.paths_of(self.db, vague["id"]), ["src/hot.py"])


if __name__ == "__main__":
    unittest.main()
