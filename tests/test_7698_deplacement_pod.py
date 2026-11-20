# SPDX-License-Identifier: AGPL-3.0-only
"""#7698 (P5 de #7633) : déplacement poste ↔ pod partagé ↔ pod dédié.

Design `docs/design/7633-hote-avatars-pod.md` §9 :

* la présence d'un hôte pod exige le couple **superviseur frais + battement du
  runner du tenant**, avec l'UID du processus vérifié contre `host_tenants`
  et, pour un hôte enfant, le UID Kubernetes de `pod_hosts` ;
* `agent move` vers un pod refuse un hôte enfant (`pod_hosts`), un template,
  une carte/solation inconnue, un passwd ou un UID hors du plan, et attend la
  présence et le profil de compte (gardes de #7108) ;
* pod partagé ↔ pod dédié : même UID de personne = PVC ré-attachable
  (`pvc: reattach`) ; UID différent = re-login sur un PVC neuf (`pvc:
  relogin`) ; poste → pod : aucun volume de pod à déplacer (`pvc: none`) ;
* la conversation suit par `conversation_moves`, jamais par une session ou un
  fichier d'identifiant copié.

Les contrôles `kind` (ré-attachement réel du PVC, identité après déplacement)
sont dans `deploy/k8s/pod-witness/` ; `WitnessMoveBaseTest` est le contrôle
base exécuté par ce témoin.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest
import uuid
from unittest import mock

from ameesh import (conversation_move, election, pod_move, registry,
                    relocation, storage, tenancy)

from . import test_canon
from .test_canon import fiche, write
from .test_placement import host_card
from .test_7694_pod_socle import pod_card

ALICE, BRUNO = "human:alice", "human:bruno"
AVATAR = "alice-avatar"
POSTE = "poste"
POD_PARTAGE = "pod-partage"
POD_DEDIE = "pod-dedie"
POD_ENFANT = "pod-enfant-7698"


def _tables_reset(db) -> None:
    # FK order: handoff events before handoffs, handoffs before the registry.
    for table in ("host_handoff_events", "host_handoffs", "conversation_moves",
                  "agent_candidates", "agent_registry", "runner_presence",
                  "pod_hosts", "pod_supervisor_leases",
                  "host_tenants", "host_tenancy", "role_standings",
                  "host_hold_events", "host_holds", "host_lifecycle",
                  "host_resources", "canon_state"):
        db.execute("DELETE FROM " + table)


class _PodMoveCase(test_canon._CanonDbCase):
    """Un avatar placé sur un poste, un pod partagé et un pod dédié."""

    def setUp(self) -> None:
        super().setUp()
        _tables_reset(self.db)
        # les guards pod lisent `policy.runtime` dans le canon temporaire.
        self.cfg = dataclasses.replace(
            self.cfg, canon=self.root, canon_untrusted=True, host=POSTE)
        write(self.root, "hotes/poste.md", host_card(
            POSTE, ALICE,
            harnesses=["claude", "codex"],
            providers=["anthropic", "openai"],
            credential_modes=["api-key"],
            workspaces="{roots: {acme-web: ~/acme/acme-web}}",
            max_agents="4"))
        write(self.root, "hotes/pod-partage.md",
              pod_card(POD_PARTAGE, tenants=(ALICE, BRUNO)))
        write(self.root, "hotes/pod-dedie.md",
              pod_card(POD_DEDIE, tenants=(ALICE,)))
        write(self.root, "agents/alice-avatar.md", fiche(
            type="Agent", title=AVATAR, responsible=ALICE, team="acme-web",
            capabilities=["read", "report-drift"], harness="codex",
            provider="openai", credential_mode="api-key", releve="preference"))
        write(self.root, "placements/alice-avatar.md", fiche(
            type="Placement", title=AVATAR, agent=AVATAR,
            hosts=[POSTE, POD_PARTAGE, POD_DEDIE], credential_mode="api-key"))
        self.sync(POSTE)
        self.sync(POD_PARTAGE)
        self.sync(POD_DEDIE)
        self.assertEqual(self.row(AVATAR)["host"], POSTE)

    # -- fixtures ----------------------------------------------------------
    def source(self) -> dict:
        """Un bail source vivant et une session native sur le poste."""
        self.db.execute(
            "UPDATE agent_registry SET session_id = 'sess-source',"
            " lease_owner = 'runner-source', lease_epoch = 3,"
            " lease_expires_at = clock_timestamp() + interval '5 minutes',"
            " status = 'idle', status_text = '' WHERE name = %s", (AVATAR,))
        return self.row(AVATAR)

    def _standing(self, host: str) -> None:
        self.db.execute(
            "INSERT INTO role_standings (name, host, evaluated_at, availability,"
            " capacity, score, ready, diagnostic)"
            " VALUES (%s, %s, clock_timestamp(), 99.0, 0.5, 99.5, true, '')",
            (AVATAR, host))

    def _reading(self, host: str, *, pod: bool) -> None:
        runner = ("pod-supervisor/%s" % host) if pod else None
        if pod:
            epoch = storage.of(self.db).pod_supervisor.acquire(host, runner, 300)
            self.assertIsNotNone(epoch)
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, load1, cpu_count,"
            " mem_available_bytes, turns_in_progress, runner_id)"
            " VALUES (%s, clock_timestamp(), %s, 4, %s, 0, %s)",
            (host, None if pod else 0.1, 8 * 1024 ** 3, runner))

    def ready_station(self, host: str = POSTE) -> None:
        """Un poste présent : relevé, verdict local, canevas synchrone."""
        self.db.execute("DELETE FROM host_resources WHERE host = %s", (host,))
        self.db.execute("DELETE FROM role_standings WHERE name = %s AND host = %s",
                        (AVATAR, host))
        self._reading(host, pod=False)
        self._standing(host)
        self.db.execute("DELETE FROM host_lifecycle WHERE host = %s", (host,))
        self.db.execute("DELETE FROM host_holds WHERE host = %s", (host,))

    def bind_tenant(self, host: str, tenant: str = ALICE,
                    uid: int | None = None) -> dict:
        uid = tenancy.pod_uid(tenant) if uid is None else int(uid)
        return storage.of(self.db).tenancy.bind(
            host, tenant, unix_user=tenancy.unix_user(tenant), uid=uid,
            runner_id=tenancy.runner_id(host, tenant), actor=ALICE)

    def beat(self, host: str, tenant: str = ALICE, *, uid: int | None = None,
             profiles: list[str] | None = None,
             pod_uid: str | None = None) -> None:
        uid = (tenancy.pod_uid(tenant) if uid is None else int(uid))
        cfg = dataclasses.replace(
            self.cfg, host=host, tenant=tenant,
            runner_id=tenancy.runner_id(host, tenant),
            accounts={"codex": [{"name": "org", "type": "config_dir",
                                 "path": self.tmp + "/codex-org"}]},
            pod_uid=pod_uid)
        # #7694 : le battement vérifie l'UID du processus contre la liaison.
        with mock.patch.object(tenancy.os, "getuid", return_value=uid):
            self.assertTrue(tenancy.beat(cfg, self.db, version="1.60.0"))
        if profiles is not None:
            self.db.execute(
                "UPDATE runner_presence SET profiles = %s WHERE host = %s"
                " AND tenant = %s", (profiles, host, tenant))

    def ready_pod(self, host: str = POD_PARTAGE, *, uid: int | None = None,
                  profiles: list[str] | None = None) -> int:
        """Un hôte pod présent : bail + relevé du superviseur, battement."""
        self.db.execute("DELETE FROM host_resources WHERE host = %s", (host,))
        self.db.execute("DELETE FROM role_standings WHERE name = %s AND host = %s",
                        (AVATAR, host))
        self.db.execute("DELETE FROM pod_supervisor_leases WHERE host = %s", (host,))
        self._reading(host, pod=True)
        self._standing(host)
        self.db.execute("DELETE FROM host_lifecycle WHERE host = %s", (host,))
        self.db.execute("DELETE FROM host_holds WHERE host = %s", (host,))
        bound = self.bind_tenant(host, ALICE, uid=uid)
        self.beat(host, ALICE, uid=bound["uid"], profiles=profiles)
        return int(bound["uid"])

    def close_move(self, result: dict) -> None:
        """La cible a fait son premier tour : la tentative est vérifiée."""
        self.db.execute(
            "UPDATE conversation_moves SET state = 'verified', target_session = 'tgt',"
            " target_owner = 'target', target_epoch = %s, digest = %s"
            " WHERE move_id = %s",
            (self.row(AVATAR)["lease_epoch"], "a" * 64, result["conversation_move"]))

    def moved_events(self) -> list[dict]:
        rows = self.db.query(
            "SELECT data FROM ameesh_events WHERE type = 'agent.moved'"
            " AND object = %s ORDER BY id", ("agent:" + AVATAR,))
        return [row["data"] for row in rows]

    def refused(self, pattern: str, target: str) -> None:
        before = self.row(AVATAR)
        events = len(self.moved_events())
        with self.assertRaisesRegex(relocation.MoveError, pattern):
            relocation.move_manual(self.cfg, self.db, AVATAR, target, actor=ALICE)
        self.assertEqual(self.row(AVATAR), before)
        self.assertEqual(len(self.moved_events()), events)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))


class PodPresenceTest(_PodMoveCase):
    """La présence d'un hôte pod dans `candidate_state` (#7698)."""

    def candidate_state(self, host: str = POD_PARTAGE) -> dict:
        return election.candidate_state(self.db, self.row(AVATAR), host)

    def test_le_couple_superviseur_et_battement_rend_present(self):
        self.ready_pod()
        state = self.candidate_state()
        self.assertTrue(state["ready"], state["diagnostic"])
        self.assertEqual(state["diagnostic"], "")
        presence = storage.of(self.db).tenancy.presence(POD_PARTAGE, ALICE)
        self.assertEqual(int(presence["uid"]), int(presence["binding_uid"]))

    def test_un_superviseur_silencieux_ferme_la_presence(self):
        # Le battement du tenant est frais, mais le superviseur n'écrit plus :
        # le relevé est périmé et l'hôte n'est plus présent.
        self.ready_pod()
        self.db.execute(
            "UPDATE host_resources SET sampled_at = clock_timestamp()"
            " - interval '1 hour' WHERE host = %s", (POD_PARTAGE,))
        state = self.candidate_state()
        self.assertFalse(state["ready"])
        self.assertIn("stale resource reading", state["diagnostic"])

    def test_un_battement_silencieux_ferme_la_presence(self):
        # Le superviseur est frais, mais le runner du tenant ne bat plus.
        self.ready_pod()
        self.db.execute(
            "UPDATE runner_presence SET seen_at = clock_timestamp()"
            " - interval '1 hour' WHERE host = %s AND tenant = %s",
            (POD_PARTAGE, ALICE))
        state = self.candidate_state()
        self.assertFalse(state["ready"])
        self.assertIn("silent", state["diagnostic"])

    def test_un_uid_observe_divergent_ferme_la_presence(self):
        # La liaison a changé (re-liaison, chart) sans nouveau battement :
        # l'ancien UID ne rend plus l'hôte présent.
        self.ready_pod()
        self.db.execute(
            "UPDATE runner_presence SET uid = 9999"
            " WHERE host = %s AND tenant = %s", (POD_PARTAGE, ALICE))
        state = self.candidate_state()
        self.assertFalse(state["ready"])
        self.assertIn("UID not verified", state["diagnostic"])

    def test_un_hote_enfant_exige_le_pod_uid_provisionne(self):
        # Un hôte enfant n'est présent que si le pod est encore celui de
        # `pod_hosts` : un pod recréé a un UID neuf, l'ancien battement ne vaut
        # plus (fail closed, comme la garde de réclamation).
        self.db.execute(
            "INSERT INTO pod_hosts (host, namespace, template, parent, tenant,"
            " uid, runner_id, pod_uid, state, expires_at)"
            " VALUES (%s, 'ns', 'ameesh-child', %s, %s, %s, %s, 'uid-pod-1',"
            " 'running', clock_timestamp() + interval '1 hour')",
            (POD_ENFANT, AVATAR, ALICE, tenancy.pod_uid(ALICE),
             tenancy.runner_id(POD_ENFANT, ALICE)))
        self.db.execute(
            "INSERT INTO host_tenancy (host, canon, shared, tenants)"
            " VALUES (%s, 'test', true, ARRAY[%s])", (POD_ENFANT, ALICE))
        self.bind_tenant(POD_ENFANT, ALICE)
        self.beat(POD_ENFANT, ALICE, pod_uid="uid-pod-1")
        now = storage.of(self.db).tenancy.presence(POD_ENFANT, ALICE)["now_ts"]
        role = self.row(AVATAR)
        self.assertEqual(tenancy.presence_reason(
            self.db, role, POD_ENFANT, now=now, limit_s=180), "")
        # le pod est recréé : `pod_hosts` porte un UID neuf
        self.db.execute("UPDATE pod_hosts SET pod_uid = 'uid-pod-2' WHERE host = %s",
                        (POD_ENFANT,))
        reason = tenancy.presence_reason(
            self.db, role, POD_ENFANT, now=now, limit_s=180)
        self.assertIn("pod-host-unknown", reason)


class PodMovePreflightTest(_PodMoveCase):
    """Les gardes pod de `agent move`, avant tout effet."""

    def setUp(self) -> None:
        super().setUp()
        self.source()

    def test_cible_enfant_refusee_avant_effet(self):
        self.ready_station()
        self.db.execute(
            "INSERT INTO pod_hosts (host, namespace, template, parent, tenant,"
            " uid, runner_id, pod_uid, state, expires_at)"
            " VALUES (%s, 'ns', 'ameesh-child', 'parent', %s, %s, %s, 'uid-1',"
            " 'running', clock_timestamp() + interval '1 hour')",
            (POD_ENFANT, ALICE, tenancy.pod_uid(ALICE),
             tenancy.runner_id(POD_ENFANT, ALICE)))
        self.db.execute(
            "UPDATE agent_registry SET admitted_hosts ="
            " ARRAY[%s, %s, %s, %s] WHERE name = %s",
            (POSTE, POD_PARTAGE, POD_DEDIE, POD_ENFANT, AVATAR))
        self.refused("move-pod-child-target", POD_ENFANT)

    def test_cible_template_refusee_avant_effet(self):
        template = "pod-template-7698"
        write(self.root, "hotes/pod-template.md",
              pod_card(template, kind="pod-template", tenants=(ALICE,),
                       max_agents=1, volatile=True,
                       workload="statefulset/ameesh-child"))
        self.sync(POSTE)
        self.ready_station()
        self.db.execute(
            "UPDATE agent_registry SET admitted_hosts = ARRAY[%s, %s]"
            " WHERE name = %s", (POSTE, template, AVATAR))
        self.refused("move-pod-template", template)

    def test_carte_pod_disparue_refusee_quand_le_bail_existe(self):
        # Une carte retirée du canon ne doit pas transformer un pod en hôte
        # classique : le bail de superviseur reste la trace du runtime.
        self.db.execute(
            "INSERT INTO pod_supervisor_leases (host, runner_id, epoch, expires_at)"
            " VALUES (%s, 'pod-supervisor/x', 1,"
            " clock_timestamp() + interval '1 hour')", (POD_PARTAGE,))
        write(self.root, "hotes/pod-partage.md", host_card(
            POD_PARTAGE, ALICE, harnesses=["claude", "codex"],
            providers=["anthropic", "openai"], credential_modes=["api-key"],
            workspaces="{root: ~/work}", max_agents="6", shared="true",
            tenants=[ALICE, BRUNO]))
        self.sync(POSTE)
        self.ready_station()
        self.refused("move-pod-runtime", POD_PARTAGE)

    def test_presence_absente_refusee_avant_effet(self):
        # liaison présente (le conteneur est provisionné) mais aucun runner du
        # tenant ne bat : la cible n'est pas présente.
        self.ready_station()
        self.bind_tenant(POD_PARTAGE, ALICE)
        self.refused("move-presence", POD_PARTAGE)

    def test_liaison_absente_refusee_avant_effet(self):
        self.ready_station()
        self.refused("move-tenant-missing", POD_PARTAGE)

    def test_compte_non_publie_refuse_avant_effet(self):
        self.ready_pod(profiles=[])
        self.refused("move-account-missing", POD_PARTAGE)

    def test_uid_observe_divergent_refuse_avant_effet(self):
        self.ready_pod()
        self.db.execute(
            "UPDATE runner_presence SET uid = 9999"
            " WHERE host = %s AND tenant = %s", (POD_PARTAGE, ALICE))
        self.refused("move-pod-uid", POD_PARTAGE)

    def test_liaison_passwd_hors_plan_refusee(self):
        self.ready_pod()
        self.db.execute(
            "UPDATE host_tenants SET unix_user = 'ameesh-other'"
            " WHERE host = %s AND tenant = %s", (POD_PARTAGE, ALICE))
        self.refused("move-pod-passwd", POD_PARTAGE)

    def test_uid_reserve_refuse(self):
        uid = tenancy.POD_SUPERVISOR_UID
        self.db.execute(
            "UPDATE host_tenants SET uid = %s WHERE host = %s AND tenant = %s",
            (uid, POD_PARTAGE, ALICE))
        self.ready_pod(uid=uid)
        self.refused("move-pod-uid", POD_PARTAGE)

    def test_plan_pod_change_sous_le_verrou_est_refuse(self):
        # Entre le préflight et l'écriture verrouillée, la personne est
        # re-liée avec un autre UID (re-login) : le plan change, rien n'est
        # écrit.
        self.ready_pod()
        before = self.row(AVATAR)
        original = conversation_move.manual
        other = tenancy.pod_uid(ALICE) + 1

        def rebind(db, agent, target_host, target_cwd, **kwargs):
            db.execute(
                "UPDATE host_tenants SET uid = %s WHERE host = %s AND tenant = %s",
                (other, POD_PARTAGE, ALICE))
            db.execute(
                "UPDATE runner_presence SET uid = %s WHERE host = %s AND tenant = %s",
                (other, POD_PARTAGE, ALICE))
            return original(db, agent, target_host, target_cwd, **kwargs)

        with mock.patch.object(conversation_move, "manual", side_effect=rebind):
            with self.assertRaisesRegex(relocation.MoveError, "move-pod-changed"):
                relocation.move_manual(self.cfg, self.db, AVATAR, POD_PARTAGE,
                                       actor=ALICE)
        self.assertEqual(self.row(AVATAR), before)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))

    def test_json_cli_reporte_le_plan_pod(self):
        self.ready_pod()
        env = self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                       AMEESH_HOST=POSTE)
        proc = self.mesh("agent", "move", AVATAR, "--to", POD_PARTAGE,
                         "--actor", ALICE, "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["pod"]["kind"], "pod")
        self.assertEqual(payload["pod"]["uid"], tenancy.pod_uid(ALICE))
        self.assertEqual(payload["pod"]["pvc"], "none")
        self.assertEqual(payload["pod"]["tenant"], ALICE)
        self.assertEqual(payload["pod"]["source_kind"], "host")
        event = self.moved_events()[-1]
        self.assertEqual(event["pod"]["uid"], tenancy.pod_uid(ALICE))


class MoveToPodTest(_PodMoveCase):
    """Déplacements réels et conversation : poste, pod partagé, pod dédié."""

    def setUp(self) -> None:
        super().setUp()
        self.source()

    def test_poste_vers_pod_et_retour(self):
        self.ready_pod()
        self.ready_station()
        result = relocation.move_manual(self.cfg, self.db, AVATAR, POD_PARTAGE,
                                        actor=ALICE)
        row = self.row(AVATAR)
        self.assertEqual((row["host"], row["lease_owner"]), (POD_PARTAGE, None))
        self.assertEqual(row["lease_epoch"], 4)
        self.assertEqual(row["session_id"], "sess-source")  # provisoire
        self.assertIn("/work/acme-web", row["cwd"], row["cwd"])
        self.assertEqual(result["pod"]["pvc"], "none")
        self.assertEqual(result["pod"]["source_kind"], "host")
        self.assertEqual(result["pod"]["account_isolation"], "per-tenant-volume")
        move = storage.of(self.db).conversation_moves.get(result["conversation_move"])
        self.assertEqual((move["source_host"], move["target_host"]),
                         (POSTE, POD_PARTAGE))
        self.assertEqual(move["reason"], "manual")
        self.assertIsNone(move["target_session"])  # aucun fichier copié
        # seul le runner du tenant peut réclamer sur le pod
        self.assertIsNotNone(registry.claim(
            self.db, AVATAR, tenancy.runner_id(POD_PARTAGE, ALICE), 120))
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL,"
                        " lease_expires_at = NULL WHERE name = %s", (AVATAR,))
        self.assertIsNone(registry.claim(
            self.db, AVATAR, "pod-supervisor/" + POD_PARTAGE, 120))

        # retour au poste : la conversation suit, aucun plan pod
        self.close_move(result)
        self.ready_station()
        back = relocation.move_manual(self.cfg, self.db, AVATAR, POSTE, actor=ALICE)
        self.assertNotIn("pod", back)
        self.assertEqual(self.row(AVATAR)["host"], POSTE)
        move = storage.of(self.db).conversation_moves.get(back["conversation_move"])
        self.assertEqual((move["source_host"], move["target_host"]),
                         (POD_PARTAGE, POSTE))
        # le premier tour du poste vérifie la tentative, puis plus rien n'est ouvert
        self.close_move(back)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))

    def move_to_shared(self) -> None:
        """Poste → pod partagé, puis premier tour vérifié sur le pod."""
        self.ready_pod(POD_PARTAGE)
        first = relocation.move_manual(self.cfg, self.db, AVATAR,
                                       POD_PARTAGE, actor=ALICE)
        self.close_move(first)

    def test_pod_partage_vers_pod_dedie_reattache_le_pvc(self):
        self.move_to_shared()
        self.ready_pod(POD_DEDIE)
        result = relocation.move_manual(self.cfg, self.db, AVATAR, POD_DEDIE,
                                        actor=ALICE)
        plan = result["pod"]
        self.assertEqual(plan["pvc"], "reattach")
        self.assertEqual(plan["source_kind"], "pod")
        self.assertEqual(plan["source_uid"], plan["uid"])
        self.assertEqual(plan["uid"], tenancy.pod_uid(ALICE))
        self.assertEqual(self.moved_events()[-1]["pod"], plan)
        self.assertEqual(self.row(AVATAR)["host"], POD_DEDIE)
        # le retour est symétrique
        self.close_move(result)
        self.ready_pod(POD_PARTAGE)
        back = relocation.move_manual(self.cfg, self.db, AVATAR, POD_PARTAGE,
                                      actor=ALICE)
        self.assertEqual(back["pod"]["pvc"], "reattach")

    def test_pod_partage_vers_pod_dedie_relogin(self):
        # D8 non tranché : repli UID par hôte. La personne garde son PVC sur
        # l'ancien pod ; la cible reçoit un PVC neuf et un UID différent : le
        # volume ne peut pas être ré-attaché, le compte doit être refait.
        self.move_to_shared()
        other = tenancy.pod_uid(ALICE) + 7
        self.ready_pod(POD_DEDIE, uid=other)
        result = relocation.move_manual(self.cfg, self.db, AVATAR, POD_DEDIE,
                                        actor=ALICE)
        self.assertEqual(result["pod"]["pvc"], "relogin")
        self.assertEqual(result["pod"]["source_uid"], tenancy.pod_uid(ALICE))
        self.assertEqual(result["pod"]["uid"], other)
        self.assertEqual(self.row(AVATAR)["host"], POD_DEDIE)

    def test_pod_vers_poste(self):
        self.ready_pod(POD_PARTAGE)
        self.source()
        first = relocation.move_manual(self.cfg, self.db, AVATAR, POD_PARTAGE,
                                       actor=ALICE)
        self.close_move(first)
        self.ready_station()
        back = relocation.move_manual(self.cfg, self.db, AVATAR, POSTE, actor=ALICE)
        self.assertNotIn("pod", back)
        self.assertEqual(self.row(AVATAR)["host"], POSTE)

    def test_aucune_session_ni_credential_copie(self):
        self.ready_pod()
        source_dir = self.tmp + "/source-state"
        os.makedirs(source_dir, exist_ok=True)
        session_file = source_dir + "/native-session.jsonl"
        credential_file = source_dir + "/credentials.json"
        with open(session_file, "w", encoding="utf-8") as handle:
            handle.write("native transcript stays here\n")
        with open(credential_file, "w", encoding="utf-8") as handle:
            handle.write('{"secret": "stays here"}\n')
        with mock.patch("shutil.copyfile") as copyfile, \
                mock.patch("shutil.copy2") as copy2, \
                mock.patch("shutil.copytree") as copytree:
            result = relocation.move_manual(self.cfg, self.db, AVATAR,
                                            POD_PARTAGE, actor=ALICE)
        copyfile.assert_not_called()
        copy2.assert_not_called()
        copytree.assert_not_called()
        with open(session_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "native transcript stays here\n")
        with open(credential_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"secret": "stays here"}\n')
        event = self.moved_events()[-1]
        self.assertFalse(event["session_copied"])
        self.assertFalse(event["credential_copied"])
        self.assertIsNone(
            storage.of(self.db).conversation_moves.get(
                result["conversation_move"])["target_session"])


class WitnessMoveBaseTest(_PodMoveCase):
    """Contrôle base du témoin `kind` (#7698) : présence + préflight pod.

    Exécuté par `deploy/k8s/pod-witness/preuve-isolation-comptes.sh` avec un
    PostgreSQL migré, à côté du ré-attachement PVC du même témoin."""

    def test_controle_p5_presence_et_preflight(self):
        self.source()
        uid = self.ready_pod()
        state = election.candidate_state(self.db, self.row(AVATAR), POD_PARTAGE)
        self.assertTrue(state["ready"], state["diagnostic"])
        plan = pod_move.preflight(self.cfg, self.db, self.row(AVATAR), POD_PARTAGE)
        self.assertEqual((plan["kind"], plan["uid"], plan["pvc"]),
                         ("pod", uid, "none"))
        self.db.execute(
            "INSERT INTO pod_hosts (host, namespace, template, parent, tenant,"
            " uid, runner_id, pod_uid, state, expires_at)"
            " VALUES (%s, 'ns', 'ameesh-child', 'parent', %s, %s, %s, 'uid-1',"
            " 'running', clock_timestamp() + interval '1 hour')",
            (POD_ENFANT, ALICE, tenancy.pod_uid(ALICE),
             tenancy.runner_id(POD_ENFANT, ALICE)))
        with self.assertRaisesRegex(pod_move.PodMoveError,
                                    "move-pod-child-target"):
            pod_move.preflight(self.cfg, self.db, self.row(AVATAR), POD_ENFANT)


if __name__ == "__main__":
    unittest.main()
