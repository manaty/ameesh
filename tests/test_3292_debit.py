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


def mark_authors(ident, freeze_id, names, authors):
    """Une marque réelle : le gel nomme aussi les auteurs de la tête."""
    import json
    return 'review-designation:%d:%d: %s' % (
        ident, freeze_id,
        json.dumps({"reviewers": [{"name": n} for n in names],
                    "authors": [{"name": n, "family": ""} for n in authors]}))


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

    def test_bloque_puis_ok_du_meme_relecteur_n_est_pas_une_correction_5328(self):
        # #5328 : le repli sans base de `build` applique la règle de #3288 —
        # le dernier verdict de chaque acteur sur la tête tranche. Le témoin
        # #5307 porte un `blocked` puis un `ok` de deepseek5 sur la même tête.
        report = run(
            items=[item(5307, assignee="claude1")],
            milestones=[frozen(5307, 10, T - 6 * H, sha="d" * 40),
                        verdict(5307, 11, T - 5 * H, "deepseek5", "blocked",
                                sha="d" * 40),
                        verdict(5307, 12, T - 4 * H, "deepseek5", "ok",
                                sha="d" * 40)],
            events=[event(5307, 1, T - 6 * H, mark(5307, 10, ["deepseek5"]))],
            agents=[{"name": "claude1", "status": "idle",
                     "host": "ameesh-agents-2", "harness": "claude"}])
        self.assertEqual(report["blocked_reviews"], [])
        self.assertEqual(report["idle_agents_with_work"], [])
        self.assertTrue([row for row in report["qa"] if row["lot"] == 5307][0]["ready"])

    def test_dispense_du_gel_retire_les_avis_et_le_faux_repos_3818(self):
        report = run(
            items=[item(7)], milestones=[frozen(7, 10, T - 5 * H)],
            agents=[{"name": "ameesh-cx2", "status": "idle"}],
            events=[event(7, 1, T - 5 * H, mark(7, 10, ["ameesh-cx2"])),
                    event(7, 2, T - 4 * H,
                          "verdict not required: no re-verdict due (#1979)")])
        self.assertEqual(report["reviews_due"], [])
        self.assertEqual(report["qa"][0]["pending_reviewers"], [])
        self.assertTrue(report["qa"][0]["ready"])
        self.assertEqual(report["idle_agents_with_work"], [])

    def test_nouveau_gel_apres_dispense_redesigne_3818(self):
        report = run(
            items=[item(7)],
            milestones=[frozen(7, 10, T - 5 * H),
                        frozen(7, 11, T - 2 * H, sha="b" * 40)],
            events=[event(7, 1, T - 5 * H, mark(7, 10, ["ameesh-cx2"])),
                    event(7, 2, T - 4 * H,
                          "verdict not required: no re-verdict due (#1979)"),
                    event(7, 3, T - 2 * H, mark(7, 11, ["ameesh-dev3"]))])
        self.assertEqual([r["reviewer"] for r in report["reviews_due"]],
                         ["ameesh-dev3"])

    def test_ancienne_dispense_sans_effet_sur_gel_courant_3818(self):
        report = run(
            items=[item(7)], milestones=[frozen(7, 10, T - 3 * H)],
            events=[event(7, 1, T - 4 * H,
                          "verdict not required: no re-verdict due (#1979)"),
                    event(7, 2, T - 3 * H, mark(7, 10, ["ameesh-cx2"]))])
        self.assertEqual(report["qa"][0]["pending_reviewers"], ["ameesh-cx2"])
        self.assertFalse(report["qa"][0]["ready"])

    def test_designation_explicite_apres_dispense_3818(self):
        report = run(
            items=[item(7)], milestones=[frozen(7, 10, T - 5 * H)],
            events=[event(7, 1, T - 4 * H,
                          "verdict not required: no re-verdict due (#1979)"),
                    event(7, 2, T - 3 * H, "reviewer ameesh-dev3 designated")])
        self.assertEqual(report["qa"][0]["pending_reviewers"], ["ameesh-dev3"])

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

    def test_un_ancien_relecteur_devenu_auteur_n_est_plus_du(self):
        # #3292, constat cx1 sur 8c25d6eb : dev1, correcteur de la tête, est
        # nommé auteur par la marque du gel #17456, qui ne requiert que cx1.
        # Sa désignation manuelle antérieure ne doit pas le réintroduire ; un
        # manuel qui n'est pas auteur reste dû.
        report = run(
            items=[item(3292)],
            milestones=[frozen(3292, 17456, T - 10 * 60, sha="e" * 40)],
            events=[event(3292, 1, T - 2 * H, "reviewer ameesh-dev1 designated",
                          actor="mesh-design"),
                    event(3292, 2, T - 2 * H + 1, "reviewer ameesh-dev3 designated",
                          actor="mesh-design"),
                    event(3292, 3, T - 9 * 60,
                          mark_authors(3292, 17456, ["ameesh-cx1"],
                                       ["ameesh-dev1", "ameesh-dev5"]),
                          actor="ameesh")])
        self.assertEqual(sorted(row["reviewer"] for row in report["reviews_due"]),
                         ["ameesh-cx1", "ameesh-dev3"])
        self.assertFalse(report["qa"][0]["ready"])

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

    def test_note_qa_perimee_apres_fusion_1982(self):
        merged_at = T - 203 * H
        report = run(
            items=[item(1982)],
            milestones=[frozen(1982, 10, merged_at - H),
                        {"work_item_id": 1982, "id": 11, "kind": "merged",
                         "at_ts": merged_at}],
            events=[event(1982, 1, merged_at - H,
                          mark(1982, 10, ["ameesh-cx2"])),
                    event(1982, 2, merged_at + 3, "note concurrente", state="qa"),
                    event(1982, 3, merged_at + 10, "note", state="merged")])
        for key in ("qa", "reviews_due", "slow_waits", "idle_agents_with_work"):
            self.assertEqual(report[key], [], key)

    def test_reouverture_dernier_etat_et_retour_terminal(self):
        open_states = ("intake", "build", "qa", "blocked", "waiting_human")
        for state in open_states + ("merged", "closed"):
            with self.subTest(state=state):
                events = [event(1, 3, T + 3, "dernier", state=state),
                          event(1, 1, T, "fusion", state="merged"),
                          event(1, 2, T + 3, "correction", state="build")]
                self.assertEqual(debit._reopened(events, T),
                                 state in open_states)
        self.assertFalse(debit._reopened(events, None))
        self.assertFalse(debit._reopened([event(1, 1, T, "note")], T))

    def test_une_fermeture_retenue_ne_rouvre_pas_la_revue(self):
        # #3497 : après la fusion, les écritures merged/blocked du harnais ne
        # sont pas une reprise ; sans intake/build/qa, le lot n'est pas en
        # revue (le jalon merge tient).
        self.assertFalse(debit._reopened(
            [event(1, 1, T, "fusion", state="merged"),
             event(1, 2, T + 1, "fermeture retenue", state="blocked"),
             event(1, 3, T + 2, "review-after-merge", state="merged"),
             event(1, 4, T + 3, "encore retenu", state="blocked")], T))
        self.assertFalse(debit._reopened(
            [event(1, 1, T, "fusion", state="merged"),
             event(1, 2, T + 1, "attente externe", state="waiting_human")], T))


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

    def test_lot_fusionne_puis_blocke_en_attente_de_deploiement(self):
        # Témoin #3497 (#3444) : fusion par un paquet, tête de branche absente
        # de main (le débit lit le jalon merge, pas la branche), fermeture
        # retenue jusqu'au déploiement sur pc-smichea. Les états merged/blocked
        # de la fermeture ne rouvrent pas la revue et ne perdent pas la
        # retenue : le lot va en undeployed_holds, pas en qa_merge.
        merged_at = T - 5 * H
        note = ("hold: le code est dans main. Reste le geste hôte : déployer "
                "main sur pc-smichea. Levée : déploiement constaté.")
        report = run(
            items=[dict(item(3444, assignee="mty-amh-impl-08"),
                        branch="lot/3444-executeurs-sans-cles-owner",
                        branch_target="main")],
            milestones=[frozen(3444, 10, merged_at - 3 * H, sha="e" * 40),
                        {"work_item_id": 3444, "id": 11, "kind": "merged",
                         "at_ts": merged_at, "sha": "e" * 40}],
            events=[event(3444, 1, merged_at - 3 * H,
                          mark(3444, 10, ["ameesh-cx3", "ameesh-dev2"])),
                    event(3444, 2, merged_at + 1, "merge vu", state="merged"),
                    event(3444, 3, merged_at + 2, "contenu fusionné",
                          state="blocked"),
                    event(3444, 4, merged_at + 3, note, actor="mesh-design",
                          state="blocked"),
                    event(3444, 5, merged_at + 4, "review-after-merge",
                          state="merged"),
                    event(3444, 6, merged_at + 5, "fermeture retenue",
                          state="blocked")])
        self.assertEqual(report["qa"], [])
        self.assertEqual([row["lot"] for row in report["undeployed_holds"]],
                         [3444])
        self.assertEqual([row["kind"] for row in report["slow_waits"]],
                         ["hold_undeployed"])
        self.assertEqual(report["reviews_due"], [])

    def test_un_lot_rouvert_puis_retenu_reste_en_revue(self):
        # #3409 conservé : un lot vraiment rouvert (build après la fusion)
        # puis retenu reste ouvert, et non en retenue de fermeture.
        merged_at = T - 6 * H
        report = run(
            items=[item(8)],
            milestones=[frozen(8, 10, merged_at - H),
                        {"work_item_id": 8, "id": 11, "kind": "merged",
                         "at_ts": merged_at}],
            events=[event(8, 1, merged_at + 1, "correction: merged → build",
                          state="build"),
                    event(8, 2, merged_at + 2, "retenu", state="blocked")])
        self.assertEqual([row["lot"] for row in report["qa"]], [8])
        self.assertEqual(report["undeployed_holds"], [])

    def test_une_levee_explicite_leve_la_retenue_de_fermeture(self):
        merged_at = T - 5 * H
        report = run(
            items=[item(7)],
            milestones=[frozen(7, 10, merged_at - H),
                        {"work_item_id": 7, "id": 11, "kind": "merged",
                         "at_ts": merged_at}],
            events=[event(7, 1, merged_at + 1,
                          "hold: correctif fusionné pas déployé",
                          state="blocked"),
                    event(7, 2, merged_at + 2, "unhold: déployé",
                          actor="orchestrateur", state="merged"),
                    event(7, 3, merged_at + 3, "fermeture retenue",
                          state="blocked")],
            closure_allowed=lambda who: who == "orchestrateur")
        self.assertEqual(report["undeployed_holds"], [])
        self.assertEqual(report["holds"], [])

    def test_unhold_ordinaire_ne_leve_pas_la_retenue_d_un_pilote(self):
        # B de cx1 (25/10) : la reconstruction de fermeture garde les règles
        # #3219. Un auteur ordinaire ne lève que sa retenue ; un pilote lève
        # toute celle du lot. Sans prédicat (lecture sans base), une levée ne
        # retire que celle de son auteur — jamais celle d'un autre.
        merged_at = T - 5 * H
        milestones = [frozen(1, 10, merged_at - H, sha="a" * 40),
                      {"work_item_id": 1, "id": 11, "kind": "merged",
                       "at_ts": merged_at}]
        base = [event(1, 1, merged_at, "fusion", state="merged"),
                event(1, 2, merged_at + 1,
                      "hold: déployer main sur pc-smichea",
                      actor="mesh-design", state="merged"),
                event(1, 3, merged_at + 2, "review-after-merge",
                      actor="ameesh", state="merged")]
        ordinary = base + [event(1, 4, merged_at + 3,
                                 "unhold: je lève la mienne",
                                 actor="mty-amh-impl-08", state="merged")]
        report = run(items=[item(1)], milestones=milestones, events=ordinary,
                     holds={})
        self.assertEqual([row["by"] for row in report["holds"]],
                         ["mesh-design"])
        self.assertIn(1, [row["lot"] for row in report["undeployed_holds"]])
        pilot = base + [event(1, 4, merged_at + 3, "unhold: staging déployé",
                              actor="orchestrateur", state="merged")]
        report = run(items=[item(1)], milestones=milestones, events=pilot,
                     holds={},
                     closure_allowed=lambda who: who == "orchestrateur")
        self.assertEqual(report["holds"], [])

    def test_un_compte_rendu_de_deploiement_n_est_pas_une_retenue(self):
        # « vidage de déploiement 06:02Z » raconte un geste déjà fait : ce n'est
        # pas une retenue de déploiement (#3458 au passage du 25/10).
        report = run(
            items=[item(3458)],
            events=[event(3458, 1, T - 3 * H,
                          "hold: geste du bureau ; le bureau est actif "
                          "(vidage de déploiement 06:02Z), sans réponse",
                          actor="auditeur", state="blocked")])
        self.assertEqual(report["undeployed_holds"], [])
        self.assertEqual(report["totals"]["holds"], 1)

    def test_formules_de_deploiement_reconnues(self):
        for text, expected in (
                ("correctif fusionné pas déployé", True),
                ("correctif fusionné pas déployée", True),
                ("correctif non déployés", True),
                ("reste à déployer main sur pc-smichea", True),
                ("attendre le déploiement staging", True),
                ("attente du déploiement de #3435", True),
                ("inerte jusqu'au déploiement staging", True),
                ("levée : déploiement constaté sur le poste", True),
                ("le bureau est actif (vidage de déploiement 06:02Z)", False),
                ("correctif déployé hier, reste la vérification", False),
                ("aucun déploiement prévu cette semaine", False)):
            with self.subTest(text=text):
                self.assertEqual(bool(debit._UNDEPLOYED.search(text)), expected)

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

    def test_qa_merge_mesure_les_lots_fusionnes_hors_items(self):
        # B1 (auditeur #4275) : `_ITEMS_SQL` écarte un lot fusionné ; la mesure
        # qa_merge doit néanmoins le compter, sinon 1 lot mesuré contre 134
        # relus composés sur la vraie base du 27/10.
        self._item(6001, "ameesh-dev5")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = 6001")
        self._milestone(6001, 6101, "frozen", T - 13 * H, sha="e" * 40)
        self._milestone(6001, 6102, "verdict", T - 12 * H, sha="e" * 40,
                        actor="ameesh-cx1", verdict="ok")
        self._milestone(6001, 6103, "merged", T - 2 * H, sha="e" * 40)
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, "
            "assignee, created_at) VALUES "
            "(6000, 'evolution', 'test', 'paquet h-test', 'ameesh', 'merged', "
            "'mesh-design', to_timestamp(%s))", (T - 10 * H,))
        self.db.execute(
            "INSERT INTO work_packet_members (package_id, work_item_id, "
            "package_branch, base_sha, head_sha, unit_sha, source_sha, "
            "frozen_sha, recorded_by, recorded_at) VALUES "
            "(6000, 6001, 'integration/paquet-h-test', %s, %s, %s, %s, %s, "
            "'test', to_timestamp(%s))",
            ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, T - 10 * H))
        report = debit.read(self.db, cfg=self.cfg, now=T, hours=6,
                            wait_s=2 * H, canons=[], installed="1.20.0",
                            jobs=False)
        yesterday = report["qa_merge"]["yesterday"]
        self.assertEqual(yesterday["count"], 1, report["qa_merge"])
        self.assertEqual(yesterday["median_s"], 2 * H)

    def test_read_garde_un_hold_apres_la_fusion(self):
        # B2 (cx1) : frozen@10, merged@20, hold@30 « correctif non déployé ».
        # `build` lisait ce hold, mais `read` écartait le lot par son jalon
        # terminal ; même perte pour merged → build sans nouveau gel.
        canons = self._seed()
        self._item(6000, "ameesh-dev5")
        self._milestone(6000, 26000, "frozen", T - 30 * H, sha="e" * 40)
        self._milestone(6000, 26001, "merged", T - 20 * H, sha="e" * 40)
        self._event(6000, 46000, T - 30 * H, "gel", state="qa")
        self._event(6000, 46001, T - 20 * H, "merge seen", state="merged")
        self._event(6000, 46002, T - 10 * H,
                    "hold: attend la 1.21.0 (correctif fusionné pas déployé)",
                    state="merged")
        self._item(6001, "ameesh-dev5")
        self._milestone(6001, 26002, "frozen", T - 30 * H, sha="f" * 40)
        self._milestone(6001, 26003, "merged", T - 20 * H, sha="f" * 40)
        self._event(6001, 46003, T - 30 * H, "gel", state="qa")
        self._event(6001, 46004, T - 20 * H, "merge seen", state="merged")
        self._event(6001, 46005, T - 10 * H, "correction: merged → build",
                    state="build")
        report = debit.read(self.db, cfg=self.cfg, now=T, canons=[canons],
                            installed="1.20.0", jobs=False)
        self.assertIn(6000, [row["lot"] for row in report["undeployed_holds"]])
        self.assertIn((6000, "hold_undeployed"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])
        self.assertIn(6001, [row["lot"] for row in report["qa"]])

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
                          "hold: report décidé par l'owner (décision signée #75418) ; "
                          "reprise sur décision owner.")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["totals"]["human_waits"], 1)
        self.assertEqual([row["lot"] for row in report["human_waits"]], [1783])
        self.assertIn("hors --check", debit.format_text(report))

    def test_consigne_owner_n_est_pas_humaine(self):
        # Revue dev1 B1 : « consigne owner 24/10 » est une simple mention ;
        # l'attente (#3033) est tenue par le bureau, pas par l'owner.
        report = run(
            items=[item(3033)],
            events=[event(3033, 1, T - 3 * H,
                          "hold: la branche est tenue par le bureau (consigne owner "
                          "24/10 transmise par le bureau)")])
        self.assertEqual([row["lot"] for row in report["slow_waits"]], [3033])
        self.assertEqual(report["human_waits"], [])

    def test_decision_owner_signee_n_est_pas_humaine(self):
        report = run(
            items=[item(2267)],
            events=[event(2267, 1, T - 3 * H,
                          "hold: décision owner #124654 = b (signée, approval #77) — "
                          "attendre et regrouper avec #2878")])
        self.assertEqual([row["lot"] for row in report["slow_waits"]], [2267])
        self.assertEqual(report["human_waits"], [])

    def test_regle_owner_n_est_pas_humaine(self):
        report = run(
            items=[item(2376)],
            events=[event(2376, 1, T - 3 * H,
                          "hold: attendre la remise à zéro du quota le 1/11 "
                          "(règle owner 23/10)")])
        self.assertEqual([row["lot"] for row in report["slow_waits"]], [2376])
        self.assertEqual(report["human_waits"], [])

    def test_go_owner_requis_est_humain(self):
        report = run(
            items=[item(2878)],
            events=[event(2878, 1, T - 3 * H,
                          "hold: go owner signé requis (à demander au retour de l'owner)")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual([row["lot"] for row in report["human_waits"]], [2878])

    def test_decision_owner_due_est_humaine(self):
        report = run(
            items=[item(9001)],
            events=[event(9001, 1, T - 3 * H,
                          "hold: décision owner due pour le go de production")])
        self.assertEqual([row["lot"] for row in report["human_waits"]], [9001])

    def test_attente_d_agent_pres_d_une_mention_owner_n_est_pas_humaine(self):
        # Relecture dev1 sur 80af09e5 : « attend … (consigne owner) » ou
        # « demandée par l'owner » reste une attente d'agent ; seul l'owner
        # objet direct de l'attente la rend humaine.
        for text in ("hold: attend la fusion de #3100 (consigne owner)",
                     "hold: attente de #3000 (règle owner 23/10)",
                     "hold: évolution demandée par l'owner, attend le lot #3100",
                     "hold: l'owner doit être notifié après fusion; attend #3100",
                     "hold: validation owner reçue, en attente du paquet #3100"):
            with self.subTest(text=text):
                self.assertFalse(debit._human_wait(None, text))
        for text in ("hold: retour de l'owner attendu",
                     "hold: attend l'owner (séance de 16:15)",
                     "hold: l'owner doit signer le go prod"):
            with self.subTest(text=text):
                self.assertTrue(debit._human_wait(None, text))

    def test_hold_undeployed_jamais_humain(self):
        # Canon §11 bis : même en waiting_human et avec un « owner » dans la
        # note, un correctif fusionné non déployé est une attente d'agent.
        report = run(
            items=[item(3024)],
            events=[event(3024, 1, T - 3 * H,
                          "hold: 1.21.0 pas déployé, décision owner due",
                          state="waiting_human")])
        self.assertEqual([row["lot"] for row in report["slow_waits"]], [3024])
        self.assertEqual(report["human_waits"], [])
        self.assertEqual([row["lot"] for row in report["undeployed_holds"]], [3024])

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
