"""#3907 : le balayage des agents arrêtés (#753) ne lit plus `overview()`.

Le balayage tourne à chaque passe de chaque exécuteur, et une passe suit
chaque NOTIFY du mesh (courrier, bail, budget). Le 27/10, `overview()` y
coûtait 0,4 à 1,2 s, et la base commune montait à une charge de 44 sur
4 vCPU. Le filtre lit maintenant deux colonnes du registre
(`agents.stopped_with`). La liste complète n'est lue, par
`measurement_actor`, que si un agent arrêté tient encore un lot.
"""

from __future__ import annotations

from unittest import mock

from ameesh import registry, storage, work
from ameesh import runner as runner_mod

from .test_2877_stop_sweep_without_lock import _Base


class StoppedWithTest(_Base):
    def test_only_stopped_agents_with_a_listed_reason(self):
        self.agent("dev1")
        self.agent("dev2")
        self.agent("dev3")
        self.agent("dev4")
        self.stopped("dev3")
        self.stopped("dev1")
        self.stopped("dev2", reason="erreur")
        agents = storage.of(self.db).agents
        self.assertEqual(agents.stopped_with(("manuel",)), ["dev1", "dev3"])
        self.assertEqual(agents.stopped_with(("manuel", "erreur")),
                         ["dev1", "dev2", "dev3"])

    def test_no_reason_no_read(self):
        with mock.patch.object(self.db, "query",
                               side_effect=AssertionError("lecture sans raison")):
            self.assertEqual(storage.of(self.db).agents.stopped_with(()), [])


class SweepWithoutOverviewTest(_Base):
    def test_idle_pass_does_not_read_the_overview(self):
        """Le cas de presque toutes les passes : un arrêt délibéré sans lot,
        un arrêt automatique avec lot, et aucune lecture de `overview()`."""
        self.agent("dev1")
        self.agent("dev2")
        self.lot("A", assignee="dev2")
        self.stopped("dev1")
        self.stopped("dev2", reason="erreur")
        with mock.patch.object(registry, "overview",
                               side_effect=AssertionError("overview à chaque passe")):
            self.assertEqual(
                runner_mod.Runner.release_stopped_lots_once(self.sweep_runner()), [])

    def test_held_lot_is_still_returned(self):
        self.agent("dev1")
        lot = self.lot("A", assignee="dev1")
        self.stopped("dev1")
        done = runner_mod.Runner.release_stopped_lots_once(self.sweep_runner())
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_promoted_lot_still_goes_to_the_living_auditor(self):
        """La liste complète n'est lue que sur ce chemin rare, par
        `measurement_actor` : l'auditeur vivant reçoit le lot promu."""
        self.agent("auditeur")
        self.agent("dev1")
        lot = self.promoted("dev1")
        self.stopped("dev1")
        with mock.patch.object(work, "configured_canons", return_value=[]):
            done = runner_mod.Runner.release_stopped_lots_once(self.sweep_runner())
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "auditeur")
