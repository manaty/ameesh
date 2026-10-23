# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP1) : relève élue et réclamation entre hôtes candidats.

Une fiche Agent `releve: election` déclare ses `candidats`. Le bail reste la
seule autorisation de tourner ; un hôte candidat peut le prendre quand il est
libre ou expiré, avec le verdict de placement de CET hôte (politique et
visibilité), et la ligne se déplace chez le gagnant. Ces agents ne passent pas
par la preuve de source de L232e : leur travail est en base, dans le canon et
sur GitHub, pas dans le worktree local.
"""
from __future__ import annotations

import os
import unittest

from ameesh import canon, canon_sync, registry, storage

from . import test_canon
from .test_canon import codes, fiche, write
from .test_placement import host_card


def election_agent(**overrides) -> str:
    fields = dict(type="Agent", title="relecteur", responsible="human:alice",
                  team="acme-web", capabilities=["read", "report-drift"],
                  harness="codex", provider="openai", credential_mode="api-key",
                  budget_usd_per_day="15.5", releve="election",
                  candidats=["atelier", "banc"])
    fields.update(overrides)
    return fiche(**fields)


def election_admission(hosts=("atelier", "banc")) -> str:
    return fiche(type="Placement", title="relecteur@candidats", agent="relecteur",
                 hosts=list(hosts), credential_mode="api-key")


class ElectionValidationTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def test_lit_releve_et_candidats(self):
        write(self.root, "agents/relecteur.md", election_agent())
        write(self.root, "placements/relecteur-atelier.md", election_admission())
        agent = self.load().agent("relecteur")
        self.assertEqual((agent.releve, agent.candidats), ("election", ["atelier", "banc"]))
        self.assertEqual(codes(canon.validate(self.load()), canon.ERROR), set())

    def test_releve_inconnu_refuse_la_fiche(self):
        write(self.root, "agents/relecteur.md", election_agent(releve="hasard"))
        self.assertIn("agent-releve-invalid", codes(canon.validate(self.load()), canon.ERROR))

    def test_election_sans_candidat_refusee(self):
        write(self.root, "agents/relecteur.md", election_agent(candidats=[]))
        findings = canon.validate(self.load())
        self.assertIn("agent-election-candidats-missing", codes(findings, canon.ERROR))

    def test_candidat_inconnu_ou_non_admis_refuse(self):
        write(self.root, "agents/relecteur.md",
              election_agent(candidats=["nulle-part", "banc"]))
        write(self.root, "placements/relecteur-atelier.md", election_admission(("atelier",)))
        findings = canon.validate(self.load())
        self.assertIn("agent-election-host-unknown", codes(findings, canon.ERROR))
        self.assertIn("agent-election-host-not-admitted", codes(findings, canon.ERROR))

    def test_candidats_sans_election_avertis(self):
        write(self.root, "agents/relecteur.md", election_agent(releve="hote"))
        self.assertIn("agent-candidats-ignored", codes(canon.validate(self.load()), canon.WARNING))


class ElectionSyncTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        # `drain` d'un essai ne doit pas fermer les réclamations du suivant
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM host_holds")
        self.db.execute("DELETE FROM host_resources")
        write(self.root, "agents/relecteur.md", election_agent())
        write(self.root, "placements/relecteur-atelier.md", election_admission())

    def candidates(self) -> dict[str, dict]:
        rows = self.db.query(
            "SELECT host, canon, cwd, placement_ok, placement_diagnostic, "
            "       placement_ref, placement_profile FROM agent_candidates "
            " WHERE name = 'relecteur' ORDER BY host")
        return {row["host"]: row for row in rows}

    def test_sync_ecrit_un_verdict_par_hote_candidat(self):
        self.sync("atelier")
        self.sync("banc")
        rows = self.candidates()
        self.assertEqual(sorted(rows), ["atelier", "banc"])
        for host, row in rows.items():
            self.assertIs(row["placement_ok"], True, row)
            self.assertTrue(row["placement_profile"], row)
            self.assertTrue(row["placement_ref"].startswith(
                "canon:placements/relecteur-atelier.md@"), row)
        self.assertEqual(rows["atelier"]["cwd"], os.path.expanduser("~/acme/acme-web"))
        self.assertEqual(rows["banc"]["cwd"], "/srv/acme/acme-web")
        row = self.row("relecteur")
        self.assertEqual((row["host"], row["releve"], row["candidats"]),
                         ("atelier", "election", ["atelier", "banc"]))
        # un second passage ne réécrit rien
        self.assertEqual(self.actions(self.sync("banc"))["relecteur"], "laissé")

    def test_reclamation_depuis_un_second_candidat(self):
        self.sync("atelier")
        self.sync("banc")
        self.assertIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        lease = registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc")
        self.assertIsNotNone(lease)
        row = self.row("relecteur")
        self.assertEqual((row["host"], row["cwd"]), ("banc", "/srv/acme/acme-web"))
        placement = self.db.query("SELECT placement_ok, placement_profile "
                                  "FROM agent_registry WHERE name = 'relecteur'")[0]
        self.assertIs(placement["placement_ok"], True)
        self.assertTrue(placement["placement_profile"])
        self.assertIsNotNone(registry.renew(self.db, "relecteur", "runner-banc",
                                            lease["lease_epoch"], 600))
        # le bail vivant ferme les deux candidats
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "atelier")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-atelier", 600,
                                         host="atelier"))
        registry.release(self.db, "relecteur", "runner-banc", lease["lease_epoch"])
        lease = registry.claim(self.db, "relecteur", "runner-atelier", 600, host="atelier")
        self.assertIsNotNone(lease)
        row = self.row("relecteur")
        self.assertEqual((row["host"], row["cwd"]),
                         ("atelier", os.path.expanduser("~/acme/acme-web")))

    def test_hote_non_candidat_ne_reclame_pas(self):
        self.sync("atelier")
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))
        # un agent `hote` reste épinglé à sa ligne
        self.assertIsNone(registry.claim(self.db, "orchestre", "runner-banc", 600,
                                         host="banc"))
        self.assertIsNotNone(registry.claim(self.db, "orchestre", "runner-atelier", 600,
                                            host="atelier"))

    def test_verdict_refuse_sur_un_candidat(self):
        self.sync("atelier")
        # banc refuse le modèle non déclaré de relecteur, mais admet ouvrier :
        # le refus ne doit fermer que le candidat relecteur, pas l'hôte.
        write(self.root, "hotes/banc.md", host_card(
            "banc", "human:alice", harnesses=["deepseek", "codex"],
            providers=["deepseek", "openai"], credential_modes=["api-key"],
            models=["deepseek-chat"], max_agents="2"))
        report = self.sync("banc")
        self.assertEqual(report.status, canon_sync.CANON_OK)
        self.assertIn("placement-policy-violation", report.diagnostic)
        self.assertIs(self.candidates()["banc"]["placement_ok"], False)
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))
        # un agent épinglé sur banc, sans lien avec le candidat refusé, reste
        # réclamable : le verdict de canon de l'hôte reste `ok`.
        self.assertIn("ouvrier", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", "runner-banc", 600,
                                            host="banc"))
        # le candidat sain reste réclamable
        self.assertIn("relecteur", [r["name"] for r in registry.claimable(self.db, "atelier")])

    def test_refus_candidat_ne_ferme_pas_un_agent_epingle_de_l_hote(self):
        self.sync("atelier")
        write(self.root, "hotes/banc.md", host_card(
            "banc", "human:alice", harnesses=["deepseek", "codex"],
            providers=["deepseek", "openai"], credential_modes=["api-key"],
            models=["deepseek-chat"], max_agents="2"))
        self.sync("banc")
        state = self.db.query("SELECT status FROM canon_state WHERE host = 'banc'")[0]
        self.assertEqual(state["status"], canon_sync.CANON_OK)
        self.assertIn("ouvrier", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", "runner-banc", 600,
                                            host="banc"))

    def test_profil_divergé_ferme_le_candidat(self):
        self.sync("atelier")
        self.sync("banc")
        self.db.execute("UPDATE agent_candidates SET placement_profile = 'profil-perime' "
                        "WHERE name = 'relecteur' AND host = 'banc'")
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))
        self.assertIn("relecteur", [r["name"] for r in registry.claimable(self.db, "atelier")])

    def test_fiche_invalide_ferme_le_secours_meme_si_le_detenteur_ne_synchronise_pas(self):
        self.sync("atelier")
        self.sync("banc")
        # le responsable ne résout plus : la fiche est fautive partout, pas
        # seulement sur l'hôte qui la relit.
        write(self.root, "agents/relecteur.md", election_agent(responsible="human:inconnu"))
        self.sync("banc")
        self.assertIsNone(self.row("relecteur")["responsible"])
        self.assertEqual({row["placement_ok"] for row in self.candidates().values()}, {False})
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))

    def test_fiches_retirees_ferment_le_secours_depuis_un_autre_candidat(self):
        self.sync("atelier")
        self.sync("banc")
        write(self.root, "agents/relecteur.md", None)
        write(self.root, "placements/relecteur-atelier.md", None)
        self.sync("banc")
        self.assertEqual(self.candidates(), {})
        row = self.row("relecteur")
        self.assertEqual((row["releve"], row["status"]), ("hote", "stopped"))
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))
        self.assertNotIn("relecteur", [r["name"] for r in registry.claimable(self.db, "atelier")])

    def test_placement_retire_ferme_les_candidats_sans_arret_du_detenteur(self):
        self.sync("atelier")
        self.sync("banc")
        write(self.root, "placements/relecteur-atelier.md", None)
        self.sync("banc")
        self.assertEqual({row["placement_ok"] for row in self.candidates().values()}, {False})
        self.assertIsNone(registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc"))

    def test_retrait_croise_ne_tue_pas_un_bail_change_depuis_la_lecture(self):
        self.sync("atelier")
        row = self.row("relecteur")
        self.assertIsNone(storage.of(self.db).canon.stop_removed(
            "relecteur", row["host"], pending_text="p", stop_text="s",
            expected_lease_epoch=int(row["lease_epoch"]) + 1))
        self.assertNotEqual(self.row("relecteur")["status"], "stopped")

    def test_pas_de_preuve_source_l232e(self):
        self.sync("atelier")
        self.sync("banc")
        api = storage.of(self.db)
        # un drain de l'hôte ne journalise pas un rôle élu (rien à déplacer)
        drained = api.host_lifecycle.drain("atelier", actor="orch", duration=3600)
        self.assertNotIn("relecteur", [r["agent"] for r in api.host_lifecycle.agents("atelier", drained["generation"])])
        # une clôture L232e posée à la main ne ferme pas non plus la relève
        self.db.execute("UPDATE agent_registry SET handoff_fenced = true "
                        "WHERE name = 'relecteur'")
        lease = registry.claim(self.db, "relecteur", "runner-banc", 600, host="banc")
        self.assertIsNotNone(lease)
        self.assertFalse(self.row("relecteur")["handoff_fenced"])

    def test_perte_d_hote_sans_role_elu_ne_cree_pas_de_perte(self):
        self.sync("atelier")
        api = storage.of(self.db)
        api.hosts.record({"host": "atelier", "gate_state": "available"})
        self.db.execute("UPDATE host_resources SET sampled_at = clock_timestamp() "
                        "- interval '10 minutes' WHERE host = 'atelier'")
        lost = api.host_lifecycle.detect_losses(actor="remote", older_than=180)
        self.assertEqual([row["mode"] for row in lost], ["lost"])
        origins = [row["agent"] for row in api.host_lifecycle.agents(
            "atelier", lost[0]["generation"])]
        self.assertNotIn("relecteur", origins)
        self.assertIn("orchestre", origins)
