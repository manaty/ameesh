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

from ameesh import (avatar_children, canon_sync, pod_admission, pod_children,
                    pod_provisioner, registry, storage, tenancy)

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
        # Le nom du Secret est dérivé du label `ameesh.dev/child` : même
        # forme DNS, et deux noms bruts qui tombent sur la même forme gardent
        # des Secrets distincts (sinon la contrainte `unique` refuserait le
        # second enfant).
        self.assertEqual(secret,
                         tenancy.POD_CHILD_SECRET_PREFIX
                         + tenancy.child_slug("enfant1"))
        self.assertNotEqual(tenancy.child_slug("Enfant.1"),
                            tenancy.child_slug("enfant-1"))
        self.assertNotEqual(tenancy.child_credential_secret("Enfant.1"),
                            tenancy.child_credential_secret("enfant-1"))

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
        # B1 (#7697) : `human:alice` n'est pas une valeur de label Kubernetes ;
        # le tenant vit en annotation, tous les labels rendus sont valides.
        self.assertNotIn("ameesh.dev/tenant", pod["metadata"]["labels"])
        self.assertEqual(pod["metadata"]["annotations"]["ameesh.dev/tenant"], ALICE)
        self.assertEqual(pod["metadata"]["annotations"]["ameesh.dev/credential-secret"],
                         self.request["credential_secret"])
        # B3 (#7697) : le label est la forme DNS de l'enfant, et le Secret est
        # exactement `ameesh-child-<label>` — ce que la VAP sait recouper.
        child_label = tenancy.child_slug("enfant1")
        self.assertEqual(pod["metadata"]["labels"]["ameesh.dev/child"], child_label)
        self.assertEqual(pod["metadata"]["annotations"]["ameesh.dev/credential-secret"],
                         "ameesh-child-" + child_label)
        self.assertEqual(
            pod["metadata"]["annotations"]["ameesh.dev/child-name"], "enfant1")
        for container in pod["spec"]["containers"]:
            self.assertEqual(container["image"], "ameesh-runner")
        child_env = {entry["name"]: entry for entry in child["env"]}
        secret_ref = next(
            entry["valueFrom"]["secretKeyRef"]
            for entry in child_env.values()
            if (entry.get("valueFrom") or {}).get("secretKeyRef", {}).get("key")
            == "api-key")
        self.assertEqual(secret_ref["name"], "ameesh-child-" + child_label)
        self.assertEqual(secret_ref["key"], "api-key")
        for value in pod["metadata"]["labels"].values():
            self.assertLessEqual(len(value), 63)
            self.assertRegex(value, r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
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

    # -- B3/N(a) (#7697) : la demande ne fait pas foi sur le Secret ni sur
    # l'identité enfant, et le rendu vérifie la même forme que la VAP.
    def test_rendu_refuse_un_secret_qui_n_est_pas_celui_de_l_enfant(self):
        request = copy.deepcopy(self.request)
        request["credential_secret"] = tenancy.child_credential_secret("enfant2")
        with self.assertRaises(pod_provisioner.ProvisionerError) as ctx:
            self.template.render(request, dsn="postgresql://ameesh@db/ameesh")
        self.assertEqual(ctx.exception.code, "request-mismatch")

    def test_rendu_refuse_un_uid_qui_n_est_pas_celui_du_plan(self):
        request = copy.deepcopy(self.request)
        request["uid"] = int(request["uid"]) + 1
        with self.assertRaises(pod_provisioner.ProvisionerError) as ctx:
            self.template.render(request, dsn="postgresql://ameesh@db/ameesh")
        self.assertEqual(ctx.exception.code, "request-mismatch")

    def _validated_rendered(self):
        rendered = self.template.render(
            self.request, image="ameesh-runner",
            dsn="postgresql://ameesh@db/ameesh")
        return rendered, {"POD_NAME": rendered.pod_name, "IMAGE": "ameesh-runner"}

    def test_validate_rendered_refuse_une_image_hors_parametre(self):
        rendered, mapping = self._validated_rendered()
        for container in rendered.pod["spec"]["containers"]:
            container["image"] = "ameesh-runner.attacker.example/x:latest"
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self.template._validate_rendered(rendered.pod, mapping, self.request)

    def test_validate_rendered_refuse_le_secret_d_un_autre_enfant(self):
        rendered, mapping = self._validated_rendered()
        child = next(c for c in rendered.pod["spec"]["containers"]
                     if c["name"] == "child")
        ref = next(entry["valueFrom"]["secretKeyRef"] for entry in child["env"]
                   if (entry.get("valueFrom") or {}).get("secretKeyRef", {}).get(
                       "key") == "api-key")
        ref["name"] = tenancy.child_credential_secret("enfant2")
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self.template._validate_rendered(rendered.pod, mapping, self.request)

    def test_validate_rendered_refuse_le_secret_enfant_dans_le_superviseur(self):
        rendered, mapping = self._validated_rendered()
        supervisor = next(c for c in rendered.pod["spec"]["containers"]
                          if c["name"] == "supervisor")
        supervisor.setdefault("env", []).append({
            "name": "AUTRE_CLE",
            "valueFrom": {"secretKeyRef": {
                "name": tenancy.child_credential_secret("enfant1"),
                "key": "api-key"}}})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self.template._validate_rendered(rendered.pod, mapping, self.request)

    def test_un_init_container_privilegie_est_refuse(self):
        def mutate(raw):
            raw["spec"]["initContainers"] = [{
                "name": "init", "image": "busybox",
                "securityContext": {"privileged": True}}]
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_un_secret_volume_est_refuse(self):
        def mutate(raw):
            raw["spec"]["volumes"].append(
                {"name": "autre", "secret": {"secretName": "ameesh-child-autre"}})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_une_source_de_secret_inconnue_est_refusee(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["env"].append({
                "name": "AUTRE",
                "valueFrom": {"secretKeyRef": {
                    "name": "ameesh-child-autre", "key": "api-key"}}})
        with self.assertRaises(pod_provisioner.ProvisionerError):
            self._tampered(mutate)

    def test_une_commande_modifiee_est_refusee(self):
        def mutate(raw):
            raw["spec"]["containers"][1]["command"] = ["sh", "-ec", "cat /run/secrets/x"]
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

    def test_vap_ferme_init_ephemeres_commandes_et_secrets(self):
        """B3 (#7697) : un SA compromis ne peut ni ajouter de processus ni
        référencer un Secret arbitraire ; la forme est celle du template."""
        docs = self.load("validatingadmissionpolicy.yaml")
        policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
        expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])
        for fragment in ("initContainers", "ephemeralContainers", "imagePullSecrets",
                         "!has(v.secret)", "credential-secret", "secretKeyRef",
                         "ameesh-db", "lifecycle", "envFrom", "command"):
            self.assertIn(fragment, expressions)
        self.assertIn("object.spec.containers[0].name == 'supervisor'", expressions)
        self.assertIn("object.spec.containers[1].name == 'child'", expressions)

    def test_vap_epingle_image_et_lie_le_secret_a_l_enfant(self):
        """B3 (#7697) : image exacte par paramètre (pas un préfixe), Secret
        dérivé du label enfant et monté dans le seul conteneur enfant."""
        docs = self.load("validatingadmissionpolicy.yaml")
        policy = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
        binding = next(d for d in docs
                       if d["kind"] == "ValidatingAdmissionPolicyBinding")
        expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])
        # L'image n'est plus un préfixe : seule l'égalité au paramètre compte.
        self.assertNotIn("startsWith('ameesh-runner')", expressions)
        self.assertIn(
            "params.metadata.?annotations['ameesh.dev/pod-child-image']"
            ".orValue('')", expressions)
        self.assertEqual(policy["spec"]["paramKind"],
                         {"apiVersion": "v1", "kind": "Namespace"})
        self.assertEqual(binding["spec"]["paramRef"]["name"], "ameesh")
        self.assertNotIn("namespace", binding["spec"]["paramRef"])
        self.assertEqual(binding["spec"]["paramRef"]["parameterNotFoundAction"],
                         "Deny")
        # Le Secret vient du label, pas d'une annotation libre.
        self.assertIn(
            "'ameesh-child-' + object.metadata.?labels['ameesh.dev/child']"
            ".orValue('')", expressions)
        self.assertIn("c.name == 'child'", expressions)

    def test_le_parametre_de_la_vap_est_l_image_du_deploiement(self):
        deployment = self.load("deployment.yaml")[0]
        namespace = self.load("namespace.yaml")[0]
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(namespace["metadata"]["name"], "ameesh")
        self.assertEqual(
            namespace["metadata"]["annotations"]["ameesh.dev/pod-child-image"],
            image)
        self.assertEqual(pod_provisioner.DEFAULT_IMAGE, image)
        kustomization = self.load("kustomization.yaml")[0]
        self.assertIn("namespace.yaml", kustomization["resources"])
        # Le paramètre doit rester hors de portée du Role namespaced : aucun
        # ConfigMap de paramètres que le SA pourrait supprimer et recréer.
        self.assertNotIn("ameesh-pod-child-params", json.dumps(
            kustomization.get("configMapGenerator") or []))

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

    def test_lecture_de_la_demande_par_uuid(self):
        """Le webhook lit la demande par son UUID, pas par le label du pod."""
        self.spawn(pod_wait_s=0)
        request = self.request()
        row = storage.of(self.db).pod_provisioning.get_by_id(request["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["child"], "enfant1")
        self.assertEqual(row["credential_secret"],
                         tenancy.child_credential_secret("enfant1"))
        self.assertIsNone(storage.of(self.db).pod_provisioning.get_by_id(
            "00000000-0000-4000-8000-000000000000"))

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

    def test_pourvoyeur_refuse_une_demande_au_secret_etranger(self):
        """N(a) (#7697) : le pourvoyeur ne fait pas foi sur
        `pod_requests.credential_secret` ; il le recompare à l'enfant."""
        self.spawn(pod_wait_s=0)
        request = self.request()
        self.db.execute(
            "UPDATE pod_requests SET credential_secret = %s WHERE id = %s",
            (tenancy.child_credential_secret("enfant2"), request["id"]))
        self.assertIsNone(self.provisioner().start_once())
        failed = self.request()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("request-mismatch", failed["error"])
        self.assertEqual(self.client.pods, {})
        self.assertEqual(self.client.configmaps, {})

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

    def test_garde_exige_un_battement_frais(self):
        """N(c) (#7697) : un pod vivant mais un runner muet ne réclame plus."""
        row = self.spawn(pod_wait_s=0)
        self.provisioner().start_once()
        request = self.request()
        host, tenant = row["host"], ALICE
        binding = storage.of(self.db).tenancy.bindings(host)[0]
        storage.of(self.db).tenancy.beat(
            host, tenant, binding["runner_id"], uid=binding["uid"], version="t",
            pod_uid="uid-" + pod_provisioner.pod_name_for(request["id"]))
        self.assertTrue(self._claim_ok(host, binding["runner_id"]))
        self.db.execute(
            "UPDATE runner_presence SET seen_at = now() - interval '10 minutes'"
            " WHERE host = %s AND tenant = %s", (host, tenant))
        self.assertFalse(self._claim_ok(host, binding["runner_id"]))
        storage.of(self.db).tenancy.beat(
            host, tenant, binding["runner_id"], uid=binding["uid"], version="t",
            pod_uid="uid-" + pod_provisioner.pod_name_for(request["id"]))
        self.assertTrue(self._claim_ok(host, binding["runner_id"]))

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

    def test_cleanup_ne_supprime_pas_une_configmap_d_un_autre_request(self):
        """N(b) (#7697) : le nettoyage vérifie `request-id` avant de
        supprimer la ConfigMap, comme le chemin d'échec."""
        row = self.spawn(pod_wait_s=0)
        self.provisioner().start_once()
        request = self.request()
        name = pod_provisioner.configmap_name_for(request["id"])
        other = copy.deepcopy(self.client.configmaps[name])
        other["metadata"]["labels"]["ameesh.dev/request-id"] = "autre"
        self.client.configmaps[name] = other
        self.db.execute(
            "UPDATE pod_requests SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE id = %s",
            (request["id"],))
        self.db.execute(
            "UPDATE pod_hosts SET requested_at = now() - interval '2 hours',"
            " expires_at = now() - interval '1 second' WHERE host = %s",
            (row["host"],))
        self.provisioner().cleanup_once()
        self.assertIn(name, self.client.configmaps)
        self.assertNotIn(name, self.client.deleted_configmaps)
        self.assertEqual(self.request()["state"], "deleted")

    def _rendered(self, request=None):
        return self.template.render(
            request or self.request(), image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=time.time())

    def test_reprise_adopte_le_pod_de_la_meme_demande(self):
        """B4 (#7697) : une reprise lit `request-id` dans les annotations et
        reprend le pod de la même demande sans le supprimer."""
        self.spawn(pod_wait_s=0)
        request = self.request()
        rendered = self._rendered(request)
        self.client.create_pod(rendered.pod)
        started = self.provisioner().start_once()
        self.assertIsNotNone(started)
        self.assertEqual(started["state"], "running")
        self.assertIn(rendered.pod_name, self.client.pods)
        self.assertNotIn(rendered.pod_name, self.client.deleted_pods)

    def test_un_pod_d_un_autre_parent_n_est_pas_supprime(self):
        """B2 (#7697) : un conflit de nom sur le pod d'un tiers ne le supprime
        pas et ne laisse pas la demande en vie."""
        self.spawn(pod_wait_s=0)
        request = self.request()
        name = pod_provisioner.pod_name_for(request["id"])
        self.client.create_pod({
            "metadata": {
                "name": name, "namespace": "ameesh",
                "labels": {"ameesh.dev/host": "autre", "ameesh.dev/parent": "autre"},
                "annotations": {"ameesh.dev/request-id": "autre"},
            },
            "spec": {"containers": []},
        })
        self.assertIsNone(self.provisioner().start_once())
        self.assertIn(name, self.client.pods)
        self.assertNotIn(name, self.client.deleted_pods)
        self.assertEqual(self.request()["state"], "failed")

    def test_une_configmap_d_un_autre_n_est_pas_supprimee(self):
        """B2 (#7697) : une ConfigMap sans notre `request-id` n'est jamais
        écrasée ni supprimée par le nettoyage d'échec."""
        self.spawn(pod_wait_s=0)
        rendered = self._rendered()
        other = copy.deepcopy(rendered.configmap)
        other["data"]["passwd"] = "autre"
        other["metadata"]["labels"]["ameesh.dev/request-id"] = "autre"
        self.client.configmaps[rendered.configmap_name] = other
        self.assertIsNone(self.provisioner().start_once())
        self.assertIn(rendered.configmap_name, self.client.configmaps)
        self.assertNotIn(rendered.configmap_name, self.client.deleted_configmaps)

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
        with self.assertRaises(canon_sync.SpawnError) as ctx:
            self.spawn(pod_wait_s=0.2)
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


class _AdmissionRows:
    def __init__(self, row):
        self.row = row

    def get_by_id(self, request_id):
        if self.row and str(request_id) == str(self.row["id"]):
            return dict(self.row)
        return None


class _AdmissionConfigmaps:
    def __init__(self, configmap):
        self.configmap = configmap

    def get_configmap(self, name):
        if self.configmap and name == self.configmap["metadata"]["name"]:
            return copy.deepcopy(self.configmap)
        return None


@unittest.skipIf(yaml is None, "PyYAML absent : template non vérifié")
class PodAdmissionTest(unittest.TestCase):
    """B du gel #44541 (#228610/#228620) : le webhook ne croit pas le label
    du pourvoyeur ; il recoupe la demande et refuse tout écart inter-tenant."""

    NOW = 1_000.0

    def setUp(self):
        self.template = pod_provisioner.PodTemplate.load(TEMPLATE)
        host = tenancy.child_host("avatar", "enfant1", "deadbeef")
        self.row = {
            "id": "abcdef01-2345-6789-abcd-ef0123456789",
            "child": "enfant1",
            "parent": "avatar",
            "tenant": ALICE,
            "host": host,
            "template": tenancy.POD_CHILD_TEMPLATE,
            "namespace": "ameesh",
            "pod_name": None,
            "harness": "claude",
            "credential_secret": tenancy.child_credential_secret("enfant1"),
            "uid": tenancy.child_uid(ALICE, "enfant1"),
            "unix_user": tenancy.child_unix_user(ALICE, "enfant1"),
            "runner_id": tenancy.child_runner_id(host, "enfant1"),
            "profile": {},
            "fingerprint": "a" * 64,
            "state": "claimed",
            "requested_by": "avatar",
            "claimed_by": pod_admission.ACTOR,
            "requested_ts": 900.0,
            "claimed_ts": 990.0,
            "started_ts": None,
            "expires_ts": 2_000.0,
            "finished_ts": None,
            "error": None,
        }
        self.rendered = self.template.render(
            self.row, image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=self.NOW)
        self.admission = pod_admission.PodAdmission(
            None, self.template, requests=_AdmissionRows(self.row),
            image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=self.NOW,
            client=_AdmissionConfigmaps(self.rendered.configmap))

    def review(self, pod, *, user=pod_admission.ACTOR):
        return {"request": {
            "uid": "review-1", "operation": "CREATE",
            "resource": {"group": "", "version": "v1", "resource": "pods"},
            "namespace": "ameesh",
            "userInfo": {"username": user},
            "object": copy.deepcopy(pod),
        }}

    def test_pod_conforme_admis(self):
        decision = self.admission.check(self.review(self.rendered.pod))
        self.assertTrue(decision["allowed"], decision.get("message"))

    def test_les_champs_systeme_de_l_api_server_sont_admis(self):
        """UID, date de création et resourceVersion sont posés avant
        l'admission : ils ne doivent pas faire refuser un pod légitime."""
        pod = copy.deepcopy(self.rendered.pod)
        pod["metadata"].update({
            "uid": "0f0e0d0c-0b0a-4908-8706-050403020100",
            "creationTimestamp": "2026-11-20T10:00:00Z",
            "resourceVersion": "12",
            "generation": 0,
            "managedFields": [],
        })
        decision = self.admission.check(self.review(pod))
        self.assertTrue(decision["allowed"], decision.get("message"))

    def test_secret_d_un_autre_enfant_refuse(self):
        """Le label et le Secret d'un autre enfant sont rejetés, même si le
        nom du pod et le request-id sont ceux de la demande légitime."""
        pod = copy.deepcopy(self.rendered.pod)
        pod["metadata"]["labels"]["ameesh.dev/child"] = tenancy.child_slug("enfant2")
        pod["metadata"]["annotations"]["ameesh.dev/credential-secret"] = (
            tenancy.child_credential_secret("enfant2"))
        for entry in pod["spec"]["containers"][1]["env"]:
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
            if ref and ref.get("key") == "api-key":
                ref["name"] = tenancy.child_credential_secret("enfant2")
        decision = self.admission.check(self.review(pod))
        self.assertFalse(decision["allowed"])
        self.assertIn("shape", decision["message"])

    def test_env_supplementaire_est_refuse(self):
        pod = copy.deepcopy(self.rendered.pod)
        pod["spec"]["containers"][1]["env"].append(
            {"name": "HTTPS_PROXY", "value": "http://attacker.invalid:8080"})
        decision = self.admission.check(self.review(pod))
        self.assertFalse(decision["allowed"])
        self.assertIn("shape", decision["message"])

    def test_volume_secret_est_refuse(self):
        pod = copy.deepcopy(self.rendered.pod)
        pod["spec"]["volumes"].append(
            {"name": "autre", "secret": {"secretName": "ameesh-child-autre"}})
        decision = self.admission.check(self.review(pod))
        self.assertFalse(decision["allowed"])

    def test_configmap_alteree_refusee(self):
        bad = copy.deepcopy(self.rendered.configmap)
        bad["data"]["config.json"] = '{"host":"attacker.invalid"}'
        admission = pod_admission.PodAdmission(
            None, self.template, requests=_AdmissionRows(self.row),
            image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=self.NOW,
            client=_AdmissionConfigmaps(bad))
        decision = admission.check(self.review(self.rendered.pod))
        self.assertFalse(decision["allowed"])
        self.assertIn("ConfigMap", decision["message"])

    def test_configmap_create_alteree_refusee(self):
        """Le webhook intercepte aussi la ConfigMap : un pourvoyeur compromis
        ne peut pas créer une ConfigMap hors plan, même avant le pod."""
        bad = copy.deepcopy(self.rendered.configmap)
        bad["data"]["config.json"] = '{"host":"attacker.invalid"}'
        review = {"request": {
            "uid": "review-cm", "operation": "CREATE",
            "resource": {"group": "", "version": "v1", "resource": "configmaps"},
            "namespace": "ameesh",
            "userInfo": {"username": pod_admission.ACTOR},
            "object": bad,
        }}
        decision = self.admission.check(review)
        self.assertFalse(decision["allowed"])
        self.assertIn("ConfigMap", decision["message"])

    def test_demande_inconnue_ou_perimee_refusee(self):
        unknown = pod_admission.PodAdmission(
            None, self.template, requests=_AdmissionRows(None),
            image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=self.NOW)
        self.assertFalse(unknown.check(self.review(self.rendered.pod))["allowed"])
        expired = pod_admission.PodAdmission(
            None, self.template, requests=_AdmissionRows(self.row),
            image="ameesh-runner", namespace="ameesh",
            dsn="postgresql://ameesh@db/ameesh", now=2_001.0)
        self.assertFalse(expired.check(self.review(self.rendered.pod))["allowed"])

    def test_un_autre_service_account_n_est_pas_filtre(self):
        decision = self.admission.check(self.review(
            {"metadata": {"name": "x", "namespace": "ameesh"}}, user="system:serviceaccount:ameesh:autre"))
        self.assertTrue(decision["allowed"])

    def test_manifests_du_webhook(self):
        """Identité distincte, fail-closed, et hors de portée du Role du
        pourvoyeur : le SA compromis ne peut ni retirer ni modifier le
        webhook, ni son Service, ni son Deployment."""
        admission = self.load("admission.yaml")
        kinds = {(doc.get("kind"), (doc.get("metadata") or {}).get("name"))
                 for doc in admission}
        self.assertIn(("ServiceAccount", "ameesh-pod-admission"), kinds)
        self.assertIn(("Deployment", "ameesh-pod-admission"), kinds)
        self.assertIn(("Service", "ameesh-pod-admission"), kinds)
        self.assertIn(("ValidatingWebhookConfiguration", "ameesh-child-pods"), kinds)
        webhook = next(doc for doc in admission
                       if doc.get("kind") == "ValidatingWebhookConfiguration")
        for hook in webhook["webhooks"]:
            self.assertEqual(hook["failurePolicy"], "Fail")
            self.assertLessEqual(hook["timeoutSeconds"], 10)
            self.assertIn("pods", hook["rules"][0]["resources"])
            self.assertIn("configmaps", hook["rules"][0]["resources"])
        role = self.load("rbac.yaml")[0]
        resources = {item for rule in role["rules"] for item in rule["resources"]}
        self.assertNotIn("validatingwebhookconfigurations", resources)
        self.assertNotIn("deployments", resources)
        self.assertNotIn("secrets", resources)
        protections = [doc for doc in self.load("validatingadmissionpolicy.yaml")
                       if doc.get("kind") == "ValidatingAdmissionPolicy"
                       and (doc.get("metadata") or {}).get("name")
                       == "ameesh-protect-admission"]
        self.assertEqual(len(protections), 1)
        rule = protections[0]["spec"]["matchConstraints"]["resourceRules"][0]
        self.assertEqual(rule["operations"], ["DELETE"])
        self.assertIn("ameesh-pod-admission",
                      protections[0]["spec"]["validations"][0]["expression"])

    @staticmethod
    def load(name):
        with open(os.path.join(PROVISIONER_DIR, name), encoding="utf-8") as stream:
            return [doc for doc in yaml.safe_load_all(stream) if doc]
