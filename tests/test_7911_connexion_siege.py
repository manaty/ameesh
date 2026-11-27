# SPDX-License-Identifier: AGPL-3.0-only
"""#7911 (P3 bis de #7633) : abonnement sur un pod, connexion du siège, état login.

Décision owner #233251 (option a, signée le 27/11 à 00:18Z, approbation #446) :
D2 oui, sous condition que le bureau vérifie les conditions du fournisseur
**avec chaque personne** avant toute connexion de siège, preuve citée sur le
lot ; D4 oui, l'admin du cluster est dans la frontière de confiance.

Ce module couvre les quatre pièces du lot :

* la canon Q2 bis (design 7633 §7.1–§7.2) : `subscription` sur un hôte `pod`
  seulement avec l'opt-in explicite, un volume par tenant et une
  `runtime.isolation_ref` citable ; jamais sur un `pod-template` ;
* la fenêtre `login` en base (migration 0610) : preuve D2 citable, TTL borné,
  une seule fenêtre ouverte, expiration, blocage de la réclamation et de
  l'attach, état publié par le battement du runner ;
* la commande `host tenant login|logout` (geste humain/avatar, hôte pod
  seulement) et la passe de drainage de l'exécuteur ;
* le pod de connexion dédié (`deploy/k8s/pod-login/render.py`) : un seul
  conteneur, le PVC du seul tenant, aucun jeton, TTL ≤ 30 min, Role
  `pods/exec` borné par `resourceNames` à ce pod — jamais au pod partagé.

Le témoin kind complet (contrôle 6 bis : `SubjectAccessReview` autorisé sur le
pod de connexion, refusé sur le pod partagé et celui d'un pair) est un geste
de bureau/opérateur, porté par `deploy/k8s/pod-witness/`. Ici on vérifie sa
partie statique et le rendu qui l'alimente.
"""
from __future__ import annotations

import copy
import dataclasses
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from ameesh import (canon, config, db as db_mod, registry, runner as runner_mod,
                    storage, tenancy)

from . import test_canon
from .support import REPO
from .test_canon import codes, write
from .test_7694_pod_socle import ALICE, BRUNO, pod_card
from .test_7695_pod_partage import _PodTenantsCase
from .test_7696_host_pod_card import pods_exec_violations

POD_LOGIN = os.path.join(REPO, "deploy", "k8s", "pod-login")
LOGIN_RENDER = os.path.join(POD_LOGIN, "render.py")
WITNESS = os.path.join(REPO, "deploy", "k8s", "pod-witness",
                       "preuve-isolation-comptes.sh")
LOGIN_POD = "ameesh-login-alice"
SHARED_POD = "ameesh-pod-alice"
#: image de banc épinglée : le rendu refuse un tag nu, aucun cluster n'est
#: appelé dans ce module.
IMAGE = "registry.example/ameesh@sha256:" + "0" * 64


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# Q2 bis : la canon, sans base
# ==========================================================================

class _CardCase(test_canon._TmpMixin, unittest.TestCase):
    """Une fiche Host pod dans un canon d'exemple, hors source approuvée."""

    def setUp(self) -> None:
        self.root = self.example_copy()

    def card(self, title: str = "pod-banc", **kwargs):
        kwargs.setdefault("ref", "manaty/ameesh#7911")
        write(self.root, "hotes/pod-banc.md", pod_card(title, **kwargs))
        return canon.load(self.root, untrusted=True)

    def errors(self):
        return test_canon.codes(canon.validate(self.card()), canon.ERROR)


class SubscriptionAllowedTest(_CardCase):
    """`canon.subscription_allowed` : la défense en profondeur de Q2 bis.

    La fonction sert aux appelants qui n'ont pas lancé `canon check`
    (`shared_violations`, placement) : elle ne doit jamais ouvrir le siège sur
    une simple déclaration.
    """

    def test_hote_ordinaire_opt_in_explicite(self):
        self.assertFalse(canon.subscription_allowed({}))
        self.assertFalse(canon.subscription_allowed(
            {"allow_subscription": True}))
        self.assertFalse(canon.subscription_allowed(
            {"require_isolation": True}))
        # Un `true` textuel n'est pas le booléen du canon.
        self.assertFalse(canon.subscription_allowed(
            {"allow_subscription": "true", "require_isolation": True}))
        self.assertTrue(canon.subscription_allowed(
            {"allow_subscription": True, "require_isolation": True}))

    def test_pod_exige_volume_par_tenant_et_reference(self):
        policy = {
            "allow_subscription": True,
            "require_isolation": True,
            "runtime": {"kind": "pod",
                        "account_isolation": "per-tenant-volume",
                        "isolation_ref": "manaty/ameesh#7911"},
        }
        self.assertTrue(canon.subscription_allowed(policy))
        for change in (
            {"account_isolation": "per-pod-empty-dir"},
            {"isolation_ref": ""},
            {"isolation_ref": "   "},
            {"isolation_ref": "pas une ref avec des espaces"},
            {"isolation_ref": "https://example.test/x y"},
        ):
            broken = copy.deepcopy(policy)
            broken["runtime"].update(change)
            self.assertFalse(canon.subscription_allowed(broken),
                             "accepté à tort: %r" % (change,))
        self.assertFalse(canon.subscription_allowed(
            {"allow_subscription": True, "require_isolation": True,
             "runtime": {"kind": "pod"}}))

    def test_pod_template_jamais_de_siege(self):
        policy = {
            "allow_subscription": True,
            "require_isolation": True,
            "runtime": {"kind": "pod-template",
                        "account_isolation": "per-tenant-volume",
                        "isolation_ref": "manaty/ameesh#7911"},
        }
        self.assertFalse(canon.subscription_allowed(policy))

    def test_shared_violations_suivent_la_meme_regle(self):
        loaded = self.card(
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        host = loaded.host("pod-banc")
        self.assertTrue(host.policy.allow_subscription)
        self.assertEqual(canon.shared_violations(
            {"responsible": ALICE}, host, "subscription"), [])
        # Sans les conditions, le refus nomme Q2 bis, pas une règle vague.
        weak = self.card(credential_modes=("api-key", "subscription"))
        reasons = canon.shared_violations(
            {"responsible": ALICE}, weak.host("pod-banc"), "subscription")
        self.assertTrue(any("Q2 bis" in reason for reason in reasons), reasons)

    def test_la_carte_admet_le_siege_seulement_avec_les_conditions(self):
        loaded = self.card(
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        self.assertEqual(test_canon.codes(
            canon.validate(loaded), canon.ERROR), set())
        broken = self.card(
            isolation="per-pod-empty-dir",
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        self.assertIn("host-pod-subscription-unproven",
                      codes(canon.validate(broken), canon.ERROR))


# ==========================================================================
# Fenêtre `login` en base (migration 0610)
# ==========================================================================

class _LoginCase(_PodTenantsCase):
    """Deux tenants liés sur `pod-banc`, UID du chart, comme #7695."""

    def setUp(self) -> None:
        super().setUp()
        # Le schéma est partagé par les méthodes de la classe : une fenêtre
        # laissée par un test précédent ne doit pas décider du suivant.
        self.db.execute("DELETE FROM tenant_logins")

    def open_login(self, **kwargs):
        params = {"terms_ref": "#233251", "actor": "human:alice", "ttl_s": 60}
        params.update(kwargs)
        return tenancy.login_open(self.db, self.host, ALICE, **params)

    def close_login(self):
        return tenancy.login_close(
            self.db, self.host, ALICE, actor="human:alice")

    def claim_ok(self, runner: str, tenant: str = ALICE) -> bool:
        rows = self.db.query(
            "SELECT ameesh_tenant_claim_ok(%s, %s, %s) AS ok",
            (self.host, runner, tenant))
        return bool(rows[0]["ok"])


class LoginWindowDbTest(_LoginCase):
    """La preuve D2, le TTL, l'unicité et l'expiration de la fenêtre."""

    def test_preuve_citable_obligatoire(self):
        for ref in ("", "   ", "conditions vérifiées avec Alice", "x" * 301,
                    "mail:234666 avec des espaces"):
            with self.assertRaises(tenancy.TenancyError) as caught:
                self.open_login(terms_ref=ref)
            self.assertEqual(caught.exception.code,
                             "tenant-login-terms-invalid", ref)
        for ref in ("#233251", "233251", "manaty/ameesh#233251",
                    "mail:234666", "bureau-goal:ef8c388a", "card@commit"):
            result = self.open_login(terms_ref=ref)
            self.assertTrue(result["opened"], ref)
            self.assertEqual(result["terms_ref"], ref.strip())
            self.assertEqual(result["opened_by"], "human:alice")
            self.close_login()

    def test_ttl_borne_a_trente_minutes(self):
        for ttl in (0, 29, -1, 1801, "abc"):
            with self.assertRaises(tenancy.TenancyError) as caught:
                self.open_login(ttl_s=ttl)
            self.assertEqual(caught.exception.code, "tenant-login-ttl-invalid",
                             ttl)
        for ttl in (30, 1800):
            self.assertTrue(self.open_login(ttl_s=ttl)["opened"])
            self.close_login()

    def test_tenant_non_lie_refuse(self):
        with self.assertRaises(tenancy.TenancyError) as caught:
            tenancy.login_open(self.db, self.host, "human:carole",
                               terms_ref="#233251", actor="human:alice",
                               ttl_s=60)
        self.assertEqual(caught.exception.code, "tenant-not-bound")

    def test_une_seule_fenetre_ouverte(self):
        self.assertTrue(self.open_login()["opened"])
        with self.assertRaises(tenancy.TenancyError) as caught:
            self.open_login()
        self.assertEqual(caught.exception.code, "tenant-login-already-open")
        rows = self.db.query(
            "SELECT count(*) AS n FROM tenant_logins"
            " WHERE host = %s AND tenant = %s AND state = 'open'",
            (self.host, ALICE))
        self.assertEqual(rows[0]["n"], 1)
        self.assertTrue(self.close_login()["closed"])
        self.assertFalse(self.close_login()["closed"])
        self.assertTrue(self.open_login()["opened"])

    def test_fenetre_expiree_ne_bloque_plus(self):
        self.open_login(ttl_s=30)
        self.db.execute(
            "UPDATE tenant_logins"
            " SET opened_at = now() - interval '10 minutes',"
            "     expires_at = now() - interval '1 second'"
            " WHERE host = %s AND tenant = %s AND state = 'open'",
            (self.host, ALICE))
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))
        self.assertTrue(self.open_login()["opened"])
        rows = self.db.query(
            "SELECT state, terms_ref, opened_by, closed_by FROM tenant_logins"
            " WHERE host = %s AND tenant = %s ORDER BY id", (self.host, ALICE))
        self.assertEqual([row["state"] for row in rows], ["closed", "open"])
        self.assertEqual(rows[0]["closed_by"], "expiry")
        self.assertEqual(rows[1]["terms_ref"], "#233251")
        self.assertEqual(rows[1]["opened_by"], "human:alice")

    def test_login_active_suit_ouverture_et_fermeture(self):
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))
        self.assertFalse(tenancy.login_active(self.db, self.host, BRUNO))
        self.open_login()
        self.assertTrue(tenancy.login_active(self.db, self.host, ALICE))
        self.assertFalse(tenancy.login_active(self.db, self.host, BRUNO))
        self.close_login()
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))


class LoginGuardDbTest(_LoginCase):
    """Pendant la fenêtre, ni réclamation ni attach ; l'autre tenant reste libre."""

    def test_login_bloque_reclamation_et_attach(self):
        attach_a = "attach:ameesh-alice@" + self.host
        self.assertTrue(self.claim_ok(self.runner_a))
        self.assertTrue(self.claim_ok(attach_a))
        self.open_login()
        self.assertFalse(self.claim_ok(self.runner_a))
        self.assertFalse(self.claim_ok(self.runner_b, ALICE))
        self.assertFalse(self.claim_ok(attach_a))
        self.assertFalse(self.claim_ok("intrus"))
        self.assertIsNone(registry.claim(self.db, "relecteur",
                                         self.runner_a, 600))
        self.assertIsNone(registry.attach_claim(
            self.db, "relecteur", attach_a, 60))
        # La fenêtre d'Alice ne ferme pas le siège de Bruno.
        self.assertTrue(self.claim_ok(self.runner_b, BRUNO))
        self.assertIsNotNone(registry.claim(self.db, "ouvrier",
                                            self.runner_b, 600))
        self.close_login()
        self.assertTrue(self.claim_ok(self.runner_a))
        self.assertIsNotNone(registry.attach_claim(
            self.db, "relecteur", attach_a, 60))

    def test_battement_publie_login_seulement_apres_acquittement(self):
        cfg = dataclasses.replace(
            self.cfg, host=self.host, tenant=ALICE, runner_id=self.runner_a,
            pod=True, accounts={"claude": [{"name": "primaire"}]})
        with mock.patch.object(tenancy.os, "getuid", return_value=self.uid_a):
            self.assertTrue(tenancy.beat(cfg, self.db, version="v"))
            self.assertEqual(self.api.presence(self.host, ALICE)["state"],
                             "running")
            self.open_login()
            # Un battement ordinaire ne dit pas que le PVC peut être monté.
            self.assertTrue(tenancy.beat(cfg, self.db, version="v"))
            self.assertEqual(self.api.presence(self.host, ALICE)["state"],
                             "running")
            # L'acquittement du runner (tours arrêtés, baux rendus) le dit.
            self.assertTrue(tenancy.beat(cfg, self.db, version="v",
                                         login_ack=True))
            presence = self.api.presence(self.host, ALICE)
            self.assertEqual(presence["state"], "login")
            self.assertEqual(presence["profiles"], ["claude/primaire"])
            line = next(line for line in tenancy.describe(
                self.api.bindings(self.host)) if ALICE in line)
            self.assertIn("state=login", line)
            self.assertIn("login until ", line)
            self.assertIn("terms=#233251", line)
            self.assertIn("by=human:alice", line)
            # Fermée : même un acquittement qui traîne ne laisse pas `login`.
            self.close_login()
            self.assertTrue(tenancy.beat(cfg, self.db, version="v",
                                         login_ack=True))
            self.assertEqual(self.api.presence(self.host, ALICE)["state"],
                             "running")


class LoginCliDbTest(_LoginCase):
    """`ameesh host tenant login|logout` : geste humain, hôte pod seulement."""

    def cli(self, *args, runtime="default"):
        out = io.StringIO()
        # `PgTestCase.connect` passe par `db_mod.connect` : le capturer avant
        # le patch, sinon la doublure se rappelle elle-même.
        real_connect = db_mod.connect
        pod_runtime = (canon.PodRuntime(
            kind="pod", namespace="ameesh",
            workload="statefulset/ameesh-pod-alice",
            account_isolation="per-tenant-volume",
            isolation_ref="manaty/ameesh#7911")
            if runtime == "default" else runtime)
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(db_mod, "connect",
                                  side_effect=lambda cfg: real_connect(cfg)), \
                mock.patch.object(tenancy, "pod_runtime",
                                  return_value=pod_runtime), \
                mock.patch.object(tenancy, "operator",
                                  return_value="human:alice"), \
                redirect_stdout(out):
            code = tenancy.main(list(args))
        return code, out.getvalue()

    def test_login_et_logout(self):
        code, out = self.cli("login", ALICE, "--host", self.host,
                             "--terms", "#233251")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload["opened"])
        self.assertTrue(tenancy.login_active(self.db, self.host, ALICE))
        code, out = self.cli("logout", ALICE, "--host", self.host)
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["closed"])
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))

    def test_login_refuse_une_preuve_non_citable(self):
        code, out = self.cli("login", ALICE, "--host", self.host,
                             "--terms", "conditions vérifiées avec Alice")
        self.assertEqual(code, 1)
        self.assertIn("tenant-login-terms-invalid", out)
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))

    def test_login_refuse_un_hote_non_pod(self):
        code, out = self.cli("login", ALICE, "--host", self.host,
                             "--terms", "#233251", runtime=None)
        self.assertEqual(code, 1)
        self.assertIn("tenant-not-pod", out)
        self.assertFalse(tenancy.login_active(self.db, self.host, ALICE))

    def test_logout_idempotent(self):
        code, out = self.cli("logout", ALICE, "--host", self.host)
        self.assertEqual(code, 0, out)
        self.assertFalse(json.loads(out)["closed"])


# ==========================================================================
# Drainage de l'exécuteur du tenant
# ==========================================================================

class _Worker:
    """Doublure d'agent : arrêt, réveil et retour de bail observables."""

    def __init__(self) -> None:
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.calls: list[tuple] = []

    def stop_group_now(self, reason: str, *, grace: float | None = None):
        self.calls.append(("stop", reason, grace))
        return True

    def release_lease(self) -> bool:
        self.calls.append(("release",))
        return True


class LoginDrainTest(unittest.TestCase):
    """`Runner._tenant_login_pass` : arrêt, baux rendus, acquittement posé."""

    def runner(self, tenant: str = ALICE):
        return types.SimpleNamespace(
            cfg=types.SimpleNamespace(tenant=tenant), host="pod-banc",
            db=object(), lock=threading.Lock(), workers={})

    def test_arret_et_baux_avant_acquittement(self):
        runner = self.runner()
        worker = _Worker()
        runner.workers["relecteur"] = worker
        with mock.patch.object(tenancy, "login_active", return_value=True):
            self.assertTrue(
                runner_mod.Runner._tenant_login_pass(runner))
        self.assertTrue(worker.stopping.is_set())
        self.assertTrue(worker.wake.is_set())
        self.assertEqual([call[0] for call in worker.calls],
                         ["stop", "release"])
        self.assertEqual(worker.calls[0][1], "tenant seat login")
        self.assertEqual(worker.calls[0][2], 3.0)
        self.assertTrue(runner._tenant_login_drained)
        self.assertTrue(runner._tenant_login_reported)

    def test_fermeture_efface_l_acquittement(self):
        runner = self.runner()
        runner._tenant_login_drained = True
        runner._tenant_login_reported = True
        with mock.patch.object(tenancy, "login_active", return_value=False):
            self.assertFalse(
                runner_mod.Runner._tenant_login_pass(runner))
        self.assertFalse(runner._tenant_login_drained)
        self.assertFalse(runner._tenant_login_reported)

    def test_sans_tenant_aucune_lecture(self):
        runner = self.runner(tenant="")
        with mock.patch.object(tenancy, "login_active") as login:
            self.assertFalse(
                runner_mod.Runner._tenant_login_pass(runner))
        login.assert_not_called()

    def test_lecture_impossible_ne_retient_pas_la_passe(self):
        # La garde SQL refuse la réclamation de toute façon ; une base
        # illisible ne doit pas arrêter le battement du runner.
        runner = self.runner()
        with mock.patch.object(tenancy, "login_active",
                               side_effect=db_mod.DbError("base indisponible")):
            self.assertFalse(
                runner_mod.Runner._tenant_login_pass(runner))
        self.assertFalse(getattr(runner, "_tenant_login_drained", False))


# ==========================================================================
# Pod de connexion : rendu statique (design 7633 §7.3)
# ==========================================================================

@unittest.skipIf(yaml is None, "PyYAML absent : chart non vérifié")
class LoginPodChartTest(unittest.TestCase):
    """Le pod de connexion et son RBAC, sans cluster."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.render = load_module(LOGIN_RENDER, "pod_login_render")
        cls.docs = cls.build()
        cls.by_kind = {doc["kind"]: doc for doc in cls.docs}

    @classmethod
    def build(cls, **kwargs):
        params = {
            "namespace": "ameesh", "host": "pod-alice", "tenant": ALICE,
            "uid": 2519, "pvc": "pvc-alice", "pod": LOGIN_POD,
            "node": "node-1", "image": IMAGE, "ttl_s": 1800,
            "subject": "User:alice",
        }
        params.update(kwargs)
        return cls.render.build(**params)

    def test_objets_rendus(self):
        self.assertEqual([doc["kind"] for doc in self.docs],
                         ["ServiceAccount", "Pod", "Role", "RoleBinding"])

    def test_un_conteneur_un_pvc_meme_noeud_et_sans_jeton(self):
        pod = self.by_kind["Pod"]
        spec = pod["spec"]
        self.assertEqual(len(spec["containers"]), 1)
        self.assertEqual(spec["nodeName"], "node-1")
        self.assertLessEqual(spec["activeDeadlineSeconds"], 1800)
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertEqual(spec["serviceAccountName"], "ameesh-login")
        claims = [volume for volume in spec["volumes"]
                  if "persistentVolumeClaim" in volume]
        self.assertEqual(
            [volume["persistentVolumeClaim"]["claimName"] for volume in claims],
            ["pvc-alice"])
        for volume in spec["volumes"]:
            self.assertNotIn("hostPath", volume)
        container = spec["containers"][0]
        self.assertEqual(container["name"], "login")
        self.assertEqual(container["workingDir"], "/home/ameesh-alice")
        self.assertEqual({mount["mountPath"] for mount in
                          container["volumeMounts"]},
                         {"/home/ameesh-alice", "/etc/passwd", "/etc/group",
                          "/tmp"})
        security = container["securityContext"]
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertFalse(security["privileged"])
        self.assertEqual(security["runAsUser"], 2519)
        self.assertEqual(security["runAsGroup"], 10000)
        self.assertEqual(security["capabilities"], {"drop": ["ALL"]})

    def test_role_borne_au_seul_pod_de_connexion(self):
        role = self.by_kind["Role"]
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["pods/exec"],
            "resourceNames": [LOGIN_POD],
            "verbs": ["create"],
        }])
        binding = self.by_kind["RoleBinding"]
        self.assertEqual(binding["roleRef"]["name"], LOGIN_POD)
        self.assertEqual(binding["roleRef"]["kind"], "Role")
        self.assertEqual(binding["subjects"], [{
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "User", "name": "alice",
        }])

    def test_service_account_sans_jeton_ni_binding(self):
        account = self.by_kind["ServiceAccount"]
        self.assertEqual(account["metadata"]["name"], "ameesh-login")
        self.assertFalse(account["automountServiceAccountToken"])
        for doc in self.docs:
            if doc["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            self.assertFalse(any(subject.get("kind") == "ServiceAccount"
                                 for subject in doc["subjects"] or ()), doc)

    def test_aucun_pods_exec_sur_le_pod_partage(self):
        # Le détecteur de #7696 ne voit rien dans le rendu de connexion…
        self.assertEqual(pods_exec_violations(self.docs, SHARED_POD), [])
        # …et il verrait un Role large ou un pod partagé nommé.
        broad = {"kind": "Role", "metadata": {"name": "large"},
                 "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                            "verbs": ["create"]}]}
        shared = {"kind": "Role", "metadata": {"name": "partage"},
                  "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                             "resourceNames": [SHARED_POD],
                             "verbs": ["create"]}]}
        self.assertEqual(pods_exec_violations([broad], SHARED_POD),
                         ["Role/large"])
        self.assertEqual(pods_exec_violations([shared], SHARED_POD),
                         ["Role/partage"])

    def test_refuse_un_pod_partage_comme_cible(self):
        with self.assertRaises(self.render.RenderError) as caught:
            self.build(pod=SHARED_POD)
        self.assertIn("shared pod", str(caught.exception))

    def test_refuse_un_ttl_hors_borne(self):
        for ttl in (0, 29, -1, 1801):
            with self.assertRaises(self.render.RenderError, msg=ttl):
                self.build(ttl_s=ttl)
        for ttl in (30, 1800):
            docs = self.build(ttl_s=ttl)
            pod = next(doc for doc in docs if doc["kind"] == "Pod")
            self.assertEqual(pod["spec"]["activeDeadlineSeconds"], ttl)

    def test_refuse_image_non_epinglee_tenant_et_sujet(self):
        for kwargs in (
            {"image": "busybox:1.36"},
            {"image": "registry.example/ameesh:latest"},
            {"tenant": "alice"},
            {"tenant": "human:"},
            {"subject": "ServiceAccount:ameesh-login"},
            {"subject": "User:"},
            {"uid": 0},
            {"pvc": ""},
        ):
            with self.assertRaises(self.render.RenderError, msg=kwargs):
                self.build(**kwargs)

    def test_rendu_deterministe(self):
        self.assertEqual(self.build(), self.build())

    def test_cli_rend_et_refuse_le_pod_partage(self):
        argv = [sys.executable, LOGIN_RENDER,
                "--namespace", "ameesh", "--host", "pod-alice",
                "--tenant", ALICE, "--uid", "2519", "--pvc", "pvc-alice",
                "--pod", LOGIN_POD, "--node", "node-1",
                "--image", IMAGE, "--subject", "User:alice"]
        proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        docs = [doc for doc in yaml.safe_load_all(proc.stdout) if doc]
        self.assertEqual({doc["kind"] for doc in docs},
                         {"ServiceAccount", "Pod", "Role", "RoleBinding"})
        index = argv.index(LOGIN_POD)
        bad = subprocess.run(
            argv[:index] + [SHARED_POD] + argv[index + 1:],
            cwd=REPO, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertIn("shared pod", bad.stderr)


class WitnessLoginTest(unittest.TestCase):
    """Le contrôle 6 bis est branché dans le témoin kind, pas seulement écrit."""

    def test_controle_6_bis_present_et_appele(self):
        with open(WITNESS, encoding="utf-8") as source:
            text = source.read()
        for token in ("check_login()", "SubjectAccessReview",
                      "resourceNames:", "ameesh-pod-alice",
                      "check_login || login_code=$?", "login_code"):
            self.assertIn(token, text)
        self.assertIn("pod de connexion", text)


if __name__ == "__main__":
    unittest.main()
