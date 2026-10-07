# SPDX-License-Identifier: AGPL-3.0-only
"""L36 (décision 0030) : correctifs immédiats de vivacité.

`ameesh list` (pied global une fois, lots par agent), étiquettes d'attente qui
disent quand l'assigné ne peut pas travailler, avertissement à l'attribution
d'un nom inconnu, hook sans bail qui n'écrase plus la session d'un agent,
`send all` limité à l'équipe de l'expéditeur."""
from __future__ import annotations

import json
import unittest

from ameesh import registry, stagnation, work
from ameesh.backend import PgBackend

from .support import PgTestCase


class StagnationAvailabilityTest(unittest.TestCase):
    def test_agent_inconnu_ou_arrete(self):
        detail = stagnation._waiting("start", "fantome")
        out = stagnation.with_availability(detail, {"vivant": "idle"})
        self.assertEqual(out["label"], "démarrage par fantome (agent inconnu)")
        self.assertEqual(out["who_state"], "inconnu")
        out = stagnation.with_availability(stagnation._waiting("build", "dort"),
                                           {"dort": "stopped"})
        self.assertEqual(out["label"], "travail de dort (agent arrêté)")

    def test_agent_disponible_humain_et_sans_registre_inchanges(self):
        detail = stagnation._waiting("build", "vivant")
        self.assertIs(stagnation.with_availability(detail, {"vivant": "working"}), detail)
        humain = stagnation._waiting("decision", "human:proprio")
        self.assertIs(stagnation.with_availability(humain, {}), humain)
        self.assertIs(stagnation.with_availability(detail, None), detail)
        self.assertIsNone(stagnation.with_availability(None, {}))

    def test_plusieurs_relecteurs(self):
        detail = stagnation._waiting("verdict", ["a", "b", "c"])
        out = stagnation.with_availability(detail, {"a": "idle", "b": "stopped"})
        self.assertEqual(out["label"], "verdict de a, b, c (b arrêté, c inconnu)")
        self.assertEqual(out["who_state"], {"b": "arrêté", "c": "inconnu"})


class L36CliTest(PgTestCase):
    def test_list_pied_global_une_fois_et_lots_par_agent(self):
        self.register("alpha", "claude", cwd=self.tmp)
        self.register("beta", "codex", cwd=self.tmp)
        for title, who in (("un", "alpha"), ("deux", "alpha"), ("trois", "beta")):
            proc = self.mesh("work", "add", "--title", title, "--assignee", who)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("lots     :"), 1, proc.stdout)
        self.assertIn("lots     : 3 (demande 3)", proc.stdout)
        lines = {line.split()[0]: line for line in proc.stdout.splitlines()
                 if line.startswith(("alpha", "beta"))}
        header = proc.stdout.splitlines()[0]
        col = header.index("LOTS")
        self.assertEqual(lines["alpha"][col:col + 5].strip(), "2")
        self.assertEqual(lines["beta"][col:col + 5].strip(), "1")

    def test_attribution_a_un_inconnu_refusee_et_l_attente_le_dit(self):
        # L37 (0030) : l'avertissement de L36 est devenu un refus
        proc = self.mesh("work", "add", "--title", "orphelin", "--assignee", "fantome")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("fantome n'est pas dans le registre", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        # un lot hérité d'avant la garde (assigné disparu du registre) : l'attente le dit
        self.register("fantome", "claude", cwd=self.tmp)
        work.add(self.db, title="orphelin", assignee="fantome")
        self.db.execute("DELETE FROM agent_registry WHERE name = 'fantome'")
        proc = self.mesh("work", "list")
        self.assertIn("démarrage par fantome (agent inconnu)", proc.stdout)

        self.register("dort", "claude", cwd=self.tmp)
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté à la main",
                            stop_reason="manuel")
        proc = self.mesh("work", "add", "--title", "en panne", "--assignee", "dort")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dort est arrêté (manuel)", proc.stderr)
        proc = self.mesh("work", "list", "--json")
        rows = {r["title"]: r for r in json.loads(proc.stdout)}
        self.assertEqual(rows["en panne"]["waiting_for"]["who_state"], "arrêté")
        proc = self.mesh("progress", "--json")
        lots = {lot["title"]: lot for lot in json.loads(proc.stdout)["lots"]}
        self.assertEqual(lots["orphelin"]["waiting_for"]["label"],
                         "démarrage par fantome (agent inconnu)")

    def test_hook_sans_bail_n_ecrase_pas_la_session(self):
        self.register("alpha", "claude", cwd=self.tmp, session="session-du-runner")
        hook = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME="alpha"),
                        stdin=json.dumps({"hook_event_name": "SessionStart",
                                          "session_id": "session-etrangere",
                                          "cwd": "/ailleurs"}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["session_id"], "session-du-runner")
        self.assertEqual(row["cwd"], self.tmp)

    def test_hook_sans_bail_enregistre_une_premiere_session(self):
        hook = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME="externe"),
                        stdin=json.dumps({"hook_event_name": "SessionStart",
                                          "session_id": "s-1", "cwd": self.tmp}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(registry.get(self.db, "externe")["session_id"], "s-1")

    def test_envoi_a_tous_limite_au_chantier(self):
        registry.upsert(self.db, "alpha", chantier="p1")
        registry.upsert(self.db, "beta", chantier="p1")
        registry.upsert(self.db, "gamma", chantier="p2")
        targets = PgBackend(self.cfg, self.db).send("alpha", "all", "Point d'équipe.")
        self.assertEqual(sorted(targets), ["beta"])
        # un expéditeur sans chantier garde la diffusion globale
        registry.upsert(self.db, "orchestre")
        targets = PgBackend(self.cfg, self.db).send("orchestre", "all", "À tous.")
        self.assertEqual(sorted(targets), ["alpha", "beta", "gamma"])


if __name__ == "__main__":
    unittest.main()
