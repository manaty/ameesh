# SPDX-License-Identifier: AGPL-3.0-only
"""#7697 (P4 de #7633) : clone en pod.

Design `docs/design/7633-hote-avatars-pod.md` §8. Les témoins couvrent :

* le plan d'un enfant pod : hôte stable, UID 30000+, `emptyDir`, aucun PVC ni
  volume du parent, `passwd` propre, Secret d'API par enfant ;
* le template figé : validation, rendu, refus d'un hostPath/PVC, d'un
  conteneur privilégié, d'un placeholder inconnu ou d'un UID de superviseur ;
* la demande écrite avec l'enfant dans la même transaction, le démarrage par
  le pourvoyeur, l'écriture `pod_hosts`/`host_tenancy`/`host_tenants`, le
  battement `pod_uid`, la garde de réclamation et le nettoyage TTL ;
* les refus : sans template/pourvoyeur vivant (`spawn-pod-unsupported`), avec
  un parent en abonnement (`spawn-pod-credential`), au-delà du délai de
  démarrage (`spawn-pod-start-timeout`) ;
* les manifestes RBAC/VAP/quota : aucun Secret, aucun `pods/exec`, un
  formulaire de pod contraint.

Les contrôles `kind` réels restent un geste de bureau (aucun script kind
n'est livré par ce lot) ; ces tests ne parlent à aucun cluster.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from ameesh import (avatar_children, canon_sync, pod_children, pod_provisioner,
                    registry, storage, tenancy)

from .support import REPO
from .test_7107_avatar_enfants import avatar_card
from .test_canon import _CanonDbCase, fiche, write

ALICE = "human:alice"
PROVISIONER_DIR = os.path.join(REPO, "deploy", "k8s", "pod-provisioner")
TEMPLATE = os.path.join(PROVISIONER_DIR, "template.yaml")


class FakeKubeClient:
    """Un cluster en mémoire : aucune requête HTTP, des UID stables."""

    namespace = "ameesh"

    def __init__(self):
        self.configmaps: dict[str, dict] = {}
        self.pods: dict[str, dict] = {}
        self.deleted_pods: list[str] = []
        self.deleted_configmaps: list[str] = []

    def create_configmap(self, body):
        name = body["metadata"]["name"]
        if name in self.configmaps:
            raise pod_provisioner.KubeConflict("kube-conflict", "exists")
        self.configmaps[name] = copy.deepcopy(body)
        return copy.deepcopy(body)

    def get_configmap(self, name):
        body = self.configmaps.get(name)
        return copy.deepcopy(body) if body is not None else None

    def delete_configmap(self, name):
        self.deleted_configmaps.append(name)
        self.configmaps.pop(name, None)
        return True

    def create_pod(self, body):
        name = body["metadata"]["name"]
        if name in self.pods:
            raise pod_provisioner.KubeConflict("kube-conflict", "exists")
        created = copy.deepcopy(body)
        created.setdefault("metadata", {})["uid"] = "uid-" + name
        created["status"] = {"phase": "Pending"}
        self.pods[name] = created
        return copy.deepcopy(created)

    def get_pod(self, name):
        body = self.pods.get(name)
        return copy.deepcopy(body) if body is not None else None

    def delete_pod(self, name):
        self.deleted_pods.append(name)
        self.pods.pop(name, None)
        return True

    def list_pods(self, label_selector=""):
        return [copy.deepcopy(pod) for pod in self.pods.values()]


@unittest.skipIf(yaml is None, "PyYAML absent : template non vérifié")
class PodChildPlanTest(unittest.TestCase):
    def test_hote_uid_et_secret_sont_propres_a_l_enfant(self):
        host = tenancy.child_host("avatar", "enfant1", "deadbeef")
        self.assertTrue(host.startswith("pod-avatar-enfant1-"))
        self.assertLessEqual(len(host), 63)
        self.assertNotEqual(host, "atelier")
        uid = tenancy.child_uid(ALICE, "enfant1")
        self.assertGreaterEqual(uid, 30000)
        self.assertLessEqual(uid, 60000)
        self.assertNotEqual(uid, tenancy.pod_uid(ALICE))
        self.assertNotEqual(tenancy.child_uid(ALICE, "enfant2"), uid)
        self.assertEqual(tenancy.child_uid(ALICE, "enfant1"), uid)
        self.assertNotEqual(tenancy.child_unix_user(ALICE, "enfant1"),
                            tenancy.child_unix_user(ALICE, "enfant2"))
        secret = tenancy.child_credential_secret("enfant1")
        self.assertTrue(secret.startswith(tenancy.POD_CHILD_SECRET_PREFIX))
        self.assertLessEqual(len(secret), 63)

    def test_plan_sans_pvc_ni_volume_du_parent(self):
        plan = tenancy.child_plan(
            "pod-avatar-enfant1-deadbeef", "enfant1", ALICE,
            parent="avatar", harness="claude")
        self.assertIn(":%d:" % plan["uid"], plan["passwd"])
        self.assertIn(plan["unix_user"], plan["passwd"])
        self.assertNotIn("persistentVolumeClaim", json.dumps(plan["volumes"]))
        self.assertNotIn("hostPath", json.dumps(plan["volumes"]))
        self.assertIn("emptyDir", json.dumps(plan["volumes"]))
        self.assertEqual(plan["config"]["accounts"]["claude"][0]["type"],
                         "api_key_env")
        self.assertNotIn("subscription", json.dumps(plan["config"]))

    def test_un_jeton_trop_court_est_refuse(self):
        with self.assertRaises(tenancy.TenancyError):
            tenancy.child_host("avatar", "enfant1", "a")

    def test_uid_hors_plage_refuse(self):
        with self.assertRaises(tenancy.TenancyError):
            tenancy.check_child_uid(2000)
        with self.assertRaises(tenancy.TenancyError):
            tenancy.check_child_uid(10001)


@unittest.skipIf(yaml is None, "PyYAML absent : template non vérifié")
class PodTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = pod_provisioner.PodTemplate.load(TEMPLATE)
        cls.request = {
            "id": "11111111-2222-3333-4444-555555555555",
            "child": "enfant1", "parent": "avatar", "tenant": ALICE,
            "host": "pod-avatar-enfant1-deadbeef", "template": "ameesh-child",
            "namespace": "ameesh", "harness": "claude",
            "credential_secret": tenancy.child_credential_secret("enfant1"),
            "uid": tenancy.child_uid(ALICE, "enfant1"),
            "unix_user": tenancy.child_unix_user(ALICE, "enfant1"),
            "runner_id": tenancy.child_runner_id("pod-avatar-enfant1-deadbeef",
                                                 "enfant1"),
            "fingerprint": "a" * 64, "expires_ts": time.time() + 600,
        }

    def test_rendu_est_un_pod_enfant_isole(self):
        rendered = self.template.render(self.request, dsn="postgresql://ameesh@db/ameesh")
        pod = rendered.pod
        self.assertEqual(pod["kind"], "Pod")
        self.assertEqual(pod["metadata"]["name"], rendered.pod_name)
        self.assertEqual(pod["metadata"]["labels"]["ameesh.dev/parent"], "avatar")
        self.assertEqual(pod["metadata"]["labels"]["ameesh.dev/host"],
                         self.request["host"])
        self.assertEqual(pod["spec"]["activeDeadlineSeconds"],
                         int(pod["spec"]["activeDeadlineSeconds"]))
        self.assertGreaterEqual(pod["spec"]["activeDeadlineSeconds"], 600)
        child = next(c for c in pod["spec"]["containers"] if c["name"] == "child")
        self.assertEqual(child["securityContext"]["runAsUser"], self.request["uid"])
        self.assertEqual(child["securityContext"]["runAsGroup"], 10000)
        self.assertNotIn("persistentVolumeClaim",
                         json.dumps(pod["spec"]["volumes"]))
        self.assertNotIn("hostPath", json.dumps(pod["spec"]["volumes"]))
        self.assertEqual(pod["spec"]["serviceAccountName"], "ameesh-child")
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        config = rendered.configmap["data"]
        self.assertIn(str(self.request["uid"]), config["passwd"])
        self.assertIn(self.request["unix_user"], config["passwd"])
        self.assertIn("api_key_env", config["config.json"])
        self.assertNotIn("api-key", config["config.json"])

    def _tampered(self, mutate):
        raw = copy.deepcopy(self.template.raw)
        mutate(raw)
        fd, path = tempfile.mkstemp(prefix="ameesh-pod-template-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yaml.safe_dump(raw, stream, sort_keys=False)
            return pod_provisioner.PodTemplate.load(path)
        finally:
            os.unlink(path)

    def test_un_host_path_est_refuse(self):
        def mutate(raw):
            raw["spec"]["volumes"].append(
                {"name": "parent", "hostPath": {"path": "/home/alice"}})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_un_pvc_est_refuse(self):
        def mutate(raw):
            raw["spec"]["volumes"].append(
                {"name": "parent", "persistentVolumeClaim": {"claimName": "pvc-alice"}})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_un_conteneur_privilegie_est_refuse(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["securityContext"]["privileged"] = True
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_placeholder_inconnu_refuse(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["env"].append(
                {"name": "X", "value": "{{NOT_ALLOWED}}"})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_uid_du_superviseur_refuse(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["securityContext"]["runAsUser"] = 10001
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_image_non_figee_refusee(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["image"] = "busybox"
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)


class PodManifestsTest(unittest.TestCase):
    def load(self, name):
        with open(os.path.join(PROVISIONER_DIR, name), encoding="utf-8") as stream:
            return list(yaml.safe_load_all(stream))

    def test_role_ne_donne_ni_secret_ni_exec(self):
        docs = self.load("rbac.yaml")
        role = next(d for d in docs if d["kind"] == "Role")
        for rule in role["rules"]:
            self.assertNotIn("secrets", rule["resources"])
            self.assertNotIn("pods/exec", rule["resources"])
            self.assertNotIn("pods/log", rule["resources"])
        self.assertEqual({r for rule in role["rules"] for r in rule["resources"]},
                         {"pods", "configmaps"})

    def test_vap_refuse_host_path_pvc_et_sa_sans_jeton(self):
        docs = self.load("validatingadmissionpolicy.yaml")
        policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
        expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])
        self.assertIn("hostPath", expressions)
        self.assertIn("persistentVolumeClaim", expressions)
        self.assertIn("automountServiceAccountToken", expressions)
        self.assertIn("ameesh-pod-provisioner", json.dumps(policy["spec"]["matchConditions"]))
        binding = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])

    def test_quota_borne_les_pods_et_les_configmaps(self):
        quota = self.load("resourcequota.yaml")[0]
        self.assertIn("pods", quota["spec"]["hard"])
        self.assertIn("count/configmaps", quota["spec"]["hard"])

    def test_serviceaccount_enfant_sans_jeton(self):
        docs = self.load("serviceaccount.yaml")
        child = next(d for d in docs if d["metadata"]["name"] == "ameesh-child")
        self.assertFalse(child.get("automountServiceAccountToken", True))


class PodRequestDbTest(_CanonDbCase):
    def setUp(self):
        super().setUp()
        write(self.root, "agents/avatar.md", avatar_card("pod"))
        write(self.root, "placements/avatar.md", fiche(
            type="Placement", title="avatar@lieux", agent="avatar",
            hosts=["atelier"], credential_mode="api-key"))
        report = self.sync()
        self.assertEqual(self.actions(report).get("avatar"), "créé", report.findings)
        self.template = pod_provisioner.PodTemplate.load(TEMPLATE)
        self.client = FakeKubeClient()
        storage.of(self.db).pod_provisioning.register_template(
            self.template.name, namespace="ameesh", image="ameesh-runner",
            fingerprint=self.template.fingerprint, spec=self.template.raw,
            actor=pod_provisioner.ACTOR)

    def spawn(self, name="enfant1", **kwargs):
        token = mock.patch.object(avatar_children.secrets, "token_hex",
                                  return_value="deadbeef")
        token.start()
        self.addCleanup(token.stop)
        return canon_sync.spawn(self.db, name, "avatar", 600,
                                canon=self.load(), **kwargs)

    def provisioner(self):
        return pod_provisioner.Provisioner(
            self.db, self.client, self.template, image="ameesh-runner",
            dsn="postgresql://ameesh@db/ameesh", actor=pod_provisioner.ACTOR)

    def request(self, child="enfant1"):
        return storage.of(self.db).pod_provisioning.get(child)

    def test_demande_et_enfant_dans_la_meme_transaction(self):
        row = self.spawn(pod_wait_s=0)
        self.assertEqual(row["host"], "pod-avatar-enfant1-deadbeef")
        self.assertEqual(row["placement"]["runtime"], "pod")
        self.assertEqual(row["placement"]["host"], row["host"])
        request = self.request()
        self.assertEqual(request["state"], "requested")
        self.assertEqual(request["host"], row["host"])
        self.assertEqual(request["child"], "enfant1")
        self.assertEqual(request["credential_secret"],
                         tenancy.child_credential_secret("enfant1"))
        self.assertNotIn("api-key", json.dumps(request))
        self.assertEqual(request["fingerprint"], pod_children.fingerprint(request))

    def test_pourvoyeur_ecrit_hote_enfant_et_garde(self):
        row = self.spawn(pod_wait_s=0)
        started = self.provisioner().start_once()
        self.assertIsNotNone(started)
        self.assertEqual(started["child"], "enfant1")
        self.assertEqual(started["state"], "running")
        host = row["host"]
        pod_host = storage.of(self.db).pod_hosts.get(host)
        self.assertEqual(pod_host["state"], "running")
        self.assertEqual(pod_host["pod_uid"], "uid-" + pod_provisioner.pod_name_for(
            self.request()["id"]))
        policy = storage.of(self.db).tenancy.policy(host)
        self.assertTrue(policy["shared"])
        self.assertEqual(policy["tenants"], [ALICE])
        binding = storage.of(self.db).tenancy.bindings(host)[0]
        self.assertEqual(binding["uid"], row["placement"]["uid"])
        self.assertEqual(binding["runner_id"], row["placement"]["runner"])
        # Le pod est bien celui du parent : labels vérifiés avant suppression.
        pod = self.client.get_pod(pod_provisioner.pod_name_for(self.request()["id"]))
        self.assertEqual(pod["metadata"]["labels"]["ameesh.dev/parent"], "avatar")

    def test_garde_pod_uid_et_expiration(self):
        row = self.spawn(pod_wait_s=0)
        self.provisioner().start_once()
        request = self.request()
        host, tenant = row["host"], ALICE
        binding = storage.of(self.db).tenancy.bindings(host)[0]
        storage.of(self.db).tenancy.beat(
            host, tenant, binding["runner_id"], uid=binding["uid"], version="t",
            pod_uid="wrong-uid")
        self.assertFalse(self._claim_ok(host, binding["runner_id"]))
        storage.of(self.db).tenancy.beat(
            host, tenant, binding["runner_id"], uid=binding["uid"], version="t",
            pod_uid="uid-" + pod_provisioner.pod_name_for(request["id"]))
        self.assertTrue(self._claim_ok(host, binding["runner_id"]))
        self.db.execute(
            "UPDATE pod_hosts SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE host = %s",
            (host,))
        self.assertFalse(self._claim_ok(host, binding["runner_id"]))
        self.db.execute(
            "UPDATE pod_hosts SET state = 'deleted' WHERE host = %s", (host,))
        self.assertFalse(self._claim_ok(host, binding["runner_id"]))

    def _claim_ok(self, host, runner):
        rows = self.db.query(
            "SELECT ameesh_tenant_claim_ok(%s, %s, %s) AS ok",
            (host, runner, ALICE))
        return bool(rows[0]["ok"])

    def test_cleanup_ttl_supprime_pod_et_liaisons(self):
        row = self.spawn(pod_wait_s=0)
        self.provisioner().start_once()
        request = self.request()
        self.db.execute(
            "UPDATE pod_requests SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE id = %s",
            (request["id"],))
        self.db.execute(
            "UPDATE pod_hosts SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE host = %s",
            (row["host"],))
        removed = self.provisioner().cleanup_once()
        self.assertEqual([item["child"] for item in removed], ["enfant1"])
        pod_name = pod_provisioner.pod_name_for(request["id"])
        self.assertIn(pod_name, self.client.deleted_pods)
        self.assertIn(pod_provisioner.configmap_name_for(request["id"]),
                      self.client.deleted_configmaps)
        self.assertFalse(storage.of(self.db).tenancy.policy(row["host"])["shared"])
        self.assertEqual(storage.of(self.db).tenancy.bindings(row["host"]), [])
        self.assertEqual(storage.of(self.db).pod_hosts.get(row["host"])["state"],
                         "deleted")
        self.assertEqual(self.request()["state"], "deleted")

    def test_refus_sans_pourvoyeur_vivant(self):
        self.db.execute("UPDATE pod_templates SET last_seen_at = now() - interval '10 minutes'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn(pod_wait_s=0)
        self.assertIn("spawn-pod-unsupported", str(ctx.exception))
        self.assertIsNone(self.row("enfant1"))

    def test_refus_parent_en_abonnement(self):
        self.db.execute(
            "UPDATE agent_registry SET credential_mode = 'subscription'"
            " WHERE name = 'avatar'")
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn(pod_wait_s=0)
        self.assertIn("spawn-pod-credential", str(ctx.exception))
        self.assertIsNone(self.row("enfant1"))

    def test_delai_de_demarrage_annule_et_nettoie(self):
        start = time.monotonic()
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn(pod_wait_s=0.2)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("spawn-pod-start-timeout", str(ctx.exception))
        self.assertEqual(self.request()["state"], "cancelled")
        self.assertIsNone(self.row("enfant1"))
        self.assertEqual(storage.of(self.db).pod_hosts.get(
            "pod-avatar-enfant1-deadbeef"), None)

    def test_cleanup_refuse_le_pod_d_un_autre_parent(self):
        row = self.spawn(pod_wait_s=0)
        self.provisioner().start_once()
        request = self.request()
        pod_name = pod_provisioner.pod_name_for(request["id"])
        self.client.pods[pod_name]["metadata"]["labels"]["ameesh.dev/parent"] = "autre"
        self.db.execute(
            "UPDATE pod_requests SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE id = %s",
            (request["id"],))
        self.provisioner().cleanup_once()
        self.assertIn(pod_name, self.client.pods)
        self.assertEqual(self.request()["state"], "failed")
        self.assertEqual(storage.of(self.db).pod_hosts.get(row["host"])["state"],
                         "running")
