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
from unittest import mock

from ameesh import (backlog, db as db_mod, debit, exploitation, impact_analysis as ia,
                    project_plan, slo, storage, work, work_footprints as fp)
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

    def test_les_chemins_cites_passent_par_le_budget(self):
        """B1 de cx1 : la résolution d'un chemin cité (rev-parse, puis ls-tree
        pour un nom seul) passe par le git injecté et le budget partagé."""
        git = _FakeGit(self.FILES)
        analysis = ia.analyze("voir work_footprints.py et b.py", repo="/r", ref="REF",
                              git=git)
        self.assertEqual([c[0] for c in git.calls], ["rev-parse", "ls-tree", "rev-parse"])
        self.assertEqual(analysis["git_calls"], len(git.calls))
        self.assertEqual([p["path"] for p in analysis["paths"]],
                         ["src/ameesh/work_footprints.py", "src/b.py"])
        for budget in (0, 1, 2):
            git = _FakeGit(self.FILES)
            analysis = ia.analyze("voir work_footprints.py, b.py et `run_pass`",
                                  repo="/r", ref="REF", git=git, max_git_calls=budget)
            self.assertLessEqual(len(git.calls), budget)
            self.assertEqual(analysis["git_calls"], len(git.calls))

    def test_l_echeance_borne_les_chemins_cites(self):
        ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        git = _FakeGit(self.FILES)
        analysis = ia.analyze("voir a.py et b.py", repo="/r", ref="REF", git=git,
                              budget_s=1.0, clock=lambda: next(ticks, 10.0))
        self.assertEqual(git.calls, [])
        self.assertEqual(sorted(p["path"] for p in analysis["paths"]), ["a.py", "b.py"])

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

    def test_changement_de_politique_sur_les_lots_deja_crees(self):
        """B3 de cx1 : open → repository marque les lots ouverts sans empreinte
        (inconnue sous open, ou d'avant #4818), repository → open les libère ;
        manuel et diff ne bougent pas."""
        occupant = self.lot("occupant")
        self.footprint(occupant, "src/a.py")
        self.give(occupant)
        libre = self.lot("libre, sans empreinte")
        connu = self.lot("connu")
        self.footprint(connu, "docs/x.md")
        vehicule = self.lot("paquet", branch="integration/paquet-h-2")
        result = self.policy(unknown_policy="repository")
        self.assertEqual(result["lots"], [libre["id"]])
        self.assertEqual(fp.paths_of(self.db, libre["id"]), ["."])
        self.assertEqual(fp.paths_of(self.db, vehicule["id"]), [])
        self.assertEqual(fp.paths_of(self.db, connu["id"]), ["docs/x.md"])
        with self.assertRaises(work.WorkError):
            work.assign(self.db, libre["id"], "human:autre", actor="test")
        analysis = storage.of(self.db).work_footprints.analyses([libre["id"]])[libre["id"]]
        self.assertEqual((analysis["status"], analysis["policy"]), ("unknown", "repository"))
        notes = [e["note"] for e in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (libre["id"],))]
        self.assertIn("policy repository", notes[-1])
        # un `.` posé à la main n'est pas un marqueur de politique
        manuel = self.lot("manuel")
        fp.set_paths(self.db, manuel["id"], ["."], actor="human:proprio")
        result = self.policy(unknown_policy="open")
        self.assertEqual(result["lots"], [libre["id"]])
        self.assertEqual(fp.paths_of(self.db, libre["id"]), [])
        self.assertEqual(fp.paths_of(self.db, manuel["id"]), ["."])
        work.assign(self.db, libre["id"], "human:autre", actor="test")
        # sans changement de politique, rien n'est touché
        self.assertEqual(self.policy(max_in_flight=3)["lots"], [])

    def test_politique_par_defaut_et_journal(self):
        self.assertEqual(fp.policy_of(self.db, "nexlink")["unknown_policy"], "open")
        result = self.policy(unknown_policy="repository", max_in_flight=4)
        self.assertTrue(result["changed"])
        again = self.policy(unknown_policy="repository")
        self.assertFalse(again["changed"])
        self.assertEqual(again["after"]["max_in_flight"], 4)


class EntrelacementTest(_Empreintes):
    """B3 (2e tour) de cx1 : une bascule de politique entre le calcul de
    l'analyse (hors verrou, git) et son écriture n'est jamais perdue — la
    politique est relue à l'écriture, sous le verrou de `set_policy`."""

    def switch_during_analysis(self, function, policy):
        """Fait basculer la politique, sur une AUTRE connexion, juste après le
        calcul de l'analyse et avant son écriture."""
        real = getattr(fp, function)

        def analyze_then_switch(*args, **kw):
            analysis = real(*args, **kw)
            other = db_mod.connect(self.cfg)
            try:
                storage.of(other).work_footprints.set_policy(
                    "nexlink", actor="human:proprio", unknown_policy=policy)
            finally:
                other.close()
            return analysis
        return mock.patch.object(fp, function, side_effect=analyze_then_switch)

    def occupant(self):
        item = self.lot("occupant")
        self.footprint(item, "src/a.py")
        self.give(item)
        return item

    def test_reestimation_open_puis_repository(self):
        self.occupant()
        libre = self.lot("vague")
        self.assertEqual(fp.paths_of(self.db, libre["id"]), [])
        with self.switch_during_analysis("analyze_lot", "repository"):
            analysis = fp.record_estimate(self.db, self.cfg, work.get(self.db, libre["id"]),
                                          actor="test")
        self.assertEqual(analysis["policy"], "repository")
        self.assertEqual(fp.paths_of(self.db, libre["id"]), ["."])
        self.assertIn("whole repository", analysis["note"])
        with self.assertRaises(work.WorkError):
            work.assign(self.db, libre["id"], "human:autre", actor="test")

    def test_reestimation_repository_puis_open(self):
        storage.of(self.db).work_footprints.set_policy(
            "nexlink", actor="human:proprio", unknown_policy="repository")
        self.occupant()
        libre = self.lot("vague", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, libre["id"]), ["."])
        with self.switch_during_analysis("analyze_lot", "open"):
            analysis = fp.record_estimate(self.db, self.cfg, work.get(self.db, libre["id"]),
                                          actor="test")
        self.assertEqual(analysis["policy"], "open")
        self.assertEqual(fp.paths_of(self.db, libre["id"]), [])
        work.assign(self.db, libre["id"], "human:autre", actor="test")

    def test_creation_open_puis_repository(self):
        self.occupant()
        with self.switch_during_analysis("analyze_for_creation", "repository"):
            item = work.add(self.db, title="vague", app="nexlink", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["."])
        analysis = storage.of(self.db).work_footprints.analyses([item["id"]])[item["id"]]
        self.assertEqual(analysis["policy"], "repository")
        with self.assertRaises(work.WorkError):
            work.assign(self.db, item["id"], "human:autre", actor="test")

    def test_creation_repository_puis_open(self):
        storage.of(self.db).work_footprints.set_policy(
            "nexlink", actor="human:proprio", unknown_policy="repository")
        self.occupant()
        with self.switch_during_analysis("analyze_for_creation", "open"):
            item = work.add(self.db, title="vague", app="nexlink", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, item["id"]), [])
        work.assign(self.db, item["id"], "human:autre", actor="test")


class RelectureTroisTest(_Empreintes):
    """Troisième tour : lots non-code exemptés (qa-01), `--set` pendant le
    calcul (B4 de cx1), diff prioritaire et file d'amélioration (B1, B2 de
    dev3), véhicule hors du refresh (observation de dev3)."""

    def policy(self, **kw):
        return storage.of(self.db).work_footprints.set_policy(
            "nexlink", actor="human:proprio", **kw)

    def test_lots_de_decision_et_de_decoupage_exemptes(self):
        self.occupant()
        libre = self.lot("libre")
        self.footprint(libre, "docs/x.md")
        decision = work._decision_lot(self.db, title="question ?", requester="human:proprio",
                                      app="nexlink", cfg=self.cfg)
        decoupage = self.lot("découper", source="file-decoupage:nexlink")
        self.assertEqual(self.policy(unknown_policy="repository")["lots"], [])
        for item in (decision, decoupage):
            self.assertEqual(fp.paths_of(self.db, item["id"]), [])
        self.assertEqual(storage.of(self.db).work_footprints.overlapping(libre["id"]), [])
        self.assertIn(libre["id"],
                      {i["id"] for i in storage.of(self.db).work.unassigned_lots(10)})
        # au plafond, une question au pilote se pose encore, et un lot de
        # découpage se confie encore au pilote
        self.policy(max_in_flight=1)
        work._decision_lot(self.db, title="autre question ?", requester="human:proprio",
                           app="nexlink", cfg=self.cfg)
        work.assign(self.db, decoupage["id"], "human:pilote", actor="test")
        # et ils ne comptent pas : un seul occupant de code
        self.assertEqual(storage.of(self.db).work_footprints.cap_reached(libre["id"]),
                         {"cap": 1, "in_flight": 1})
        self.assertEqual([b["id"] for b in slo.footprint_blockers(
            work.get(self.db, libre["id"]),
            storage.of(self.db).operations.open_lots_activity(None),
            {libre["id"]: ["docs/x.md"]},
            fp.policies_of(self.db, ["nexlink"]))], [self.occupant_id])

    def occupant(self):
        item = self.lot("occupant")
        self.footprint(item, "src/a.py")
        self.give(item)
        self.occupant_id = item["id"]
        return item

    def set_during_analysis(self, paths):
        real = fp.analyze_lot

        def analyze_then_set(cfg, db, item, *args, **kw):
            analysis = real(cfg, db, item, *args, **kw)
            other = db_mod.connect(self.cfg)
            try:
                fp.set_paths(other, int(item["id"]), paths, actor="human:proprio")
            finally:
                other.close()
            return analysis
        return mock.patch.object(fp, "analyze_lot", side_effect=analyze_then_set)

    def test_set_pendant_le_calcul_prime(self):
        self.policy(unknown_policy="repository")
        for title in ("vague", "corriger src/b.py"):
            item = self.lot(title, cfg=self.cfg)
            before = storage.of(self.db).work_footprints.analyses([item["id"]])[item["id"]]
            with self.set_during_analysis(["src/a.py"]):
                self.assertIsNone(fp.record_estimate(
                    self.db, self.cfg, work.get(self.db, item["id"]), actor="test"))
            self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/a.py"], title)
            after = storage.of(self.db).work_footprints.analyses([item["id"]])[item["id"]]
            self.assertEqual(after["attempts"], before["attempts"], title)

    def test_le_diff_prime_sur_une_reanalyse_inconnue(self):
        self.policy(unknown_policy="repository")
        item = self.lot("vague", cfg=self.cfg)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["."])
        storage.of(self.db).work_footprints.replace(
            item["id"], "diff", ["src/feature.py"], actor="runner", head="a" * 40)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/feature.py"])
        store = storage.of(self.db).work_footprints
        self.assertEqual(store.analyses([item["id"]])[item["id"]]["status"], "known")
        # même vieillie, l'analyse n'est plus reprise ; --estimate ne repose rien
        self.db.execute("UPDATE work_item_impact_analyses SET status = 'unknown',"
                        " analyzed_at = analyzed_at - interval '1 hour'"
                        " WHERE work_item_id = %s", (item["id"],))
        self.assertEqual(store.claim_unknown(retry_after_s=60, limit=5), [])
        analysis = fp.record_estimate(self.db, self.cfg, work.get(self.db, item["id"]),
                                      actor="test")
        self.assertEqual(analysis["policy"], "open")
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/feature.py"])

    def amelioration(self, title):
        item = self.lot(title, cfg=self.cfg)
        self.db.execute("UPDATE work_items SET type = 'improvement', expected_value = 'gain',"
                        " value_score = 40 WHERE id = %s", (item["id"],))
        return work.get(self.db, item["id"])

    def test_la_file_d_amelioration_suit_la_regle_commune(self):
        # B de cx1 sur 176dc95 : un lot `improvement` est du code, pris par
        # `auto_take` ; il prend le marqueur, compte au plafond, est retenu
        # par lui et son diff se relève au push
        self.policy(unknown_policy="repository")
        with mock.patch.object(backlog, "rules", return_value=backlog.Rules(
                lot_min_value=30, timezone="Europe/Paris", commit="c" * 40)):
            file = backlog.add(self.db, title="amélioration vague", expected_value="gain",
                               value_score=40, app="nexlink", actor="test", cfg=self.cfg,
                               new_project=True)
        self.assertEqual(work.get(self.db, file["id"])["type"], "improvement")
        self.assertEqual(fp.paths_of(self.db, file["id"]), ["."])
        store = storage.of(self.db).work_footprints
        self.assertEqual(store.analyses([file["id"]])[file["id"]]["policy"], "repository")
        self.db.execute("UPDATE work_item_impact_analyses SET analyzed_at = analyzed_at"
                        " - interval '1 hour' WHERE work_item_id = %s", (file["id"],))
        self.assertEqual([r["work_item_id"] for r in store.claim_unknown(
            retry_after_s=60, limit=5)], [file["id"]])
        self.assertFalse(fp.is_exempt(work.get(self.db, file["id"])))
        # pris et en vol, il compte au plafond et il est candidat au refresh
        prise = self.amelioration("autre amélioration")
        self.footprint(prise, "src/b.py")
        self.db.execute("UPDATE work_items SET branch = 'lot/1-b' WHERE id = %s",
                        (prise["id"],))
        self.give(prise)
        libre = self.lot("libre")
        self.footprint(libre, "docs/x.md")
        self.policy(max_in_flight=1)
        self.assertEqual(store.cap_reached(libre["id"]), {"cap": 1, "in_flight": 1})
        self.assertIn(prise["id"], [r["id"] for r in store.refresh_candidates(limit=10)])
        # et une amélioration à prendre est retenue par le plafond
        attente = self.amelioration("encore une")
        self.footprint(attente, "docs/y.md")
        self.assertEqual(store.cap_reached(attente["id"]), {"cap": 1, "in_flight": 1})

    def rules(self):
        return mock.patch.object(backlog, "rules", return_value=backlog.Rules(
            lot_min_value=30, timezone="Europe/Paris", commit="c" * 40))

    def test_un_panier_vague_porte_le_marqueur(self):
        # B1 de cx3 sur d68ca19 : la route du panier pose le marqueur et
        # l'analyse comme un lot direct ; une ligne connue ne les efface pas
        self.policy(unknown_policy="repository")
        self.occupant()
        with self.rules():
            panier = backlog.add(self.db, title="panier vague", expected_value="gain",
                                 value_score=10, app="nexlink", actor="test",
                                 cfg=self.cfg, new_project=True, chantier="ch",
                                 estimate="1h")
        self.assertTrue(panier["basket"])
        self.assertEqual(fp.paths_of(self.db, panier["id"]), ["."])
        store = storage.of(self.db).work_footprints
        analysis = store.analyses([panier["id"]])[panier["id"]]
        self.assertEqual((analysis["status"], analysis["policy"]), ("unknown", "repository"))
        with self.assertRaises(work.WorkError):
            work.assign(self.db, panier["id"], "human:autre", actor="test")
        with self.rules():
            ligne = backlog.add(self.db, title="ligne précise", expected_value="gain",
                                body="corrige src/c.py", value_score=10, app="nexlink",
                                actor="test", cfg=self.cfg, chantier="ch")
        self.assertEqual(ligne["id"], panier["id"])
        self.assertEqual(fp.paths_of(self.db, panier["id"]), [".", "src/c.py"])
        self.assertEqual(store.analyses([panier["id"]])[panier["id"]]["status"], "unknown")
        self.db.execute("UPDATE work_item_impact_analyses SET analyzed_at = analyzed_at"
                        " - interval '1 hour' WHERE work_item_id = %s", (panier["id"],))
        # B de dev3 sur 055b48b : un panier inconnu n'est pas repris
        self.assertEqual(store.claim_unknown(retry_after_s=60, limit=5), [])

    def test_la_reprise_ne_perd_pas_la_ligne_vague_du_panier(self):
        # B de dev3 sur 055b48b : ligne connue puis ligne vague, sous `open`
        store = storage.of(self.db).work_footprints
        self.occupant()
        with self.rules():
            panier = backlog.add(self.db, title="ligne précise", expected_value="gain",
                                 body="corrige src/a.py", value_score=10, app="nexlink",
                                 actor="test", cfg=self.cfg, new_project=True, chantier="ch",
                                 estimate="1h")
            backlog.add(self.db, title="ligne vague", expected_value="gain", value_score=10,
                        app="nexlink", actor="test", cfg=self.cfg, chantier="ch")
        self.assertEqual(fp.paths_of(self.db, panier["id"]), ["src/a.py"])
        self.db.execute("UPDATE work_item_impact_analyses SET analyzed_at = analyzed_at"
                        " - interval '1 hour' WHERE work_item_id = %s", (panier["id"],))
        self.assertEqual(store.claim_unknown(retry_after_s=60, limit=5), [])
        self.assertIsNone(fp.record_estimate(self.db, self.cfg, work.get(self.db, panier["id"]),
                                             actor="runner"))
        self.assertEqual(store.analyses([panier["id"]])[panier["id"]]["status"], "unknown")
        # la bascule retrouve l'inconnue malgré le chemin connu, et la rend
        result = store.set_policy("nexlink", actor="human:proprio",
                                  unknown_policy="repository")
        self.assertEqual(result["lots"], [panier["id"]])
        self.assertEqual(fp.paths_of(self.db, panier["id"]), [".", "src/a.py"])
        with self.assertRaises(work.WorkError):
            work.assign(self.db, panier["id"], "human:autre", actor="test")
        store.set_policy("nexlink", actor="human:proprio", unknown_policy="open")
        self.assertEqual(fp.paths_of(self.db, panier["id"]), ["src/a.py"])
        # un lot connu avec des chemins n'est pas touché par la bascule
        connu = work.add(self.db, title="corrige src/b.py", app="nexlink", cfg=self.cfg)
        result = store.set_policy("nexlink", actor="human:proprio",
                                  unknown_policy="repository")
        self.assertNotIn(connu["id"], result["lots"])
        self.assertEqual(fp.paths_of(self.db, connu["id"]), ["src/b.py"])

    def test_une_prise_juste_apres_la_creation_voit_le_marqueur(self):
        # B2 de cx3 sur d68ca19 : une prise sur une AUTRE connexion, juste
        # après le commit de la création, voit déjà le marqueur
        self.policy(unknown_policy="repository")
        self.occupant()
        real = WorkItems.backlog_add
        seen = {}

        def create_then_take(this, **kw):
            item = real(this, **kw)
            other = db_mod.connect(self.cfg)
            try:
                work.assign(other, item["id"], "human:autre", actor="test")
                seen["taken"] = True
            except work.WorkError as exc:
                seen["refused"] = str(exc)
            finally:
                other.close()
            return item
        with self.rules(), mock.patch.object(WorkItems, "backlog_add", create_then_take):
            item = backlog.add(self.db, title="amélioration vague", expected_value="gain",
                               value_score=40, app="nexlink", actor="test", cfg=self.cfg,
                               new_project=True, estimate="1h")
        self.assertNotIn("taken", seen)
        self.assertIn("(unknown footprint: whole repository)", seen["refused"])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])

    def test_un_vehicule_n_est_pas_un_candidat_au_refresh(self):
        vehicule = self.lot("paquet", branch="integration/paquet-h-3")
        lot = self.lot("lot", branch="lot/9-x")
        for item in (vehicule, lot):
            self.give(item)
        ids = [r["id"] for r in storage.of(self.db).work_footprints.refresh_candidates(limit=10)]
        self.assertEqual(ids, [lot["id"]])


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
        self.root = root
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

    def _lot_pousse_d_un_autre_clone(self, branch):
        """Le lot poussé depuis le clone du porteur, inconnu de celui de l'hôte."""
        item = work.add(self.db, title="quelque chose de vague", app="tpk", branch=branch,
                        cfg=self.cfg)
        self.db.execute("UPDATE work_items SET assignee = 'agent1', state = 'build'"
                        " WHERE id = %s", (item["id"],))
        origin = self.repo.git("remote", "get-url", "origin").strip()
        author = os.path.join(self.root, "porteur-" + branch.replace("/", "-"))
        self.repo.git("clone", "-q", "-b", "main", origin, author)
        self.repo.git("-C", author, "checkout", "-qb", branch)
        os.makedirs(os.path.join(author, "src"), exist_ok=True)
        with open(os.path.join(author, "src", "ailleurs.py"), "w") as out:
            out.write("x\n")
        self.repo.git("-C", author, "add", ".")
        self.repo.git("-C", author, "-c", "user.name=a", "-c", "user.email=a@b",
                      "commit", "-qm", "lot")
        self.repo.git("-C", author, "push", "-q", "origin", branch)
        return item

    def test_un_push_depuis_un_autre_clone_est_vu(self):
        # B de cx2 sur 055b48b : l'hôte n'a ni la ref ni l'objet de la branche
        item = self._lot_pousse_d_un_autre_clone("lot/2-ailleurs")
        self.assertEqual(fp._published_tip(self.repo.root, "lot/2-ailleurs"), "")
        report = fp.run_pass(self.cfg, self.db, actor="runner", agents={"agent1"})
        self.assertEqual([r["lot"] for r in report["refreshed"]], [item["id"]], report)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/ailleurs.py"])
        self.assertEqual(fp._published_tip(self.repo.root, "lot/2-ailleurs"), "",
                         "the shared clone gets no ref")
        self.assertEqual(fp.run_pass(self.cfg, self.db, actor="runner",
                                     agents={"agent1"})["refreshed"], [])

    def test_forge_injoignable_les_refs_du_clone_et_la_passe_continue(self):
        item = work.add(self.db, title="quelque chose de vague", app="tpk",
                        branch="lot/3-local", cfg=self.cfg)
        self.db.execute("UPDATE work_items SET assignee = 'agent1', state = 'build'"
                        " WHERE id = %s", (item["id"],))
        self.repo.git("checkout", "-qb", "lot/3-local")
        self.repo.write("src/local.py", "x\n")
        self.repo.commit("lot")
        self.repo.git("push", "-q", "origin", "lot/3-local")
        self.repo.git("remote", "set-url", "origin", os.path.join(self.root, "absent.git"))
        report = fp.run_pass(self.cfg, self.db, actor="runner", agents={"agent1"})
        self.assertEqual([r["lot"] for r in report["refreshed"]], [item["id"]], report)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["src/local.py"])

    def test_le_curseur_atteint_les_lots_au_dela_de_la_page(self):
        """B2 de cx1 : des candidats déjà à jour en tête de liste ne cachent
        pas un premier push plus loin."""
        lots = []
        for i in range(3):
            branch = "lot/%d-c" % i
            item = work.add(self.db, title="lot %d" % i, app="tpk", branch=branch,
                            cfg=self.cfg)
            self.db.execute("UPDATE work_items SET assignee = 'agent1', state = 'build'"
                            " WHERE id = %s", (item["id"],))
            self.repo.git("checkout", "-q", "main")
            self.repo.git("checkout", "-qb", branch)
            self.repo.write("src/c%d.py" % i, "x\n")
            self.repo.commit("lot %d" % i)
            lots.append(item)
        for item in lots[:2]:
            self.repo.git("push", "-q", "origin", item["branch"])
        first = fp.run_pass(self.cfg, self.db, actor="runner", scan=2)
        self.assertEqual([r["lot"] for r in first["refreshed"]],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertEqual(first["cursor"], lots[1]["id"])
        self.repo.git("push", "-q", "origin", lots[2]["branch"])
        second = fp.run_pass(self.cfg, self.db, actor="runner", scan=2,
                             cursor=first["cursor"])
        self.assertEqual([r["lot"] for r in second["refreshed"]], [lots[2]["id"]])
        self.assertEqual(second["cursor"], 0)
        self.assertEqual(fp.paths_of(self.db, lots[2]["id"]), ["src/c2.py"])
        # plafond par passe : la passe suivante reprend sur le lot non lu
        self.db.execute("DELETE FROM work_item_footprints WHERE source = 'diff'")
        capped = fp.run_pass(self.cfg, self.db, actor="runner", limit=1, scan=10)
        self.assertEqual([r["lot"] for r in capped["refreshed"]], [lots[0]["id"]])
        self.assertEqual(capped["cursor"], lots[1]["id"] - 1)
        after = fp.run_pass(self.cfg, self.db, actor="runner", limit=1, scan=10,
                            cursor=capped["cursor"])
        self.assertEqual([r["lot"] for r in after["refreshed"]], [lots[1]["id"]])

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
