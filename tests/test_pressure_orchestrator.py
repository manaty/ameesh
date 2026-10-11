# SPDX-License-Identifier: AGPL-3.0-only
"""Pression de l'hôte : l'orchestrateur passe, la rotation attend, un épisode
= une ligne de journal au début et une à la fin (2026-10-11).

Constat du 2026-10-10 : charge jusqu'à 43 pour 12 CPU (seuil 2 × CPU) ; plus
aucun tour ne démarrait, pas même ceux de l'orchestrateur qui distribue le
travail ; la rotation de session était relancée toutes les ~7 s puis annulée
(62 fois), sans trace utile. Désormais : sous une pression NON critique (hors
batterie), l'orchestrateur — rôle `orchestrateur` au canon, ou
`AMEESH_ALERT_ORCHESTRATORS` — démarre ses tours ; une rotation due attend la
fin de la pression ; le journal dit le début et la fin de l'épisode (durée,
tours retardés), pas chaque tentative.
Postgres réel.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from ameesh import registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

CHARGE = {"key": "max_load", "label": "charge 1 min", "value": 43.0, "limit": 24.0,
          "critical": False}
NON_CRITIQUE = {"blocked": True, "critical": False, "breaches": [CHARGE], "limits": {}}
CRITIQUE = {"blocked": True, "critical": True,
            "breaches": [dict(CHARGE, value=50.0, critical=True)], "limits": {}}
BATTERIE = {"blocked": True, "critical": False, "limits": {},
            "breaches": [{"key": "min_battery_percent", "label": "batterie (sur batterie)",
                          "value": 20, "limit": 25, "critical": False, "power": "low"}]}
LIBRE = {"blocked": False, "critical": False, "breaches": [], "limits": {}}
MIB = 1024 ** 2


class OrchestrateursTest(unittest.TestCase):
    """Reconnaissance d'un orchestrateur par l'exécuteur (sans base)."""

    def _runner(self) -> Runner:
        runner = Runner.__new__(Runner)  # seules les tables d'orchestrateurs servent
        runner._orchestrators_declared = frozenset()
        runner._orchestrators_roles = frozenset()
        return runner

    def test_role_au_canon(self):
        def agent(titre, **data):
            return types.SimpleNamespace(title=titre, fiche=types.SimpleNamespace(data=data))
        canon = types.SimpleNamespace(agents=[agent("chef", roles=["orchestrateur"]),
                                              agent("lead", role="Orchestrator"),
                                              agent("ouvrier", roles=["reviewer"])])
        runner = self._runner()
        runner.refresh_orchestrators(canon)
        self.assertTrue(runner.is_orchestrator("chef"))
        self.assertTrue(runner.is_orchestrator("lead"))
        self.assertFalse(runner.is_orchestrator("ouvrier"))

    def test_declare_par_l_environnement(self):
        from ameesh import sous_utilisation as su
        self.assertEqual(su.declared_orchestrators({"AMEESH_ALERT_ORCHESTRATORS":
                                                    "agent:chef, deux"}), {"chef", "deux"})
        self.assertEqual(su.declared_orchestrators({}), set())


class PressionTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.executeur = Runner(self.cfg, self.db, once=True)
        self.executeur._orchestrators_declared = frozenset({"orch"})
        self.workers: list[AgentWorker] = []

    def tearDown(self) -> None:
        for worker in self.workers:
            worker.watchdog_stop.set()
        super().tearDown()

    def _worker(self, name: str, prompt: str | None = "travail") -> AgentWorker:
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd, prompt=prompt)
        lease = registry.claim(self.db, name, self.executeur.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(self.executeur, registry.get(self.db, name), lease)
        self.workers.append(worker)
        return worker

    def _sous(self, verdict: dict):
        return mock.patch.object(self.executeur, "host_pressure", return_value=dict(verdict))

    def test_seul_l_orchestrateur_passe_sous_pression_non_critique(self):
        orch, ouvrier = self._worker("orch"), self._worker("ouvrier")
        with self._sous(NON_CRITIQUE):
            self.assertIsNone(ouvrier.pick())
            spec = orch.pick()
        self.assertEqual(spec["kind"], "prompt")
        self.assertEqual(spec["prompt"], "travail")
        texte = registry.get(self.db, "ouvrier")["status_text"]
        self.assertIn("en attente : pression de l'hôte", texte)
        self.assertTrue(registry.get(self.db, "ouvrier")["pending_prompt"])

    def test_jamais_sous_pression_critique_ni_sur_batterie(self):
        orch = self._worker("orch")
        self.assertFalse(orch._held_by_pressure(LIBRE))
        self.assertFalse(orch._held_by_pressure(NON_CRITIQUE))
        self.assertTrue(orch._held_by_pressure(CRITIQUE))
        self.assertTrue(orch._held_by_pressure(BATTERIE))
        ouvrier = self._worker("ouvrier")
        self.assertTrue(ouvrier._held_by_pressure(NON_CRITIQUE))

    def test_la_rotation_attend_la_fin_de_la_pression(self):
        worker = self._worker("tourne", prompt=None)
        worker.agent["session_id"] = "sess-1"
        worker.rotation_forcee = "plafond de contexte : essai"
        with mock.patch.object(worker, "_rotate", return_value=True) as rotation:
            with self._sous(NON_CRITIQUE):
                for _ in range(5):
                    self.assertFalse(worker.maybe_rotate())
            rotation.assert_not_called()  # ni tentée ni annulée à chaque passage
            self.assertEqual(worker.rotation_forcee, "plafond de contexte : essai")
            with self._sous(LIBRE):
                self.assertTrue(worker.maybe_rotate())
        rotation.assert_called_once_with("plafond de contexte : essai")

    def _releve(self, mem_bytes: int) -> None:
        storage.of(self.db).hosts.record({
            "host": self.executeur.host, "mem_available_bytes": mem_bytes,
            "swap_used_bytes": 0, "load1": 0.0, "cpu_count": 8,
            "disk_free_bytes": 10 ** 12, "disk_path": "/", "turns_in_progress": 0})

    def test_une_ligne_au_debut_et_une_a_la_fin_de_l_episode(self):
        a, b = self._worker("a"), self._worker("b")
        lignes: list[str] = []
        with mock.patch("ameesh.runner.log_async", side_effect=lignes.append):
            # mémoire sous le plancher (1 Gio) sans être critique (> 512 Mio)
            self._releve(768 * MIB)
            self.assertTrue(self.executeur.host_pressure(now=1000.0)["blocked"])
            for _ in range(3):  # tentatives répétées : rien de plus au journal
                self.assertIsNone(a.pick())
                self.assertIsNone(b.pick())
                self.executeur.host_pressure(now=self.executeur._pressure_at + 11.0)
            self._releve(8 * 1024 * MIB)
            self.assertFalse(self.executeur.host_pressure(
                now=self.executeur._pressure_at + 11.0)["blocked"])
            self.executeur.host_pressure(now=self.executeur._pressure_at + 11.0)
        pression = [l for l in lignes if "pression de l'hôte" in l]
        self.assertEqual(len(pression), 2, lignes)
        self.assertIn("début — mémoire disponible", pression[0])
        self.assertIn("sauf ceux d'un orchestrateur", pression[0])
        self.assertRegex(pression[1], r"fin — de \d\d:\d\d:\d\d à \d\d:\d\d:\d\d \(\d+ s\)")
        self.assertIn("2 tour(s) retardé(s) (a, b)", pression[1])


if __name__ == "__main__":
    unittest.main()
