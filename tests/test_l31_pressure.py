# SPDX-License-Identifier: AGPL-3.0-only
"""Seuils de ressources au canon, contre-pression avant chaque tour et alertes
de pression (lot L31, décision 0028)."""
from __future__ import annotations

import os
import unittest

from ameesh import canon, exploitation, registry, resources, storage

from . import test_canon
from .test_canon import write

HOST_FICHE = """---
type: Host
title: atelier
responsible: human:bruno
policy:
  harnesses: [claude, codex, deepseek]
  providers: [anthropic, openai, deepseek]
  credential_modes: [api-key, subscription]
  max_agents: 4
  resources: {%s}
---

# atelier
"""


def set_host_resources(root: str, **resources_decl) -> None:
    declared = ", ".join("%s: %s" % (k, v) for k, v in resources_decl.items())
    write(root, "hotes/atelier.md", HOST_FICHE % declared)


# ==========================================================================
# profil du canon (sans base)
# ==========================================================================

class CanonPolicyTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self) -> canon.Canon:
        return canon.load(self.root, untrusted=True)

    def codes(self, loaded, severity=None):
        findings = canon.validate(loaded)
        return [f.code for f in findings if severity is None or f.severity == severity]

    def test_seuils_valides(self):
        set_host_resources(self.root, min_mem_available="2GiB", max_swap_used="512MiB",
                           max_load=8, min_disk_free="10GB")
        loaded = self.load()
        atelier = loaded.host("atelier")
        self.assertEqual(atelier.policy.resources, {
            "min_mem_available": 2 * 1024 ** 3,
            "max_swap_used": 512 * 1024 ** 2,
            "max_load": 8.0,
            "min_disk_free": 10 * 1000 ** 3,
        })
        self.assertEqual(self.codes(loaded, canon.ERROR), [])

    def test_seuil_illisible_est_une_erreur(self):
        set_host_resources(self.root, min_mem_available="beaucoup", max_load=-1)
        loaded = self.load()
        self.assertIn("host-policy-invalid", self.codes(loaded, canon.ERROR))
        # la politique garde les clés valides, ignore les illisibles
        self.assertIsNone(loaded.host("atelier").policy.resources)

    def test_cle_inconnue_avertissement(self):
        set_host_resources(self.root, min_mem_available="1GiB", mystere=3)
        loaded = self.load()
        self.assertIn("host-resources-unknown", self.codes(loaded, canon.WARNING))
        self.assertEqual(loaded.host("atelier").policy.resources, {"min_mem_available": 1024 ** 3})

    def test_priorite_lue_et_invalide_signalee(self):
        chemin = os.path.join(self.root, "agents/orchestre.md")
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        write(self.root, "agents/orchestre.md",
              texte.replace("budget_usd_per_day: 30", "budget_usd_per_day: 30\npriority: 7"))
        self.assertEqual(self.load().agent("orchestre").priority, 7)
        write(self.root, "agents/orchestre.md",
              texte.replace("budget_usd_per_day: 30", "budget_usd_per_day: 30\npriority: -2"))
        loaded = self.load()
        self.assertEqual(loaded.agent("orchestre").priority, 0)
        self.assertIn("agent-priority-invalid", self.codes(loaded, canon.WARNING))


# ==========================================================================
# contre-pression et alertes (base réelle)
# ==========================================================================

class PressureDbTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("DELETE FROM turn_resources")

    def test_sync_ecrit_la_priorite(self):
        chemin = os.path.join(self.root, "agents/orchestre.md")
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        write(self.root, "agents/orchestre.md",
              texte.replace("budget_usd_per_day: 30", "budget_usd_per_day: 30\npriority: 4"))
        self.sync()
        self.assertEqual(int(registry.get(self.db, "orchestre")["priority"]), 4)
        # inchangé au second passage : la priorité est une colonne déclarative
        self.assertEqual({a.agent: a.action for a in self.sync().actions},
                         {"orchestre": "inchangé", "relecteur": "inchangé"})

    def test_pression_critique_bloque_le_tour(self):
        # un plancher de mémoire inatteignable : toute mesure le franchit
        set_host_resources(self.root, min_mem_available="100TiB")
        self.sync()
        registry.set_pending_prompt(self.db, "orchestre", "consigne qui doit attendre")
        proc = self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="30"))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])  # aucun harnais lancé
        row = registry.get(self.db, "orchestre")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("pression", row["status_text"])
        # la consigne n'a pas été consommée
        self.assertTrue(row.get("pending_prompt"))

    def test_attente_de_pression_visible(self):
        # L31b : un agent prioritaire n'est pas mis en pause, mais son attente
        # se lit dans le texte de statut — sans quoi il restait `queued`, muet.
        chemin = os.path.join(self.root, "agents/orchestre.md")
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        write(self.root, "agents/orchestre.md",
              texte.replace("budget_usd_per_day: 30", "budget_usd_per_day: 30\npriority: 4"))
        set_host_resources(self.root, min_mem_available="100TiB")
        self.sync()
        registry.set_pending_prompt(self.db, "orchestre", "consigne qui doit attendre")
        proc = self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="30"))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])
        row = registry.get(self.db, "orchestre")
        self.assertEqual(row["status"], "queued")  # pas de pause : priorité 4
        self.assertIn("en attente : pression de l'hôte atelier", row["status_text"])
        self.assertIn("mémoire disponible", row["status_text"])
        self.assertIn("seuil 100.0 TiB", row["status_text"])
        self.assertTrue(row.get("pending_prompt"))

    def test_pas_d_attente_affichee_sans_travail(self):
        chemin = os.path.join(self.root, "agents/orchestre.md")
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        write(self.root, "agents/orchestre.md",
              texte.replace("budget_usd_per_day: 30", "budget_usd_per_day: 30\npriority: 4"))
        set_host_resources(self.root, min_mem_available="100TiB")
        self.sync()
        self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="30"))
        row = registry.get(self.db, "orchestre")
        self.assertNotIn("pression", row.get("status_text") or "")

    def _bail(self, nom):
        self.sync()
        row = registry.claim(self.db, nom, "runner-test", 60.0)
        self.assertIsNotNone(row)
        return int(row["lease_epoch"])

    def test_levee_de_l_attente_rend_le_texte_d_avant(self):
        epoch = self._bail("orchestre")
        registry.set_status(self.db, "orchestre", "queued", "reprise : même session")
        attente = "en attente : pression de l'hôte atelier — swap"
        self.assertTrue(registry.hold_note(self.db, "orchestre", "runner-test", epoch, attente))
        row = registry.get(self.db, "orchestre")
        self.assertEqual((row["status"], row["status_text"]), ("queued", attente))
        # un autre bail ne lève rien
        self.assertFalse(registry.release_hold(self.db, "orchestre", "autre", epoch, attente))
        self.assertTrue(registry.release_hold(self.db, "orchestre", "runner-test", epoch,
                                              attente, "reprise : même session"))
        row = registry.get(self.db, "orchestre")
        self.assertEqual((row["status"], row["status_text"]),
                         ("queued", "reprise : même session"))

    def test_levee_de_la_pause_critique(self):
        epoch = self._bail("orchestre")
        self.assertTrue(registry.pause(self.db, "orchestre", "runner-test", epoch,
                                       "pause : pression critique"))
        # texte remplacé par quelqu'un d'autre : la levée ne l'écrase pas
        self.assertFalse(registry.release_hold(self.db, "orchestre", "runner-test", epoch,
                                               "pause : autre chose"))
        registry.set_pending_prompt(self.db, "orchestre", "à faire")
        self.assertTrue(registry.release_hold(self.db, "orchestre", "runner-test", epoch,
                                              "pause : pression critique"))
        row = registry.get(self.db, "orchestre")
        self.assertEqual((row["status"], row["status_text"]), ("queued", ""))

    def test_attente_refusee_en_tour(self):
        epoch = self._bail("orchestre")
        registry.set_status(self.db, "orchestre", "running", "tour")
        self.assertFalse(registry.hold_note(self.db, "orchestre", "runner-test", epoch, "x"))

    def test_alerte_host_pressure(self):
        storage.of(self.db).hosts.record({
            "host": "atelier", "mem_available_bytes": 0, "swap_used_bytes": 0,
            "load1": 0.0, "cpu_count": 8, "disk_free_bytes": 10 ** 12,
            "disk_path": "/tmp", "turns_in_progress": 1})
        alerte = [a for a in exploitation.alerts(self.cfg, self.db)
                  if a["type"] == "host_pressure"]
        self.assertEqual(len(alerte), 1)
        self.assertEqual(alerte[0]["host"], "atelier")
        self.assertTrue(alerte[0]["critical"])
        self.assertIn("min_mem_available", alerte[0]["breaches"])

    def test_aucune_alerte_sans_releve(self):
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "host_pressure"], [])


class DescribeTest(unittest.TestCase):
    def test_franchissements_lisibles(self):
        texte = resources.describe([
            {"key": "max_swap_used", "label": "swap utilisé", "value": int(17.5 * 1024 ** 3),
             "limit": 16 * 1024 ** 3, "critical": False},
            {"key": "max_load", "label": "charge 1 min", "value": 30.0, "limit": 24.0,
             "critical": False}])
        self.assertEqual(texte, "swap utilisé 17.5 GiB (seuil 16.0 GiB) ; "
                                "charge 1 min 30.00 (seuil 24.00)")
        self.assertEqual(resources.describe([]), "")
