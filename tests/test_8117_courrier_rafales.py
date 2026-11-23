# SPDX-License-Identifier: AGPL-3.0-only
"""#8117 — regrouper les rafales de courrier sans interrompre un tour.

Constat (auditeur #231248, 22/11) : 176 tours sur 748 réveillés par du
courrier système sans travail ; une rafale de 12 réponses de décision à 09:52Z
a interrompu 13 tours de l'orchestrateur (3 SIGKILL) ; 9 états du bureau sur
un même lot, espacés de 1,5 à 4 min, ont réveillé le pilote à chaque fois
(fenêtre fixe de 90 s).

Désormais : la fenêtre de regroupement s'allonge par expéditeur et par lot tant
que la rafale continue (plafond `BURST_CAP_S`) ; une réponse de décision est
regroupée et n'interrompt jamais un tour ; une alerte dont le geste revient à
un opérateur (`mail_undeliverable`) part en `info`, sauf critique.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import types
import unittest
from unittest import mock

from ameesh import alert_briefs, mail, registry
from ameesh import runner as runner_mod
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l125_courrier_regroupe import _Base

T0 = 1_800_000_000.0


def _reponse(ident: int, lot: int, created: float, sender: str = "human:smichea") -> dict:
    return {"id": ident, "sender": sender, "kind": "reply", "work_item_id": str(lot),
            "created_ts": created, "body": "réponse",
            "payload": {"human": True, "urgent": True,
                        "decision_answer": {"decision": ident, "lot": lot}}}


def _etat(ident: int, created: float, *, lot: int = 6002, sender: str = "bureau",
          payload: dict | None = None) -> dict:
    return {"id": ident, "sender": sender, "kind": "notify", "work_item_id": str(lot),
            "created_ts": created, "body": "état", "payload": payload or {}}


class _Horloge:
    """Horloge virtuelle partagée par `time.time` et `time.monotonic`."""

    def __init__(self) -> None:
        self.t = 0.0

    def module(self):
        import time as vrai
        return types.SimpleNamespace(time=lambda: T0 + self.t,
                                     monotonic=lambda: self.t,
                                     strftime=vrai.strftime, localtime=vrai.localtime,
                                     gmtime=vrai.gmtime, sleep=lambda _s: None)


def _ouvrier(batch: float = 90.0) -> AgentWorker:
    worker = AgentWorker.__new__(AgentWorker)
    threading.Thread.__init__(worker, name="pilote")
    worker.agent = {}
    worker.cfg = types.SimpleNamespace(human_names=(), mail_batch=batch)
    worker.runner = types.SimpleNamespace(mail_batch=batch)
    worker._state_read = lambda _key: None
    return worker


def _simuler(arrivees: list[tuple[float, dict]], *, duree_tour: float = 60.0,
             fin: float = 900.0, pas: float = 5.0,
             politique: dict | None = None) -> list[float]:
    """Les instants des réveils de courrier, comme `_pick` les ouvrirait
    (`_mail_due` ou `_mail_overdue` ; sous une cadence `reveil` déclarée,
    `_reveil_due`, comme `_mail_spec`) ; le courrier arrivé pendant un tour
    attend sa fin (pire cas : le hook n'en remet rien)."""
    horloge = _Horloge()
    worker = _ouvrier()
    if politique is not None:
        worker.agent = {"reveil": politique}
    reveils: list[float] = []
    non_lus: list[dict] = []
    occupe_jusqu = -1.0
    file = sorted(arrivees, key=lambda a: a[0])
    with mock.patch.object(runner_mod, "time", horloge.module()):
        while horloge.t <= fin:
            while file and file[0][0] <= horloge.t:
                non_lus.append(file.pop(0)[1])
            if non_lus and horloge.t >= occupe_jusqu:
                if politique is not None:
                    du = worker._reveil_due(politique, non_lus)
                else:
                    du = worker._mail_due(non_lus) or worker._mail_overdue(non_lus)
                if du:
                    reveils.append(horloge.t)
                    non_lus = []
                    occupe_jusqu = horloge.t + duree_tour
                    worker._turn_end_mono = occupe_jusqu
            horloge.t += pas
    return reveils


class FenetreAdaptativeTest(unittest.TestCase):
    def test_neuf_etats_en_quinze_minutes_au_plus_deux_reveils(self):
        # le témoin : 9 états du bureau sur un même lot en 15 min, espacés de
        # 1,5 à 4 min (la fenêtre fixe de 90 s en faisait 9 réveils)
        for ecarts in ([112.5] * 8, [90.0] * 8, [90, 240, 90, 120, 90, 90, 90, 90],
                       [240, 90, 90, 90, 90, 90, 90, 120]):
            instants = [0.0]
            for ecart in ecarts:
                instants.append(instants[-1] + ecart)
            self.assertLessEqual(instants[-1], 900.0)
            arrivees = [(t, _etat(i + 1, T0 + t)) for i, t in enumerate(instants)]
            reveils = [t for t in _simuler(arrivees, fin=1800) if t <= 900]
            self.assertLessEqual(len(reveils), 2, (ecarts, reveils))

    def test_un_message_seul_garde_la_fenetre_de_base(self):
        self.assertEqual(_simuler([(0.0, _etat(1, T0))], fin=300), [90.0])

    def test_plafond_de_la_rafale(self):
        # un état toutes les 60 s pendant 20 min : jamais plus de 10 min
        # d'attente pour le plus ancien non lu
        arrivees = [(i * 60.0, _etat(i + 1, T0 + i * 60.0)) for i in range(21)]
        reveils = _simuler(arrivees, fin=1300)
        self.assertTrue(reveils)
        precedent = 0.0
        for t in reveils:
            self.assertLessEqual(t - precedent, AgentWorker.BURST_CAP_S + 60 + 5,
                                 reveils)
            precedent = t

    def test_les_rafales_sont_par_expediteur_et_par_lot(self):
        # deux expéditeurs sur deux lots, un message chacun : fenêtre de base
        arrivees = [(0.0, _etat(1, T0, lot=1, sender="a")),
                    (30.0, _etat(2, T0 + 30, lot=2, sender="b"))]
        self.assertEqual(_simuler(arrivees, fin=300), [90.0])

    def test_urgent_reveille_tout_de_suite(self):
        arrivees = [(i * 60.0, _etat(i + 1, T0 + i * 60.0)) for i in range(5)]
        arrivees.append((300.0, _etat(99, T0 + 300, payload={"urgent": True})))
        reveils = _simuler(arrivees, fin=400)
        self.assertIn(300.0, reveils)

    def test_douze_reponses_de_decision_un_seul_tour(self):
        arrivees = [(i * 2.0, _reponse(i + 1, 7000 + i, T0 + i * 2.0)) for i in range(12)]
        self.assertEqual(_simuler(arrivees, fin=900), [90.0])

    def test_une_reponse_de_decision_n_interrompt_pas(self):
        worker = _ouvrier()
        worker.cfg.interrupt_senders = ("human:smichea", "orch")
        self.assertFalse(worker.interrupt_allowed(_reponse(1, 7000, T0, sender="orch")))
        self.assertTrue(mail.is_decision_answer(_reponse(1, 7000, T0)))
        self.assertFalse(mail.is_decision_answer(_etat(1, T0)))


class CheminsCompletsTest(unittest.TestCase):
    """B de mty-amh-impl-11 (gel 43f204d1) : le retard #7976 et la cadence
    `reveil` ne contournent plus le regroupement."""

    def _a(self, t: float):
        horloge = _Horloge()
        horloge.t = t
        return mock.patch.object(runner_mod, "time", horloge.module())

    def test_le_premier_d_une_rafale_n_est_pas_en_retard_a_300_s(self):
        worker = _ouvrier()
        rafale = [_etat(1, T0), _etat(2, T0 + 200)]
        with self._a(AgentWorker.MAIL_OVERDUE_S + 10):
            self.assertFalse(worker._mail_overdue(rafale))
        with self._a(AgentWorker.BURST_CAP_S + 1):
            self.assertTrue(worker._mail_overdue(rafale))
        # un message seul garde le retard de 300 s
        with self._a(AgentWorker.MAIL_OVERDUE_S + 10):
            self.assertTrue(_ouvrier()._mail_overdue([_etat(1, T0)]))

    def test_rafale_par_le_chemin_complet_du_retard(self):
        # le pilote est occupé quand la rafale commence : à sa sortie, les
        # deux premiers ont plus de 300 s mais la rafale continue
        arrivees = [(i * 100.0, _etat(i + 1, T0 + i * 100.0)) for i in range(6)]
        horloge = _Horloge()
        worker = _ouvrier()
        worker._turn_end_mono = 0.0
        with mock.patch.object(runner_mod, "time", horloge.module()):
            horloge.t = 320.0
            vus = [m for t, m in arrivees if t <= horloge.t]
            self.assertFalse(worker._mail_overdue(vus))

    def test_reponse_de_decision_sous_cadence_regroupee(self):
        worker = _ouvrier()
        reponses = [_reponse(i + 1, 7000 + i, T0 + i * 2.0) for i in range(12)]
        with self._a(30.0):
            self.assertFalse(worker._reveil_due({"immediat": ["humain", "urgent",
                                                              "decision"],
                                                 "fenetre_s": 1800}, reponses))
        with self._a(120.0):
            self.assertTrue(worker._reveil_due({"immediat": ["humain", "urgent",
                                                             "decision"],
                                                "fenetre_s": 1800}, reponses))
        # un humain ordinaire reste immédiat sous la cadence
        humain = dict(_etat(50, T0, sender="human:smichea"), payload={"human": True})
        with self._a(1.0):
            self.assertTrue(_ouvrier()._reveil_due({"immediat": ["humain"],
                                                    "fenetre_s": 1800}, [humain]))

    def test_cadence_minimale_regroupe_aussi(self):
        # « humain » reste toujours immédiat (décision owner du 17/11) : la
        # réponse n'est pas ouverte sur-le-champ, mais à la fin de la fenêtre
        # adaptative de base, sans attendre les 30 min déclarées
        politique = {"immediat": ["humain"], "fenetre_s": 1800}
        with self._a(30.0):
            self.assertFalse(_ouvrier()._reveil_due(politique, [_reponse(1, 7000, T0)]))
        with self._a(91.0):
            self.assertTrue(_ouvrier()._reveil_due(politique, [_reponse(1, 7000, T0)]))


class CadenceDeclareeTest(unittest.TestCase):
    """B de mty-amh-impl-11 (gel e753f725) : sous une cadence `reveil`, la
    fenêtre déclarée est la base de la fenêtre adaptative ; les natures
    immédiates restent immédiates."""

    def test_rafale_sous_cadence_courte_adaptative(self):
        politique = {"immediat": ["humain", "urgent"], "fenetre_s": 90}
        instants = [0.0]
        for ecart in [112.5] * 8:
            instants.append(instants[-1] + ecart)
        arrivees = [(t, _etat(i + 1, T0 + t)) for i, t in enumerate(instants)]
        reveils = [t for t in _simuler(arrivees, fin=1800, politique=politique)
                   if t <= 900]
        self.assertLessEqual(len(reveils), 2, reveils)

    def test_message_seul_attend_la_fenetre_declaree(self):
        politique = {"immediat": ["humain"], "fenetre_s": 1800}
        self.assertEqual(_simuler([(0.0, _etat(1, T0))], fin=2000, politique=politique),
                         [1800.0])

    def test_cadence_par_defaut_inchangee(self):
        # 100 courriers ordinaires en une heure, 3 expéditeurs sur 3 lots :
        # au plus deux tours (témoin #7632), la rafale ne recule rien au-delà
        politique = {"immediat": ["humain", "urgent"], "fenetre_s": 1800}
        arrivees = [(i * 36.0, _etat(i + 1, T0 + i * 36.0, lot=1 + i % 3,
                                     sender="pair%d" % (i % 3))) for i in range(100)]
        reveils = _simuler(arrivees, fin=3600, pas=10.0, politique=politique)
        self.assertLessEqual(len(reveils), 2, reveils)
        self.assertEqual(reveils[0], 1800.0)  # comme la fenêtre fixe

    def test_natures_immediates_preservees(self):
        politique = {"immediat": ["humain", "urgent"], "fenetre_s": 90}
        arrivees = [(i * 60.0, _etat(i + 1, T0 + i * 60.0)) for i in range(5)]
        arrivees.append((300.0, _etat(99, T0 + 300, payload={"urgent": True})))
        self.assertIn(300.0, _simuler(arrivees, fin=400, politique=politique))


class ReponseDeDecisionTest(_Base):
    def _reponse(self, to: str, lot: int, sender: str = "human:smichea") -> int:
        return mail.send(self.db, sender, to, "réponse : a", kind="reply",
                         work_item_id=str(lot),
                         payload={"human": True, "urgent": True,
                                  "decision_answer": {"decision": lot, "lot": lot}})

    def test_regroupee_avant_le_tour(self):
        _runner, worker = self._worker("orch", batch=60.0)
        ids = [self._reponse("orch", 7000 + i) for i in range(12)]
        self.assertIsNone(worker.pick())  # ni rang humain, ni urgent
        self._vieillir("orch", 61)
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(sorted(spec["ids"]), sorted(ids))

    def test_un_humain_ordinaire_reveille_toujours_tout_de_suite(self):
        _runner, worker = self._worker("orch2", batch=60.0)
        mail.send(self.db, "human:smichea", "orch2", "regarde le lot 12",
                  payload={"human": True})
        self.assertIsNotNone(worker.pick())


class PasDePreemptionTest(PgTestCase):
    def _worker(self, name: str = "pre", *, senders=("orch",)):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        runner.workers[name] = worker
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def test_notify_d_une_reponse_de_decision_ne_preempte_pas(self):
        runner, worker = self._worker(senders=("orch",))
        mail.send(self.db, "orch", "pre", "réponse : a", kind="reply",
                  payload={"urgent": True, "human": True,
                           "decision_answer": {"decision": 1, "lot": None}})
        row = mail.unread(self.db, "pre")[-1]
        generation = worker.preempt_gen
        runner.dispatch({"channel": "agent_mail",
                         "payload": json.dumps({"to": "pre", "id": int(row["id"])})})
        self.assertFalse(worker.preempting.is_set())
        self.assertEqual(worker.preempt_gen, generation)
        self.assertTrue(worker.wake.is_set())


class AlerteOperateurTest(unittest.TestCase):
    def test_mail_undeliverable_en_info(self):
        modes = alert_briefs.quiet_modes(
            {"type": "mail_undeliverable", "agent": "mort"}, {"pilote": "texte"}, {})
        self.assertEqual(modes["pilote"]["payload"], {"info": True, "urgent": False})
        self.assertEqual(mail.passive_reason({"payload": {
            "alert": "mail_undeliverable", "urgent": True, **modes["pilote"]["payload"]}}),
            "info")

    def test_critique_ou_urgente_reveille(self):
        for alerte in ({"type": "mail_undeliverable", "critical": True},
                       {"type": "mail_undeliverable", "urgent": True}):
            self.assertEqual(alert_briefs.quiet_modes(alerte, {"pilote": "t"}, {}), {})


if __name__ == "__main__":
    unittest.main()
