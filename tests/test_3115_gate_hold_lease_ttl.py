# SPDX-License-Identifier: AGPL-3.0-only
"""#3115 : `Gate.hold` ne raccourcit jamais le bail de l'hôte.

Le runner renouvelle le bail pour `lease_ttl / 3` à chaque battement. Un geste
du pilote qui renouvelait à 300 s absolus faisait expirer le bail d'un hôte
réglé plus haut (`policy.lease_ttl > 300`) avant ce battement : une élection
parasite pouvait prendre le rôle. `Gate.hold` reprend la durée que le runner
applique (fiche Host, sinon configuration) et ne descend jamais sous l'échéance
déjà tenue.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import time
import types
import unittest
from unittest import mock

from ameesh import canon, pilot_gesture, registry

from .support import PgTestCase

HOST = "h1"
NAME = "p3115"
RUNNER = "runner-h1"
IDENTITY_KEYS = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                 "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
                 "AMEESH_ROLE", "AMEESH_ROLE_HOST", "AMEESH_LEASE_OWNER")


class GateHoldLeaseTtlTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, NAME, harness="deepseek", host=HOST,
                        cwd=self.tmp, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election', candidats = ARRAY[%s, %s] "
            " WHERE name = %s", (HOST, "h2", NAME))
        lease = registry.claim(self.db, NAME, RUNNER, 300, host=HOST)
        self.assertIsNotNone(lease)
        self.epoch = int(lease["lease_epoch"])

    # -- helpers ---------------------------------------------------------
    @contextlib.contextmanager
    def live(self, **extra: str):
        """Pose l'identité d'une session, sans hériter celle du lanceur."""
        with mock.patch.dict(os.environ):
            for key in IDENTITY_KEYS:
                os.environ.pop(key, None)
            os.environ.update({
                "AGENT_MAIL_NAME": NAME,
                "AMEESH_RUNNER_ID": RUNNER,
                "AMEESH_LEASE_EPOCH": str(self.epoch),
            })
            os.environ.update(extra)
            yield

    def remaining(self) -> float:
        """Secondes de bail restantes, selon l'horloge de la base."""
        row = self.db.query(
            "SELECT extract(epoch from lease_expires_at)::float8 AS ts "
            "FROM agent_registry WHERE name = %s", (NAME,))[0]
        return float(row["ts"]) - time.time()

    def host_canon(self, lease_ttl: float) -> types.SimpleNamespace:
        """Le strict nécessaire de `Canon.host` pour `host_gate.lease_ttl_for`."""
        policy = types.SimpleNamespace(lease_ttl=lease_ttl, volatile=False)
        fiche = types.SimpleNamespace(policy=policy)
        return types.SimpleNamespace(
            host=lambda name: fiche if name == HOST else None)

    # -- the configured TTL is taken back ---------------------------------
    def test_hold_reprend_le_bail_de_la_configuration(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=900.0)
        with self.live():
            gate = pilot_gesture.gate(cfg, self.db, "assign", "#900")
            self.assertIsNotNone(gate)
            self.assertEqual(gate.lease_ttl, 900.0)
            gate.hold(self.db)
        self.assertGreater(self.remaining(), 870.0)
        self.assertLess(self.remaining(), 930.0)

    def test_hold_reprend_la_fiche_hote_au_dessus_de_la_configuration(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=300.0)
        with mock.patch.object(canon, "load_configured",
                               return_value=[self.host_canon(1200.0)]):
            with self.live():
                gate = pilot_gesture.gate(cfg, self.db, "merge", "#1200")
                self.assertIsNotNone(gate)
                self.assertEqual(gate.lease_ttl, 1200.0)
                gate.hold(self.db)
        self.assertGreater(self.remaining(), 1170.0)
        self.assertLess(self.remaining(), 1230.0)

    def test_un_canon_illisible_laisse_la_configuration(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=600.0)
        with mock.patch.object(canon, "load_configured",
                               side_effect=RuntimeError("canon illisible")):
            with self.live():
                gate = pilot_gesture.gate(cfg, self.db, "packet", "#600")
                self.assertIsNotNone(gate)
                self.assertEqual(gate.lease_ttl, 600.0)
                gate.hold(self.db)
        self.assertGreater(self.remaining(), 570.0)

    # -- never shorten an expiry already held ------------------------------
    def test_hold_ne_raccourcit_pas_le_bail_du_runner(self):
        # Le runner vient de renouveler à 1200 s ; ce processus ne sait pas
        # encore lire ce réglage : le geste doit conserver l'échéance tenue,
        # pas la ramener au défaut de 300 s.
        self.assertIsNotNone(registry.renew(self.db, NAME, RUNNER, self.epoch, 1200))
        cfg = dataclasses.replace(self.cfg, lease_ttl=300.0)
        with self.live():
            gate = pilot_gesture.gate(cfg, self.db, "assign", "#1200")
            self.assertIsNotNone(gate)
            gate.hold(self.db, ttl_s=300.0)
        self.assertGreater(self.remaining(), 1170.0)
        self.assertLess(self.remaining(), 1230.0)

    def test_hold_sous_le_defaut_reste_au_defaut(self):
        # Sans réglage d'hôte, le geste garde le comportement d'avant : 300 s.
        cfg = dataclasses.replace(self.cfg, lease_ttl=300.0)
        with self.live():
            gate = pilot_gesture.gate(cfg, self.db, "decision", "#300")
            self.assertIsNotNone(gate)
            self.assertEqual(gate.lease_ttl, pilot_gesture.DEFAULT_TTL_S)
            gate.hold(self.db)
        self.assertGreater(self.remaining(), 270.0)
        self.assertLess(self.remaining(), 330.0)

    # -- the EP5 timer path carries the same TTL ---------------------------
    def test_role_timer_reprend_le_bail_de_l_hote(self):
        cfg = dataclasses.replace(self.cfg, lease_ttl=600.0)
        with self.live(AMEESH_ROLE=NAME):
            gate = pilot_gesture.gate(cfg, self.db, "merge", "#role 600")
            self.assertIsNotNone(gate)
            self.assertEqual(gate.role, NAME)
            self.assertEqual(gate.lease_ttl, 600.0)
            gate.hold(self.db)
        self.assertGreater(self.remaining(), 570.0)
        self.assertLess(self.remaining(), 630.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
