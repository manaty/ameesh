# SPDX-License-Identifier: AGPL-3.0-only
"""#7694 (P1 de #7633) : socle d'un hôte pod.

Design `docs/design/7633-hote-avatars-pod.md` : `policy.runtime` au canon,
plan de tenant (`pod_plan`, aucun geste root), UID vérifié par le battement,
relevé d'hôte pod (capacité déclarée, pas de `load1`), `pod_hosts` minimal,
chart squelette et témoin kind (identité, montage, permissions). Les contrôles
`kind` réels sont dans `deploy/k8s/pod-witness/` (geste de bureau) ; ce module
vérifie le contrats statique et rejoue le plan sans cluster.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import urllib.parse
import uuid
from contextlib import redirect_stdout
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from ameesh import (canon, config, db as db_mod, resources,
                    runner as runner_mod, storage, tenancy)

from . import test_canon
from .support import REPO, PgTestCase
from .test_canon import _CanonDbCase, codes, write

ALICE, BRUNO = "human:alice", "human:bruno"
POD_HOST = "pod-alice-exemple"
POD_DIR = os.path.join(REPO, "deploy", "k8s", "pod-socle")
WITNESS_DIR = os.path.join(REPO, "deploy", "k8s", "pod-witness")


def pod_card(title: str = "pod-banc", *, kind: str = "pod",
             namespace: str = "ameesh",
             workload: str = "statefulset/ameesh-pod-alice",
             isolation: str = "per-tenant-volume",
             ref: str = "manaty/ameesh#7694",
             shared: bool = True, tenants=(ALICE, BRUNO),
             max_agents: int = 6, volatile: bool = False,
             credential_modes=("api-key",), resources_yaml: str = "    max_cgroup_memory_used: 80%",
             runtime_extra: str = "", policy_extra: str = "") -> str:
    """Fiche Host pod en frontmatter YAML, pour `canon.load`."""
    tenants_yaml = "[%s]" % ", ".join(tenants) if tenants else "[]"
    lines = [
        "---",
        "type: Host",
        "title: %s" % title,
        "responsible: %s" % ALICE,
        "tags: [pod]",
        "policy:",
        "  harnesses: [claude, codex]",
        "  providers: [anthropic, openai]",
        "  credential_modes: [%s]" % ", ".join(credential_modes),
        "  shared: %s" % ("true" if shared else "false"),
        "  tenants: %s" % tenants_yaml,
        "  max_agents: %d" % max_agents,
        "  lease_ttl: 120",
        "  volatile: %s" % ("true" if volatile else "false"),
        "  runtime:",
        "    kind: %s" % kind,
        "    namespace: %s" % namespace,
        "    workload: %s" % workload,
        "    account_isolation: %s" % isolation,
        "    isolation_ref: \"%s\"" % ref,
    ]
    if runtime_extra:
        lines.extend("    " + line for line in runtime_extra.splitlines())
    lines += [
        "  resources:",
        resources_yaml,
        "  workspaces:",
        "    root: \"~/work\"",
    ]
    if policy_extra:
        lines.extend("  " + line for line in policy_extra.splitlines())
    lines += ["---", "", "# fiche", ""]
    return "\n".join(lines)


class PodCanonTest(test_canon._TmpMixin, unittest.TestCase):
    """`policy.runtime` : lecture, invariants et refus (design 7633 §7.1)."""

    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def errors(self):
        return codes(canon.validate(self.load()), canon.ERROR)

    def warnings(self):
        return codes(canon.validate(self.load()), canon.WARNING)

    def test_fiche_pod_valide(self):
        write(self.root, "hotes/pod-banc.md", pod_card())
        host = self.load().host("pod-banc")
        self.assertEqual((host.policy.runtime.kind, host.policy.runtime.namespace,
                          host.policy.runtime.workload,
                          host.policy.runtime.account_isolation),
                         ("pod", "ameesh", "statefulset/ameesh-pod-alice",
                          "per-tenant-volume"))
        self.assertEqual(host.policy.runtime.isolation_ref, "manaty/ameesh#7694")
        self.assertEqual(self.errors(), set())
        self.assertNotIn("host-pod-load-unmeasured", self.warnings())

    def test_runtime_exige_partage_et_tenants(self):
        write(self.root, "hotes/pod-banc.md", pod_card(shared=False, tenants=()))
        found = self.errors()
        self.assertIn("host-pod-not-shared", found)
        self.assertIn("host-pod-no-tenant", found)

    def test_kind_pod_exige_volume_par_tenant(self):
        write(self.root, "hotes/pod-banc.md",
              pod_card(isolation="per-pod-empty-dir"))
        self.assertIn("host-pod-account-isolation", self.errors())

    def test_kind_pod_template_fige(self):
        write(self.root, "hotes/template.md", pod_card(
            "template", kind="pod-template", isolation="per-pod-empty-dir",
            max_agents=2, volatile=False))
        found = self.errors()
        self.assertIn("host-pod-template-max-agents", found)
        self.assertIn("host-pod-template-volatile", found)
        self.assertNotIn("host-pod-account-isolation", found)
        write(self.root, "hotes/template.md", pod_card(
            "template", kind="pod-template", isolation="per-tenant-volume",
            max_agents=1, volatile=True))
        self.assertIn("host-pod-account-isolation", self.errors())

    def test_kind_pod_template_sans_siege(self):
        write(self.root, "hotes/template.md", pod_card(
            "template", kind="pod-template", isolation="per-pod-empty-dir",
            max_agents=1, volatile=True,
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true"))
        self.assertIn("host-pod-template-subscription", self.errors())

    def test_isolation_ref_obligatoire_et_citable(self):
        write(self.root, "hotes/pod-banc.md", pod_card(ref=""))
        self.assertIn("host-runtime-invalid", self.errors())
        write(self.root, "hotes/pod-banc.md", pod_card(ref="pas-une-ref"))
        self.assertIn("host-runtime-ref-weak", self.warnings())

    def test_namespace_et_workload(self):
        write(self.root, "hotes/pod-banc.md",
              pod_card(namespace="Am_eesh", workload="foo"))
        self.assertIn("host-runtime-invalid", self.errors())
        self.assertIn("host-runtime-workload-weak", self.warnings())

    def test_max_load_averti_mais_accepte(self):
        write(self.root, "hotes/pod-banc.md", pod_card(
            resources_yaml="    max_load: 4\n    max_cgroup_throttled: 20%"))
        self.assertEqual(self.errors(), set())
        self.assertIn("host-pod-load-unmeasured", self.warnings())

    def test_cle_runtime_inconnue_avertie(self):
        write(self.root, "hotes/pod-banc.md",
              pod_card(runtime_extra="inconnue: 1"))
        self.assertIn("host-runtime-unknown", self.warnings())
        self.assertEqual(self.errors(), set())

    def test_kind_inconnu_refuse(self):
        write(self.root, "hotes/pod-banc.md", pod_card(kind="vm"))
        self.assertIn("host-runtime-invalid", self.errors())
        self.assertIsNone(self.load().host("pod-banc").policy.runtime)

    def test_placement_interdit_sur_un_template(self):
        write(self.root, "hotes/template.md", pod_card(
            "template", kind="pod-template", isolation="per-pod-empty-dir",
            max_agents=1, volatile=True))
        write(self.root, "hotes/pod-banc.md", pod_card())
        loaded = self.load()
        reasons = canon.placement_violations(
            {"responsible": ALICE, "harness": "codex", "provider": "openai",
             "credential_mode": "api-key"}, loaded.host("template"))
        self.assertTrue(any("pod-template" in reason for reason in reasons), reasons)
        self.assertEqual(canon.placement_violations(
            {"responsible": ALICE, "harness": "codex", "provider": "openai",
             "credential_mode": "api-key"}, loaded.host("pod-banc")), [])


class PodPlanTest(unittest.TestCase):
    """`pod_plan()` : plan de données, aucun geste root, fsGroup unique."""

    def test_uid_stable_et_dans_la_plage(self):
        self.assertEqual(tenancy.pod_uid(ALICE), tenancy.pod_uid(ALICE))
        self.assertGreaterEqual(tenancy.pod_uid(ALICE), tenancy.POD_UID_BASE)
        self.assertLess(tenancy.pod_uid(ALICE),
                        tenancy.POD_UID_BASE + tenancy.POD_UID_SPAN)
        self.assertNotEqual(tenancy.pod_uid(ALICE), tenancy.pod_uid(BRUNO))

    def test_uid_reserves_et_hors_plage_refuses(self):
        for uid in (0, 999, 10000, 10001, 60001):
            with self.subTest(uid=uid):
                with self.assertRaises(tenancy.TenancyError) as caught:
                    tenancy.check_pod_uid(uid)
                self.assertEqual(caught.exception.code, "tenant-uid-invalid")

    def test_plan_alice(self):
        plan = tenancy.pod_plan(POD_HOST, ALICE)
        uid = tenancy.pod_uid(ALICE)
        self.assertEqual((plan["tenant"], plan["unix_user"], plan["uid"],
                          plan["fs_group"], plan["home"], plan["runner_id"]),
                         (ALICE, "ameesh-alice", uid, 10000,
                          "/home/ameesh-alice", POD_HOST + "/alice"))
        self.assertIn("ameesh:x:10001:10000:ameesh pod supervisor:/home/ameesh:/bin/sh",
                      plan["passwd"])
        self.assertIn("ameesh-alice:x:%d:10000:tenant alice:/home/ameesh-alice:/bin/sh"
                      % uid, plan["passwd"])
        self.assertIn("ameesh-pod:x:10000:", plan["group"])
        self.assertEqual(plan["pod_security_context"]["runAsUser"],
                         tenancy.POD_SUPERVISOR_UID)
        self.assertEqual(plan["pod_security_context"]["runAsGroup"], 10000)
        self.assertTrue(plan["pod_security_context"]["runAsNonRoot"])
        self.assertEqual(plan["container"]["resources"]["limits"]["cpu"], "2")
        self.assertEqual(plan["volumes"]["home"]["persistentVolumeClaim"]["claimName"],
                         "pvc-alice")
        self.assertEqual(plan["volumes"]["home"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(plan["volumes"]["home"]["mountPath"], "/home/ameesh-alice")
        self.assertEqual(plan["pod_security_context"]["fsGroup"], 10000)
        self.assertEqual(plan["pod_security_context"]["fsGroupChangePolicy"],
                         "OnRootMismatch")
        security = plan["container"]["securityContext"]
        self.assertEqual((security["runAsUser"], security["runAsGroup"]),
                         (uid, 10000))
        self.assertTrue(security["runAsNonRoot"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertFalse(security["privileged"])
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertEqual(security["seccompProfile"]["type"], "RuntimeDefault")
        env = {entry["name"]: entry["value"] for entry in plan["container"]["env"]}
        self.assertEqual(env["AMEESH_TENANT"], ALICE)
        self.assertEqual(env["AMEESH_CONFIG"], "/etc/ameesh/tenant/config.json")
        self.assertEqual(env["AMEESH_CANON_SYNC_INTERVAL"], "0")
        # B1 de #44265 : le canon est cloné sous 10001 et lu sous l'UID du
        # tenant ; la confiance Git est bornée à CE dépôt, jamais `*`.
        self.assertEqual(env["AMEESH_CANON"], tenancy.POD_CANON_REPO)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], tenancy.POD_CANON_REPO)
        self.assertNotIn("*", [env["GIT_CONFIG_VALUE_0"]])
        mounts = {m["name"]: m for m in plan["container"]["volumeMounts"]}
        self.assertEqual(mounts["home-alice"]["mountPath"], "/home/ameesh-alice")
        self.assertTrue(mounts["passwd"]["readOnly"])
        self.assertTrue(mounts["group"]["readOnly"])
        self.assertEqual(mounts["canon"]["mountPath"], "/var/lib/ameesh/canon")
        self.assertTrue(mounts["canon"]["readOnly"])
        self.assertNotIn("gid", plan)

    def test_uid_du_chart_est_la_source_de_verite(self):
        plan = tenancy.pod_plan(POD_HOST, ALICE, uid=2600)
        self.assertEqual(plan["uid"], 2600)
        self.assertIn("ameesh-alice:x:2600:10000:tenant alice:", plan["passwd"])
        with self.assertRaises(tenancy.TenancyError):
            tenancy.pod_plan(POD_HOST, ALICE, uid=10000)

    def test_plan_sans_geste_root(self):
        plan = tenancy.pod_plan(POD_HOST, ALICE)
        text = tenancy.pod_plan_text(plan)
        self.assertNotIn("useradd --", text)
        self.assertNotIn("loginctl", text)
        self.assertNotIn("chown", text)
        self.assertIn("one neutral fsGroup 10000", text)
        self.assertIn("--register", text)
        self.assertIn("--uid %d" % plan["uid"], text)

    def test_cli_add_pod_imprime_le_plan(self):
        runtime = canon.PodRuntime(kind="pod", namespace="ameesh",
                                   workload="statefulset/ameesh-pod-alice",
                                   account_isolation="per-tenant-volume",
                                   isolation_ref="manaty/ameesh#7694")
        sortie = io.StringIO()
        with mock.patch.object(config, "load",
                               return_value=types.SimpleNamespace(host=POD_HOST)), \
                mock.patch.object(tenancy, "pod_runtime", return_value=runtime), \
                redirect_stdout(sortie):
            code = tenancy.main(["add", ALICE, "--host", POD_HOST, "--pod"])
        self.assertEqual(code, 0)
        texte = sortie.getvalue()
        self.assertIn("no useradd", texte)
        self.assertIn("human:alice", texte)
        self.assertIn("--uid %d" % tenancy.pod_uid(ALICE), texte)

    def test_cli_add_pod_refuse_un_hote_non_pod(self):
        sortie = io.StringIO()
        with mock.patch.object(config, "load",
                               return_value=types.SimpleNamespace(host=POD_HOST)), \
                mock.patch.object(tenancy, "pod_runtime", return_value=None), \
                redirect_stdout(sortie):
            code = tenancy.main(["add", ALICE, "--host", POD_HOST, "--pod"])
        self.assertEqual(code, 1)
        self.assertIn("tenant-not-pod", sortie.getvalue())

    def test_cli_uid_sans_pod_refuse(self):
        sortie = io.StringIO()
        with mock.patch.object(config, "load",
                               return_value=types.SimpleNamespace(host=POD_HOST)), \
                redirect_stdout(sortie):
            code = tenancy.main(["add", ALICE, "--host", POD_HOST,
                                 "--uid", "2519"])
        self.assertEqual(code, 1)
        self.assertIn("tenant-pod-flag-required", sortie.getvalue())

    def test_deux_tenants_ne_partagent_pas_de_volume(self):
        alice = tenancy.pod_plan(POD_HOST, ALICE)
        bruno = tenancy.pod_plan(POD_HOST, BRUNO)
        self.assertNotEqual(alice["uid"], bruno["uid"])
        self.assertNotEqual(alice["volumes"]["home"]["persistentVolumeClaim"]["claimName"],
                            bruno["volumes"]["home"]["persistentVolumeClaim"]["claimName"])
        self.assertEqual(alice["pod_security_context"]["fsGroup"],
                         bruno["pod_security_context"]["fsGroup"])


class PodConfigTest(unittest.TestCase):
    def test_env_pod(self):
        cfg = config.load(env={
            "AMEESH_POD": "1",
            "AMEESH_POD_MEMORY_BYTES": "2147483648",
            "AMEESH_POD_CPU_CORES": "2",
        })
        self.assertTrue(cfg.pod)
        self.assertEqual(cfg.pod_memory_bytes, 2147483648)
        self.assertEqual(cfg.pod_cpu_cores, 2.0)

    def test_capacite_illisible_n_est_pas_zero(self):
        cfg = config.load(env={"AMEESH_POD": "1",
                               "AMEESH_POD_CPU_CORES": "beaucoup",
                               "AMEESH_POD_MEMORY_BYTES": "-1"})
        self.assertIsNone(cfg.pod_cpu_cores)
        self.assertIsNone(cfg.pod_memory_bytes)


class PodResourcesTest(unittest.TestCase):
    def test_sample_pod_capacite_declaree_pas_load1(self):
        mib = 1024 * 1024
        reading = resources.sample_pod(
            POD_HOST, now=100.0, memory_bytes=2048 * mib, cpu_cores=2.0,
            cgroup={"mem_current_bytes": 512 * mib, "cpu_throttled_usec": 0})
        self.assertIsNone(reading["load1"])
        self.assertEqual(reading["mem_total_bytes"], 2048 * mib)
        self.assertEqual(reading["mem_available_bytes"], 1536 * mib)
        self.assertEqual(reading["cpu_count"], 2)
        self.assertTrue(reading["pod_runtime"])
        self.assertIsNone(reading["disk_free_bytes"])

    def test_host_limits_ignore_max_load_sur_un_pod(self):
        fiche = types.SimpleNamespace(policy=types.SimpleNamespace(
            runtime=canon.PodRuntime(kind="pod", namespace="ameesh",
                                     workload="statefulset/x",
                                     account_isolation="per-tenant-volume",
                                     isolation_ref="manaty/ameesh#7694"),
            resources={"max_load": 2, "max_cgroup_memory_used": "80%"}))
        class FauxCanon:
            id = "test"
            def host(self, name):
                return fiche if name == POD_HOST else None
        limits = resources.host_limits([FauxCanon()], POD_HOST)["limits"]
        self.assertNotEqual(limits["max_load"], 2.0)
        self.assertEqual(limits["max_cgroup_memory_used"], 0.8)


class TenantPodDbTest(_CanonDbCase):
    """Liaison pod : UID du chart, battement vérifié, canon non synchronisé."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "hotes/pod-banc.md", pod_card("pod-banc"))
        self.sync("pod-banc")
        self.host = "pod-banc"
        self.uid = tenancy.pod_uid(ALICE)

    def test_enregistrement_exige_l_uid_du_chart(self):
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.register(self.db, self.host, ALICE, actor="human:alice",
                             local_host=self.host, pod=True)
        self.assertEqual(caught.exception.code, "tenant-uid-required")
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.register(self.db, self.host, ALICE, actor="human:alice",
                             local_host=self.host, pod=True, uid=10000)
        self.assertEqual(caught.exception.code, "tenant-uid-invalid")
        row = tenancy.register(self.db, self.host, ALICE, actor="human:alice",
                               local_host=self.host, pod=True, uid=self.uid)
        self.assertEqual((row["uid"], row["runner_id"], row["unix_user"]),
                         (self.uid, self.host + "/alice", "ameesh-alice"))

    def test_battement_verifie_l_uid(self):
        tenancy.register(self.db, self.host, ALICE, actor="human:alice",
                         local_host=self.host, pod=True, uid=self.uid)
        cfg = dataclasses.replace(self.cfg, host=self.host, tenant=ALICE,
                                  runner_id=self.host + "/alice")
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid):
            self.assertTrue(tenancy.beat(cfg, self.db, version="v"))
        presence = storage.of(self.db).tenancy.presence(self.host, ALICE)
        binding = storage.of(self.db).tenancy.bindings(self.host)[0]
        self.assertEqual((binding["uid"], presence["runner_id"]),
                         (self.uid, self.host + "/alice"))
        # Un processus sous un autre UID ne publie AUCUNE présence.
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid + 1):
            self.assertFalse(tenancy.beat(cfg, self.db, version="intrus"))
        presence = storage.of(self.db).tenancy.presence(self.host, ALICE)
        self.assertEqual(presence["version"], "v")

    def test_canon_sync_once_saute_un_runner_de_tenant(self):
        fake = types.SimpleNamespace(
            mediated=False,
            cfg=dataclasses.replace(self.cfg, tenant=ALICE, host=self.host))
        with mock.patch.object(canon, "configured",
                               side_effect=AssertionError("canon relu par un tenant")):
            self.assertTrue(runner_mod.Runner.canon_sync_once.__wrapped__(fake))


class PodHostsDbTest(PgTestCase):
    """`pod_hosts` : ligne du pourvoyeur (P4), lecture, expiration, retrait."""

    def setUp(self) -> None:
        super().setUp()
        self.api = storage.of(self.db).pod_hosts
        self.host = "pod-enfant-%s" % uuid.uuid4().hex[:10]
        self.pod_uid = "uid-%s" % uuid.uuid4().hex

    def record(self, **overrides):
        values = dict(namespace="ameesh", template="pod-template",
                      parent="agent:parent", tenant=ALICE, uid=2519,
                      runner_id=self.host + "/alice", pod_uid=self.pod_uid,
                      expires_ts=time.time() + 600)
        values.update(overrides)
        return self.api.record(self.host, **values)

    def test_ecriture_lecture_et_activite(self):
        row = self.record()
        self.assertEqual((row["host"], row["state"], row["tenant"], row["uid"]),
                         (self.host, "running", ALICE, 2519))
        self.assertTrue(self.api.active(self.host, self.pod_uid))
        self.assertFalse(self.api.active(self.host, self.pod_uid + "-autre"))
        self.assertEqual(self.api.get(self.host)["pod_uid"], self.pod_uid)

    def test_expiration_ferme_la_reclamation(self):
        self.record()
        self.db.execute(
            "UPDATE pod_hosts SET requested_at = now() - interval '10 seconds',"
            " expires_at = now() - interval '1 second' WHERE host = %s",
            (self.host,))
        self.assertFalse(self.api.active(self.host, self.pod_uid))
        self.assertFalse(self.api.active(self.host, ""))

    def test_retrait(self):
        self.record()
        self.assertTrue(self.api.remove(self.host))
        self.assertEqual(self.api.get(self.host)["state"], "deleted")
        self.assertFalse(self.api.active(self.host, self.pod_uid))
        self.assertFalse(self.api.remove("absent-%s" % uuid.uuid4().hex))

    def test_etat_et_pod_uid_contraints(self):
        with self.assertRaises(ValueError):
            self.record(state="bogus")
        with self.assertRaises(ValueError):
            self.api.remove(self.host, state="bogus")
        self.record()
        with self.assertRaises(db_mod.DbError):
            self.api.record("autre-%s" % uuid.uuid4().hex,
                            namespace="ameesh", template="pod-template",
                            parent="agent:parent", tenant=ALICE, uid=2519,
                            runner_id="autre/alice", pod_uid=self.pod_uid,
                            expires_ts=time.time() + 600)


@unittest.skipIf(yaml is None, "PyYAML absent : chart non vérifié (kubectl kustomize le fait)")
class ChartTest(unittest.TestCase):
    """Chart squelette : invariants statiques (design 7633 §4, §6)."""

    @classmethod
    def setUpClass(cls) -> None:
        with open(os.path.join(POD_DIR, "kustomization.yaml"), encoding="utf-8") as fh:
            cls.kustomization = yaml.safe_load(fh)
        cls.docs = []
        for name in cls.kustomization["resources"]:
            with open(os.path.join(POD_DIR, name), encoding="utf-8") as fh:
                cls.docs.extend(d for d in yaml.safe_load_all(fh) if d)
        cls.plan = tenancy.pod_plan(POD_HOST, ALICE)

    def of_kind(self, kind):
        return [d for d in self.docs if d.get("kind") == kind]

    def statefulset(self):
        (sts,) = self.of_kind("StatefulSet")
        return sts

    def pod_spec(self):
        return self.statefulset()["spec"]["template"]["spec"]

    def test_chargement_et_un_seul_statefulset(self):
        self.assertEqual(len(self.of_kind("StatefulSet")), 1)
        self.assertEqual(self.statefulset()["spec"]["replicas"], 1)
        self.assertEqual(self.of_kind("Secret"), [])
        self.assertEqual(self.of_kind("Service"), [])
        self.assertEqual(self.of_kind("Role"), [])
        self.assertEqual(self.of_kind("RoleBinding"), [])

    def test_security_context_du_pod(self):
        pod = self.pod_spec()
        sc = pod["securityContext"]
        self.assertTrue(sc["runAsNonRoot"])
        self.assertEqual(sc["fsGroup"], tenancy.POD_FS_GROUP)
        self.assertEqual(sc["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(sc["seccompProfile"]["type"], "RuntimeDefault")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["hostNetwork"])
        self.assertFalse(pod["hostPID"])
        self.assertFalse(pod["hostIPC"])
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 120)
        # Un seul fsGroup, au niveau du pod : jamais un fsGroup par tenant.
        occurrences = []
        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "fsGroup":
                        occurrences.append((path + "/" + key, value))
                    walk(value, path + "/" + key)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, path + "/%d" % index)
        walk(self.statefulset())
        self.assertEqual([value for _, value in occurrences], [tenancy.POD_FS_GROUP])

    def test_superviseur_sans_volume_de_tenant(self):
        pod = self.pod_spec()
        supervisor = next(c for c in pod["containers"] if c["name"] == "supervisor")
        mounts = {m["name"]: m for m in supervisor["volumeMounts"]}
        self.assertNotIn("home-alice", mounts)
        self.assertNotIn("pvc-alice", [m.get("name") for m in supervisor["volumeMounts"]])
        self.assertEqual({m["mountPath"] for m in supervisor["volumeMounts"]},
                         {"/home/ameesh", "/etc/ameesh/supervisor",
                          "/var/lib/ameesh/canon", "/tmp"})
        env = {e["name"]: e.get("value") for e in supervisor["env"]}
        self.assertEqual(env["AMEESH_POD"], "1")
        self.assertEqual(env["AMEESH_POD_MEMORY_BYTES"], "2147483648")
        self.assertEqual(env["AMEESH_POD_CPU_CORES"], "2")
        self.assertEqual(env["AMEESH_HOST"], POD_HOST)

    def test_tenant_monte_son_seul_pvc(self):
        pod = self.pod_spec()
        tenant = next(c for c in pod["containers"] if c["name"] == "tenant-alice")
        mounts = {m["name"]: m for m in tenant["volumeMounts"]}
        self.assertEqual(mounts["home-alice"]["mountPath"], "/home/ameesh-alice")
        claims = [v for v in pod["volumes"]
                  if v.get("persistentVolumeClaim", {}).get("claimName", "").startswith("pvc-")]
        self.assertEqual([v["name"] for v in claims], ["home-alice"])
        self.assertEqual(claims[0]["persistentVolumeClaim"]["claimName"], "pvc-alice")
        self.assertNotIn("canon-key", mounts)
        self.assertEqual(mounts["canon"]["readOnly"], True)
        self.assertTrue(mounts["passwd"]["readOnly"])
        self.assertEqual(mounts["passwd"]["subPath"], "passwd")
        self.assertEqual(mounts["group"]["subPath"], "group")
        self.assertEqual(mounts["tenant-config"]["mountPath"], "/etc/ameesh/tenant")
        self.assertEqual(mounts["tenant-tmp"]["mountPath"], "/tmp")
        env = {e["name"]: e.get("value") for e in tenant["env"]}
        self.assertEqual((env["AMEESH_HOST"], env["AMEESH_TENANT"],
                          env["AMEESH_RUNNER_ID"], env["AMEESH_CONFIG"]),
                         (POD_HOST, ALICE, POD_HOST + "/alice",
                          "/etc/ameesh/tenant/config.json"))
        self.assertEqual(env["AMEESH_CANON_SYNC_INTERVAL"], "0")

    def test_tenant_safe_directory_borne_au_canon(self):
        """B1 de #44265 : le chart porte la même paire que le plan, bornée au
        dépôt du canon (le clone est sous 10001, la lecture sous l'UID du
        tenant) — jamais `*`, jamais un fichier global."""
        tenant = next(c for c in self.pod_spec()["containers"]
                      if c["name"] == "tenant-alice")
        chart = {e["name"]: e.get("value") for e in tenant["env"]}
        plan = {e["name"]: e.get("value") for e in self.plan["container"]["env"]}
        cles = ("AMEESH_CANON", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0")
        self.assertEqual({k: chart[k] for k in cles}, {k: plan[k] for k in cles})
        self.assertEqual(chart["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(chart["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(chart["GIT_CONFIG_VALUE_0"], tenancy.POD_CANON_REPO)
        self.assertNotEqual(chart["GIT_CONFIG_VALUE_0"], "*")
        self.assertFalse([k for k in chart
                          if k.startswith("GIT_CONFIG_KEY_")
                          and k != "GIT_CONFIG_KEY_0"])

    def test_uid_et_security_context_du_tenant(self):
        tenant = next(c for c in self.pod_spec()["containers"]
                      if c["name"] == "tenant-alice")
        sc = tenant["securityContext"]
        self.assertEqual(sc["runAsUser"], self.plan["uid"])
        self.assertEqual(sc["runAsGroup"], tenancy.POD_FS_GROUP)
        self.assertTrue(sc["runAsNonRoot"])
        self.assertFalse(sc["allowPrivilegeEscalation"])
        self.assertFalse(sc["privileged"])
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
        self.assertEqual(sc["seccompProfile"]["type"], "RuntimeDefault")
        self.assertFalse([v for v in self.pod_spec()["volumes"] if "hostPath" in v])
        self.assertNotIn("ports", tenant)
        self.assertNotIn("canon-key", [m["name"] for m in tenant["volumeMounts"]])

    def test_configmaps_passwd_et_configuration(self):
        by_name = {d["metadata"]["name"]: d for d in self.of_kind("ConfigMap")}
        passwd = by_name["ameesh-pod-passwd"]["data"]["passwd"]
        group = by_name["ameesh-pod-passwd"]["data"]["group"]
        # La ConfigMap du chart est EXACTEMENT le fichier du plan : le
        # `host_tenants.uid` et l'entrée passwd ne peuvent pas diverger en
        # silence (design 7633 §3.1).
        self.assertEqual(passwd.rstrip("\n"), self.plan["passwd"])
        self.assertEqual(group.rstrip("\n"), self.plan["group"])
        tenant = json.loads(by_name["ameesh-pod-tenant-alice"]["data"]["config.json"])
        self.assertEqual((tenant["host"], tenant["tenant"], tenant["runner_id"],
                          tenant["canon_sync_interval"]),
                         (POD_HOST, ALICE, POD_HOST + "/alice", 0))
        supervisor = json.loads(by_name["ameesh-pod-supervisor"]["data"]["config.json"])
        self.assertEqual((supervisor["host"], supervisor["pod"]), (POD_HOST, True))
        for config_map in (tenant, supervisor):
            netloc = urllib.parse.urlsplit(config_map["dsn"]).netloc
            self.assertNotIn(":", netloc.split("@")[0])

    def test_aucun_secret_dans_le_chart(self):
        with open(os.path.join(POD_DIR, "secrets.exemple.yaml"), encoding="utf-8") as fh:
            declares = [d for d in yaml.safe_load_all(fh) if d]
        self.assertTrue(declares)
        for doc in declares:
            for champ in ("data", "stringData"):
                for value in (doc.get(champ) or {}).values():
                    self.assertIn(value, ("", None))
        noms = {d["metadata"]["name"] for d in declares}
        self.assertIn("ameesh-db", noms)
        self.assertIn("ameesh-canon-deploy-key", noms)
        self.assertIn("configmaps.yaml", self.kustomization["resources"])


class SafeDirectoryTest(unittest.TestCase):
    """B1 de #44265 : la paire du plan lève « dubious ownership » sans `*`.

    `GIT_TEST_ASSUME_DIFFERENT_OWNER` force le contrôle d'appropriation que le
    tenant rencontre face au clone du superviseur ; la preuve à UID réellement
    différent est le témoin kind (`temoin-canon.yaml`).
    """

    def env_base(self, tmp: str) -> dict:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "true",
        }

    def test_dubious_ownership_leve_par_la_paire_du_plan(self):
        if shutil.which("git") is None:  # pragma: no cover - environnement
            self.skipTest("git absent")
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            subprocess.run(["git", "init", "-q", repo], check=True)
            base = self.env_base(tmp)
            refus = subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"],
                                   env=base, capture_output=True, text=True)
            self.assertNotEqual(refus.returncode, 0)
            self.assertIn("dubious ownership", refus.stderr)
            plan = tenancy.pod_plan(POD_HOST, ALICE)
            pair = {e["name"]: e["value"] for e in plan["container"]["env"]
                    if e["name"].startswith("GIT_CONFIG_") or e["name"] == "AMEESH_CANON"}
            self.assertEqual(pair["GIT_CONFIG_VALUE_0"], tenancy.POD_CANON_REPO)
            self.assertEqual(pair["GIT_CONFIG_COUNT"], "1")
            self.assertNotEqual(pair["GIT_CONFIG_VALUE_0"], "*")
            accepte = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--git-dir"],
                env={**base,
                     "GIT_CONFIG_COUNT": pair["GIT_CONFIG_COUNT"],
                     "GIT_CONFIG_KEY_0": pair["GIT_CONFIG_KEY_0"],
                     "GIT_CONFIG_VALUE_0": repo},
                capture_output=True, text=True)
            self.assertEqual(accepte.returncode, 0, accepte.stderr)
            self.assertEqual(accepte.stdout.strip(), ".git")

    def test_canon_git_ne_fait_confiance_qu_au_canon_provisionne(self):
        """Le lecteur RÉEL (`canon._git`, dont l'environnement filtre les
        `GIT_CONFIG_*`) ne déclare `safe.directory` que pour le dépôt du canon
        d'un tenant pod : un dépôt étranger, ou un hôte hors pod, gardent le
        refus de Git pour un propriétaire différent (B de #44282/#44283)."""
        if shutil.which("git") is None:  # pragma: no cover - environnement
            self.skipTest("git absent")
        self.assertEqual(canon.POD_CANON_REPO, tenancy.POD_CANON_REPO)
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "canon")
            other = os.path.join(tmp, "autre")
            for path in (repo, other):
                subprocess.run(["git", "init", "-q", path], check=True)
            # hors pod : jamais de confiance, même sur le chemin provisionné
            with mock.patch.object(canon, "POD_CANON_REPO", repo), \
                    mock.patch.dict(os.environ, {"AMEESH_TENANT": ""}):
                self.assertIsNone(canon._git_safe_directory(repo))
            with mock.patch.object(canon, "POD_CANON_REPO", repo):
                # pod, mais dépôt étranger : refusé
                with mock.patch.dict(os.environ, {"AMEESH_TENANT": ALICE}):
                    self.assertEqual(canon._git_safe_directory(repo),
                                     os.path.realpath(repo))
                    self.assertIsNone(canon._git_safe_directory(other))
                    with mock.patch.dict(
                            os.environ,
                            {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "true"}):
                        proc = canon._git(repo, "rev-parse", "--git-dir")
                        refus = canon._git(other, "rev-parse", "--git-dir")
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.strip(), b".git")
                    self.assertNotEqual(refus.returncode, 0)
                    self.assertIn(b"dubious ownership", refus.stderr)
            # hors pod, même chemin : le dépôt d'un autre propriétaire est refusé
            with mock.patch.object(canon, "POD_CANON_REPO", repo), \
                    mock.patch.dict(os.environ, {"AMEESH_TENANT": "",
                                                 "GIT_TEST_ASSUME_DIFFERENT_OWNER": "true"}):
                refus = canon._git(repo, "rev-parse", "--git-dir")
            self.assertNotEqual(refus.returncode, 0)
            self.assertIn(b"dubious ownership", refus.stderr)
            # …sans relâcher le filtrage de la configuration Git injectée.
            with mock.patch.dict(os.environ,
                                 {"GIT_CONFIG_COUNT": "1",
                                  "GIT_CONFIG_KEY_0": "safe.directory",
                                  "GIT_CONFIG_VALUE_0": "*"}):
                self.assertNotIn("GIT_CONFIG_COUNT", canon._git_env())
                self.assertNotIn("GIT_CONFIG_KEY_0", canon._git_env())


@unittest.skipIf(yaml is None, "PyYAML absent : témoin non vérifié (bash -n reste possible)")
class WitnessTest(unittest.TestCase):
    """Témoin kind : manifestes, script, absence de fsGroup au contrôle 3."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = os.path.join(WITNESS_DIR, "preuve-isolation-comptes.sh")
        cls.rendu = subprocess.run(
            [cls.script, "--render-only"], check=True, text=True,
            capture_output=True)
        cls.docs = [d for d in yaml.safe_load_all(cls.rendu.stdout) if d]

    def of_kind(self, kind):
        return [d for d in self.docs if d.get("kind") == kind]

    def test_script_executable_et_syntaxe(self):
        self.assertTrue(os.access(self.script, os.X_OK))
        subprocess.run(["bash", "-n", self.script], check=True)
        with open(self.script, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("exit 77", text)
        self.assertIn("kind delete cluster", text)
        self.assertIn("kind create cluster", text)
        self.assertIn("--render-only", text)

    def test_rendu_placeholders_remplaces(self):
        self.assertEqual(len(self.of_kind("PersistentVolumeClaim")), 2)
        # #7698 : le pod dédié du déplacement pod partagé -> pod dédié.
        self.assertEqual(len(self.of_kind("Pod")), 4)
        rendered = self.rendu.stdout
        for placeholder in ("__UID_A__", "__UID_B__", "__FS_GROUP__", "__IMAGE__",
                            "__GIT_IMAGE__", "__SUPERVISOR_UID__"):
            self.assertNotIn(placeholder, rendered)
        self.assertIn("2519", rendered)
        self.assertIn("2520", rendered)
        self.assertIn("10000", rendered)
        self.assertIn("alpine/git", rendered)

    def test_temoin_dedie_meme_pvc_et_un_seul_tenant(self):
        # #7698 : pod dédié de A, même PVC et même UID que dans le pod partagé,
        # sans le volume ni le home de B.
        pod = next(d for d in self.of_kind("Pod")
                   if d["metadata"]["name"] == "temoin-dedie")
        self.assertEqual(pod["spec"]["securityContext"]["fsGroup"], 10000)
        containers = pod["spec"]["containers"]
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["name"], "a")
        self.assertEqual(containers[0]["securityContext"]["runAsUser"], 2519)
        volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
        self.assertEqual(volumes["home-a"]["persistentVolumeClaim"]["claimName"],
                         "pvc-temoin-a")
        mount_paths = [m["mountPath"] for m in containers[0]["volumeMounts"]]
        self.assertIn("/home/ameesh-a", mount_paths)
        self.assertNotIn("/home/ameesh-b", mount_paths)

    def test_temoin_deux_un_seul_fsgroup(self):
        pod = next(d for d in self.of_kind("Pod") if d["metadata"]["name"] == "temoin-deux")
        self.assertEqual(pod["spec"]["securityContext"]["fsGroup"], 10000)
        containers = {c["name"]: c for c in pod["spec"]["containers"]}
        self.assertEqual(containers["a"]["securityContext"]["runAsUser"], 2519)
        self.assertEqual(containers["b"]["securityContext"]["runAsUser"], 2520)
        for c in containers.values():
            self.assertNotIn("fsGroup", c.get("securityContext") or {})
        volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
        self.assertEqual(volumes["home-a"]["persistentVolumeClaim"]["claimName"],
                         "pvc-temoin-a")
        self.assertEqual(volumes["home-b"]["persistentVolumeClaim"]["claimName"],
                         "pvc-temoin-b")

    def test_temoin_lecture_sans_fsgroup(self):
        pod = next(d for d in self.of_kind("Pod")
                   if d["metadata"]["name"] == "temoin-lecture")
        self.assertNotIn("fsGroup", pod["spec"].get("securityContext") or {})
        mounts = {m["mountPath"]: m for m in pod["spec"]["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["/mnt/a"]["subPath"], ".config/ameesh/accounts")
        self.assertEqual(mounts["/mnt/b"]["subPath"], ".config/ameesh/accounts")
        self.assertTrue(mounts["/mnt/a"]["readOnly"])
        self.assertTrue(mounts["/mnt/b"]["readOnly"])
        self.assertNotIn("fsGroup", pod["spec"]["containers"][0]["securityContext"])

    def test_temoin_canon_clone_et_lecture_sous_deux_uid(self):
        """B1 de #44265 : clone sous le superviseur, lecture sous le tenant,
        la paire `safe.directory` du plan bornée à CE dépôt et le montage
        de lecture en lecture seule."""
        pod = next(d for d in self.of_kind("Pod")
                   if d["metadata"]["name"] == "temoin-canon")
        init = next(c for c in pod["spec"]["initContainers"] if c["name"] == "clone")
        lecture = next(c for c in pod["spec"]["containers"] if c["name"] == "lecture")
        self.assertEqual(init["securityContext"]["runAsUser"], 10001)
        self.assertEqual(lecture["securityContext"]["runAsUser"], 2519)
        self.assertNotEqual(init["securityContext"]["runAsUser"],
                            lecture["securityContext"]["runAsUser"])
        env = {e["name"]: e["value"] for e in lecture["env"]}
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "/canon/repo")
        self.assertNotEqual(env["GIT_CONFIG_VALUE_0"], "*")
        self.assertFalse([k for k in env
                          if k.startswith("GIT_CONFIG_KEY_") and k != "GIT_CONFIG_KEY_0"])
        mounts = {m["name"]: m for m in lecture["volumeMounts"]}
        self.assertTrue(mounts["canon"]["readOnly"])
        self.assertFalse([m for m in init["volumeMounts"]
                          if m["name"] == "canon" and m.get("readOnly")])

    def test_temoin_canon_ordre_binding_wait_for_first_consumer(self):
        """B2 de #44265 : le consommateur (temoin-deux) est créé AVANT
        l'attente `Bound` — kind installe une StorageClass
        WaitForFirstConsumer, un PVC seul resterait Pending."""
        with open(self.script, encoding="utf-8") as fh:
            text = fh.read()
        pvc = text.index("apply_manifest temoin-pvc.yaml")
        pod = text.index("apply_manifest temoin-deux.yaml")
        bound = text.index("pvc/pvc-temoin-a")
        self.assertLess(pvc, pod)
        self.assertLess(pod, bound)
        self.assertIn("WaitForFirstConsumer", text)


if __name__ == "__main__":
    unittest.main()
