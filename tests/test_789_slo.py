# SPDX-License-Identifier: AGPL-3.0-only
"""#789 : SLO internes et escalades (canon §9, home#40).

Le moteur : la table du §9, les niveaux 1×/2×/3×, les destinataires, et la
mesure des étapes qui vivent dans les événements du lot (signalement →
assigné, intake → build, build sans nouvel événement, verdict ok → fusion).
"""
from __future__ import annotations

import unittest

from ameesh import progress, slo

T0 = 1_700_000_000.0


def _item(**extra):
    item = {"state": "intake", "assignee": "", "created_ts": T0, "updated_ts": T0}
    item.update(extra)
    return item


def _ev(state, at, note=""):
    return {"state": state, "created_ts": at, "note": note, "actor": "chef"}


def _jalons(item, events):
    return progress._work_item_milestones(item, events, [])


class TableTest(unittest.TestCase):
    """La table est celle du canon §9, avec ses deux colonnes de délai."""

    def test_les_neuf_etapes_du_canon(self):
        self.assertEqual(len(slo.STEPS), 9)
        self.assertEqual([s.key for s in slo.STEPS], [
            "signal_assigned", "intake_started", "build_quiet", "green_verdict",
            "verdict_merge", "merge_prod", "prod_verified", "ci_queue",
            "agent_paused"])

    def test_delais_du_canon(self):
        attendus = {
            "signal_assigned": (3600, 900),
            "intake_started": (3600, 900),
            "build_quiet": (7200, 3600),
            "green_verdict": (7200, 1800),
            "verdict_merge": (1800, 900),
            "merge_prod": (86400, 14400),
            "prod_verified": (7200, 3600),
            "ci_queue": (900, None),
            "agent_paused": (600, None),
        }
        for key, (cible, p1) in attendus.items():
            step = slo.step_of(key)
            self.assertEqual(step.target_s, cible, key)
            self.assertEqual(step.critical_s, p1, key)

    def test_seules_les_etapes_des_evenements_sont_mesurees(self):
        mesurees = {s.key for s in slo.STEPS if s.measured}
        self.assertEqual(mesurees, {"signal_assigned", "intake_started",
                                    "build_quiet", "verdict_merge"})
        for step in slo.STEPS:
            if not step.measured:
                self.assertEqual(step.source, slo.EXTERNAL, step.key)

    def test_etape_inconnue_refusee(self):
        with self.assertRaises(ValueError):
            slo.step_of("pas_une_etape")


class NiveauxTest(unittest.TestCase):
    def test_seuils(self):
        self.assertEqual(slo.level(3599, 3600), 0)
        self.assertEqual(slo.level(3600, 3600), 1)
        self.assertEqual(slo.level(7199, 3600), 1)
        self.assertEqual(slo.level(7200, 3600), 2)
        self.assertEqual(slo.level(10799, 3600), 2)
        self.assertEqual(slo.level(10800, 3600), 3)
        self.assertEqual(slo.level(99999, 3600), 3)

    def test_cibles_par_niveau(self):
        self.assertEqual(slo.targets(0), ())
        self.assertEqual(slo.targets(1), ("assignee",))
        self.assertEqual(slo.targets(2), ("pilot",))
        self.assertEqual(slo.targets(3), ("bureau", "auditeur"))

    def test_p1_ajoute_bureau_et_auditeur(self):
        # #2395 : le niveau 4 vise le bureau et l'auditeur, jamais l'owner
        # (canon §9, décision owner 18/10).
        self.assertEqual(slo.targets(1, critical=True),
                         ("assignee", "bureau", "auditeur"))
        self.assertEqual(slo.targets(3, critical=True), ("bureau", "auditeur"))

    def test_delai_p1(self):
        self.assertEqual(slo.target_s(slo.step_of("build_quiet")), 7200)
        self.assertEqual(slo.target_s(slo.step_of("build_quiet"), critical=True), 3600)
        # pas de colonne P1 : le délai cible tient
        self.assertEqual(slo.target_s(slo.step_of("ci_queue"), critical=True), 900)


class MesureTest(unittest.TestCase):
    def _row(self, rows, key):
        got = [r for r in rows if r["step"] == key]
        self.assertEqual(len(got), 1, "%s absent de %s" % (key, rows))
        return got[0]

    def test_signal_assigne_tenu(self):
        events = [_ev("intake", T0, "created")]
        item = _item(assignee="executant", updated_ts=T0 + 1800)
        events.append(_ev("intake", T0 + 1800, "assigned to executant (before: nobody)"))
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 3600)
        row = self._row(rows, "signal_assigned")
        self.assertEqual(row["elapsed_s"], 1800)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["done_ts"], T0 + 1800)

    def test_signal_assigne_depasse_puis_pilote(self):
        item = _item()
        rows = slo.lot_steps(item, _jalons(item, []), [], now=T0 + 7200)
        row = self._row(rows, "signal_assigned")
        self.assertEqual(row["level"], 2)
        self.assertEqual(row["targets"], ("pilot",))
        self.assertEqual(row["overdue_s"], 7200 - 3600)

    def test_p1_signal_assigne_vise_bureau_et_auditeur(self):
        item = _item(priority=1)
        rows = slo.lot_steps(item, _jalons(item, []), [], now=T0 + 7200)
        row = self._row(rows, "signal_assigned")
        self.assertEqual(row["target_s"], 900)
        self.assertEqual(row["level"], 3)
        self.assertEqual(row["targets"], ("bureau", "auditeur"))

    def test_intake_demarre(self):
        events = [_ev("intake", T0), _ev("build", T0 + 1800)]
        item = _item(state="build", assignee="executant", updated_ts=T0 + 1800)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 3600)
        row = self._row(rows, "intake_started")
        self.assertEqual(row["elapsed_s"], 1800)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["done_ts"], T0 + 1800)

    def test_intake_pas_demarre(self):
        events = [_ev("intake", T0)]
        item = _item(assignee="executant")
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 3660)
        row = self._row(rows, "intake_started")
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["targets"], ("assignee",))

    def test_intake_reporte_part_du_jour_planifie(self):
        from ameesh.plan import day_start_ts
        planned = day_start_ts("2026-10-19")
        item = _item(assignee="executant", planned_start="2026-10-19")
        for offset, expected in ((-1, None), (0, 0), (180, 0),
                                 (3599, 0), (3600, 1), (10800, 3)):
            with self.subTest(offset=offset):
                rows = slo.lot_steps(item, _jalons(item, []), [], now=planned + offset)
                intake = [r for r in rows if r["step"] == "intake_started"]
                if expected is None:
                    self.assertEqual(intake, [])
                else:
                    self.assertEqual(intake[0]["started_ts"], planned)
                    self.assertEqual(intake[0]["elapsed_s"], offset)
                    self.assertEqual(intake[0]["level"], expected)

    def test_intake_planifie_et_prerequis_combines(self):
        from ameesh.plan import day_start_ts
        planned = day_start_ts("2026-10-19")
        item = _item(assignee="executant", planned_start="2026-10-19")
        cases = (
            (True, None, planned + 10800, None),
            (False, planned - 3600, planned - 1, None),
            (False, planned - 3600, planned + 1800, planned),
            (False, planned + 3600, planned + 5400, planned + 3600),
        )
        for blocking, satisfied, now, expected in cases:
            with self.subTest(blocking=blocking, satisfied=satisfied, now=now):
                edges = [{"blocking": blocking, "satisfied_ts": satisfied}]
                rows = slo.lot_steps(item, _jalons(item, []), [], now=now,
                                     dependencies=edges)
                intake = [r for r in rows if r["step"] == "intake_started"]
                if expected is None:
                    self.assertEqual(intake, [])
                else:
                    self.assertEqual(intake[0]["started_ts"], expected)
                    self.assertEqual(intake[0]["elapsed_s"], 1800)
                    self.assertEqual(intake[0]["level"], 0)

    def test_intake_assignation_apres_date_planifiee(self):
        from ameesh.plan import day_start_ts
        planned = day_start_ts("2026-10-19")
        item = _item(assignee="executant", planned_start="2026-10-19")
        events = [_ev("intake", planned + 7200,
                      "assigned to executant (before: nobody)")]
        before = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                        now=planned + 10800), "intake_started")
        self.assertEqual(before["started_ts"], planned + 7200)
        self.assertEqual(before["elapsed_s"], 3600)
        events.append(_ev("build", planned + 10800))
        item["state"] = "build"
        after = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                       now=planned + 14400), "intake_started")
        self.assertEqual(after["started_ts"], before["started_ts"])
        self.assertEqual(after["elapsed_s"], 3600)
        self.assertEqual(after["level"], 0)

    def test_intake_garde_son_origine_au_demarrage(self):
        events = [_ev("intake", T0), _ev("intake", T0 + 7200,
                  "assigned to executant (before: nobody)")]
        item = _item(assignee="executant")
        before = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                        now=T0 + 8100), "intake_started")
        events.append(_ev("build", T0 + 8100))
        item.update(state="build", updated_ts=T0 + 8100)
        after = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                       now=T0 + 8200), "intake_started")
        self.assertEqual(before["elapsed_s"], 900)
        self.assertEqual(after["started_ts"], before["started_ts"])
        self.assertEqual(after["elapsed_s"], before["elapsed_s"])

    def test_intake_avec_prerequis_bloquant_ouvert_ne_court_pas(self):
        # #2551 : un lot retenu par une dépendance bloquante ouverte n'est pas
        # « prêt » ; son porteur ne peut rien y faire, aucun retard intake.
        events = [_ev("intake", T0)]
        item = _item(assignee="executant")
        edges = [{"blocker_id": 7, "blocking": True, "satisfied_ts": None}]
        rows = slo.lot_steps(item, _jalons(item, events), events,
                             now=T0 + 13 * 3600, dependencies=edges)
        self.assertEqual([r for r in rows if r["step"] == "intake_started"], [])
        self.assertEqual(slo.breaches(rows), [])

    def test_intake_prerequis_satisfait_compte_depuis_la_levee(self):
        # #2551 : le blocage a duré 10 h ; le délai part de la fusion du
        # bloqueur, pas de l'attribution (sinon niveau 3 à la seconde).
        events = [_ev("intake", T0)]
        item = _item(assignee="executant")
        unblocked = T0 + 10 * 3600
        edges = [{"blocker_id": 7, "blocking": False, "satisfied_ts": unblocked}]
        rows = slo.lot_steps(item, _jalons(item, events), events,
                             now=unblocked + 1800, dependencies=edges)
        row = self._row(rows, "intake_started")
        self.assertEqual(row["started_ts"], unblocked)
        self.assertEqual(row["elapsed_s"], 1800)
        self.assertEqual(row["level"], 0)
        rows = slo.lot_steps(item, _jalons(item, events), events,
                             now=unblocked + 3660, dependencies=edges)
        self.assertEqual(self._row(rows, "intake_started")["level"], 1)

    def test_intake_dernier_prerequis_satisfait_gouverne(self):
        # #2551 : c'est la levée du DERNIER bloqueur qui rend le lot prêt.
        events = [_ev("intake", T0)]
        item = _item(assignee="executant")
        edges = [{"blocker_id": 7, "blocking": False, "satisfied_ts": T0 + 100},
                 {"blocker_id": 8, "blocking": False, "satisfied_ts": T0 + 200}]
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 300,
                             dependencies=edges)
        self.assertEqual(self._row(rows, "intake_started")["started_ts"], T0 + 200)

    def test_intake_repart_au_retour_d_une_retenue(self):
        # #2916 : 2 jours en attente d'une décision owner (ou sous une autre
        # retenue) ne comptent pas comme retard intake ; au retour, l'horloge
        # repart de ce retour — témoin de l'auditeur.
        release = T0 + 2 * 86400
        item = _item(assignee="executant")
        for held in ("waiting_human", "blocked"):
            with self.subTest(held=held):
                events = [_ev("intake", T0),
                          _ev(held, T0 + 60, "decision #1 requested"),
                          _ev("intake", release, "decision #1 answered")]
                jalons = _jalons(item, events)
                for offset, lvl in ((300, 0), (3660, 1)):
                    with self.subTest(offset=offset):
                        row = self._row(slo.lot_steps(item, jalons, events,
                                                     now=release + offset),
                                        "intake_started")
                        self.assertEqual(row["started_ts"], release)
                        self.assertEqual(row["elapsed_s"], offset)
                        self.assertEqual(row["level"], lvl)

    def test_intake_derniere_retenue_levee_gouverne(self):
        # #2916 : seule la DERNIÈRE sortie de retenue fixe le départ.
        release = T0 + 7200
        events = [_ev("intake", T0),
                  _ev("waiting_human", T0 + 60),
                  _ev("intake", T0 + 1800),
                  _ev("blocked", T0 + 3600),
                  _ev("intake", release)]
        item = _item(assignee="executant")
        row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                     now=release + 300), "intake_started")
        self.assertEqual(row["started_ts"], release)
        self.assertEqual(row["elapsed_s"], 300)

    def test_intake_retenue_apres_le_debut_ne_reecrit_pas_la_mesure(self):
        # #2916 : une retenue ajoutée après le début ne réécrit pas la mesure
        # intake déjà acquise (même règle que #2551 pour une dépendance).
        events = [_ev("intake", T0),
                  _ev("waiting_human", T0 + 60),
                  _ev("intake", T0 + 1800),
                  _ev("build", T0 + 3600),
                  _ev("waiting_human", T0 + 7200),
                  _ev("intake", T0 + 9000)]
        item = _item(state="intake", assignee="executant", updated_ts=T0 + 9000)
        row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                     now=T0 + 10_000), "intake_started")
        self.assertEqual(row["started_ts"], T0)
        self.assertEqual(row["done_ts"], T0 + 3600)
        self.assertEqual(row["elapsed_s"], 3600)

    def test_intake_demarre_ne_reecrit_pas_sa_mesure(self):
        # #2551 : une dépendance ajoutée après le début ne réécrit pas la
        # mesure d'intake déjà acquise.
        events = [_ev("intake", T0), _ev("build", T0 + 1800)]
        item = _item(state="build", assignee="executant", updated_ts=T0 + 1800)
        edges = [{"blocker_id": 7, "blocking": True, "satisfied_ts": None}]
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 7200,
                             dependencies=edges)
        row = self._row(rows, "intake_started")
        self.assertEqual(row["started_ts"], T0)
        self.assertEqual(row["done_ts"], T0 + 1800)

    def test_intake_passee_sans_evenement_build_n_est_pas_un_retard(self):
        # #1982 : un lot en build ou en qa sans événement `build` a franchi
        # intake ; la durée est non mesurable — on ne fabrique ni date de build
        # ni retard (cas réel #377).
        events = [_ev("intake", T0), _ev("qa", T0 + 900)]
        for state, at in (("build", T0 + 900), ("qa", T0 + 900)):
            with self.subTest(state=state):
                item = _item(state=state, assignee="executant", updated_ts=at)
                rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 7200)
                self.assertEqual([r for r in rows if r["step"] == "intake_started"], [])

    def test_intake_hors_intake_sans_evenement_build_n_est_pas_un_retard(self):
        # #1982/#1658 : un lot volontairement bloqué ou en attente d'un humain
        # n'est pas « prêt, pas démarré » — pas de retard intake inventé.
        events = [_ev("intake", T0)]
        for state in ("blocked", "waiting_human"):
            with self.subTest(state=state):
                item = _item(state=state, assignee="executant", updated_ts=T0)
                rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 7200)
                self.assertEqual([r for r in rows if r["step"] == "intake_started"], [])

    def test_build_sans_evenement(self):
        events = [_ev("intake", T0), _ev("build", T0 + 600)]
        item = _item(state="build", assignee="executant", updated_ts=T0 + 600)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 600 + 7200)
        row = self._row(rows, "build_quiet")
        self.assertEqual(row["elapsed_s"], 7200)
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["targets"], ("assignee",))

    def test_build_sans_evenement_p1(self):
        events = [_ev("intake", T0), _ev("build", T0 + 600)]
        item = _item(state="build", assignee="executant", priority=1,
                     updated_ts=T0 + 600)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 600 + 7200)
        row = self._row(rows, "build_quiet")
        self.assertEqual(row["target_s"], 3600)
        self.assertEqual(row["level"], 2)

    def test_verdict_ok_attend_la_fusion(self):
        events = [_ev("intake", T0), _ev("build", T0 + 600), _ev("qa", T0 + 1200)]
        item = _item(state="qa", assignee="executant", updated_ts=T0 + 1200)
        jalons = _jalons(item, events)
        jalons["verdict"], jalons["verdict_kind"] = T0 + 1800, "ok"
        jalons["verdict_current"] = True  # GO postérieur au dernier gel
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 1800)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["elapsed_s"], 1800)
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["targets"], ("assignee",))

    def test_verdict_blocked_ne_compte_pas_comme_go(self):
        events = [_ev("intake", T0), _ev("build", T0 + 600), _ev("qa", T0 + 1200)]
        item = _item(state="build", assignee="executant", updated_ts=T0 + 1300)
        jalons = _jalons(item, events)
        jalons["verdict"], jalons["verdict_kind"] = T0 + 1800, "blocked"
        rows = slo.lot_steps(item, jalons, events, now=T0 + 10_000)
        self.assertEqual([r for r in rows if r["step"] == "verdict_merge"], [])

    def test_nouveau_gel_invalide_le_go_precedent(self):
        item = _item(state="qa", assignee="executant")
        declared = [{"kind": "verdict", "verdict": "ok", "at_ts": T0 + 1800},
                    {"kind": "frozen", "at_ts": T0 + 2000}]
        jalons = progress._work_item_milestones(item, [], [], declared)
        self.assertFalse(jalons["verdict_current"])
        rows = slo.lot_steps(item, jalons, [], now=T0 + 10000)
        self.assertEqual([r for r in rows if r["step"] == "verdict_merge"], [])

    def test_breaches_trie_par_gravite(self):
        rows = [{"step": "a", "level": 1, "overdue_s": 10},
                {"step": "b", "level": 3, "overdue_s": 5},
                {"step": "c", "level": 0, "overdue_s": 0},
                {"step": "d", "level": 2, "overdue_s": 99}]
        got = [r["step"] for r in slo.breaches(rows)]
        self.assertEqual(got, ["b", "d", "a"])


class GracePriseTest(unittest.TestCase):
    """#2217 : une prise en charge récente suspend l'escalade, sans effacer la mesure.

    Cas réels du 17/10 (heures locales) : #2180 réassigné cx3→dev7 à 10:50,
    alerte de niveau 3 à 10:53 demandant de réassigner « --if-holder
    ameesh-dev7 » alors que dev7 venait de prendre le lot ; même chose sur
    #660, pris dans la file d'équipe par rel-01 à 10:39, alerte à 10:42.
    Le retard de l'étape court depuis le jalon, pas depuis la prise du porteur
    actuel : le nouveau porteur n'a pas à recevoir aussitôt une demande de
    réassignation.
    """

    def _lot_qa(self, *, verdict_age_s, handover_age_s, note=None):
        handover = T0 + 10 * 86400
        verdict = handover - verdict_age_s
        events = [_ev("intake", T0), _ev("build", T0 + 600),
                  _ev("qa", T0 + 1200),
                  _ev("qa", handover,
                      note or "assigned to nouveau (before: ancien)")]
        item = _item(state="qa", assignee="nouveau", updated_ts=handover)
        jalons = _jalons(item, events)
        jalons["verdict"], jalons["verdict_kind"] = verdict, "ok"
        jalons["verdict_current"] = True
        return slo.lot_steps(item, jalons, events, now=handover + handover_age_s)

    def test_prise_recente_suspend_l_escalade(self):
        rows = self._lot_qa(verdict_age_s=3 * 86400, handover_age_s=10 * 60)
        row = [r for r in rows if r["step"] == "verdict_merge"][0]
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["targets"], ())
        self.assertEqual(row["overdue_s"], 0)
        self.assertEqual(row["handover_grace_s"],
                         int(slo.HANDOVER_GRACE_S) - 600)
        # le retard reste mesuré, il n'est pas effacé
        self.assertEqual(row["elapsed_s"], 3 * 86400 + 600)
        self.assertGreater(row["elapsed_s"], row["target_s"])
        # aucune escalade ne part pendant la grâce
        self.assertEqual(slo.breaches(rows), [])

    def test_prise_ancienne_laisse_l_escalade(self):
        rows = self._lot_qa(verdict_age_s=3 * 86400, handover_age_s=1860)
        row = [r for r in rows if r["step"] == "verdict_merge"][0]
        self.assertEqual(row["level"], 3)
        self.assertEqual(row["targets"], ("bureau", "auditeur"))
        self.assertNotIn("handover_grace_s", row)
        self.assertEqual([r["step"] for r in slo.breaches(rows)],
                         ["verdict_merge"])

    def test_prise_automatique_suspend_l_escalade(self):
        # #660 : le lot a été pris dans la file d'équipe, pas par `work
        # assign` ; la note de prise doit ouvrir la grâce comme une
        # assignation.
        from ameesh import backlog

        note = "%s par rel-01, au repos" % backlog.LOT_TAKE_NOTE
        rows = self._lot_qa(verdict_age_s=3 * 86400, handover_age_s=180,
                            note=note)
        row = [r for r in rows if r["step"] == "verdict_merge"][0]
        self.assertEqual(row["level"], 0)
        self.assertIn("handover_grace_s", row)
        self.assertEqual(slo.breaches(rows), [])

    def test_prise_automatique_date_l_attribution(self):
        # #2217 : sans la note de prise, `assigned_ts` retombait sur la
        # création — `signal_assigned` restait ouvert et courait depuis la
        # création, ce qui produisait l'alerte de réassignation de #660.
        from ameesh import backlog

        events = [_ev("intake", T0),
                  _ev("intake", T0 + 7200,
                      "%s par executant, au repos" % backlog.LOT_TAKE_NOTE)]
        item = _item(assignee="executant")
        self.assertEqual(slo.assigned_ts(item, events), T0 + 7200)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 7300)
        signal = [r for r in rows if r["step"] == "signal_assigned"][0]
        self.assertEqual(signal["level"], 0)
        self.assertEqual(signal["done_ts"], T0 + 7200)
        self.assertEqual(slo.breaches(rows), [])

    def test_derniere_prise_en_compte(self):
        events = [_ev("intake", T0, "assigned to a (before: nobody)"),
                  _ev("qa", T0 + 100, "assigned to b (before: a)"),
                  _ev("qa", T0 + 200, "assigned to c (before: b)")]
        self.assertEqual(slo.handover_ts(_item(assignee="c"), events), T0 + 200)

    def test_toutes_les_prises_datent_le_porteur(self):
        from ameesh import backlog, handoff

        cases = {
            "prise équipe": "%s par b, au repos" % backlog.LOT_TAKE_NOTE,
            "prise file": "%s par b (valeur 30) : attendu" % backlog.AUTO_TAKE_NOTE,
            "reprise pause": "%s par b : a est en pause budget (plafond). Lot en "
                             "build." % handoff.HANDOFF_NOTE,
            "délégation": "delegated to b by a, due in 2h (before: a)",
        }
        for label, note in cases.items():
            with self.subTest(label):
                events = [_ev("intake", T0, "assigned to a (before: nobody)"),
                          _ev("build", T0 + 100, note)]
                self.assertEqual(slo.handover_ts(_item(assignee="b"), events),
                                 T0 + 100)

    def test_rendu_a_la_file_n_ouvre_pas_de_grace(self):
        from ameesh import handoff

        # un retour à la file laisse le lot sans porteur : pas de prise à
        # dater, donc pas de grâce.
        events = [_ev("intake", T0, "assigned to a (before: nobody)"),
                  _ev("intake", T0 + 100,
                      "%s : a est en pause budget (plafond)" % handoff.RETURN_NOTE)]
        self.assertIsNone(slo.handover_ts(_item(), events))
        self.assertNotIn(handoff.RETURN_NOTE, slo.handover_prefixes())

    def test_attribution_initiale_sans_grace(self):
        # un lot créé déjà assigné (`work add --assignee`) n'a pas changé de
        # main : aucune prise à dater, donc pas de grâce.
        self.assertIsNone(slo.handover_ts(_item(assignee="executant"), []))
        self.assertIsNone(slo.handover_ts(_item(), []))


if __name__ == "__main__":
    unittest.main()
