# SPDX-License-Identifier: AGPL-3.0-only
"""#1070 : une pause budget ne se lit plus comme un `blocked` quelconque.

Le bilan du bureau comptait « aucun agent erreur/pause » alors que dix agents
étaient en pause budget : dans le tableau de `ameesh list`, la colonne STATUS
est tronquée à onze caractères et « blocked/budget : pause (… » devenait
« blocked/bud », le mot « pause » disparaissant. La vue montre désormais
« paused/… » pour une pause budget, garde « blocked/… » pour un `blocked`
d'une autre cause (hôte non prêt, authentification), et `list --json` porte
`paused_on_budget` (schéma `ameesh-agent/1`) pour les lecteurs machine.

Témoin : bloqué + budget contre bloqué hors budget — aucun faux positif.
"""
from __future__ import annotations

import json

from ameesh import progress, registry

from .support import PgTestCase

PAUSE = ("budget : pause (account secondaire, codex-300min 21 %) until 20/10 "
         "06:57; all codex accounts at threshold")
AUTRE = "auth_blocked: codex/compte — log in again"


class PauseBudgetVisibleTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("en-pause", "codex", cwd=self.tmp)
        self.register("bloque", "codex", cwd=self.tmp)
        registry.set_status(self.db, "en-pause", "blocked", status_text=PAUSE)
        registry.set_status(self.db, "bloque", "blocked", status_text=AUTRE)

    def _lignes(self, sortie: str) -> dict:
        return {nom: [l for l in sortie.splitlines() if l.startswith(nom)][0]
                for nom in ("en-pause", "bloque")}

    def test_tableau_montre_la_pause_budget(self):
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = self._lignes(proc.stdout)
        # bloqué + budget : le mot « pause » survit à la troncature
        self.assertIn("paused", lignes["en-pause"])
        self.assertNotIn("blocked", lignes["en-pause"])
        # bloqué hors budget : pas de faux positif
        self.assertIn("blocked", lignes["bloque"])
        self.assertNotIn("paused", lignes["bloque"])

    def test_json_distingue_la_pause_budget(self):
        proc = self.mesh("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = {r["name"]: r for r in json.loads(proc.stdout)}
        self.assertTrue(rows["en-pause"]["paused_on_budget"])
        self.assertFalse(rows["bloque"]["paused_on_budget"])
        # `state` seul ne les sépare pas : un `blocked` hors budget est
        # « paused » lui aussi (L13) — d'où le champ dédié.
        self.assertEqual(rows["en-pause"]["state"], "paused")
        self.assertEqual(rows["bloque"]["state"], "paused")

    def test_predicat_strict(self):
        self.assertTrue(progress.paused_on_budget(
            {"status": "blocked", "status_text": PAUSE}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "blocked", "status_text": AUTRE}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "blocked", "status_text": None}))
        # le préfixe seul ne suffit pas : c'est la pause ANNONCÉE (blocked)
        self.assertFalse(progress.paused_on_budget(
            {"status": "running", "status_text": PAUSE}))
        self.assertFalse(progress.paused_on_budget(
            {"status": "stopped", "status_text": PAUSE}))
