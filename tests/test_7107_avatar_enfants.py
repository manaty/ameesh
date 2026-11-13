# SPDX-License-Identifier: AGPL-3.0-only
"""#7107 (avatar B5) : placement des enfants sur un hôte propre.

docs/design/5322-avatar-humain.md §3.6, §6 B5 ; décision Q6 (#213955).

Témoins d'acceptation :

* un avatar `children: own-host` place son enfant sur un autre hôte que ceux
  de sa lignée, avec le runner et l'UID du tenant de cet hôte — jamais ceux du
  parent ; la base refuse le bail au runner du parent ;
* `children: none` refuse (`spawn-forbidden`), `pod` est hors lot
  (`spawn-pod-unsupported`) ;
* sans placement propre, `spawn` refuse (`spawn-no-placement`) en nommant
  l'hôte/le tenant à provisionner, sans replier sur l'hôte du parent ;
* un petit-enfant ne revient pas sur l'hôte d'un ancêtre.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ameesh import avatar_children, canon, canon_sync, registry, storage, tenancy

from . import test_canon
from .test_canon import codes, fiche, write
from .test_placement import host_card

ALICE = "human:alice"


def shared_children_host(**overrides) -> str:
    """Hôte dédié aux enfants, partagé avec le tenant d'alice.

    `~/{agent}` donne à chaque enfant son propre dossier sous le home 0700 du
    tenant ; la politique n'admet que le profil de l'avatar (Claude/anthropic,
    clé d'API, décision Q2 #213954)."""
    policy = dict(shared="true", tenants=[ALICE], harnesses=["claude"],
                  providers=["anthropic"], credential_modes=["api-key"],
                  workspaces='{roots: {acme-web: "~/{agent}"}}', max_agents="4")
    policy.update(overrides)
    return host_card("enfants", ALICE,
                     **{key: value for key, value in policy.items() if value is not None})


def avatar_card(children: str | None = "own-host") -> str:
    lines = [
        "---",
        "type: Persona",
        "title: avatar",
        "responsible: human:alice",
        "team: acme-web",
        "capabilities: [read, propose]",
        "harness: claude",
        "model: claude-opus",
        "provider: anthropic",
        "credential_mode: api-key",
    ]
    if children is not None:
        lines += ["avatar:", "  children: %s" % children]
    lines += ["---", "", "# avatar", ""]
    return "\n".join(lines)


class ChildrenPlacementTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "hotes/enfants.md", shared_children_host())
        write(self.root, "agents/avatar.md", avatar_card())
        self.write_placement(["atelier", "enfants"])
        self.sync_both()
        # #7200 : liaison écrite depuis atelier pour l'hôte enfants ; l'UID
        # 52001 est celui que le passwd de la cible rend (jamais celui de
        # l'appelant), il est donc fourni explicitement.
        self.binding = tenancy.register(
            self.db, "enfants", ALICE, actor=ALICE, local_host="atelier",
            uid=52001, user="ameesh-alice")

    # -- fixtures ---------------------------------------------------------
    def write_placement(self, hosts: list[str]) -> None:
        write(self.root, "placements/avatar.md", fiche(
            type="Placement", title="avatar@lieux", agent="avatar",
            hosts=hosts, credential_mode="api-key"))

    def sync_both(self) -> None:
        loaded = self.load()
        findings = canon.validate(loaded)
        self.assertEqual(codes(findings, canon.ERROR), set(), findings)
        canon_sync.sync(self.db, loaded, "atelier")
        canon_sync.sync(self.db, self.load(), "enfants")

    def spawn(self, name: str = "enfant1", **kwargs):
        return canon_sync.spawn(self.db, name, "avatar", 600,
                                canon=self.load(), **kwargs)

    # -- placement propre -------------------------------------------------
    def test_enfant_sur_un_autre_hote_avec_le_runner_du_tenant(self):
        row = self.spawn()
        self.assertEqual(row["host"], "enfants")
        self.assertTrue(row["placement_ok"], row["placement_diagnostic"])
        placed = row["placement"]
        self.assertEqual(placed["tenant"], ALICE)
        self.assertEqual(placed["uid"], 52001)
        self.assertEqual(placed["runner"], "enfants/alice")
        self.assertEqual(placed["cwd"], "/home/ameesh-alice/enfant1")
        # l'hôte du parent (atelier) est le premier de l'admission : jamais pris
        self.assertNotEqual(row["host"], self.row("avatar")["host"])
        full = self.row("enfant1")
        self.assertEqual(full["admitted_hosts"], ["enfants"])
        self.assertEqual(full["responsible"], ALICE)
        self.assertEqual(full["credential_mode"], "api-key")
        profile = self.db.query(
            "SELECT placement_profile FROM agent_registry WHERE name = %s",
            ("enfant1",))[0]["placement_profile"]
        self.assertIsNotNone(profile)
        self.assertIn("enfants", profile)
        # l'UID de l'enfant est celui de la liaison du tenant, pas celui du
        # processus qui a lancé le spawn
        self.assertNotEqual(placed["uid"], os.getuid())

    def test_le_runner_du_parent_ne_prend_pas_le_bail(self):
        self.spawn()
        self.assertIsNone(registry.claim(self.db, "enfant1", "atelier/alice", 600))
        self.assertIsNone(registry.claim(self.db, "enfant1", "runner-atelier", 600))
        lease = registry.claim(self.db, "enfant1", "enfants/alice", 600)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["lease_owner"], "enfants/alice")

    def test_enfant_sur_son_propre_dossier_pas_celui_du_parent(self):
        self.spawn()
        child = self.row("enfant1")
        parent = self.row("avatar")
        self.assertNotEqual(child["cwd"], parent["cwd"])
        self.assertTrue(child["cwd"].startswith(tenancy.tenant_home(ALICE) + "/"))

    def test_petit_enfant_ne_revient_pas_sur_un_hote_ancetre(self):
        self.spawn()
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "petit1", "enfant1", 600,
                             canon=self.load())
        message = str(ctx.exception)
        self.assertIn("spawn-no-placement", message)
        self.assertIn("atelier", message)
        self.assertIn("enfants", message)
        self.assertIsNone(self.row("petit1"))

    # -- le sync de l'hôte d'arrivée ne ferme pas le placement propre -------
    def test_sync_de_l_hote_ne_ferme_pas_le_placement_propre(self):
        self.spawn()
        report = canon_sync.sync(self.db, self.load(), "enfants")
        self.assertEqual(report.status, canon_sync.CANON_OK, report.diagnostic)
        full = self.row("enfant1")
        self.assertEqual(full["host"], "enfants")
        self.assertEqual(full["admitted_hosts"], ["enfants"])
        placement = self.db.query(
            "SELECT placement_ok, placement_diagnostic FROM agent_registry "
            "WHERE name = %s", ("enfant1",))[0]
        self.assertTrue(placement["placement_ok"], placement["placement_diagnostic"])
        lease = registry.claim(self.db, "enfant1", "enfants/alice", 600)
        self.assertIsNotNone(lease)

    # -- profil réellement inséré ------------------------------------------
    def test_parent_au_profil_divergent_refuse(self):
        # la fiche de l'avatar (canon) sera évaluée avec claude-opus ; la
        # ligne du parent porte un autre modèle : profil jamais évalué
        self.db.execute(
            "UPDATE agent_registry SET model = 'claude-haiku-20251001' "
            "WHERE name = 'avatar'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        message = str(ctx.exception)
        self.assertIn("spawn-profile-diverged", message)
        self.assertIn("claude-opus", message)
        self.assertIsNone(self.row("enfant2"))

    def test_profil_modifie_entre_evaluation_et_insertion_refuse(self):
        # course : le parent change après l'évaluation, avant l'INSERT ; la
        # garde de l'INSERT doit refuser de copier un profil non évalué.
        original = avatar_children.place_child

        def raced(*args, **kwargs):
            placement = original(*args, **kwargs)
            self.db.execute(
                "UPDATE agent_registry SET provider = 'openai' "
                "WHERE name = 'avatar'")
            return placement

        with mock.patch.object(avatar_children, "place_child", side_effect=raced):
            with self.assertRaises(canon_sync.SpawnError) as ctx:
                self.spawn("enfant2")
        self.assertIn("changed during creation", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    def test_parent_deplace_sur_l_hote_choisi_entre_evaluation_et_insertion_refuse(self):
        original = avatar_children.place_child

        def raced(*args, **kwargs):
            placement = original(*args, **kwargs)
            self.db.execute(
                "UPDATE agent_registry SET host = 'enfants' WHERE name = 'avatar'")
            return placement

        with mock.patch.object(avatar_children, "place_child", side_effect=raced):
            with self.assertRaises(canon_sync.SpawnError) as ctx:
                self.spawn("enfant2")
        self.assertIn("changed during creation", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    def test_responsable_change_entre_evaluation_et_insertion_refuse(self):
        original = avatar_children.place_child

        def raced(*args, **kwargs):
            placement = original(*args, **kwargs)
            self.db.execute(
                "UPDATE agent_registry SET responsible = 'human:bob' "
                "WHERE name = 'avatar'")
            return placement

        with mock.patch.object(avatar_children, "place_child", side_effect=raced):
            with self.assertRaises(canon_sync.SpawnError) as ctx:
                self.spawn("enfant2")
        self.assertIn("changed during creation", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    # -- lignée incomplète : refus, jamais de repli ------------------------
    def test_lignee_avec_ancetre_absent_refuse(self):
        self.spawn()
        self.db.execute(
            "UPDATE agent_registry SET created_by = 'fantome' WHERE name = 'enfant1'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "petit1", "enfant1", 600, canon=self.load())
        self.assertIn("spawn-lineage-broken", str(ctx.exception))
        self.assertIsNone(self.row("petit1"))

    def test_lignee_en_cycle_refuse(self):
        self.spawn()
        self.db.execute(
            "UPDATE agent_registry SET created_by = 'enfant1' WHERE name = 'enfant1'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "petit1", "enfant1", 600, canon=self.load())
        self.assertIn("spawn-lineage-broken", str(ctx.exception))
        self.assertIsNone(self.row("petit1"))

    def test_lignee_trop_profonde_refuse(self):
        self.db.execute(
            """
            INSERT INTO agent_registry
                (name, harness, host, cwd, responsible, team, ephemeral,
                 ephemeral_expires_at, created_by, status)
            SELECT 'profond' || i, 'claude', 'enfants', '/tmp/profond' || i,
                   %s, 'acme-web', true, now() + interval '1 hour',
                   CASE WHEN i = 1 THEN 'avatar' ELSE 'profond' || (i - 1) END,
                   'idle'
              FROM generate_series(1, 33) AS i
            """, (ALICE,))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "profond34", "profond33", 600,
                             canon=self.load())
        self.assertIn("spawn-lineage-broken", str(ctx.exception))
        self.assertIsNone(self.row("profond34"))

    # -- refus ------------------------------------------------------------
    def test_children_none_refuse(self):
        write(self.root, "agents/avatar.md", avatar_card("none"))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        self.assertIn("spawn-forbidden", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    def test_children_pod_hors_lot_refuse(self):
        write(self.root, "agents/avatar.md", avatar_card("pod"))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        self.assertIn("spawn-pod-unsupported", str(ctx.exception))
        self.assertIn("B5b", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    def test_children_illisible_refuse(self):
        write(self.root, "agents/avatar.md", avatar_card("dedie"))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        self.assertIn("spawn-policy-invalid", str(ctx.exception))

    def test_sans_autre_hote_refus_nomme_l_hote_a_provisionner(self):
        self.write_placement(["atelier"])
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        message = str(ctx.exception)
        self.assertIn("spawn-no-placement", message)
        self.assertIn("atelier", message)
        self.assertIn("Placement", message)
        self.assertIn("owner spending decision", message)
        self.assertIsNone(self.row("enfant2"))

    def test_liaison_de_tenant_absente_refus_nomme_le_geste(self):
        storage.of(self.db).tenancy.unbind("enfants", ALICE)
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        message = str(ctx.exception)
        self.assertIn("spawn-no-placement", message)
        self.assertIn("enfants", message)
        self.assertIn("ameesh host tenant add human:alice --host enfants --register",
                      message)

    def test_dossier_par_enfant_absent_refuse(self):
        write(self.root, "hotes/enfants.md", shared_children_host(
            workspaces="{roots: {acme-web: /srv/acme/acme-web}}"))
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2")
        message = str(ctx.exception)
        self.assertIn("spawn-no-placement", message)
        self.assertIn("no per-child work directory", message)
        self.assertIn("enfants", message)

    def test_cwd_explicite_refuse(self):
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn("enfant2", cwd=os.path.join(tempfile.gettempdir(), "partage"))
        self.assertIn("spawn-cwd-refused", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    def test_canon_illisible_ne_retombe_pas_sur_le_parent(self):
        # la CLI passe toujours la liste des canons lus ; si elle est vide et
        # que la racine est gouvernée, spawn refuse au lieu de replier
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            canon_sync.spawn(self.db, "enfant2", "avatar", 600, canon=[])
        self.assertIn("spawn-canon-unreadable", str(ctx.exception))
        self.assertIsNone(self.row("enfant2"))

    # -- pas de régression -------------------------------------------------
    def test_agent_sans_bloc_avatar_garde_son_hote(self):
        row = canon_sync.spawn(self.db, "aide1", "orchestre", 600,
                               canon=self.load())
        self.assertEqual(row["host"], "atelier")
        self.assertNotIn("placement", row)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
