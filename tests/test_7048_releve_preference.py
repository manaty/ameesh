# SPDX-License-Identifier: AGPL-3.0-only
"""#7048 (avatar B1) : `releve: preference`, ordre d'admission, rang, présence.

docs/design/5322-avatar-humain.md §3.2 : un agent `releve: preference` reprend
la relève croisée d'EP1 (bail, epoch, `agent_candidates`, brief) ; ses
candidats sont les hôtes de sa fiche Placement, dans l'ordre déclaré, et le
classement suit cet ordre, pas la disponibilité. Témoin : poste présent → il
prend le bail ; poste absent → le cloud le prend au tour suivant.
"""
from __future__ import annotations

import time
import unittest

from ameesh import canon, election, placement, registry

from . import test_canon
from .test_canon import codes, fiche, write
from .test_l165_ep2_election_score import _RoleCase


def avatar(**overrides) -> str:
    fields = dict(type="Agent", title="relecteur", responsible="human:alice",
                  team="acme-web", capabilities=["read", "report-drift"],
                  harness="codex", provider="openai", credential_mode="api-key",
                  budget_usd_per_day="15.5", releve="preference")
    fields.update(overrides)
    return fiche(**{k: v for k, v in fields.items() if v is not None})


def admission(hosts=("banc", "atelier"), **extra) -> str:
    return fiche(type="Placement", title="relecteur@atelier", agent="relecteur",
                 hosts=list(hosts), credential_mode="api-key", **extra)


class PreferenceCanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def test_ordre_d_admission_conserve(self):
        # banc avant atelier : ni tri par nom, ni perte de l'ordre
        write(self.root, "agents/relecteur.md", avatar())
        write(self.root, "placements/relecteur-atelier.md", admission())
        loaded = self.load()
        agent = loaded.agent("relecteur")
        self.assertEqual(agent.releve, "preference")
        self.assertIsNone(agent.candidats)
        self.assertEqual(placement.admitted_hosts(
            loaded, placement.admission_of(loaded, "relecteur")), ["banc", "atelier"])
        self.assertEqual(canon.lease_candidates(loaded, agent), ["banc", "atelier"])
        self.assertEqual(codes(canon.validate(loaded), canon.ERROR), set())
        self.assertNotIn("agent-preference-no-fallback",
                         codes(canon.validate(loaded), canon.WARNING))

    def test_etiquettes_apres_les_hotes_nommes(self):
        write(self.root, "agents/relecteur.md", avatar())
        with open(f"{self.root}/hotes/atelier.md", encoding="utf-8") as handle:
            text = handle.read()
        write(self.root, "hotes/atelier.md",
              text.replace("type: Host", "type: Host\ntags: [cloud]", 1))
        write(self.root, "placements/relecteur-atelier.md",
              admission(("banc",), host_tags=["cloud"]))
        loaded = self.load()
        self.assertEqual(canon.lease_candidates(loaded, loaded.agent("relecteur")),
                         ["banc", "atelier"])

    def test_candidats_refuses_avec_preference(self):
        write(self.root, "agents/relecteur.md", avatar(candidats=["atelier", "banc"]))
        write(self.root, "placements/relecteur-atelier.md", admission())
        self.assertIn("agent-preference-candidats",
                      codes(canon.validate(self.load()), canon.ERROR))

    def test_un_seul_hote_averti_sans_repli(self):
        write(self.root, "agents/relecteur.md", avatar())
        write(self.root, "placements/relecteur-atelier.md", admission(("atelier",)))
        findings = canon.validate(self.load())
        self.assertIn("agent-preference-no-fallback", codes(findings, canon.WARNING))
        self.assertEqual(codes(findings, canon.ERROR), set())

    def test_releve_inconnue_nomme_preference(self):
        write(self.root, "agents/relecteur.md", avatar(releve="hasard"))
        found = [f for f in canon.validate(self.load()) if f.code == "agent-releve-invalid"]
        self.assertTrue(found and "`preference`" in found[0].message, found)

    def test_une_seule_place_dans_le_maillage(self):
        # comme un rôle élu (#3101) : l'admission n'est assise qu'une fois
        write(self.root, "agents/relecteur.md", avatar())
        write(self.root, "placements/relecteur-atelier.md", admission())
        loaded = self.load()
        covered = placement.covered_by_host(loaded)
        _pinned, seats = canon.admission_load(loaded, covered)
        self.assertEqual(seats.get("relecteur"), "banc")

    def test_aucune_place_erreur_sans_exception(self):
        """B qa-01 (#39780) : candidats pleins → erreur de fiche, pas TypeError."""
        from .test_placement import host_card
        write(self.root, "agents/relecteur.md", avatar())
        write(self.root, "placements/relecteur-atelier.md", admission())
        # orchestre est épinglé sur atelier, ouvrier sur banc : plus de place
        write(self.root, "hotes/atelier.md", host_card(
            "atelier", harnesses=["claude", "codex", "deepseek"],
            providers=["anthropic", "openai", "deepseek"],
            credential_modes=["api-key", "subscription"],
            work_roots="{acme-web: ~/acme/acme-web}", max_agents="1"))
        write(self.root, "hotes/banc.md", host_card(
            "banc", "human:alice", harnesses=["deepseek", "codex"],
            providers=["deepseek", "openai"], credential_modes=["api-key"],
            work_roots="{acme-web: /srv/acme/acme-web}", max_agents="1"))
        found = [f for f in canon.validate(self.load())
                 if f.code == "agent-election-max-agents"]
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0].severity, canon.ERROR)
        self.assertIn("(banc, atelier)", found[0].message)


class PreferenceSyncTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM host_holds")
        self.db.execute("DELETE FROM host_resources")
        write(self.root, "agents/relecteur.md", avatar())
        write(self.root, "placements/relecteur-atelier.md", admission())

    def ranks(self) -> dict[str, int | None]:
        rows = self.db.query("SELECT host, preference_rank, placement_ok FROM agent_candidates"
                             " WHERE name = 'relecteur' ORDER BY host")
        self.assertTrue(all(row["placement_ok"] for row in rows), rows)
        return {row["host"]: row["preference_rank"] for row in rows}

    def test_sync_ecrit_ordre_et_rang(self):
        self.sync("banc")
        self.sync("atelier")
        self.assertEqual(self.ranks(), {"banc": 0, "atelier": 1})
        row = self.row("relecteur")
        self.assertEqual((row["releve"], row["candidats"], row["admitted_hosts"]),
                         ("preference", ["banc", "atelier"], ["banc", "atelier"]))
        self.assertEqual(self.actions(self.sync("atelier"))["relecteur"], "laissé")

    def test_reclamation_croisee_vers_le_cloud(self):
        self.sync("banc")
        self.sync("atelier")
        self.assertIn("relecteur", [r["name"] for r in registry.claimable(self.db, "atelier")])
        lease = registry.claim(self.db, "relecteur", "runner-atelier", 600, host="atelier",
                               election_brief="brief-atelier")
        self.assertIsNotNone(lease)
        self.assertEqual(self.row("relecteur")["host"], "atelier")
        # bail vivant : le poste ne le prend pas
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600,
                                         host="banc"))
        registry.release(self.db, "relecteur", "runner-atelier", lease["lease_epoch"])
        self.assertIsNotNone(registry.claim(self.db, "relecteur", "runner-banc", 600,
                                            host="banc", election_brief="brief-banc"))
        self.assertEqual(self.row("relecteur")["host"], "banc")

    def test_ordre_inverse_change_le_rang(self):
        self.sync("banc")
        write(self.root, "placements/relecteur-atelier.md", admission(("atelier", "banc")))
        self.sync("banc")
        self.sync("atelier")
        self.assertEqual(self.ranks(), {"atelier": 0, "banc": 1})


class PreferenceRankTest(_RoleCase):
    """Le classement suit `preference_rank` ; la présence filtre les absents."""

    POSTE, CLOUD = "atelier", "banc"

    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role(hosts=(self.POSTE, self.CLOUD))
        self.db.execute("UPDATE agent_registry SET releve = 'preference' WHERE name = 'pilote'")
        self.role_row = registry.get(self.db, "pilote")
        for rank, host in enumerate((self.POSTE, self.CLOUD)):
            self.ready_host("pilote", host)
            self.db.execute("UPDATE agent_candidates SET preference_rank = %s"
                            " WHERE name = 'pilote' AND host = %s", (rank, host))

    def test_poste_present_passe_avant_un_cloud_plus_disponible(self):
        self.standing("pilote", self.CLOUD, 99.0, capacity=0.9)
        self.standing("pilote", self.POSTE, 10.0, capacity=0.0)
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual([(i["host"], i["rank"], i["delay_s"]) for i in ranked],
                         [(self.POSTE, 0, 0.0), (self.CLOUD, 1, 20.0)])
        allowed, _, _ = election.claim_wait(self.db, self.role_row, self.POSTE,
                                            1000.0, now=1000.0)
        self.assertTrue(allowed)
        allowed, reason, _ = election.claim_wait(self.db, self.role_row, self.CLOUD,
                                                 1000.0, now=1010.0)
        self.assertFalse(allowed)
        self.assertIn("rank 1", reason)

    def test_poste_absent_le_cloud_prend_sans_attendre(self):
        self.standing("pilote", self.CLOUD, 99.0)
        # poste éteint : son dernier standing a une heure (runner silencieux)
        self.standing("pilote", self.POSTE, 10.0, evaluated_ts=time.time() - 3600)
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual((ranked[0]["host"], ranked[0]["rank"]), (self.CLOUD, 0))
        self.assertIsNone(ranked[1]["rank"])
        allowed, _, _ = election.claim_wait(self.db, self.role_row, self.CLOUD,
                                            1000.0, now=1000.0)
        self.assertTrue(allowed)

    def test_porte_fermee_du_poste_le_rend_absent(self):
        self.standing("pilote", self.CLOUD, 50.0)
        self.standing("pilote", self.POSTE, 50.0)
        self.db.execute("DELETE FROM canon_state WHERE host = %s", (self.POSTE,))
        ranked = election.candidacy(self.db, "pilote")
        self.assertEqual((ranked[0]["host"], ranked[0]["rank"]), (self.CLOUD, 0))
        self.assertIn("canon not synced", ranked[1]["diagnostic"])

    def test_role_preference_publie_son_standing(self):
        from ameesh import role_standings_pg as pg
        self.assertIn("pilote", [r["name"] for r in pg.elected_roles(self.db, self.CLOUD)])


if __name__ == "__main__":
    unittest.main()
