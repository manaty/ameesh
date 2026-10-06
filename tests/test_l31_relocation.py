# SPDX-License-Identifier: AGPL-3.0-only
"""Déplacement entre hôtes admis (lot L31, décision 0028) : planifié entre deux
tours quand l'hôte courant est sous pression, testé avec deux hôtes simulés."""
from __future__ import annotations

from ameesh import registry, relocation, storage

from . import test_canon
from .test_canon import write

ATELIER = """---
type: Host
title: atelier
responsible: human:bruno
policy:
  harnesses: [claude, codex, deepseek]
  providers: [anthropic, openai, deepseek]
  credential_modes: [api-key, subscription]
  work_roots: {acme-web: %s}
  max_agents: 4
---

# atelier
"""

BANC = """---
type: Host
title: banc
responsible: human:alice
policy:
  harnesses: [deepseek, codex]
  providers: [deepseek, openai]
  credential_modes: [api-key]
  work_roots: {acme-web: %s}
  max_agents: 2
---

# banc
"""

ADMISSION = """---
type: Placement
title: ouvrier
agent: ouvrier
hosts: [atelier, banc]
credential_mode: api-key
---

# ouvrier
"""


class _TwoHosts(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("DELETE FROM visibility_checks")
        write(self.root, "hotes/atelier.md", ATELIER % self.tmp)
        write(self.root, "hotes/banc.md", BANC % self.tmp)
        write(self.root, "placements/ouvrier-banc.md", ADMISSION)

    def releve(self, host, mem):
        storage.of(self.db).hosts.record({
            "host": host, "mem_available_bytes": mem, "swap_used_bytes": 0,
            "load1": 0.0, "cpu_count": 8, "disk_free_bytes": 10 ** 12,
            "disk_path": "/tmp", "turns_in_progress": 0})


class PlanTest(_TwoHosts):
    def test_pas_de_deplacement_sans_pression(self):
        self.releve("atelier", 10 ** 10)
        self.releve("banc", 10 ** 10)
        self.assertIsNone(relocation.plan(
            ["atelier", "banc"], "atelier", db=self.db))

    def test_deplace_vers_l_hote_le_plus_disponible(self):
        self.releve("atelier", 0)
        self.releve("banc", 8 * 1024 ** 3)
        plan = relocation.plan(["atelier", "banc"], "atelier", db=self.db)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["target"], "banc")
        self.assertFalse(plan["keep_session"])
        self.assertIn("sous pression", plan["reason"])

    def test_aucun_candidat_disponible(self):
        self.releve("atelier", 0)
        self.releve("banc", 0)
        self.assertIsNone(relocation.plan(["atelier", "banc"], "atelier", db=self.db))

    def test_session_conservee_si_stockage_partage(self):
        self.releve("atelier", 0)
        self.releve("banc", 8 * 1024 ** 3)
        plan = relocation.plan(["atelier", "banc"], "atelier", db=self.db,
                               shared_sessions=True)
        self.assertTrue(plan["keep_session"])


class _MoveCase(_TwoHosts):
    def _prepare(self):
        """Crée l'agent sur atelier, avec session et consigne, sans bail."""
        self.sync("atelier")
        row = registry.get(self.db, "ouvrier")
        self.assertEqual(row["host"], "atelier")
        self.assertEqual(row["admitted_hosts"], ["atelier", "banc"])
        registry.set_session(self.db, "ouvrier", "sess-1")
        registry.set_pending_prompt(self.db, "ouvrier", "consigne en attente")
        return registry.get(self.db, "ouvrier")

    def _pret(self, owner="runner-a"):
        """Comme `_prepare`, puis réclame le bail ; rend (ligne, owner, epoch)."""
        self._prepare()
        lease = registry.claim(self.db, "ouvrier", owner, 600)
        self.assertIsNotNone(lease)
        return registry.get(self.db, "ouvrier"), owner, int(lease["lease_epoch"])

    def _plan(self, **kwargs):
        self.releve("atelier", 0)
        self.releve("banc", 8 * 1024 ** 3)
        return relocation.plan(["atelier", "banc"], "atelier", db=self.db, **kwargs)


class MoveTest(_MoveCase):
    def test_deplacement_rend_le_bail_et_repare_la_session(self):
        _row, owner, epoch = self._pret()
        result = relocation.move(self.db, registry.get(self.db, "ouvrier"),
                                 self._plan(), current_host="atelier",
                                 owner=owner, epoch=epoch)
        self.assertEqual(result["target"], "banc")
        self.assertFalse(result["keep_session"])
        row = registry.get(self.db, "ouvrier")
        self.assertEqual(row["host"], "banc")
        self.assertIsNone(row["session_id"])                 # session non portable
        self.assertIn("Reprise après déplacement", row["pending_prompt"])
        self.assertIn("consigne en attente", row["pending_prompt"])
        verdict = self.db.query("SELECT placement_ok FROM agent_registry "
                                "WHERE name = 'ouvrier'")[0]
        self.assertIsNone(verdict["placement_ok"])           # à réévaluer sur banc

    def test_deux_hotes_simules_bail_rendu_puis_repris(self):
        _row, owner, epoch = self._pret()

        def claimables(host):
            return sorted(r["name"] for r in registry.claimable(
                self.db, host, require_responsible=False))

        self.assertNotIn("ouvrier", claimables("atelier"))
        relocation.move(self.db, registry.get(self.db, "ouvrier"),
                        self._plan(), current_host="atelier", owner=owner, epoch=epoch)
        registry.release(self.db, "ouvrier", owner, epoch)
        # l'ancien hôte ne le voit plus, le nouveau l'admet après son sync
        self.assertNotIn("ouvrier", claimables("atelier"))
        self.sync("banc")
        self.assertIn("ouvrier", claimables("banc"))
        reprise = registry.claim(self.db, "ouvrier", "runner-b", 600)
        self.assertIsNotNone(reprise)

    def test_session_conservee_si_stockage_partage(self):
        _row, owner, epoch = self._pret()
        relocation.move(self.db, registry.get(self.db, "ouvrier"),
                        self._plan(shared_sessions=True), current_host="atelier",
                        owner=owner, epoch=epoch)
        row = registry.get(self.db, "ouvrier")
        self.assertEqual(row["host"], "banc")
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["pending_prompt"], "consigne en attente")

    def test_deplacement_refuse_un_worker_perime(self):
        _row, owner, epoch = self._pret()
        # un autre exécuteur a repris le bail : epoch suivant
        self.db.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1, "
                        "lease_owner = 'runner-b' WHERE name = 'ouvrier'")
        self.assertIsNone(relocation.move(
            self.db, registry.get(self.db, "ouvrier"), self._plan(),
            current_host="atelier", owner=owner, epoch=epoch))
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")

    def test_deplacement_refuse_un_tour_en_cours(self):
        _row, owner, epoch = self._pret()
        self.assertTrue(registry.begin_turn(self.db, "ouvrier", owner, epoch, "tour"))
        self.assertIsNone(relocation.move(
            self.db, registry.get(self.db, "ouvrier"), self._plan(),
            current_host="atelier", owner=owner, epoch=epoch))
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")

    def test_deplacement_refuse_un_bail_expire(self):
        _row, owner, epoch = self._pret()
        self.db.execute("UPDATE agent_registry SET lease_expires_at = "
                        "clock_timestamp() - interval '1 second' WHERE name = 'ouvrier'")
        self.assertIsNone(relocation.move(
            self.db, registry.get(self.db, "ouvrier"), self._plan(),
            current_host="atelier", owner=owner, epoch=epoch))
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")

    def test_deplacement_refuse_une_destination_plus_admise(self):
        _row, owner, epoch = self._pret()
        self.db.execute("UPDATE agent_registry SET admitted_hosts = ARRAY['atelier'] "
                        "WHERE name = 'ouvrier'")
        self.assertIsNone(relocation.move(
            self.db, registry.get(self.db, "ouvrier"), self._plan(),
            current_host="atelier", owner=owner, epoch=epoch))
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")


class PauseTest(_MoveCase):
    def test_pause_refusee_si_bail_perdu_ou_remplace(self):
        _row, owner, epoch = self._pret()
        self.assertTrue(registry.pause(self.db, "ouvrier", owner, epoch, "pression"))
        self.assertEqual(registry.get(self.db, "ouvrier")["status"], "blocked")
        # un worker périmé (mauvais epoch) ne peut plus rien écrire
        self.assertFalse(registry.pause(self.db, "ouvrier", owner, epoch + 1, "pression"))
        self.assertFalse(registry.pause(self.db, "ouvrier", "runner-b", epoch, "pression"))

    def test_pause_refusee_pendant_un_tour(self):
        _row, owner, epoch = self._pret()
        self.assertTrue(registry.begin_turn(self.db, "ouvrier", owner, epoch, "tour"))
        self.assertFalse(registry.pause(self.db, "ouvrier", owner, epoch, "pression"))
        self.assertEqual(registry.get(self.db, "ouvrier")["status"], "running")

    def test_pause_refusee_sur_bail_expire(self):
        _row, owner, epoch = self._pret()
        self.db.execute("UPDATE agent_registry SET lease_expires_at = "
                        "clock_timestamp() - interval '1 second' WHERE name = 'ouvrier'")
        self.assertFalse(registry.pause(self.db, "ouvrier", owner, epoch, "pression"))


class RunnerRelocationTest(_MoveCase):
    def test_executeur_se_deplace_puis_un_autre_hote_reprend(self):
        self._prepare()                     # pas de bail : l'exécuteur le prendra
        self.releve("atelier", 0)
        self.releve("banc", 8 * 1024 ** 3)
        env = self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                       AMEESH_HOST="atelier", AMEESH_RELOCATE="1")
        proc = self.runner("--once", "--agents", "ouvrier", env=env)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        row = registry.get(self.db, "ouvrier")
        self.assertEqual(row["host"], "banc")
        self.assertIn("Reprise après déplacement", row["pending_prompt"])
        # l'exécuteur de l'hôte d'arrivée réclame et fait le tour
        env_b = self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                         AMEESH_HOST="banc")
        proc = self.runner("--once", "--agents", "ouvrier", env=env_b)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tours = self.turns()
        self.assertTrue(tours, proc.stdout)
        self.assertEqual(tours[-1]["harness"], "deepseek")
