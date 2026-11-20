# SPDX-License-Identifier: AGPL-3.0-only
"""#7695 (P2 de #7633) : pod partagé à N tenants.

Design `docs/design/7633-hote-avatars-pod.md` §2–§6 : le chart rend N
conteneurs de tenant et N PVC, un superviseur sans volume de tenant, un seul
`fsGroup` neutre par PVC ; les profils de compte sont publiés par le battement
(noms seulement, hors du home refusés) ; `require_isolation` et
`ameesh_tenant_claim_ok` protègent les agents d'un tenant sur un hôte pod ;
la base n'admet qu'un superviseur (§3.3, migration 0599) ; le témoin kind
complet est dans `deploy/k8s/pod-witness/`.

Les contrôles `kind` réels (identité, montage, permissions, superviseur) sont
un geste de bureau ; ce module vérifie les contrats statiques, la base et le
rendu du chart sans cluster.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from ameesh import (accounts, canon, db as db_mod, persona_memory, registry,
                    runner as runner_mod, storage, tenancy)

from .support import REPO
from .test_canon import _CanonDbCase, write
from .test_7694_pod_socle import ALICE, BRUNO, POD_HOST, pod_card

POD_DIR = os.path.join(REPO, "deploy", "k8s", "pod-shared")
RENDER = os.path.join(POD_DIR, "render.py")


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(yaml is None, "PyYAML absent : chart non vérifié")
class PodSharedChartTest(unittest.TestCase):
    """Le chart d'un pod partagé : N conteneurs, N PVC, UN fsGroup."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.render = load_module(RENDER, "pod_shared_render")
        cls.config = cls.render.load_config(
            __import__("pathlib").Path(POD_DIR) / "host.exemple.yaml")
        cls.docs = cls.render.render_all(cls.config)
        cls.tenants = [entry["plan"] for entry in cls.config["tenants"]]
        cls.statefulset = next(d for d in cls.docs if d["kind"] == "StatefulSet")
        cls.pod = cls.statefulset["spec"]["template"]["spec"]
        cls.by_name = {d["metadata"]["name"]: d for d in cls.docs}

    def container(self, name: str) -> dict:
        return next(c for c in self.pod["containers"] if c["name"] == name)

    def test_rendu_a_jour_et_deterministe(self):
        proc = subprocess.run([sys.executable, RENDER, "--check"],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        first = self.render.render(self.config, "source")
        second = self.render.render(self.config, "source")
        self.assertEqual(first, second)

    def test_n_conteneurs_et_n_pvc(self):
        self.assertEqual(len(self.tenants), 2, "deux tenants dans l'exemple")
        tenant_containers = [c for c in self.pod["containers"]
                             if c["name"].startswith("tenant-")]
        self.assertEqual(len(tenant_containers), len(self.tenants))
        claims = [v for v in self.pod["volumes"]
                  if v.get("persistentVolumeClaim")]
        self.assertEqual(len(claims), len(self.tenants))
        self.assertEqual(len({v["name"] for v in claims}), len(claims))
        self.assertEqual(len({v["persistentVolumeClaim"]["claimName"]
                              for v in claims}), len(claims))
        pvcs = [d for d in self.docs if d["kind"] == "PersistentVolumeClaim"]
        self.assertEqual(len(pvcs), len(self.tenants))
        self.assertEqual(len({p["metadata"]["name"] for p in pvcs}), len(pvcs))
        for pvc in pvcs:
            self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])

    def test_un_seul_fsgroup_et_uid_du_superviseur(self):
        occurrences = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "fsGroup":
                        occurrences.append(value)
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.statefulset)
        self.assertEqual(occurrences, [tenancy.POD_FS_GROUP])
        sc = self.pod["securityContext"]
        self.assertEqual(sc["fsGroup"], tenancy.POD_FS_GROUP)
        self.assertEqual(sc["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(sc["runAsUser"], tenancy.POD_SUPERVISOR_UID)
        self.assertEqual(sc["runAsGroup"], tenancy.POD_FS_GROUP)
        self.assertFalse(self.pod["automountServiceAccountToken"])
        for key in ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace"):
            self.assertFalse(self.pod[key], key)
        self.assertGreaterEqual(self.pod["terminationGracePeriodSeconds"], 120)
        self.assertEqual(self.statefulset["spec"]["replicas"], 1)
        self.assertEqual(self.statefulset["spec"]["podManagementPolicy"],
                         "OrderedReady")

    def test_superviseur_sans_volume_de_tenant(self):
        supervisor = self.container("supervisor")
        mounts = {m["name"]: m for m in supervisor["volumeMounts"]}
        self.assertNotIn("home-alice", mounts)
        self.assertNotIn("home-bruno", mounts)
        self.assertEqual({m["mountPath"] for m in supervisor["volumeMounts"]},
                         {"/home/ameesh", "/etc/ameesh/supervisor",
                          tenancy.POD_CANON, "/tmp"})
        self.assertEqual({e["name"]: e.get("value") for e in supervisor["env"]
                          }["AMEESH_POD"], "1")
        # La capacité déclarée du pod = somme des limites de ses tenants.
        limits = [plan["container"]["resources"]["limits"] for plan in self.tenants]
        memory = sum(self.render._memory_bytes(limit["memory"]) for limit in limits)
        cpu = sum(self.render._cpu_cores(limit["cpu"]) for limit in limits)
        env = {e["name"]: e.get("value") for e in supervisor["env"]}
        self.assertEqual(env["AMEESH_POD_MEMORY_BYTES"], str(memory))
        self.assertEqual(env["AMEESH_POD_CPU_CORES"], "%g" % cpu)

    def test_chaque_tenant_monte_son_seul_pvc(self):
        for plan in self.tenants:
            slug = self.render._slug(plan["tenant"].split(":", 1)[1])
            tenant = self.container("tenant-" + slug)
            mounts = {m["name"]: m for m in tenant["volumeMounts"]}
            self.assertEqual(mounts["home-" + slug]["mountPath"], plan["home"])
            self.assertIn(plan["home"], [m["mountPath"] for m in tenant["volumeMounts"]])
            self.assertNotIn("canon-key", mounts)
            self.assertEqual(mounts["canon"]["readOnly"], True)
            self.assertTrue(mounts["passwd"]["readOnly"])
            self.assertEqual(mounts["passwd"]["subPath"], "passwd")
            self.assertEqual(mounts["tenant-config-" + slug]["mountPath"],
                             "/etc/ameesh/tenant")
            self.assertIn("tenant-tmp-" + slug, mounts)
            self.assertNotIn("ports", tenant)
            self.assertEqual(tenant["securityContext"]["runAsUser"], plan["uid"])
            self.assertEqual(tenant["securityContext"]["runAsGroup"],
                             tenancy.POD_FS_GROUP)
            self.assertTrue(tenant["securityContext"]["runAsNonRoot"])
            self.assertFalse(tenant["securityContext"]["privileged"])
            self.assertTrue(tenant["securityContext"]["readOnlyRootFilesystem"])
            env = {e["name"]: e.get("value") for e in tenant["env"]}
            self.assertEqual((env["HOME"], env["AMEESH_TENANT"],
                              env["AMEESH_RUNNER_ID"]),
                             (plan["home"], plan["tenant"], plan["runner_id"]))
            self.assertEqual(env["AMEESH_CANON_SYNC_INTERVAL"], "0")
            self.assertEqual(env["AMEESH_POD"], "1")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], tenancy.POD_CANON_REPO)
            self.assertNotEqual(env["GIT_CONFIG_VALUE_0"], "*")
            # Le PVC de CE tenant, et lui seul, dans les volumes du pod.
            claim = next(v for v in self.pod["volumes"]
                         if v["name"] == "home-" + slug)
            self.assertEqual(claim["persistentVolumeClaim"]["claimName"],
                             plan["volumes"]["home"]["persistentVolumeClaim"]["claimName"])
            # Aucun volume de tenant d'un autre dans CE conteneur.
            for other in self.tenants:
                if other["tenant"] == plan["tenant"]:
                    continue
                other_slug = self.render._slug(other["tenant"].split(":", 1)[1])
                self.assertNotIn("home-" + other_slug, mounts)

    def test_configmaps_passwd_et_comptes(self):
        passwd = self.by_name["ameesh-pod-passwd"]["data"]["passwd"]
        group = self.by_name["ameesh-pod-passwd"]["data"]["group"]
        self.assertIn("root:x:0:0:", passwd)
        self.assertIn("ameesh:x:%d:%d:" % (tenancy.POD_SUPERVISOR_UID,
                                           tenancy.POD_FS_GROUP), passwd)
        self.assertIn("nobody:x:65534:", passwd)
        self.assertIn("ameesh-pod:x:%d:" % tenancy.POD_FS_GROUP, group)
        for plan in self.tenants:
            self.assertIn("%s:x:%d:%d:" % (plan["unix_user"], plan["uid"],
                                           tenancy.POD_FS_GROUP), passwd)
            config_map = self.by_name[plan["config_maps"]["tenant"]["name"]]
            tenant_cfg = json.loads(config_map["data"]["config.json"])
            self.assertEqual((tenant_cfg["host"], tenant_cfg["tenant"],
                              tenant_cfg["runner_id"], tenant_cfg["pod"],
                              tenant_cfg["canon_sync_interval"]),
                             (POD_HOST, plan["tenant"], plan["runner_id"], True, 0))
            self.assertEqual(sorted(tenant_cfg["accounts"]), ["claude", "codex"])
            published = tenancy.published_profiles(types.SimpleNamespace(
                pod=True, accounts=tenant_cfg["accounts"]))
            self.assertEqual(published, ["claude/primaire", "codex/primaire"])
        supervisor = self.by_name["ameesh-pod-supervisor"]["data"]["config.json"]
        supervisor_cfg = json.loads(supervisor)
        self.assertEqual((supervisor_cfg["host"], supervisor_cfg["pod"]), (POD_HOST, True))
        self.assertNotIn("tenant", supervisor_cfg)
        self.assertNotIn("accounts", supervisor_cfg)
        canon = self.by_name["ameesh-pod-canon"]["data"]
        self.assertEqual(set(canon), {"url", "branch", "fetch-interval"})

    def test_aucun_secret_ni_droit_dans_le_chart(self):
        kinds = {d["kind"] for d in self.docs}
        self.assertEqual(kinds, {"StatefulSet", "ConfigMap", "PersistentVolumeClaim"})
        text = "\n".join(self.render.render(self.config, "source").values()).lower()
        for forbidden in ("hostpath", "privileged: true", "password:",
                          "ssh-privatekey:", "-----begin"):
            self.assertNotIn(forbidden, text)
        # Aucun port exposé : le pod reste sans Service.
        for container in self.pod["containers"]:
            self.assertNotIn("ports", container)

    def test_rendu_refuse_les_collisions(self):
        path = os.path.join(POD_DIR, "host.exemple.yaml")
        base = self.render.load_config(__import__("pathlib").Path(path))
        duplicate = {k: v for k, v in base.items() if k != "tenants"}
        duplicate["tenants"] = [{"tenant": ALICE}, {"tenant": ALICE}]
        # Le chargeur refuse un fichier temporaire aux tenants dupliqués.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(duplicate, fh, sort_keys=False)
            temp = fh.name
        try:
            with self.assertRaises(self.render.RenderError):
                self.render.load_config(__import__("pathlib").Path(temp))
        finally:
            os.unlink(temp)


class PodProfilePublicationTest(unittest.TestCase):
    """Les profils d'un tenant pod : noms seulement, rien hors du home."""

    def test_profil_dans_le_home_publie(self):
        cfg = types.SimpleNamespace(pod=True, accounts={
            "claude": [{"name": "primaire"}],
            "codex": [{"name": "secondaire", "path": "/home/ameesh-alice/.codex-2"}],
        })
        with mock.patch.dict(os.environ, {"HOME": "/home/ameesh-alice"}):
            self.assertEqual(tenancy.published_profiles(cfg),
                             ["claude/primaire", "codex/secondaire"])

    def test_profil_hors_du_home_refuse(self):
        cfg = types.SimpleNamespace(pod=True, accounts={
            "claude": [{"name": "primaire"}],
            "codex": [{"name": "vole", "path": "/etc/codex"}],
        })
        with mock.patch.dict(os.environ, {"HOME": "/home/ameesh-alice"}):
            self.assertEqual(tenancy.published_profiles(cfg), ["claude/primaire"])
        profile = accounts.parse(cfg.accounts)["codex"][0]
        problem = tenancy.pod_profile_problem(
            cfg, profile, home="/home/ameesh-alice")
        self.assertIn("outside the tenant home", problem)

    def test_cle_hors_du_home_refusee(self):
        cfg = types.SimpleNamespace(pod=True, accounts={
            "deepseek": [{"name": "cle", "type": "api_key_env",
                          "key_env": "DEEPSEEK_API_KEY",
                          "key_file": "/run/secrets/ds"}],
        })
        with mock.patch.dict(os.environ, {"HOME": "/home/ameesh-alice"}):
            self.assertEqual(tenancy.published_profiles(cfg), [])

    def test_hors_pod_inchange(self):
        cfg = types.SimpleNamespace(pod=False, accounts={
            "codex": [{"name": "banc", "path": "/srv/codex"}],
        })
        self.assertEqual(tenancy.published_profiles(cfg), ["codex/banc"])
        profile = accounts.parse(cfg.accounts)["codex"][0]
        self.assertEqual(tenancy.pod_profile_problem(
            cfg, profile, home="/home/ameesh-alice"), "")


class PodRunnerLeaseUnitTest(unittest.TestCase):
    """Le câblage du bail dans l'exécuteur : seul le superviseur le prend."""

    def runner(self, *, pod=True, tenant="", runtime=None):
        runner = types.SimpleNamespace(
            cfg=types.SimpleNamespace(pod=pod, tenant=tenant,
                                      resource_interval=60),
            _host_runtime=runtime,
            host="pod-banc", runner_id="pod-banc/superviseur", db=object())
        # Les méthodes de `Runner` s'appellent entre elles : les lier à la
        # doublure, sans construire un exécuteur complet.
        runner._pod_supervisor = lambda: runner_mod.Runner._pod_supervisor(runner)
        runner._pod_lease_ttl = lambda: runner_mod.Runner._pod_lease_ttl(runner)
        return runner

    def test_superviseur_seulement_sans_tenant(self):
        self.assertTrue(runner_mod.Runner._pod_supervisor(self.runner()))
        self.assertFalse(runner_mod.Runner._pod_supervisor(
            self.runner(tenant="human:alice")))
        self.assertFalse(runner_mod.Runner._pod_supervisor(
            self.runner(pod=False)))
        # Après le canon sync, la fiche Host suffit à reconnaître un pod.
        self.assertTrue(runner_mod.Runner._pod_supervisor(
            self.runner(pod=False, runtime=types.SimpleNamespace(kind="pod"))))

    def test_ttl_au_moins_trois_passes(self):
        runner = self.runner()
        self.assertEqual(runner_mod.Runner._pod_lease_ttl(runner), 180.0)
        runner.cfg.resource_interval = 300
        self.assertEqual(runner_mod.Runner._pod_lease_ttl(runner), 900.0)

    def test_standby_n_ecrit_pas_et_detenteur_renouvelle(self):
        appels = []

        class Bail:
            def __init__(self, epoch):
                self.epoch = epoch

            def acquire(self, host, runner_id, ttl):
                appels.append("acquire")
                return self.epoch

            def renew(self, host, runner_id, epoch, ttl):
                appels.append("renew")
                return True

            def holder(self, host):
                return "pod-banc/autre"

        runner = self.runner()
        with mock.patch.object(runner_mod.storage, "of",
                               return_value=types.SimpleNamespace(
                                   pod_supervisor=Bail(None))):
            self.assertFalse(runner_mod.Runner._pod_lease_tick(runner))
            self.assertFalse(hasattr(runner, "_pod_lease"))

        runner = self.runner()
        with mock.patch.object(runner_mod.storage, "of",
                               return_value=types.SimpleNamespace(
                                   pod_supervisor=Bail(3))):
            self.assertTrue(runner_mod.Runner._pod_lease_tick(runner))
            self.assertEqual(runner._pod_lease["epoch"], 3)
            self.assertTrue(runner_mod.Runner._pod_lease_tick(runner))
        self.assertEqual(appels, ["acquire", "acquire", "renew"])


class PodSupervisorLeaseDbTest(_CanonDbCase):
    """Migration 0599 : un seul superviseur vivant par hôte pod."""

    def setUp(self) -> None:
        super().setUp()
        self.host = "pod-bail-%s" % uuid.uuid4().hex[:8]
        self.api = storage.of(self.db).pod_supervisor
        self.a = self.host + ":1001"
        self.b = self.host + ":1002"

    def test_un_seul_detenteur(self):
        self.assertEqual(self.api.acquire(self.host, self.a, 60), 1)
        self.assertEqual(self.api.holder(self.host), self.a)
        self.assertIsNone(self.api.acquire(self.host, self.b, 60))
        lease = self.api.lease(self.host)
        self.assertEqual((lease["runner_id"], lease["epoch"], lease["live"]),
                         (self.a, 1, True))
        self.assertTrue(self.api.renew(self.host, self.a, 1, 60))
        self.assertFalse(self.api.renew(self.host, self.b, 1, 60))
        self.assertFalse(self.api.renew(self.host, self.a, 99, 60))

    def test_remise_puis_reprise_avec_une_epoque_neuve(self):
        self.assertEqual(self.api.acquire(self.host, self.a, 60), 1)
        self.assertTrue(self.api.release(self.host, self.a, 1))
        self.assertFalse(self.api.renew(self.host, self.a, 1, 60))
        self.assertEqual(self.api.acquire(self.host, self.b, 60), 2)
        self.assertFalse(self.api.release(self.host, self.a, 1))
        self.assertTrue(self.api.renew(self.host, self.b, 2, 60))
        self.assertEqual(self.api.holder(self.host), self.b)

    def test_expiration_laisse_la_main_au_suivant(self):
        self.assertEqual(self.api.acquire(self.host, self.a, 60), 1)
        self.db.execute(
            "UPDATE pod_supervisor_leases SET acquired_at = clock_timestamp() "
            " - interval '2 hours', expires_at = clock_timestamp() "
            " - interval '1 second' WHERE host = %s", (self.host,))
        self.assertIsNone(self.api.holder(self.host))
        self.assertFalse(self.api.renew(self.host, self.a, 1, 60))
        self.assertEqual(self.api.acquire(self.host, self.b, 60), 2)
        self.assertFalse(self.api.renew(self.host, self.a, 1, 60))

    def test_releve_refuse_a_un_non_detenteur(self):
        def reading(runner: str) -> None:
            self.db.execute(
                "INSERT INTO host_resources (host, runner_id) VALUES (%s, %s)",
                (self.host, runner))

        # Sans bail, un relevé ordinaire n'est pas affecté.
        reading("autre")
        self.assertEqual(self.api.acquire(self.host, self.a, 60), 1)
        reading(self.a)
        with self.assertRaises(db_mod.DbError):
            reading(self.b)
        with self.assertRaises(db_mod.DbError):
            reading("")
        # Le détenteur expiré ne publie plus ; le nouveau prend la main.
        self.db.execute(
            "UPDATE pod_supervisor_leases SET acquired_at = clock_timestamp() "
            " - interval '2 hours', expires_at = clock_timestamp() "
            " - interval '1 second' WHERE host = %s", (self.host,))
        with self.assertRaises(db_mod.DbError):
            reading(self.a)
        self.assertEqual(self.api.acquire(self.host, self.b, 60), 2)
        reading(self.b)
        with self.assertRaises(db_mod.DbError):
            reading(self.a)

    def test_deux_connexions_ne_prennent_pas_le_meme_bail(self):
        other = self.connect()
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)
        results: dict[str, int | None] = {}

        def take(name: str, db, runner: str) -> None:
            api = storage.of(db).pod_supervisor
            barrier.wait(timeout=10)
            results[name] = api.acquire(self.host, runner, 60)

        threads = [
            threading.Thread(target=take, args=("a", self.db, self.a)),
            threading.Thread(target=take, args=("b", other, self.b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(sorted(epoch for epoch in results.values()
                                if epoch is not None), [1])
        self.assertEqual(sum(1 for epoch in results.values() if epoch is None), 1)


class _PodTenantsCase(_CanonDbCase):
    """Deux tenants liés sur une fiche Host `runtime.kind: pod`."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "hotes/pod-banc.md", pod_card("pod-banc"))
        self.sync("pod-banc")
        self.host = "pod-banc"
        self.api = storage.of(self.db).tenancy
        self.uid_a = tenancy.pod_uid(ALICE)
        self.uid_b = tenancy.pod_uid(BRUNO)
        for tenant, uid in ((ALICE, self.uid_a), (BRUNO, self.uid_b)):
            tenancy.register(self.db, self.host, tenant, actor="human:alice",
                             local_host=self.host, pod=True, uid=uid)
        self.runner_a = tenancy.runner_id(self.host, ALICE)
        self.runner_b = tenancy.runner_id(self.host, BRUNO)
        # Les agents du banc ne viennent pas du canon : inscrits à la main sur
        # l'hôte pod, ils ne sont pas gouvernés (le prédicat de réclamation ne
        # dépend donc pas d'un `canon_state` que ce banc ne synchronise pas).
        for name, responsible in (("relecteur", ALICE), ("ouvrier", BRUNO)):
            registry.upsert(self.db, name, harness="deepseek", host=self.host,
                            status="idle", mode="execute")
            self.db.execute(
                "UPDATE agent_registry SET responsible = %s WHERE name = %s",
                (responsible, name))

    def isolation(self, agent: str, uid: int) -> None:
        cfg = dataclasses.replace(self.cfg, host=self.host)
        with mock.patch.object(persona_memory, "isolation_policy",
                               return_value="required"), \
                mock.patch.object(os, "getuid", return_value=uid):
            persona_memory.require_isolation(cfg, self.db, agent)

    def claim_ok(self, runner: str, responsible: str) -> bool:
        rows = self.db.query(
            "SELECT ameesh_tenant_claim_ok(%s, %s, %s) AS ok",
            (self.host, runner, responsible))
        return bool(rows[0]["ok"])


class PodClaimDbTest(_PodTenantsCase):
    """La garde de réclamation et l'isolation par UID, sur un hôte POD."""

    def test_fiche_host_est_bien_un_pod(self):
        host = canon.load(self.root, untrusted=True).host(self.host)
        self.assertEqual(host.policy.runtime.kind, "pod")
        self.assertEqual(host.policy.runtime.account_isolation,
                         "per-tenant-volume")
        self.assertEqual(tenancy.host_policy(self.db, self.host),
                         {"shared": True, "tenants": [ALICE, BRUNO]})

    def test_un_runner_ne_prend_pas_le_bail_d_un_autre_tenant(self):
        self.assertEqual(self.claim_ok(self.runner_a, ALICE), True)
        self.assertEqual(self.claim_ok(self.runner_b, ALICE), False)
        self.assertEqual(self.claim_ok(self.runner_b, BRUNO), True)
        self.assertEqual(self.claim_ok("intrus", ALICE), False)
        self.assertEqual(self.claim_ok(self.runner_a, "human:inconnu"), False)
        self.assertIsNone(registry.claim(self.db, "relecteur", self.runner_b, 600))
        lease = registry.claim(self.db, "relecteur", self.runner_a, 600)
        self.assertIsNotNone(lease)
        registry.release(self.db, "relecteur", self.runner_a, lease["lease_epoch"])
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", self.runner_b, 600))
        self.assertIsNone(registry.claim(self.db, "ouvrier", self.runner_a, 600))

    def test_attach_refuse_a_l_utilisateur_d_un_autre_tenant(self):
        self.assertIsNone(registry.attach_claim(
            self.db, "relecteur", "attach:ameesh-bruno@" + self.host, 60))
        self.assertIsNotNone(registry.attach_claim(
            self.db, "relecteur", "attach:ameesh-alice@" + self.host, 60))

    def test_isolation_entre_tenants_distincts_sur_pod(self):
        registry.claim(self.db, "ouvrier", self.runner_b, 600)
        self.isolation("relecteur", self.uid_a)
        with self.assertRaisesRegex(persona_memory.MemoryError,
                                    "is not the UID"):
            self.isolation("relecteur", self.uid_b)

    def test_isolation_refuse_un_pair_sans_liaison_sur_pod(self):
        self.db.execute("DELETE FROM host_tenants WHERE tenant = %s", (BRUNO,))
        self.db.execute("UPDATE agent_registry SET status = 'running'"
                        " WHERE name = 'ouvrier'")
        with self.assertRaisesRegex(persona_memory.MemoryError,
                                    "no verified tenant UID"):
            self.isolation("relecteur", self.uid_a)

    def test_isolation_refuse_le_meme_tenant_sur_pod(self):
        self.db.execute("UPDATE agent_registry SET responsible = %s"
                        " WHERE name = 'ouvrier'", (ALICE,))
        registry.claim(self.db, "ouvrier", self.runner_a, 600)
        with self.assertRaisesRegex(persona_memory.MemoryError, "shares UID"):
            self.isolation("relecteur", self.uid_a)

    def test_profils_publies_par_le_battement_du_tenant(self):
        cfg_a = dataclasses.replace(
            self.cfg, host=self.host, tenant=ALICE, runner_id=self.runner_a,
            pod=True, accounts={"claude": [{"name": "primaire"}],
                                "codex": [{"name": "primaire"}]})
        cfg_b = dataclasses.replace(
            self.cfg, host=self.host, tenant=BRUNO, runner_id=self.runner_b,
            pod=True, accounts={"claude": [{"name": "secondaire"}]})
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid_a):
            self.assertTrue(tenancy.beat(cfg_a, self.db, version="v"))
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid_b):
            self.assertTrue(tenancy.beat(cfg_b, self.db, version="v"))
        profile_a = self.api.presence(self.host, ALICE)
        profile_b = self.api.presence(self.host, BRUNO)
        self.assertEqual(profile_a["profiles"], ["claude/primaire", "codex/primaire"])
        self.assertEqual(profile_b["profiles"], ["claude/secondaire"])
        self.assertEqual((profile_a["runner_id"], profile_b["runner_id"]),
                         (self.runner_a, self.runner_b))
        lines = tenancy.describe(self.api.bindings(self.host))
        self.assertTrue(any("profiles=claude/primaire,codex/primaire" in line
                            for line in lines), lines)
        # Un battement sous l'UID d'un autre tenant ne publie rien.
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid_b):
            self.assertFalse(tenancy.beat(cfg_a, self.db, version="intrus"))
        self.assertEqual(self.api.presence(self.host, ALICE)["profiles"],
                         ["claude/primaire", "codex/primaire"])


class WitnessBaseTest(_PodTenantsCase):
    """Contrôle 5 du témoin complet : la base sur un pod.

    Exécuté par `deploy/k8s/pod-witness/preuve-isolation-comptes.sh` quand un
    DSN de banc est fourni : le témoin ne se contente pas des conteneurs, il
    vérifie que le runner d'un tenant ne peut pas réclamer l'agent d'un autre
    et que l'isolation par UID tient sur la fiche Host pod."""

    def test_controle_5_base_sur_pod(self):
        self.assertTrue(self.claim_ok(self.runner_a, ALICE))
        self.assertFalse(self.claim_ok(self.runner_b, ALICE))
        self.assertFalse(self.claim_ok("intrus", ALICE))
        lease = registry.claim(self.db, "relecteur", self.runner_a, 600)
        self.assertIsNotNone(lease)
        registry.release(self.db, "relecteur", self.runner_a, lease["lease_epoch"])
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", self.runner_b, 600))
        self.isolation("relecteur", self.uid_a)
        with self.assertRaises(persona_memory.MemoryError):
            self.isolation("relecteur", self.uid_b)
        cfg = dataclasses.replace(
            self.cfg, host=self.host, tenant=ALICE, runner_id=self.runner_a,
            pod=True, accounts={"claude": [{"name": "primaire"}]})
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid_a):
            self.assertTrue(tenancy.beat(cfg, self.db, version="témoin"))
        self.assertEqual(self.api.presence(self.host, ALICE)["profiles"],
                         ["claude/primaire"])


if __name__ == "__main__":
    unittest.main()
