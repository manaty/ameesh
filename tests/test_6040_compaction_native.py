# SPDX-License-Identifier: AGPL-3.0-only
"""#6040 (manaty/ameesh#4637) : seuil de compaction native du harnais, par agent.

Les longs tours Codex relisent un contexte qui croît à chaque appel d'outil ;
Codex sait compacter en cours de tour (`model_auto_compact_token_limit`), mais
ameesh ne le réglait pas. `ameesh set <agent> compact_tokens=…` le passe au
lancement par l'option `ameesh.compact` du descripteur ; sans réglage, rien
n'est passé et le harnais garde son défaut.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import adapters, cost, exploitation, harnesses, registry

from .test_l105_plafonds_du_tour import _Base

CODEX = os.path.join(harnesses.package_dir(), "codex.json")
CLAUDE = os.path.join(harnesses.package_dir(), "claude.json")
DRAPEAU = "model_auto_compact_token_limit=120000"


def _adaptateur(path: str) -> adapters.HarnessAdapter:
    return adapters.HarnessAdapter(harnesses.load(path, source="test"), "/bin/true")


class DescripteurTest(unittest.TestCase):
    def test_codex_recoit_le_seuil(self):
        argv = _adaptateur(CODEX).command("consigne", compact=120000)
        self.assertEqual(argv[argv.index(DRAPEAU) - 1], "-c")
        # Option du lanceur : avant `exec`, comme l'effort.
        self.assertLess(argv.index(DRAPEAU), argv.index("exec"))

    def test_sans_reglage_rien_n_est_passe(self):
        argv = _adaptateur(CODEX).command("consigne")
        self.assertFalse(any("auto_compact" in a for a in argv))

    def test_harnais_sans_option_ignore_le_seuil(self):
        adapter = _adaptateur(CLAUDE)
        self.assertFalse(adapter.supports_compact())
        self.assertEqual(adapter.command("consigne", compact=120000),
                         adapter.command("consigne"))

    def test_livraison_par_fichier_refusee(self):
        with open(CODEX, encoding="utf-8") as fh:
            document = json.load(fh)
        self.assertEqual(harnesses.errors(harnesses.validate(document)), [])
        document["ameesh"]["compact"] = {"flag": ["-c", "x={compact}"],
                                         "file": {"template": "x"}}
        self.assertTrue(any("ameesh.compact" in f.message
                            for f in harnesses.errors(harnesses.validate(document))))

    def test_reglage_effectif(self):
        lire = exploitation.effective_compact_tokens
        self.assertIsNone(lire(None))
        self.assertIsNone(lire({"compact_tokens": None}))
        self.assertIsNone(lire({"compact_tokens": ""}))
        self.assertIsNone(lire({"compact_tokens": 0}))
        self.assertEqual(lire({"compact_tokens": "80000"}), 80000)


class ReglageTest(_Base):
    def test_ameesh_set_compact_tokens(self):
        self._worker("cdx-set")
        registry_row = registry.get(self.db, "cdx-set")
        self.assertIsNone(registry_row["compact_tokens"])
        # L'agent de `_worker` tourne sous Claude : aucun seuil déclaré.
        proc = self.mesh("set", "cdx-set", "compact_tokens=100k")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("declares no compaction threshold", proc.stderr)
        self.db.execute("UPDATE agent_registry SET harness = 'codex' WHERE name = 'cdx-set'")
        proc = self.mesh("set", "cdx-set", "compact_tokens=100k")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("compact=100000", proc.stdout)
        self.assertEqual(registry.get(self.db, "cdx-set")["compact_tokens"], 100000)
        for refus in ("compact_tokens=0", "compact_tokens=5k", "compact_tokens=beaucoup"):
            self.assertEqual(self.mesh("set", "cdx-set", refus).returncode, 2, refus)
        proc = self.mesh("set", "cdx-set", "compact_tokens=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("compact=harness default", proc.stdout)
        self.assertIsNone(registry.get(self.db, "cdx-set")["compact_tokens"])

    def test_vue_d_exploitation(self):
        _runner, worker = self._worker("cdx-vue", compact_tokens=90000)
        self.assertEqual(worker.compact_tokens(), 90000)
        vue = exploitation.agent_view(worker.cfg, registry.get(self.db, "cdx-vue"))
        self.assertEqual(vue["compact_tokens"], 90000)

    def test_tour_codex_recoit_le_seuil(self):
        from ameesh.runner import AgentWorker, Runner
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "cdx-tour")
        os.makedirs(cwd, exist_ok=True)
        self.register("cdx-tour", "codex", cwd=cwd)
        self.mesh("set", "cdx-tour", "compact_tokens=120k")
        lease = registry.claim(self.db, "cdx-tour", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "cdx-tour"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        with self._env():
            worker.run_turn({"kind": "prompt", "prompt": "x", "ids": []})
        argv = self.turns()[-1]["argv"]
        self.assertEqual(argv[argv.index(DRAPEAU) - 1], "-c")
        # Le seuil passé est publié avec la politique du tour.
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        self.assertTrue(rows, "aucun événement turn.context publié")
        data = rows[0]["data"]
        data = json.loads(data) if isinstance(data, str) else data
        self.assertEqual(data["policy"]["compact_tokens"], 120000)


if __name__ == "__main__":
    unittest.main()
