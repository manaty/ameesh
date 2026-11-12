# SPDX-License-Identifier: AGPL-3.0-only
"""#7106 (avatar B3) : hôte partagé multi-tenant.

docs/design/5322-avatar-humain.md §3.4 : `policy.shared`/`tenants`, garde de
réclamation par `responsible` en base, runner par utilisateur système,
`runner_presence`, `require_isolation` par UID vérifié, provisionnement
source only. Décision Q2 (#213954) : clés d'organisation seulement.

Témoins d'acceptation : la base refuse au runner de A le bail d'un agent de
B (même si son filtre d'agents était modifié) ; le runner de A le prend.
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from ameesh import canon, election, persona_memory, placement, registry, storage, tenancy

from . import test_canon
from .test_canon import codes, write
from .test_placement import host_card

ALICE, BRUNO = "human:alice", "human:bruno"


def banc_partage(**overrides) -> str:
    policy = dict(harnesses=["deepseek", "codex"], providers=["deepseek", "openai"],
                  credential_modes=["api-key"], max_agents="2", shared="true",
                  tenants=[ALICE, BRUNO])
    policy.update(overrides)
    return host_card("banc", ALICE, **{k: v for k, v in policy.items() if v is not None})


class SharedCanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def errors(self):
        return codes(canon.validate(self.load()), canon.ERROR)

    def test_politique_lue(self):
        write(self.root, "hotes/banc.md", banc_partage())
        host = self.load().host("banc")
        self.assertTrue(host.policy.shared)
        self.assertEqual(host.policy.tenants, [ALICE, BRUNO])
        self.assertEqual(self.errors(), set())

    def test_partage_sans_tenant_refuse(self):
        write(self.root, "hotes/banc.md", banc_partage(tenants=None))
        self.assertIn("host-shared-no-tenants", self.errors())

    def test_tenants_sans_partage_refuses(self):
        write(self.root, "hotes/banc.md", banc_partage(shared=None))
        self.assertIn("host-tenants-not-shared", self.errors())

    def test_tenant_inconnu_refuse(self):
        write(self.root, "hotes/banc.md", banc_partage(tenants=[ALICE, "human:carole"]))
        self.assertIn("host-tenant-unresolved", self.errors())

    def test_abonnement_refuse_q2(self):
        write(self.root, "hotes/banc.md",
              banc_partage(credential_modes=["api-key", "subscription"]))
        found = [f for f in canon.validate(self.load())
                 if f.code == "host-shared-credential-mode"]
        self.assertTrue(found and "#213954" in found[0].message, found)
        write(self.root, "hotes/banc.md", banc_partage(credential_modes=None))
        self.assertIn("host-shared-credential-mode", self.errors())

    def test_partage_illisible_ferme(self):
        write(self.root, "hotes/banc.md", banc_partage(shared="oui"))
        self.assertTrue(self.load().host("banc").policy.shared)
        self.assertIn("host-policy-invalid", self.errors())

    def test_placement_reserve_aux_tenants(self):
        write(self.root, "hotes/banc.md", banc_partage(tenants=[BRUNO]))
        loaded = self.load()
        # ouvrier (bruno) admis ; relecteur (alice) refusé sur banc
        self.assertTrue(placement.evaluate(loaded, "ouvrier", "banc").ok)
        write(self.root, "placements/relecteur-atelier.md", test_canon.fiche(
            type="Placement", title="relecteur@atelier", agent="relecteur",
            hosts=["banc"], credential_mode="api-key"))
        verdict = placement.evaluate(self.load(), "relecteur", "banc")
        self.assertFalse(verdict.ok)
        self.assertIn("not a tenant of shared host banc", verdict.diagnostic)

    def test_mode_d_abonnement_refuse_meme_sans_restriction(self):
        # défense en profondeur : une politique fautive (sans
        # credential_modes) n'admet pas pour autant un abonnement
        host = {"title": "banc", "policy": {"shared": True, "tenants": [ALICE]}}
        reasons = canon.placement_violations(
            {"responsible": ALICE, "credential_mode": "subscription"}, host)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("organization keys only", reasons[0])
        self.assertEqual(canon.placement_violations(
            {"responsible": ALICE, "credential_mode": "api-key"}, host), [])

    def test_hote_non_partage_inchange(self):
        self.assertEqual(canon.shared_violations(
            {"responsible": ALICE}, self.load().host("banc"), "subscription"), [])


class SharedClaimTest(test_canon._CanonDbCase):
    """La garde en base : runner lié au tenant `responsible`, et lui seul."""

    def setUp(self) -> None:
        super().setUp()
        for table in ("runner_presence", "host_tenants", "host_tenancy"):
            self.db.execute("DELETE FROM " + table)
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM host_holds")
        write(self.root, "hotes/banc.md", banc_partage())
        write(self.root, "placements/relecteur-atelier.md", test_canon.fiche(
            type="Placement", title="relecteur@banc", agent="relecteur",
            hosts=["banc"], credential_mode="api-key"))
        self.sync("banc")
        self.sync("atelier")
        api = storage.of(self.db).tenancy
        api.bind("banc", ALICE, unix_user="ameesh-alice", uid=2001,
                 runner_id="banc/alice", actor="human:alice")
        api.bind("banc", BRUNO, unix_user="ameesh-bruno", uid=2002,
                 runner_id="banc/bruno", actor="human:alice")

    def test_politique_synchronisee(self):
        self.assertEqual(tenancy.host_policy(self.db, "banc"),
                         {"shared": True, "tenants": [ALICE, BRUNO]})
        self.assertEqual(tenancy.host_policy(self.db, "atelier"),
                         {"shared": False, "tenants": []})

    def test_la_base_refuse_le_bail_d_un_autre_tenant(self):
        self.assertEqual(self.row("relecteur")["responsible"], ALICE)
        self.assertIn("relecteur", [r["name"] for r in registry.claimable(self.db, "banc")])
        # runner de bruno, superviseur, identifiant inconnu : refusés
        for owner in ("banc/bruno", "banc:4242", "intrus"):
            with self.subTest(owner=owner):
                self.assertIsNone(registry.claim(self.db, "relecteur", owner, 600))
        lease = registry.claim(self.db, "relecteur", "banc/alice", 600)
        self.assertIsNotNone(lease)
        registry.release(self.db, "relecteur", "banc/alice", lease["lease_epoch"])
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", "banc/bruno", 600))

    def test_tenant_retire_du_canon_ne_reclame_plus(self):
        write(self.root, "hotes/banc.md", banc_partage(tenants=[BRUNO]))
        self.sync("banc")
        self.assertIsNone(registry.claim(self.db, "relecteur", "banc/alice", 600))

    def test_attach_par_l_utilisateur_du_tenant(self):
        self.assertIsNone(registry.attach_claim(self.db, "relecteur",
                                                "attach:ameesh-bruno@banc", 60))
        self.assertIsNotNone(registry.attach_claim(self.db, "relecteur",
                                                   "attach:ameesh-alice@banc", 60))

    def test_hote_non_partage_inchange(self):
        self.assertIsNotNone(registry.claim(self.db, "orchestre", "n-importe-qui", 600))

    def test_filtre_du_runner(self):
        cfg = dataclasses.replace(self.cfg, host="banc", tenant=ALICE)
        policy = tenancy.host_policy(self.db, "banc")
        rows = registry.claimable(self.db, "banc")
        self.assertEqual([r["name"] for r in rows if tenancy.admits(cfg, policy, r)],
                         ["relecteur"])
        supervisor = dataclasses.replace(cfg, tenant="")
        self.assertEqual([r for r in rows if tenancy.admits(supervisor, policy, r)], [])

    def test_retrait_refuse_tant_qu_un_agent_reste(self):
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.remove(self.db, "banc", ALICE)
        self.assertEqual(caught.exception.code, "tenant-busy")
        self.assertIn("relecteur", str(caught.exception))
        self.db.execute("UPDATE agent_registry SET host = 'atelier' WHERE name = 'relecteur'")
        self.assertTrue(tenancy.remove(self.db, "banc", ALICE)["removed"])

    def test_enregistrement_exige_le_tenant_du_canon(self):
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.register(self.db, "atelier", ALICE, actor="human:alice", uid=3001)
        self.assertEqual(caught.exception.code, "tenant-not-declared")
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.register(self.db, "banc", ALICE, actor="human:alice",
                             user="ameesh-utilisateur-absent-7106")
        self.assertEqual(caught.exception.code, "tenant-user-missing")
        row = tenancy.register(self.db, "banc", ALICE, actor="human:alice", uid=3001)
        self.assertEqual((row["uid"], row["runner_id"], row["unix_user"]),
                         (3001, "banc/alice", "ameesh-alice"))

    # -- présence --------------------------------------------------------------
    def test_battement_sous_liaison_seulement(self):
        cfg = dataclasses.replace(self.cfg, host="banc", tenant=ALICE, runner_id="banc/alice")
        self.assertTrue(tenancy.beat(cfg, self.db, version="1.51.0"))
        intrus = dataclasses.replace(cfg, runner_id="banc/bruno")
        self.assertFalse(tenancy.beat(intrus, self.db, version="x"))
        self.assertFalse(tenancy.beat(dataclasses.replace(cfg, tenant=""), self.db))
        row = storage.of(self.db).tenancy.presence("banc", ALICE)
        self.assertEqual((row["runner_id"], row["version"]), ("banc/alice", "1.51.0"))

    def test_presence_du_tenant(self):
        role = registry.get(self.db, "relecteur")
        now = storage.of(self.db).tenancy.bindings("banc")[0]["now_ts"]
        self.assertIn("no runner of tenant human:alice",
                      tenancy.presence_reason(self.db, role, "banc", now=now, limit_s=180))
        cfg = dataclasses.replace(self.cfg, host="banc", tenant=ALICE, runner_id="banc/alice")
        tenancy.beat(cfg, self.db, version="v")
        self.assertEqual(tenancy.presence_reason(self.db, role, "banc", now=now,
                                                 limit_s=180), "")
        self.assertIn("silent", tenancy.presence_reason(self.db, role, "banc",
                                                        now=now + 3600, limit_s=180))
        # hôte non partagé : la présence reste le relevé de l'hôte
        self.assertEqual(tenancy.presence_reason(self.db, role, "atelier", now=now,
                                                 limit_s=180), "")

    def test_candidature_publiee_par_le_seul_tenant(self):
        self.db.execute("UPDATE agent_registry SET releve = 'preference',"
                        " candidats = '{banc}' WHERE name = 'relecteur'")
        bruno = dataclasses.replace(self.cfg, host="banc", tenant=BRUNO)
        with mock.patch.object(election, "evaluate") as evaluate:
            self.assertEqual(election.publish_self(bruno, self.db, "banc", force=True), 0)
            evaluate.assert_not_called()

    # -- isolation par UID vérifié --------------------------------------------
    def isolation(self, uid: int) -> None:
        cfg = dataclasses.replace(self.cfg, host="banc")
        with mock.patch.object(persona_memory, "isolation_policy", return_value="required"), \
                mock.patch.object(os, "getuid", return_value=uid):
            persona_memory.require_isolation(cfg, self.db, "relecteur")

    def test_isolation_entre_tenants_distincts(self):
        registry.claim(self.db, "ouvrier", "banc/bruno", 600)
        self.isolation(2001)  # alice, pair bruno sous un autre UID : admis

    def test_isolation_refuse_un_autre_uid_de_processus(self):
        with self.assertRaisesRegex(persona_memory.MemoryError, "is not the UID 2001"):
            self.isolation(2002)

    def test_isolation_refuse_le_meme_tenant(self):
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'ouvrier'",
                        (ALICE,))
        registry.claim(self.db, "ouvrier", "banc/alice", 600)
        with self.assertRaisesRegex(persona_memory.MemoryError, "shares UID 2001"):
            self.isolation(2001)

    def test_isolation_refuse_un_pair_sans_uid(self):
        self.db.execute("DELETE FROM host_tenants WHERE tenant = %s", (BRUNO,))
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'ouvrier'")
        with self.assertRaisesRegex(persona_memory.MemoryError, "no verified tenant UID"):
            self.isolation(2001)


class ProvisioningTest(unittest.TestCase):
    def test_utilisateur_derive(self):
        self.assertEqual(tenancy.unix_user("human:Alice.Martin"), "ameesh-alice-martin")
        self.assertEqual(tenancy.runner_id("banc", " human:alice"), "banc/alice")
        with self.assertRaises(tenancy.TenancyError):
            tenancy.unix_user("agent:relecteur")

    def test_plan_sans_secret_ni_execution(self):
        with mock.patch("subprocess.run") as run:
            text = tenancy.shell_plan("banc", ALICE)
        run.assert_not_called()
        self.assertIn("useradd --create-home --home-dir /home/ameesh-alice", text)
        self.assertIn("chmod 0700 /home/ameesh-alice", text)
        self.assertIn("loginctl enable-linger ameesh-alice", text)
        self.assertIn("AMEESH_TENANT=human:alice", text)
        self.assertIn("UMask=0077", text)
        self.assertNotIn("KEY", text.upper().replace("KEYS", ""))

    def test_runner_de_tenant_bat_sans_relever(self):
        from ameesh import runner as runner_mod
        fake = mock.Mock(cfg=mock.Mock(tenant=ALICE, runner="banc/alice", host="banc"),
                         mediated=False, db=object())
        with mock.patch.object(tenancy, "beat", return_value=True) as beat, \
                mock.patch("ameesh.scoped_db.ScopedDb"), \
                mock.patch("ameesh.resources.collect") as collect:
            self.assertIsNone(runner_mod.Runner.resource_once.__wrapped__(fake)
                              if hasattr(runner_mod.Runner.resource_once, "__wrapped__")
                              else runner_mod.Runner.resource_once(fake))
        beat.assert_called_once()
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
