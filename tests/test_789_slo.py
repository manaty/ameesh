# SPDX-License-Identifier: AGPL-3.0-only
"""#789 : SLO internes et escalades (canon §9, home#40).

Le moteur : la table du §9, les niveaux 1×/2×/3×, les destinataires, et la
mesure des étapes qui vivent dans les événements du lot (signalement →
assigné, intake → build, build sans nouvel événement, verdict ok → fusion).
"""
from __future__ import annotations

import unittest

from ameesh import packet_link, progress, slo

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

    def test_les_dix_etapes_du_canon(self):
        # #3288 : « avis bloquant → nouveau gel » s'ajoute au §9.
        self.assertEqual(len(slo.STEPS), 10)
        self.assertEqual([s.key for s in slo.STEPS], [
            "signal_assigned", "intake_started", "build_quiet", "green_verdict",
            "blocked_freeze", "verdict_merge", "merge_prod", "prod_verified",
            "ci_queue", "agent_paused"])

    def test_delais_du_canon(self):
        attendus = {
            "signal_assigned": (3600, 900),
            "intake_started": (3600, 900),
            "build_quiet": (7200, 3600),
            "green_verdict": (7200, 1800),
            "blocked_freeze": (7200, 3600),
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
        # #3033 : `green_verdict` est mesurée depuis la désignation au gel
        # #3288 : `blocked_freeze` l'est depuis le premier avis bloquant
        self.assertEqual(mesurees, {"signal_assigned", "intake_started",
                                    "build_quiet", "green_verdict",
                                    "blocked_freeze", "verdict_merge"})
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
        # #2916/#2921 : une retenue postérieure au premier build ne réécrit pas
        # la mesure acquise ; la dernière levée ANTÉRIEURE au début gouverne.
        events = [_ev("intake", T0),
                  _ev("waiting_human", T0 + 60),
                  _ev("intake", T0 + 1800),
                  _ev("build", T0 + 3600),
                  _ev("waiting_human", T0 + 7200),
                  _ev("intake", T0 + 9000)]
        item = _item(state="intake", assignee="executant", updated_ts=T0 + 9000)
        row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                     now=T0 + 10_000), "intake_started")
        self.assertEqual(row["started_ts"], T0 + 1800)
        self.assertEqual(row["done_ts"], T0 + 3600)
        self.assertEqual(row["elapsed_s"], 1800)
        # sans levée antérieure, la levée postérieure au début est ignorée
        events = [_ev("intake", T0),
                  _ev("build", T0 + 3600),
                  _ev("waiting_human", T0 + 7200),
                  _ev("intake", T0 + 9000)]
        item = _item(state="intake", assignee="executant", updated_ts=T0 + 9000)
        row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                     now=T0 + 10_000), "intake_started")
        self.assertEqual(row["started_ts"], T0)
        self.assertEqual(row["elapsed_s"], 3600)

    def test_intake_repart_apres_un_hold_de_pilote(self):
        # #2916/#1225 : un hold explicite de pilote (#1664) n'est pas un état ;
        # sa levée, autorisée par l'appelant, fait repartir l'horloge comme
        # une retenue d'état. Cas réel nexlink #1225.
        release = T0 + 2 * 86400
        events = [_ev("intake", T0),
                  _ev("intake", T0 + 60, "hold: attente des gestes owner")]
        item = _item(assignee="executant")
        for offset, lvl in ((300, 0), (3660, 1)):
            with self.subTest(offset=offset):
                row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                             now=release + offset,
                                             hold_releases=[release]),
                                "intake_started")
                self.assertEqual(row["started_ts"], release)
                self.assertEqual(row["elapsed_s"], offset)
                self.assertEqual(row["level"], lvl)

    def test_intake_hold_de_pilote_apres_le_debut_ne_reecrit_pas_la_mesure(self):
        # #2916/#2921 : même filtre pour les holds de pilote — la dernière
        # levée antérieure au premier build gouverne.
        events = [_ev("intake", T0),
                  _ev("intake", T0 + 60, "hold: attente"),
                  _ev("build", T0 + 3600)]
        item = _item(state="build", assignee="executant", updated_ts=T0 + 3600)
        row = self._row(slo.lot_steps(item, _jalons(item, events), events,
                                     now=T0 + 10_000,
                                     hold_releases=[T0 + 1800, T0 + 9000]),
                        "intake_started")
        self.assertEqual(row["started_ts"], T0 + 1800)
        self.assertEqual(row["elapsed_s"], 1800)

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

    def test_porteur_en_composition_n_est_pas_en_retard_de_demarrage(self):
        # #4002 : le porteur d'un paquet horaire reste en intake jusqu'au gel ;
        # sa première composition enregistrée est son début (cas réel h-41,
        # #3997 : 16 min en composition, alerte P1 à tort).
        note = slo.PACKET_COMPOSITION_NOTE + "3 unit(s) aaa..bbb (branch integration/x)"
        events = [_ev("intake", T0), _ev("intake", T0 + 300, note),
                  _ev("intake", T0 + 900, note)]
        item = _item(assignee="pilote", priority=1)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 1200)
        row = self._row(rows, "intake_started")
        self.assertEqual(row["done_ts"], T0 + 300)
        self.assertEqual(row["level"], 0)
        self.assertEqual(slo.breaches(rows), [])

    def test_note_de_composition_concorde_avec_packet_link(self):
        # #4002 : le préfixe lu par le SLO est celui qu'écrit l'enregistrement.
        import inspect
        self.assertIn('"%s%%d unit(s)' % slo.PACKET_COMPOSITION_NOTE,
                      inspect.getsource(packet_link))

    def test_porteur_sans_composition_reste_en_retard_de_demarrage(self):
        # #4002 : sans aucune composition enregistrée, l'alerte reste due.
        events = [_ev("intake", T0), _ev("intake", T0 + 300, "intention : composition n=1")]
        item = _item(assignee="pilote", priority=1)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 1200)
        row = self._row(rows, "intake_started")
        self.assertNotIn("done_ts", row)
        self.assertEqual(row["level"], 1)

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


class PaquetTest(unittest.TestCase):
    """#2919 : la cible « GO → fusion » d'une unité d'un paquet ouvert est le
    p80 réel de la CI du paquet, et l'unité n'a aucune cible d'escalade propre
    (canon §9, décision owner #103509 option a, home#147). Hors paquet
    (incident ou déblocage de la CI), la cible de la table reste."""

    def _qa(self, *, priority=None):
        events = [_ev("intake", T0), _ev("build", T0 + 600), _ev("qa", T0 + 1200)]
        item = _item(state="qa", assignee="executant", updated_ts=T0 + 1200)
        if priority is not None:
            item["priority"] = priority
        jalons = _jalons(item, events)
        jalons["verdict"], jalons["verdict_kind"] = T0 + 1800, "ok"
        jalons["verdict_current"] = True
        return item, jalons, events

    def _row(self, rows, key):
        got = [r for r in rows if r["step"] == key]
        self.assertEqual(len(got), 1, "%s absent de %s" % (key, rows))
        return got[0]

    def test_cible_p80_et_aucune_escalade_propre(self):
        item, jalons, events = self._qa()
        # 45 min de CI de paquet (le p80 mesuré), dépassées d'une heure.
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 3600,
                             packet=True, p80_s=2700)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["target_s"], 2700)
        self.assertEqual(row["elapsed_s"], 3600)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["targets"], ())
        self.assertEqual(row["overdue_s"], 0)
        self.assertTrue(row["packet"])
        self.assertEqual(row["target_source"], "p80")
        self.assertFalse(row["escalation"])
        self.assertEqual(slo.breaches(rows), [])

    def test_chemin_normal_p80_garde_l_escalade(self):
        # #2919 : un lot à GO courant hors paquet ouvert est mesuré contre le
        # p80 ; l'escalade ×2/×3 reste (le pilote porte la fusion).
        item, jalons, events = self._qa()
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 3600,
                             p80_s=2700)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["target_s"], 2700)
        self.assertEqual(row["elapsed_s"], 3600)
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["overdue_s"], 900)
        self.assertEqual(row["targets"], ("assignee",))
        self.assertEqual(row["target_source"], "p80")
        self.assertNotIn("packet", row)
        self.assertNotIn("escalation", row)

    def test_chemin_normal_p1_p80_40_min(self):
        # Témoin du N auditeur : P1, GO courant hors paquet, p80 40 min ;
        # 20 min après le créneau → tenu, 41 min → niveau 1.
        item, jalons, events = self._qa(priority=1)
        for offset, level in ((20 * 60, 0), (41 * 60, 1)):
            with self.subTest(offset=offset):
                rows = slo.lot_steps(item, jalons, events,
                                     now=T0 + 1800 + offset, p80_s=2400)
                row = self._row(rows, "verdict_merge")
                self.assertEqual(row["target_s"], 2400)
                self.assertEqual(row["level"], level)

    def test_sans_mesure_repli_sur_la_cible_de_la_table(self):
        # Aucun run de suite complète mesuré : la cible de la table (30 min)
        # reste le repli. L'unité de paquet garde ses cibles vides ; le
        # chemin normal garde l'escalade de la table.
        for measured in (None, 0, -1, "illisible"):
            with self.subTest(measured=measured, packet=True):
                item, jalons, events = self._qa()
                rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 3600,
                                     packet=True, p80_s=measured)
                row = self._row(rows, "verdict_merge")
                self.assertEqual(row["target_s"], 1800)
                self.assertEqual(row["level"], 0)
                self.assertEqual(row["targets"], ())
                self.assertEqual(row["target_source"], "fallback")
                self.assertEqual(slo.breaches(rows), [])
            with self.subTest(measured=measured, packet=False):
                item, jalons, events = self._qa()
                rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 1900,
                                     p80_s=measured)
                row = self._row(rows, "verdict_merge")
                self.assertEqual(row["target_s"], 1800)
                self.assertEqual(row["level"], 1)
                self.assertEqual(row["overdue_s"], 100)
                self.assertEqual(row["targets"], ("assignee",))
                self.assertEqual(row["target_source"], "fallback")

    def test_chemin_normal_p1_sans_mesure_repli_30_min(self):
        # Témoin cx2 (#15024) : P1 normal sans mesure, la ligne paquet a une
        # colonne P1 « — » ; 20 min après le créneau → tenu (pas 900/15 min),
        # 31 min → niveau 1 sur 1800.
        item, jalons, events = self._qa(priority=1)
        for offset, level in ((20 * 60, 0), (31 * 60, 1)):
            with self.subTest(offset=offset):
                rows = slo.lot_steps(item, jalons, events,
                                     now=T0 + 1800 + offset)
                row = self._row(rows, "verdict_merge")
                self.assertEqual(row["target_s"], 1800)
                self.assertEqual(row["level"], level)
                self.assertEqual(row["target_source"], "fallback")

    def test_paquet_p1_garde_le_p80_pas_la_cible_p1(self):
        # La colonne P1 de la ligne « paquet » est « — » : un P1 ne reprend pas
        # la cible de 15 min, il suit le p80 comme les autres.
        item, jalons, events = self._qa(priority=1)
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 1000,
                             packet=True, p80_s=2700)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["target_s"], 2700)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["targets"], ())

    def test_exception_p1_garde_15_min_et_l_escalade(self):
        # Incident/déblocage CI déclaré (`outside_packet`) : la cible de la
        # table reprend sa colonne P1 (15 min) et l'escalade depuis le GO —
        # ici une CI verte antérieure au GO, donc le départ reste le GO.
        item, jalons, events = self._qa(priority=1)
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 1000,
                             outside_packet=True, ci_green_ts=T0 + 600)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["target_s"], 900)
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["targets"],
                         ("assignee", "bureau", "auditeur"))
        self.assertEqual(row["target_source"], "table")
        self.assertEqual(row["started_ts"], T0 + 1800)
        self.assertNotIn("packet", row)

    def test_hors_paquet_le_vert_de_la_ci_fixe_le_depart(self):
        # #3341 : GO à T0+1800, CI verte à T0+4200 (40 min après le GO).
        # L'horloge P1 (15 min) part du vert : rien avant T0+5100, niveau 1 à
        # T0+5100 — pas à GO+15 min pendant que la CI tourne encore.
        item, jalons, events = self._qa(priority=1)
        green = T0 + 4200.0
        before = slo.lot_steps(item, jalons, events, now=green + 899,
                               outside_packet=True, ci_green_ts=green)
        row = self._row(before, "verdict_merge")
        self.assertEqual(row["started_ts"], green)
        self.assertEqual(row["elapsed_s"], 899)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["ci_green_ts"], green)
        at = slo.lot_steps(item, jalons, events, now=green + 900,
                           outside_packet=True, ci_green_ts=green)
        row = self._row(at, "verdict_merge")
        self.assertEqual(row["started_ts"], green)
        self.assertEqual(row["level"], 1)
        self.assertEqual(row["overdue_s"], 0)
        self.assertEqual(row["targets"], ("assignee", "bureau", "auditeur"))

    def test_hors_paquet_vert_avant_le_go_part_du_go(self):
        # Témoin : une CI verte avant le GO ne fait pas remonter le départ
        # avant le GO — max(GO, vert) = GO, comme aujourd'hui.
        item, jalons, events = self._qa(priority=1)
        green = T0 + 600.0
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 899,
                             outside_packet=True, ci_green_ts=green)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 1800)
        self.assertEqual(row["level"], 0)
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 900,
                             outside_packet=True, ci_green_ts=green)
        self.assertEqual(self._row(rows, "verdict_merge")["level"], 1)

    def test_hors_paquet_sans_ci_verte_aucune_horloge(self):
        # Témoin : une CI absente, encore en cours ou rouge ne fait pas partir
        # l'horloge ; hors paquet sans vert, aucune ligne `verdict_merge` (donc
        # aucune alerte), même longtemps après le GO.
        item, jalons, events = self._qa(priority=1)
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 10 * 3600,
                             outside_packet=True)
        self.assertEqual([r for r in rows if r["step"] == "verdict_merge"], [])
        self.assertEqual(slo.breaches(rows), [])

    def test_hors_paquet_la_ci_ne_change_pas_le_chemin_paquet(self):
        # Le chemin paquet (#2335) garde son créneau et sa cible : un vert de
        # CI plus tardif ne le décale pas, et une mesure absente ne le tait
        # pas.
        item, jalons, events = self._qa()
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 5400,
                             packet_slot=T0 + 7200, packet=True, p80_s=2700,
                             ci_green_ts=T0 + 9000)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 7200)
        self.assertEqual(row["target_s"], 2700)
        self.assertEqual(row["level"], 0)

    def test_paquet_fusionne_garde_la_duree_et_le_p80(self):
        item, jalons, events = self._qa()
        jalons["merged"] = T0 + 1800 + 3000
        rows = slo.lot_steps(item, jalons, events, now=T0 + 10_000,
                             packet=True, p80_s=2700)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["elapsed_s"], 3000)
        self.assertEqual(row["target_s"], 2700)
        self.assertEqual(row["done_ts"], T0 + 4800)
        self.assertFalse(row["escalation"])

    def test_creneau_de_paquet_garde_le_report(self):
        # #2335/A2 : l'horloge part du premier créneau après le GO ; la cible
        # p80 ne change pas ce départ.
        item, jalons, events = self._qa()
        rows = slo.lot_steps(item, jalons, events, now=T0 + 1800 + 5400,
                             packet_slot=T0 + 7200,
                             packet=True, p80_s=2700)
        row = self._row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 7200)
        self.assertEqual(row["packet_slot"], T0 + 7200)
        self.assertEqual(row["target_s"], 2700)


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


class AttributionAcquiseTest(unittest.TestCase):
    """#3424 : un lot créé déjà assigné puis rendu à la file garde sa première
    attribution. Le journal le prouve (rendu, début mesuré, réassignation) ;
    `signal_assigned`, déjà conclu, ne rouvre pas l'alerte de niveau 3
    « reassign it now » (#3223)."""

    def test_lot_cree_assigne_puis_rendu_garde_sa_premiere_attribution(self):
        events = [_ev("intake", T0, "created; estimate: 20 min"),
                  _ev("intake", T0 + 600,
                      "returned to the queue by executant (§4: lots in build per "
                      "agent, capped by the canon)"),
                  _ev("qa", T0 + 601, "passage en qa sans assigné")]
        item = _item(state="qa", assignee="")
        self.assertEqual(slo.assigned_ts(item, events), T0)
        rows = slo.lot_steps(item, _jalons(item, events), events, now=T0 + 4 * 3600)
        signal = [r for r in rows if r["step"] == "signal_assigned"][0]
        self.assertEqual(signal["level"], 0)
        self.assertEqual(signal["done_ts"], T0)
        self.assertEqual(slo.breaches(rows), [])

    def test_le_rendu_et_le_debut_mesure_prouvent_le_porteur(self):
        from ameesh import estimates, handoff

        cases = {
            "rendu à la file": "%s par executant" % handoff.RETURN_NOTE,
            "début mesuré": "%sfirst turn of executant on the lot"
                            % estimates.START_NOTE,
        }
        for label, note in cases.items():
            with self.subTest(label):
                events = [_ev("intake", T0, "created"),
                          _ev("build", T0 + 10, note)]
                self.assertEqual(slo.assigned_ts(_item(assignee=""), events), T0)

    def test_reassignation_nommant_le_porteur_precedent(self):
        # « before: a » prouve que a a tenu le lot, même sans retour au journal.
        events = [_ev("intake", T0, "created"),
                  _ev("build", T0 + 10, "assigned to b (before: a)")]
        self.assertEqual(slo.assigned_ts(_item(assignee=""), events), T0)

    def test_une_reprise_apres_le_rendu_ne_remplace_pas_la_creation(self):
        # créé assigné, rendu, repris par un autre agent (before: nobody),
        # puis rendu de nouveau : la première attribution reste la création.
        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 600, "returned to the queue by a"),
                  _ev("intake", T0 + 1200, "assigned to b (before: nobody)"),
                  _ev("intake", T0 + 1800, "returned to the queue by b")]
        self.assertEqual(slo.assigned_ts(_item(assignee=""), events), T0)

    def test_lot_cree_sans_porteur_date_sa_premiere_prise(self):
        # créé sans porteur puis attribué (before: nobody), rendu, repris :
        # la première attribution datée reste celle de la prise, pas la
        # création.
        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 600, "assigned to a (before: nobody)"),
                  _ev("intake", T0 + 1200, "returned to the queue by a"),
                  _ev("intake", T0 + 1800, "assigned to b (before: nobody)")]
        self.assertEqual(slo.assigned_ts(_item(assignee=""), events), T0 + 600)

    def test_une_premiere_attribution_datee_prime_sur_la_creation(self):
        from ameesh import backlog

        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 7200,
                      "%s par executant, au repos" % backlog.LOT_TAKE_NOTE),
                  _ev("intake", T0 + 7300, "returned to the queue by executant")]
        self.assertEqual(slo.assigned_ts(_item(assignee=""), events), T0 + 7200)

    def test_lot_jamais_assigne_reste_sans_attribution(self):
        # une note de création seule, ou une mise en file refusée, ne prouve
        # aucun porteur : l'étape reste ouverte et l'alerte part.
        for note in ("created; estimate: 20 min",
                     "not assigned to a (before: nobody, now: nobody)"):
            with self.subTest(note=note):
                events = [_ev("intake", T0, note)]
                self.assertIsNone(slo.assigned_ts(_item(assignee=""), events))
                rows = slo.lot_steps(_item(assignee=""), _jalons(_item(), events),
                                     events, now=T0 + 3 * 3600)
                signal = [r for r in rows if r["step"] == "signal_assigned"][0]
                self.assertEqual(signal["level"], 3)


class ExecuteurExterneTest(unittest.TestCase):
    """#4704 : un lot réservé à un exécutant externe (bureau, owner) n'a jamais
    d'`assignee`. La réservation est sa première attribution : `signal_assigned`
    conclut à la déclaration (ou à la création si le lot naît réservé), et au
    plus tard au passage en build / au début mesuré par l'exécutant. Sans quoi
    l'alerte « signalé → assigné » court indéfiniment et escalade même en build
    (#4696)."""

    def test_lot_cree_reserve_au_bureau_conclut_signal_assigned(self):
        # Témoin demandé par le lot : né réservé au bureau, en build, P1.
        item = _item(state="build", external_executor="bureau", priority=1)
        events = [_ev("intake", T0,
                      "created; estimate: 1 h; reserved to external executor bureau"),
                  _ev("build", T0 + 600, "garde tenue par le bureau")]
        self.assertEqual(slo.assigned_ts(item, events), T0)
        rows = slo.lot_steps(item, _jalons(item, events), events,
                             now=T0 + 30 * 60)
        signal = [r for r in rows if r["step"] == "signal_assigned"][0]
        self.assertEqual((signal["level"], signal["done_ts"]), (0, T0))
        self.assertEqual(slo.breaches(rows), [])

    def test_reservation_declaree_apres_la_creation_date_l_executeur(self):
        # Créé sans exécutant, réservé plus tard : la déclaration date
        # l'attribution, pas la création.
        item = _item(state="intake", external_executor="bureau")
        events = [_ev("intake", T0, "created; estimate: 1 h"),
                  _ev("intake", T0 + 1200, "external executor: bureau")]
        self.assertEqual(slo.assigned_ts(item, events), T0 + 1200)

    def test_reservation_sans_trace_journalisee_est_bornee_par_le_build(self):
        # Réservation lue seulement sur le champ courant (aucune déclaration
        # datée au journal) : le passage en build borne la date au plus tard.
        item = _item(state="build", external_executor="bureau")
        events = [_ev("intake", T0, "created; estimate: 1 h"),
                  _ev("build", T0 + 900, "the external executor starts")]
        self.assertEqual(slo.assigned_ts(item, events), T0 + 900)

    def test_reservation_levee_garde_la_premiere_attribution(self):
        # #3424 : la réservation est la première attribution ; sa levée puis
        # une prise par un agent ne rouvrent pas l'étape.
        item = _item(state="intake", assignee="")
        events = [_ev("intake", T0, "created; estimate: 1 h"),
                  _ev("intake", T0 + 600, "external executor: bureau"),
                  _ev("intake", T0 + 1200,
                      "external executor cleared (was bureau)"),
                  _ev("intake", T0 + 1800, "assigned to a (before: nobody)")]
        self.assertEqual(slo.assigned_ts(item, events), T0 + 600)


if __name__ == "__main__":
    unittest.main()
