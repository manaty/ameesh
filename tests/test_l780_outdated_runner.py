# SPDX-License-Identifier: AGPL-3.0-only
"""L780 : un exécuteur non redémarré après une livraison ne juge pas le canon.

Incident du 13/10 (1.10.0 sur ameesh-agents-1) : un exécuteur resté sur
l'ancien code a relu le canon avec son ancien validateur de descripteurs. Le
`codex.json` neuf (`ameesh.interactive_permission.modes`, sans `flag`) a
produit `harness-permission-mode-invalid`, un constat SANS SUJET ; `canon sync`
a écrit `canon_state = invalid` et vidé le `responsible` des agents de l'hôte,
si bien que `work assign` les a refusés (« has no human owner »).

Trois garde-fous, testés ici :

1. `harnesses.stale_reason` : l'empreinte du paquet (version installée et
   descripteurs livrés) est prise à l'import ; un processus dont le disque a
   changé depuis se sait périmé (le dossier de l'hôte, lui, est hors empreinte).
2. `canon_sync.sync` refuse alors d'écrire (`CanonStale`) et
   `Runner.canon_sync_once` saute la passe : `canon_state` et les responsables
   restent au dernier passage valide.
3. Même avec un code à jour, un canon globalement invalide ne vide plus le
   `responsible` déjà résolu (`keep last_good`) : la réclamation est déjà
   fermée par `canon_state`, l'attribution reste ouverte (L46), et la
   réparation du canon rend les agents réclamables sans réécriture manuelle.
   Le vidage pour une erreur PROPRE à un agent (R14) reste couvert par
   `test_canon.SyncTest.test_erreur_bloquante_vide_le_responsable`.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon, canon_sync, harnesses, registry
from ameesh import runner as runner_mod

from .test_canon import _CanonDbCase, write


class StaleReasonTest(unittest.TestCase):
    """L'empreinte du paquet : version installée et descripteurs livrés."""

    def test_processus_a_jour_sans_raison(self):
        self.assertEqual(harnesses.stale_reason(), "")

    def test_version_installee_changee(self):
        with mock.patch.object(harnesses, "_BOOT_VERSION", "0.9.0-old"):
            reason = harnesses.stale_reason()
        self.assertIn("0.9.0-old", reason)
        self.assertIn("installed package changed", reason)

    def test_descripteur_livre_change(self):
        boot = dict(harnesses._BOOT_PACKAGE_DIGESTS)
        boot["codex.json"] = "0" * 64
        with mock.patch.object(harnesses, "_BOOT_PACKAGE_DIGESTS", boot):
            reason = harnesses.stale_reason()
        self.assertIn("codex.json", reason)
        self.assertIn("descriptors changed", reason)

    def test_dossier_de_l_hote_hors_empreinte(self):
        """Un épinglage local change sans redémarrer l'exécuteur : il ne rend
        pas le processus périmé (`adapters.SPECS` relit le dossier à chaque
        tour)."""
        directory = tempfile.mkdtemp(prefix="ameesh-l780-harnesses-")
        self.addCleanup(shutil.rmtree, directory, True)
        with open(os.path.join(directory, "codex.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": directory}):
            self.assertEqual(harnesses.stale_reason(), "")


class OutdatedRunnerTest(_CanonDbCase):
    """Le validateur périmé n'écrit rien : canon_state et responsables tiennent."""

    def snapshot(self) -> dict[str, str | None]:
        return {row["name"]: row["responsible"] for row in self.db.query(
            "SELECT name, responsible FROM agent_registry ORDER BY name")}

    @staticmethod
    def old_package_digests() -> dict[str, str]:
        """L'empreinte d'avant la livraison : les descripteurs d'aujourd'hui,
        sauf `codex.json`, celui que l'ancien validateur refusait."""
        boot = dict(harnesses._BOOT_PACKAGE_DIGESTS)
        boot["codex.json"] = "0" * 64
        return boot

    def test_validateur_perime_n_ecrit_rien(self):
        self.sync()
        before_state = canon_sync.state(self.db, "atelier")
        before_rows = self.snapshot()

        def old_validator(canon_obj):
            # ce que produisait l'ancien code devant le `codex.json` neuf :
            # un constat sans sujet, donc un canon invalide pour tout l'hôte
            return [canon.Finding("harness-permission-mode-invalid", canon.ERROR,
                                  "`ameesh.interactive_permission.flag`: list of "
                                  "strings containing `{mode}` expected",
                                  "harnesses/codex.json")]

        with mock.patch.object(harnesses, "_BOOT_PACKAGE_DIGESTS",
                               self.old_package_digests()), \
                mock.patch.object(canon, "validate", side_effect=old_validator):
            with self.assertRaises(canon_sync.CanonStale) as ctx:
                self.sync()
        self.assertIn("codex.json", str(ctx.exception))
        self.assertEqual(canon_sync.state(self.db, "atelier"), before_state)
        self.assertEqual(self.snapshot(), before_rows)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_executeur_perime_saute_la_passe(self):
        self.sync()
        before_state = canon_sync.state(self.db, "atelier")
        before_rows = self.snapshot()
        fake = types.SimpleNamespace(
            cfg=dataclasses.replace(self.cfg, canon=self.root), host="atelier", db=None)
        with mock.patch.object(harnesses, "_BOOT_VERSION", "0.9.0-old"):
            self.assertFalse(runner_mod.Runner.canon_sync_once(fake))
        self.assertIs(fake._canon_complete, False)   # porte d'hôte fermée
        self.assertEqual(canon_sync.state(self.db, "atelier"), before_state)
        self.assertEqual(self.snapshot(), before_rows)


class InvalidCanonOwnersTest(_CanonDbCase):
    """Un canon invalide garde le dernier responsable résolu (L780)."""

    def test_erreur_globale_garde_le_responsable(self):
        report = self.sync()
        self.assertEqual(report.status, canon_sync.CANON_OK)
        self.assertEqual(self.row("orchestre")["responsible"], "human:alice")
        self.assertEqual(self.row("relecteur")["responsible"], "human:alice")
        # un constat SANS SUJET (fiche illisible) : avant L780, chaque agent de
        # l'hôte perdait son responsable et `work assign` le refusait
        write(self.root, "agents/casse.md", "---\ntype: Agent\ntitle: [x\n---\n")
        report = self.sync()
        self.assertEqual(report.status, canon_sync.CANON_INVALID)
        self.assertIn("frontmatter-invalid", report.diagnostic)
        self.assertEqual(self.row("orchestre")["responsible"], "human:alice")
        self.assertEqual(self.row("relecteur")["responsible"], "human:alice")
        # la réclamation reste fermée (fail closed) ...
        self.assertEqual(registry.claimable(self.db, "atelier",
                                            require_responsible=False), [])
        # ... et la réparation du canon rouvre sans réécrire le responsable
        write(self.root, "agents/casse.md", None)
        self.assertEqual(self.sync().status, canon_sync.CANON_OK)
        self.assertEqual(
            sorted(r["name"] for r in registry.claimable(
                self.db, "atelier", require_responsible=True)),
            ["orchestre", "relecteur"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
