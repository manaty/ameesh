# SPDX-License-Identifier: AGPL-3.0-only
"""#7699 (P6 de #7633) : chart de référence d'un hôte pod.

Le chart `deploy/k8s/pod-shared/` est complété pour une exploitation réelle :
NetworkPolicy (entrée refusée, sortie déclarée), ResourceQuota, PDB, image
épinglée par digest, labels de version hors sélecteur immuable, `preStop` de
drain borné par la grâce, sauvegarde/restauration par `VolumeSnapshot`.
La qualification sur le cluster TPK (`Q-TPK`) reste hors série ; ces tests
vérifient les contrats statiques et le rendu, pas l'application réelle.
"""
from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from .support import REPO

POD_DIR = os.path.join(REPO, "deploy", "k8s", "pod-shared")
RENDER = os.path.join(POD_DIR, "render.py")
SNAPSHOT = os.path.join(POD_DIR, "snapshot.py")
HOST_YAML = os.path.join(POD_DIR, "host.exemple.yaml")
DIGEST = "sha256:" + "0" * 64


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(yaml is None, "PyYAML absent : chart non vérifié")
class ReferenceChartTest(unittest.TestCase):
    """Le chart de référence : objets, réseau, quota, PDB, mise à jour."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.render = load_module(RENDER, "pod_reference_render")
        cls.config = cls.render.load_config(Path(HOST_YAML))
        cls.docs = cls.render.render_all(cls.config)
        cls.statefulset = next(d for d in cls.docs if d["kind"] == "StatefulSet")
        cls.pod = cls.statefulset["spec"]["template"]["spec"]
        cls.by_name = {d["metadata"]["name"]: d for d in cls.docs}
        cls.base_yaml = yaml.safe_load(Path(HOST_YAML).read_text(encoding="utf-8"))

    def load_variant(self, **changes):
        """Charge une variante du fichier d'exemple (validation de load_config)."""
        base = copy.deepcopy(self.base_yaml)
        base.update(changes)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as fh:
            yaml.safe_dump(base, fh, sort_keys=False)
            path = fh.name
        try:
            return self.render.load_config(Path(path))
        finally:
            os.unlink(path)

    def container(self, name: str) -> dict:
        return next(c for c in self.pod["containers"] if c["name"] == name)

    # -- objets et rendu -------------------------------------------------
    def test_objets_de_reference(self):
        kinds = [d["kind"] for d in self.docs]
        self.assertEqual(kinds.count("StatefulSet"), 1)
        self.assertEqual(kinds.count("PersistentVolumeClaim"), 2)
        self.assertEqual(kinds.count("NetworkPolicy"), 5)
        self.assertEqual(kinds.count("ResourceQuota"), 1)
        self.assertEqual(kinds.count("PodDisruptionBudget"), 1)
        self.assertIn("ameesh-pod-alice-default-deny", self.by_name)
        self.assertIn("ameesh-pod-alice-egress-dns", self.by_name)
        self.assertIn("ameesh-pod-alice-egress-base", self.by_name)
        self.assertIn("ameesh-pod-alice-egress-canon", self.by_name)
        self.assertIn("ameesh-pod-alice-egress-fournisseurs", self.by_name)
        self.assertIn("ameesh-pod-alice-quota", self.by_name)
        self.assertIn("ameesh-pod-alice-pdb", self.by_name)

    def test_rendu_a_jour_et_flux_multi_documents(self):
        proc = subprocess.run([sys.executable, RENDER, "--check"],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = subprocess.run([sys.executable, RENDER, "--stdout"],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [doc for doc in yaml.safe_load_all(proc.stdout) if doc]
        self.assertEqual(len(documents), 15)
        proc = subprocess.run(
            [sys.executable, RENDER, "--stdout", "--kind", "NetworkPolicy",
             "--kind", "ResourceQuota", "--kind", "PodDisruptionBudget"],
            cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        selected = [doc for doc in yaml.safe_load_all(proc.stdout) if doc]
        self.assertEqual({doc["kind"] for doc in selected},
                         {"NetworkPolicy", "ResourceQuota",
                          "PodDisruptionBudget"})

    # -- réseau ----------------------------------------------------------
    def test_reseau_entree_refusee_et_dns_autorise(self):
        deny = self.by_name["ameesh-pod-alice-default-deny"]
        self.assertEqual(deny["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertNotIn("ingress", deny["spec"])
        self.assertNotIn("egress", deny["spec"])
        dns = self.by_name["ameesh-pod-alice-egress-dns"]["spec"]
        self.assertEqual(dns["policyTypes"], ["Egress"])
        rule = dns["egress"][0]
        self.assertEqual(rule["to"][0]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "kube-system"})
        self.assertEqual(rule["to"][0]["podSelector"]["matchLabels"],
                         {"k8s-app": "kube-dns"})
        self.assertEqual({(port["protocol"], port["port"])
                          for port in rule["ports"]},
                         {("UDP", 53), ("TCP", 53)})
        # Aucune politique n'ouvre une entrée : le pod n'écoute rien.
        for document in self.docs:
            if document["kind"] == "NetworkPolicy":
                self.assertNotIn("ingress", document["spec"])

    def test_reseau_sortie_declaree_et_metadonnees_exclues(self):
        base = self.by_name["ameesh-pod-alice-egress-base"]["spec"]["egress"][0]
        self.assertEqual(base["to"], [{"ipBlock": {"cidr": "192.0.2.0/24"}}])
        self.assertEqual(base["ports"], [{"protocol": "TCP", "port": 5432}])
        providers = self.by_name[
            "ameesh-pod-alice-egress-fournisseurs"]["spec"]["egress"][0]
        block = providers["to"][0]["ipBlock"]
        self.assertEqual(block["cidr"], "0.0.0.0/0")
        self.assertIn("169.254.0.0/16", block["except"])
        self.assertEqual(providers["ports"], [{"protocol": "TCP", "port": 443}])

    def test_reseau_refuse_les_regles_invalides(self):
        with self.assertRaises(self.render.RenderError):
            self.load_variant(network={"egress": [{
                "name": "cassé", "to": [{"ipBlock": {"cidr": "10.0.0.0/99"}}]}]})
        with self.assertRaises(self.render.RenderError):
            self.load_variant(network={"egress": [{
                "name": "port", "to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}],
                "ports": [{"protocol": "TCP", "port": 70000}]}]})
        with self.assertRaises(self.render.RenderError):
            self.load_variant(network={"egress": [{
                "name": "pair", "to": [{}]}]})

    def test_reseau_dns_desactivable(self):
        config = self.load_variant(network={
            "dns": False,
            "egress": [{"name": "base",
                        "to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}],
                        "ports": [{"protocol": "TCP", "port": 5432}]}]})
        names = [document["metadata"]["name"]
                 for document in self.render.render_all(config)
                 if document["kind"] == "NetworkPolicy"]
        self.assertEqual(names, ["ameesh-pod-alice-default-deny",
                                 "ameesh-pod-alice-egress-base"])

    # -- quota et PDB ----------------------------------------------------
    def test_quota_couvre_les_ressources_du_chart(self):
        quota = self.by_name["ameesh-pod-alice-quota"]["spec"]["hard"]
        self.assertEqual(quota["pods"], "1")
        self.assertEqual(quota["persistentvolumeclaims"], "2")
        self.assertEqual(quota["requests.storage"], "40Gi")
        # La somme des conteneurs du chart (superviseur, init, fetch, tenants).
        self.assertEqual(quota["requests.cpu"], "660m")
        self.assertEqual(quota["limits.cpu"], "4850m")
        self.assertEqual(quota["requests.memory"], "1376Mi")
        self.assertEqual(quota["limits.memory"], "4992Mi")
        self.assertEqual(quota["services"], "0")
        self.assertEqual(quota["ingresses.networking.k8s.io"], "0")
        self.assertEqual(quota["count/statefulsets.apps"], "1")

    def test_quota_ajoute_les_cles_explicites(self):
        config = self.load_variant(quota={"pods": 1, "hard": {"secrets": "20"}})
        quota = next(d for d in self.render.render_all(config)
                     if d["kind"] == "ResourceQuota")
        self.assertEqual(quota["spec"]["hard"]["secrets"], "20")
        self.assertEqual(quota["spec"]["hard"]["pods"], "1")

    def test_pdb_une_replique(self):
        pdb = self.by_name["ameesh-pod-alice-pdb"]["spec"]
        self.assertEqual(pdb["minAvailable"], 1)
        self.assertEqual(pdb["selector"]["matchLabels"],
                         self.statefulset["spec"]["selector"]["matchLabels"])

    def test_pdb_max_unavailable_exclusif(self):
        with self.assertRaises(self.render.RenderError):
            self.load_variant(pdb={"min_available": 1, "max_unavailable": 1})

    # -- arrêt, mise à jour, image, labels -------------------------------
    def test_prestop_et_grace_bornee(self):
        self.assertEqual(self.pod["terminationGracePeriodSeconds"], 120)
        for name in ("supervisor", "tenant-alice", "tenant-bruno"):
            command = " ".join(self.container(name)["lifecycle"]["preStop"]
                               ["exec"]["command"])
            self.assertIn("kill -TERM 1", command)
            self.assertIn("-lt 90", command)
            env = {item["name"]: item.get("value")
                   for item in self.container(name)["env"]}
            self.assertEqual(env["AMEESH_DRAIN_SECONDS"], "80")
        with self.assertRaises(self.render.RenderError):
            self.load_variant(grace_period=60)
        with self.assertRaises(self.render.RenderError):
            self.load_variant(grace_period=120, prestop_wait=115)

    def test_mise_a_jour_et_retour_arriere(self):
        spec = self.statefulset["spec"]
        self.assertEqual(spec["revisionHistoryLimit"], 5)
        self.assertEqual(spec["minReadySeconds"], 10)
        self.assertEqual(spec["updateStrategy"]["type"], "RollingUpdate")
        self.assertEqual(spec["updateStrategy"]["rollingUpdate"]["partition"], 0)
        self.assertEqual(spec["podManagementPolicy"], "OrderedReady")

    def test_image_epinglee_par_digest(self):
        images = [container["image"] for container in self.pod["containers"]]
        images += [container["image"] for container in self.pod["initContainers"]]
        for image in images:
            self.assertRegex(image, r"^registry\.example\.invalid/ameesh-runner"
                                    r"@sha256:[0-9a-f]{64}$")
        with self.assertRaises(self.render.RenderError):
            self.load_variant(image_digest="sha256:xyz")
        with self.assertRaises(self.render.RenderError):
            self.load_variant(image="registry.example.invalid/ameesh-runner:1.2.3")
        with self.assertRaises(self.render.RenderError):
            self.load_variant(
                image_digest=DIGEST,
                image="registry.example.invalid/ameesh-runner:1.2.3")
        # Banc local seulement : le tag redevient possible quand le digest est
        # explicitement relâché.
        config = self.load_variant(
            image="registry.example.invalid/ameesh-runner:1.2.3",
            image_digest=None, require_digest=False)
        statefulset = next(d for d in self.render.render_all(config)
                           if d["kind"] == "StatefulSet")
        self.assertEqual(statefulset["spec"]["template"]["spec"]["containers"][0]
                         ["image"], "registry.example.invalid/ameesh-runner:1.2.3")

    def test_labels_de_version_hors_du_selecteur(self):
        labels = self.statefulset["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/version"], "1.1.0")
        self.assertEqual(labels["app.kubernetes.io/instance"],
                         "ameesh-pod-alice")
        self.assertEqual(labels["ameesh.dev/host"], "pod-alice-exemple")
        selector = self.statefulset["spec"]["selector"]["matchLabels"]
        self.assertNotIn("app.kubernetes.io/version", selector)
        template = self.statefulset["spec"]["template"]["metadata"]["labels"]
        self.assertEqual({key: template[key] for key in selector}, selector)
        config = copy.deepcopy(self.config)
        config["version"] = "2.0.0"
        statefulset = self.render._statefulset(config)
        self.assertEqual(statefulset["spec"]["selector"]["matchLabels"], selector)
        self.assertEqual(statefulset["spec"]["template"]["metadata"]["labels"]
                         ["app.kubernetes.io/version"], "2.0.0")

    def test_kustomization_liste_les_objets_rendus(self):
        kustomization = yaml.safe_load(Path(POD_DIR, "kustomization.yaml")
                                       .read_text(encoding="utf-8"))
        resources = kustomization["resources"]
        self.assertEqual(resources, [
            "serviceaccount.yaml",
            "rendered/configmaps.yaml",
            "rendered/pvcs.yaml",
            "rendered/statefulset.yaml",
            "rendered/networkpolicy.yaml",
            "rendered/resourcequota.yaml",
            "rendered/pdb.yaml",
        ])
        self.assertNotIn("images", kustomization)


@unittest.skipIf(yaml is None, "PyYAML absent : outil de sauvegarde non vérifié")
class SnapshotToolTest(unittest.TestCase):
    """Sauvegarde et restauration : un snapshot par PVC, même nom au retour."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.render = load_module(RENDER, "pod_snapshot_render")
        cls.snapshot = load_module(SNAPSHOT, "pod_snapshot_tool")
        cls.config = cls.render.load_config(Path(HOST_YAML))
        cls.stamp = "20261120t190000z"

    def test_un_snapshot_par_pvc(self):
        documents = self.snapshot.snapshot_documents(
            self.config, stamp_value=self.stamp,
            snapshot_class="ameesh-pod-snapshots")
        self.assertEqual(len(documents), 2)
        self.assertEqual([d["metadata"]["name"] for d in documents],
                         ["pvc-alice-" + self.stamp, "pvc-bruno-" + self.stamp])
        for document, slug in zip(documents, ("alice", "bruno")):
            self.assertEqual(document["kind"], "VolumeSnapshot")
            self.assertEqual(document["apiVersion"],
                             "snapshot.storage.k8s.io/v1")
            self.assertEqual(document["spec"]["source"],
                             {"persistentVolumeClaimName": "pvc-" + slug})
            self.assertEqual(document["spec"]["volumeSnapshotClassName"],
                             "ameesh-pod-snapshots")
            self.assertEqual(document["metadata"]["labels"]
                             ["ameesh.dev/tenant"], slug)
            self.assertEqual(document["metadata"]["labels"]
                             ["ameesh.dev/snapshot-of"], "pvc-" + slug)

    def test_restauration_meme_nom_et_datasource(self):
        documents = self.snapshot.restore_documents(
            self.config, stamp_value=self.stamp)
        self.assertEqual([d["metadata"]["name"] for d in documents],
                         ["pvc-alice", "pvc-bruno"])
        for document, slug in zip(documents, ("alice", "bruno")):
            self.assertEqual(document["spec"]["dataSource"], {
                "name": "pvc-%s-%s" % (slug, self.stamp),
                "kind": "VolumeSnapshot",
                "apiGroup": "snapshot.storage.k8s.io",
            })
            self.assertEqual(document["spec"]["accessModes"], ["ReadWriteOnce"])
            self.assertEqual(document["spec"]["resources"]["requests"]
                             ["storage"], "20Gi")

    def test_horodatage_et_classe_refuses(self):
        with self.assertRaises(self.snapshot.SnapshotError):
            self.snapshot.stamp("2026-11-20")
        with self.assertRaises(self.snapshot.SnapshotError):
            self.snapshot.snapshot_documents(
                self.config, stamp_value=self.stamp,
                snapshot_class="Classe Invalide")
        self.assertEqual(self.snapshot.stamp("20261120T190000Z"),
                         "20261120t190000z")


if __name__ == "__main__":
    unittest.main()
