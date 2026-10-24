# SPDX-License-Identifier: AGPL-3.0-only
"""#3292 : le débit de l'auditeur, mesuré sur les jalons.

Témoin du 24/10 vers 03:20Z : #3033 (avis bloquant de 22 h), #3195 (9 h),
#99 et #3076 (holds sur des appareils déclarés indisponibles), les 8 lots
retenus par les correctifs fusionnés de 1.21.0 — et aucun faux avis dû venu
de la colonne `reviewer` (l'erreur du bureau à 03:20Z).
"""
from __future__ import annotations

import types
import unittest

from ameesh import debit
from tests.support import PgTestCase

T = 1792812000.0            # 2026-10-24 03:20:00Z
H = 3600.0


def item(ident, title="", assignee="", app="ameesh"):
    return {"id": ident, "title": title or "lot %d" % ident, "app": app,
            "assignee": assignee or "ameesh-dev5", "priority": 2}


def frozen(ident, mid, at, sha="a" * 40):
    return {"work_item_id": ident, "id": mid, "kind": "frozen", "at_ts": at,
            "sha": sha, "actor": "auteur", "verdict": None, "note": ""}


def verdict(ident, mid, at, actor, kind, sha="a" * 40):
    return {"work_item_id": ident, "id": mid, "kind": "verdict", "at_ts": at,
            "sha": sha, "actor": actor, "verdict": kind, "note": ""}


def event(ident, eid, at, note, actor="pilote", state="qa"):
    return {"work_item_id": ident, "id": eid, "state": state, "at_ts": at,
            "actor": actor, "note": note}


def mark(ident, freeze_id, names):
    import json
    return 'review-designation:%d:%d: %s' % (
        ident, freeze_id, json.dumps({"reviewers": [{"name": n} for n in names]}))


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], now=T, installed="1.20.0",
                  devices={"names": ["nx-carina"], "phrases": ["Mac de Carina", "PC de Marco"],
                           "sources": ["canon"], "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


class ReviewDueTest(unittest.TestCase):
    def test_avis_du_sur_le_gel_sans_verdict(self):
        report = run(
            items=[item(3033, assignee="ameesh-dev7")],
            milestones=[frozen(3033, 10, T - 22 * H),
                        verdict(3033, 11, T - 21 * H, "ameesh-dev3", "blocked")],
            events=[event(3033, 1, T - 22 * H, mark(3033, 10, ["ameesh-cx2", "ameesh-dev3"]))])
        due = report["reviews_due"]
        self.assertEqual([row["reviewer"] for row in due], ["ameesh-cx2"])
        self.assertEqual(due[0]["lot"], 3033)
        self.assertGreaterEqual(due[0]["age_s"], 21 * H)
        self.assertEqual([row["lot"] for row in report["blocked_reviews"]], [3033])

    def test_verdict_sur_une_autre_tete_ne_compte_pas(self):
        report = run(
            items=[item(7)],
            milestones=[frozen(7, 10, T - 5 * H, sha="b" * 40),
                        verdict(7, 11, T - 4 * H, "ameesh-cx2", "ok", sha="a" * 40)],
            events=[event(7, 1, T - 5 * H, mark(7, 10, ["ameesh-cx2"]))])
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-cx2"])

    def test_verdict_ok_retire_l_avis(self):
        report = run(
            items=[item(7)],
            milestones=[frozen(7, 10, T - 5 * H),
                        verdict(7, 11, T - 4 * H, "ameesh-cx2", "ok")],
            events=[event(7, 1, T - 5 * H, mark(7, 10, ["ameesh-cx2"]))])
        self.assertEqual(report["reviews_due"], [])
        self.assertEqual(len(report["qa"]), 1)
        self.assertTrue(report["qa"][0]["ready"])

    def test_lot_leger_sans_avis_n_est_pas_un_defaut(self):
        report = run(
            items=[item(8)],
            milestones=[frozen(8, 10, T - 5 * H)],
            events=[event(8, 1, T - 5 * H,
                          "review-designation-none:8:10: aucun avis")])
        self.assertEqual(len(report["qa"]), 1)
        self.assertTrue(report["qa"][0]["ready"])
        self.assertEqual(report["reviews_due"], [])

    def test_seule_une_marque_du_dernier_gel_designe(self):
        # Une marque d'un gel antérieur ne nomme personne sur la nouvelle tête.
        report = run(
            items=[item(9)],
            milestones=[frozen(9, 10, T - 10 * H),
                        frozen(9, 11, T - 2 * H, sha="c" * 40)],
            events=[event(9, 1, T - 10 * H, mark(9, 10, ["ameesh-dev3"]))])
        self.assertEqual(report["reviews_due"], [])
        self.assertEqual([row["lot"] for row in report["qa"]], [9])
        self.assertTrue(report["qa"][0]["missing_reviewer"])

    def test_retrait_manuel_du_relecteur(self):
        report = run(
            items=[item(10)],
            milestones=[frozen(10, 10, T - 5 * H)],
            events=[event(10, 1, T - 5 * H, mark(10, 10, ["ameesh-dev3"])),
                    event(10, 2, T - 4 * H, "reviewer ameesh-dev3 removed",
                          actor="mesh-design")])
        self.assertEqual(report["reviews_due"], [])

    def test_designation_manuelle_qui_survit_a_la_rotation(self):
        report = run(
            items=[item(11)],
            milestones=[frozen(11, 10, T - 4 * H)],
            events=[event(11, 1, T - 4 * H, "review-designation-none:11:10: x"),
                    event(11, 2, T - 3 * H, "reviewer ameesh-cx1 designated",
                          actor="mesh-design")])
        self.assertEqual([row["reviewer"] for row in report["reviews_due"]],
                         ["ameesh-cx1"])

    def test_lot_termine_ne_reclame_aucun_avis(self):
        report = run(
            items=[item(12)],
            milestones=[frozen(12, 10, T - 5 * H),
                        {"work_item_id": 12, "id": 11, "kind": "merged",
                         "at_ts": T - H, "sha": "", "actor": "", "verdict": None,
                         "note": ""}],
            events=[event(12, 1, T - 5 * H, mark(12, 10, ["ameesh-dev3"]))])
        self.assertEqual(report["reviews_due"], [])
        self.assertEqual(report["blocked_reviews"], [])


class HoldsAndWakesTest(unittest.TestCase):
    def test_hold_et_releve(self):
        report = run(
            items=[item(99, assignee="deepseek7")],
            events=[event(99, 1, T - 11 * H,
                          "hold: attente du retour nx-carina et PC de Marco",
                          actor="orchestrateur", state="build"),
                    event(99, 2, T - H, "unhold: retour reçu",
                          actor="orchestrateur", state="build")])
        self.assertEqual(report["holds"], [])
        self.assertEqual(report["device_holds"], [])

    def test_hold_sur_appareil_indisponible(self):
        report = run(
            items=[item(99, assignee="deepseek7", app="nexlink"),
                   item(3076, assignee="codex1", app="nexlink")],
            events=[event(99, 1, T - 11 * H,
                          "hold: attend nx-carina (Mac de Carina)", actor="orchestrateur",
                          state="build"),
                    event(3076, 1, T - 10 * H,
                          "hold: attente du PC de Marco (WSL2) et de nx-carina",
                          actor="codex1", state="build")])
        self.assertEqual(sorted(row["lot"] for row in report["device_holds"]),
                         [99, 3076])
        self.assertEqual(report["totals"]["device_holds"], 2)
        self.assertTrue(all(row["kind"] == "hold_device"
                            for row in report["slow_waits"] if row["lot"] in (99, 3076)))

    def test_hold_sur_un_correctif_non_deploye(self):
        lots = [3015, 1606, 1288, 2914, 2614, 2878, 2267, 3231]
        items = [item(lot, assignee="ameesh-dev5") for lot in lots]
        events = [event(lot, 1, T - (3 + index) * H,
                        "hold: attend la 1.21.0, correctif fusionné pas déployé",
                        actor="mesh-design", state="qa")
                  for index, lot in enumerate(lots)]
        report = run(items=items, events=events)
        self.assertEqual(report["totals"]["undeployed_holds"], 8)
        self.assertEqual(sorted(row["lot"] for row in report["undeployed_holds"]),
                         sorted(lots))
        self.assertTrue(all(row["kind"] == "hold_undeployed"
                            for row in report["slow_waits"]))

    def test_hold_sur_une_version_installee_ne_signale_pas(self):
        report = run(
            items=[item(1)],
            events=[event(1, 1, T - 3 * H, "hold: attend la 1.20.0")])
        self.assertEqual(report["undeployed_holds"], [])
        self.assertEqual(report["totals"]["holds"], 1)

    def test_reveil_arme_et_echu(self):
        report = run(
            items=[item(3064, assignee="ameesh-cx2")],
            events=[event(3064, 1, T - 5 * H,
                          "wake: reprise de la mesure (due 2026-10-23 23:00 UTC)",
                          actor="ameesh-cx2", state="build")])
        self.assertEqual(len(report["wakes"]), 1)
        wake = report["wakes"][0]
        self.assertTrue(wake["overdue"])
        self.assertAlmostEqual(wake["due_ts"], T - 4 * H - 20 * 60, delta=60)
        self.assertIn("wake_overdue", [row["kind"] for row in report["slow_waits"]])

    def test_reveil_annule(self):
        report = run(
            items=[item(1)],
            events=[event(1, 1, T - 5 * H,
                          "wake: x (due 2026-10-30 08:00 UTC)", state="build"),
                    event(1, 2, T - 4 * H, "wake: cancelled (was due 2026-10-30 08:00 UTC)",
                          state="build")])
        self.assertEqual(report["wakes"], [])

    def test_reveil_programme_par_une_note(self):
        report = run(
            items=[item(1)],
            events=[event(1, 1, T - 5 * H,
                          "hold: attente [réveil programmé: 2026-10-24 06:00 UTC]",
                          state="build")])
        self.assertEqual(len(report["wakes"]), 1)
        self.assertAlmostEqual(report["wakes"][0]["due_ts"], T + 2 * H + 40 * 60,
                               delta=60)


class ThroughputAndCiTest(unittest.TestCase):
    def test_tours_et_fusions_par_heure(self):
        report = run(
            items=[item(1)],
            turns=[{"at_ts": T - 5 * 60}, {"at_ts": T - 30 * 60},
                   {"at_ts": T - 90 * 60}],
            merges=[{"at_ts": T - 10 * 60}, {"at_ts": T - 3 * H}])
        hours = report["throughput"]["hours"]
        self.assertEqual(len(hours), 6)
        self.assertEqual(hours[-1]["turns"], 1)
        self.assertEqual(hours[-1]["merges"], 1)
        self.assertEqual(sum(row["turns"] for row in hours), 3)
        self.assertEqual(sum(row["merges"] for row in hours), 2)

    def test_ci_par_workflow_et_par_job(self):
        report = run(
            items=[item(1)],
            ci_runs=[{"repo": "manaty/ameesh", "run_id": 1, "workflow": "tests.yml",
                      "status": "success", "started_ts": T - 4 * H,
                      "concluded_ts": T - H},
                     {"repo": "manaty/ameesh", "run_id": 2, "workflow": "tests.yml",
                      "status": "success", "started_ts": T - 2 * H,
                      "concluded_ts": T - int(1.9 * H)}],
            jobs=[{"repo": "manaty/ameesh", "run_id": 1, "job": "plateforme macOS",
                   "duration_s": 3.5 * H}])
        group = report["ci"]["by_workflow"][0]
        self.assertEqual((group["repo"], group["workflow"]), ("manaty/ameesh", "tests.yml"))
        self.assertEqual(group["count"], 2)
        self.assertEqual(report["ci"]["slow_runs"][0]["run_id"], 1)
        self.assertEqual(report["ci"]["slow_jobs"][0]["job"], "plateforme macOS")

    def test_lecture_illisible_dite(self):
        report = run(items=[item(1)], ci_unreadable=["GitHub: quota"])
        self.assertIn("GitHub: quota", report["ci"]["unreadable"])


class DeviceRuleTest(unittest.TestCase):
    RULE = ("Les tests restent exigés. Appareils indisponibles en vigueur : "
            "**Mac de Carina (`nx-carina`) et PC de Marco, du 23/10/2026 au "
            "30/10/2026**. Les bugs remontent de la prod.")

    def test_regle_lue(self):
        parsed = debit.parse_device_rule(self.RULE)
        self.assertEqual(parsed["names"], ["nx-carina"])
        self.assertEqual(parsed["phrases"], ["Mac de Carina", "PC de Marco"])
        self.assertIsNone(parsed["error"])

    def test_date_illisible_dite(self):
        parsed = debit.parse_device_rule("Appareils indisponibles en vigueur : nx-carina")
        self.assertTrue(parsed["error"])

    def test_seuil_d_une_duree(self):
        self.assertEqual(debit._duration_arg("2h"), 7200)
        self.assertEqual(debit._duration_arg("30m"), 1800)
        with self.assertRaises(debit.DebitError):
            debit._duration_arg("deux heures")


class WitnessTest(PgTestCase):
    """L'état du 24/10 vers 03:20Z, rejoué en base (lecture réelle, SQL)."""

    RULE = ("Les tests restent exigés. Appareils indisponibles en vigueur : "
            "**Mac de Carina (`nx-carina`) et PC de Marco, du 23/10/2026 au "
            "30/10/2026**. Les bugs remontent de la prod.")
    UNDEPLOYED = [3015, 1606, 1288, 2914, 2614, 2878, 2267, 3231]

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, ci_runs CASCADE")
        self.lot = {}

    def _item(self, ident, assignee, app="ameesh"):
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, assignee) "
            "VALUES (%s, 'evolution', 'test', %s, %s, 'qa', %s)",
            (ident, "lot %d" % ident, app, assignee))
        self.lot[ident] = ident

    def _milestone(self, ident, mid, kind, at, sha="a" * 40, actor="auteur",
                   verdict=None):
        self.db.execute(
            "INSERT INTO work_item_milestones "
            "(id, work_item_id, kind, at, sha, actor, verdict, note) "
            "VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, '')",
            (mid, ident, kind, at, sha, actor, verdict))

    def _event(self, ident, eid, at, note, actor="pilote", state="qa"):
        self.db.execute(
            "INSERT INTO work_item_events "
            "(id, work_item_id, state, note, actor, created_at) "
            "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))",
            (eid, ident, state, note, actor, at))

    def _mark(self, ident, eid, at, freeze_id, names):
        import json
        self._event(ident, eid, at,
                    "review-designation:%d:%d: %s" % (
                        ident, freeze_id,
                        json.dumps({"reviewers": [{"name": n} for n in names]})),
                    actor="ameesh")

    def _seed(self):
        # #3033 : gel du 23/10 04:53Z, avis bloquant de dev3, cx2 encore dû.
        self._item(3033, "ameesh-dev7")
        self._milestone(3033, 1001, "frozen", T - 22 * H - 27 * 60)
        self._mark(3033, 3001, T - 22 * H - 26 * 60, 1001,
                   ["ameesh-cx2", "ameesh-dev3"])
        self._milestone(3033, 1002, "verdict", T - 22 * H - 10 * 60,
                        actor="ameesh-dev3", verdict="blocked")
        # #3195 : gel du 23/10 18:38Z, avis bloquant de cx2 (9 h).
        self._item(3195, "mesh-design")
        self._milestone(3195, 1003, "frozen", T - 9 * H, sha="b" * 40)
        self._mark(3195, 3002, T - 9 * H + 60, 1003, ["ameesh-cx2"])
        self._milestone(3195, 1004, "verdict", T - 9 * H + 4 * 60,
                        sha="b" * 40, actor="ameesh-cx2", verdict="blocked")
        # #99 et #3076 : holds sur des appareils déclarés indisponibles.
        for ident, assignee, eid, age, note in (
                (99, "deepseek7", 3103, 11 * H,
                 "hold: recette sur nx-carina (indisponible jusqu'au 30/10)"),
                (3076, "codex1", 3203, 10 * H,
                 "hold: attente du retour de nx-carina (Lima) et du PC de Marco (WSL2)")):
            self._item(ident, assignee, app="nexlink")
            self._event(ident, eid, T - age - 60, "build → qa", state="build")
            self._event(ident, eid + 1, T - age, note, actor="orchestrateur",
                        state="build")
            self._event(ident, eid + 2, T - age + 60,
                        "wake: retour des appareils (due 2026-10-30 08:00 UTC)",
                        actor=assignee, state="build")
        # Les 8 lots retenus par 1.21.0 (correctif fusionné, non déployé).
        for index, ident in enumerate(self.UNDEPLOYED):
            self._item(ident, "ameesh-dev5")
            self._milestone(ident, 2000 + index, "frozen", T - (28 - index) * H,
                            sha=("%02d" % index) * 20)
            self._event(ident, 4000 + index, T - (28 - index) * H,
                        "review-designation-none:%d:%d: aucun avis"
                        % (ident, 2000 + index), actor="ameesh")
            self._event(ident, 4100 + index, T - (3 + index) * H,
                        "hold: attend la 1.21.0 (correctif fusionné pas déployé)",
                        actor="mesh-design")
        # Piège : la colonne reviewer nomme un relecteur, aucun jalon ne le fait.
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, assignee, "
            "reviewer, reviewers) VALUES "
            "(9999, 'evolution', 'test', 'piège colonne', 'ameesh', 'qa', "
            "'ameesh-dev5', 'ameesh-dev3', ARRAY['ameesh-dev3'])")
        # Un lot réellement relu ne doit plus rien devoir.
        self._item(5000, "ameesh-dev5")
        self._milestone(5000, 5001, "frozen", T - 6 * H, sha="c" * 40)
        self._mark(5000, 5002, T - 6 * H + 60, 5001, ["ameesh-cx1"])
        self._milestone(5000, 5003, "verdict", T - 5 * H, sha="c" * 40,
                        actor="ameesh-cx1", verdict="ok")
        # Le registre : les relecteurs et les auteurs sont au repos.
        for name, host in (("ameesh-cx2", "pc-smichea"), ("ameesh-cx1", "pc-smichea"),
                           ("ameesh-dev3", "ameesh-agents-1"),
                           ("ameesh-dev7", "pc-smichea"),
                           ("mesh-design", "pc-smichea"), ("deepseek7", "ameesh-agents-2"),
                           ("codex1", "pc-smichea")):
            self.db.execute(
                "INSERT INTO agent_registry (name, status, host, harness) "
                "VALUES (%s, 'idle', %s, 'claude')", (name, host))
        # Tours et fusions de l'heure.
        for offset in (60, 120, 600):
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, usd, recorded_at) "
                "VALUES ('ameesh-dev5', 'deepseek', 0, to_timestamp(%s))",
                (T - offset,))
        self._milestone(5000, 5004, "merged", T - 120, sha="c" * 40)
        # Une CI de paquet à 3 h 30, comme le job macOS avant #3198.
        self.db.execute(
            "INSERT INTO ci_runs (repo, run_id, head_sha, branch, status, workflow, "
            "started_at, concluded_at) VALUES "
            "('manaty/ameesh', 42, %s, 'lot/1', 'success', 'tests.yml', "
            "to_timestamp(%s), to_timestamp(%s))",
            ("d" * 40, T - 4 * H, T - 30 * 60))
        return types.SimpleNamespace(
            hosts=[], processes=[types.SimpleNamespace(
                title="Processus de lot", body=self.RULE)])

    def test_temoin_du_24_10_03_20z(self):
        canons = self._seed()
        report = debit.read(self.db, cfg=self.cfg, now=T, hours=6, wait_s=2 * H,
                            ci_hours=24, ci_slow_s=30 * 60, canons=[canons],
                            installed="1.20.0", jobs=False)
        # Les deux avis bloquants non traités, par âge.
        blocked = {row["lot"]: row for row in report["blocked_reviews"]}
        self.assertEqual(sorted(blocked), [3033, 3195])
        self.assertGreaterEqual(blocked[3033]["age_s"], 22 * H)
        self.assertGreaterEqual(blocked[3195]["age_s"], 8.5 * H)
        # Les avis dus : cx2 sur #3033, jamais la colonne reviewer (#9999).
        due = {(row["lot"], row["reviewer"]) for row in report["reviews_due"]}
        self.assertIn((3033, "ameesh-cx2"), due)
        self.assertNotIn(9999, {row["lot"] for row in report["reviews_due"]})
        self.assertNotIn((5000, "ameesh-cx1"), due)
        # Les holds d'appareil et les 8 lots retenus par 1.21.0.
        self.assertEqual(sorted(row["lot"] for row in report["device_holds"]),
                         [99, 3076])
        self.assertEqual(sorted(row["lot"] for row in report["undeployed_holds"]),
                         sorted(self.UNDEPLOYED))
        self.assertEqual(report["totals"]["undeployed_holds"], 8)
        # Les gestes sont nommés pour chaque attente de plus de 2 h.
        waits = {(row["lot"], row["kind"]) for row in report["slow_waits"]}
        self.assertIn((3033, "qa"), waits)
        self.assertIn((3033, "blocking_review"), waits)
        self.assertIn((3195, "blocking_review"), waits)
        self.assertIn((99, "hold_device"), waits)
        self.assertIn((3076, "hold_device"), waits)
        self.assertIn((3015, "hold_undeployed"), waits)
        # Agents au repos avec du travail dû : le débiteur de #3033.
        idle = {row["agent"]: row for row in report["idle_agents_with_work"]}
        self.assertIn("ameesh-dev7", idle)
        self.assertIn(3033, idle["ameesh-dev7"]["fixes"])
        # Débit : tours et fusions, CI lente par run.
        self.assertEqual(report["throughput"]["turns_total"], 3)
        self.assertEqual(report["throughput"]["merges_total"], 1)
        self.assertEqual(report["ci"]["slow_runs"][0]["run_id"], 42)
        self.assertEqual(report["unreadable"], [])

    def test_ci_par_job_lue_par_l_api_bornee(self):
        canons = self._seed()
        seen = []

        def api(path):
            seen.append(path)
            return {"jobs": [{
                "name": "plateforme macOS", "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-24T00:00:00Z",
                "completed_at": "2026-10-24T03:30:00Z"}]}

        report = debit.read(self.db, cfg=self.cfg, now=T, canons=[canons],
                            installed="1.20.0", jobs=True, api=api)
        self.assertTrue(seen)
        self.assertTrue(seen[0].startswith("/repos/"), seen[0])
        self.assertEqual(report["ci"]["slow_jobs"][0]["job"], "plateforme macOS")
        self.assertEqual(report["ci"]["slow_jobs"][0]["duration_s"], 3.5 * H)

    def test_temoin_texte_et_check(self):
        canons = self._seed()
        report = debit.read(self.db, cfg=self.cfg, now=T, canons=[canons],
                            installed="1.20.0", jobs=False)
        text = debit.format_text(report)
        self.assertIn("#3033", text)
        self.assertIn("#3195", text)
        self.assertIn("hold_device", text)
        self.assertTrue(report["totals"]["slow_waits"] >= 5)


class VersionEtAttentesHumainesTest(unittest.TestCase):
    """Constats de l'auditeur du 24/10 (#3292) : version toujours « unknown »,
    attentes humaines mêlées aux attentes d'agent dans `--check`."""

    def test_version_lue_sans_option(self):
        from ameesh import version
        self.assertEqual(debit.installed_version(), version())
        self.assertNotEqual(debit.installed_version(), "unknown")

    def test_hold_sur_version_superieure_sans_installed(self):
        # Sans `installed=`, le rapport doit comparer à la version du paquet,
        # pas à « unknown » : sinon `_version_gt` ne compare jamais.
        report = run(
            installed=None,
            items=[item(3015)],
            events=[event(3015, 1, T - 3 * H,
                          "hold: attend la 999.0.0, correctif fusionné non déployé")])
        self.assertEqual([row["lot"] for row in report["undeployed_holds"]], [3015])
        self.assertEqual(report["totals"]["undeployed_holds"], 1)

    def test_hold_owner_hors_check(self):
        report = run(
            items=[item(1783, assignee="orchestrateur")],
            events=[event(1783, 1, T - 3 * H,
                          "hold: report décidé par l'owner (décision signée #75418)")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["totals"]["human_waits"], 1)
        self.assertEqual([row["lot"] for row in report["human_waits"]], [1783])
        self.assertIn("hors --check", debit.format_text(report))

    def test_hold_agent_reste_dans_les_attentes(self):
        report = run(
            items=[item(2381)],
            events=[event(2381, 1, T - 3 * H,
                          "hold: attend un paquet Nexlink réel coupé par le train ameesh")])
        self.assertEqual([row["lot"] for row in report["slow_waits"]], [2381])
        self.assertEqual(report["human_waits"], [])

    def test_lot_waiting_human_hors_check(self):
        report = run(
            items=[item(666, assignee="orchestrateur")],
            events=[event(666, 1, T - 3 * H, "hold: attends la réponse",
                          state="waiting_human")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual([row["lot"] for row in report["human_waits"]], [666])


if __name__ == "__main__":
    unittest.main()
