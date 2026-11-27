# SPDX-License-Identifier: AGPL-3.0-only
"""#7696 (P3 de #7633) : carte Host d'un pod, socle sans abonnement.

Décision owner #228416 b (signée le 20/11 à 10:42Z, approval #401) : la
tranche P3 part **sans abonnement**. La carte Host d'un pod reste en
`credential_modes: [api-key]` avec une `runtime.isolation_ref` citable ; Q2 bis
(D1), les conditions du fournisseur (D2) et le PVC monté par l'admin du
cluster (D4) restent réservés à l'owner, et la connexion du siège comme l'état
`login` sont partis dans #7911.

#7911 a rouvert la partie abonnement (décision owner #233251, option a) : le
design `docs/design/7633-hote-avatars-pod.md` §7.1 admet `subscription` sur un
pod avec Q2 bis, un volume par tenant et une référence d'isolation citable.
Les tests ci-dessous gardent les refus qui restent vrais (sans opt-in, sans
référence, template) et vérifient l'ouverture étroite ; l'état `login` et le
pod de connexion sont testés par `tests/test_7911_connexion_siege.py`.

Le témoin A↛B « sur le compte » et le contrôle 6 du design §5.3 restent des
gestes de bureau/opérateur (`deploy/k8s/pod-witness/`) : ce module vérifie leur
partie statique disponible sans cluster, et refuse par le canon le seul chemin
qui les contournerait — un siège déclaré sans les preuves de Q2 bis.
"""
from __future__ import annotations

import os
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - dépend de l'environnement
    yaml = None

from ameesh import canon

from . import test_canon
from .support import REPO
from .test_7694_pod_socle import ALICE, BRUNO, pod_card

POD_SOCLE = os.path.join(REPO, "deploy", "k8s", "pod-socle")
POD_SHARED = os.path.join(REPO, "deploy", "k8s", "pod-shared")
POD_WITNESS = os.path.join(REPO, "deploy", "k8s", "pod-witness")
SHARED_POD = "ameesh-pod-alice"

#: Le verbe RBAC d'un `kubectl exec` est `create` sur la sous-ressource
#: `pods/exec` ; `*` couvre la même chose. La ressource, elle, est ci-dessous.
EXEC_VERBS = ("create", "*")
EXEC_RESOURCES = ("pods/exec", "pods/*", "*")


class _CardCase(test_canon._TmpMixin, unittest.TestCase):
    """Une fiche Host pod dans un canon d'exemple, hors source approuvée."""

    def setUp(self) -> None:
        self.root = self.example_copy()

    def card(self, title: str = "pod-banc", **kwargs):
        kwargs.setdefault("ref", "manaty/ameesh#7696")
        test_canon.write(self.root, "hotes/pod-banc.md", pod_card(title, **kwargs))
        return canon.load(self.root, untrusted=True)

    def findings(self):
        return canon.validate(self.card())

    def errors(self):
        return test_canon.codes(self.findings(), canon.ERROR)

    def warnings(self):
        return test_canon.codes(self.findings(), canon.WARNING)


class PodCardWithoutSubscriptionTest(_CardCase):
    """P3 bis : la carte d'un pod ouvre le siège aux conditions de Q2 bis."""

    def test_carte_api_key_avec_isolation_ref_valide(self):
        loaded = self.card()
        host = loaded.host("pod-banc")
        self.assertEqual(host.policy.credential_modes, ["api-key"])
        self.assertEqual(host.policy.runtime.kind, "pod")
        self.assertEqual(host.policy.runtime.account_isolation,
                         "per-tenant-volume")
        self.assertEqual(host.policy.runtime.isolation_ref,
                         "manaty/ameesh#7696")
        self.assertEqual(self.errors(), set())

    def test_abonnement_refuse_sans_opt_in(self):
        loaded = self.card(credential_modes=("api-key", "subscription"))
        errors = test_canon.codes(canon.validate(loaded), canon.ERROR)
        # Le refus propre au pod nomme Q2 bis ; le constat générique des
        # hôtes partagés reste vrai (Q2, #224052) sans l'opt-in explicite.
        self.assertIn("host-pod-subscription-unproven", errors)
        self.assertIn("host-shared-credential-mode", errors)
        host = loaded.host("pod-banc")
        reasons = canon.placement_violations(
            {"responsible": ALICE, "harness": "codex", "provider": "openai",
             "credential_mode": "subscription"}, host)
        self.assertTrue(any("Q2 bis" in reason for reason in reasons), reasons)

    def test_abonnement_admis_avec_l_opt_in_et_les_conditions(self):
        # #7911 (Q2 bis, décision #233251) : l'opt-in explicite des hôtes
        # partagés ouvre le siège quand le pod a un volume par tenant et une
        # `isolation_ref` citable.
        loaded = self.card(
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        host = loaded.host("pod-banc")
        self.assertTrue(host.policy.allow_subscription)
        self.assertTrue(host.policy.require_isolation)
        self.assertEqual(self.errors(), set())
        self.assertEqual(canon.shared_violations(
            {"responsible": ALICE}, host, "subscription"), [])
        self.assertEqual(canon.placement_violations(
            {"responsible": ALICE, "harness": "claude",
             "provider": "anthropic", "credential_mode": "subscription"}, host), [])

    def test_opt_in_seul_admis_avec_les_conditions(self):
        # L'opt-in sans `subscription` dans la liste n'ouvre aucun mode, mais
        # la carte reste valide si les conditions du pod sont prouvées.
        loaded = self.card(
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        self.assertEqual(self.errors(), set())
        # Sans la référence citable, la même opt-in ne prouve rien.
        weak = self.card(
            ref="pas-une-ref", credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        errors = test_canon.codes(canon.validate(weak), canon.ERROR)
        self.assertIn("host-pod-subscription-unproven", errors)

    def test_une_carte_sans_conditions_bloque_les_agents(self):
        # Un constat d'erreur n'est pas décoratif : `canon.blocking` refuse
        # tout agent placé sur l'hôte dont la carte est en faute.
        loaded = self.card(
            isolation="per-pod-empty-dir",
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        blocked = canon.blocking(canon.validate(loaded)).by_host.get("pod-banc") or []
        self.assertIn("host-pod-subscription-unproven",
                      {finding.code for finding in blocked})

    def test_carte_api_key_sans_opt_in_admet_son_tenant(self):
        loaded = self.card()
        host = loaded.host("pod-banc")
        self.assertEqual(canon.placement_violations(
            {"responsible": ALICE, "harness": "codex", "provider": "openai",
             "credential_mode": "api-key"}, host), [])
        # A ne prend pas le compte de B : un agents hors tenants est refusé,
        # et le mode d'un autre compte (siège) n'est pas admis non plus.
        self.assertTrue(any("not a tenant" in reason for reason in
                            canon.placement_violations(
                                {"responsible": "human:carole",
                                 "credential_mode": "api-key"}, host)))
        self.assertTrue(canon.shared_violations(
            {"responsible": BRUNO}, host, "subscription"))

    def test_mode_inconnu_reste_refuse(self):
        loaded = self.card(credential_modes=("api-key", "oauth"))
        errors = test_canon.codes(canon.validate(loaded), canon.ERROR)
        self.assertIn("host-shared-credential-mode", errors)
        self.assertNotIn("host-pod-subscription-unproven", errors)

    def test_abonnement_du_template_toujours_refuse(self):
        loaded = self.card(
            "template", kind="pod-template", isolation="per-pod-empty-dir",
            max_agents=1, volatile=True, ref="manaty/ameesh#7696",
            credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        self.assertIn("host-pod-template-subscription",
                      test_canon.codes(canon.validate(loaded), canon.ERROR))
        self.assertEqual(canon.placement_violations(
            {"responsible": ALICE, "credential_mode": "api-key"},
            loaded.host("template")),
            ["host template is a `pod-template`, not a placeable host "
             "(design 7633 §7.1)"])


class IsolationRefTest(_CardCase):
    """`runtime.isolation_ref` : présente, citable, et sans siège implicite."""

    def test_reference_obligatoire(self):
        loaded = self.card(ref="")
        self.assertIn("host-runtime-invalid",
                      test_canon.codes(canon.validate(loaded), canon.ERROR))

    def test_reference_faible_avertie(self):
        loaded = self.card(ref="pas-une-ref")
        self.assertEqual(self.errors(), set())
        self.assertIn("host-runtime-ref-weak",
                      test_canon.codes(canon.validate(loaded), canon.WARNING))

    def test_reference_citable_sans_abonnement(self):
        loaded = self.card(ref="manaty/ameesh#5894")
        self.assertEqual(self.errors(), set())
        self.assertEqual(loaded.host("pod-banc").policy.credential_modes,
                         ["api-key"])

    def test_abonnement_exige_la_reference_du_pod(self):
        # Même avec l'opt-in, une carte sans `isolation_ref` ne peut pas
        # prétendre avoir prouvé l'isolation : les deux refus sont dus.
        loaded = self.card(
            ref="", credential_modes=("api-key", "subscription"),
            policy_extra="allow_subscription: true\nrequire_isolation: true")
        errors = test_canon.codes(canon.validate(loaded), canon.ERROR)
        self.assertIn("host-runtime-invalid", errors)
        self.assertIn("host-pod-subscription-unproven", errors)


def pods_exec_violations(docs, shared_pod: str = SHARED_POD) -> list[str]:
    """Les objets RBAC qui donneraient `pods/exec` sur le pod partagé.

    Règle du design §7.3 : `kubectl exec` choisit le conteneur dans la requête,
    pas dans l'objet RBAC ; un `resourceNames` sur le pod partagé ouvrirait
    donc tous ses conteneurs, ceux des autres tenants compris. Seul un Role
    borné à un **pod de connexion d'un seul conteneur** est acceptable. La
    fonction est volontairement indépendante du rendu : elle sert de contrôle
    positif au test et resterait valable si un chart de connexion apparaît en
    #7911.
    """
    out: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        name = str((doc.get("metadata") or {}).get("name") or "?")
        if kind in ("Role", "ClusterRole"):
            for rule in doc.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                verbs = [str(v) for v in rule.get("verbs") or []]
                resources = [str(r) for r in rule.get("resources") or []]
                if not any(v in EXEC_VERBS for v in verbs):
                    continue
                if not any(r in EXEC_RESOURCES for r in resources):
                    continue
                names = [str(n) for n in rule.get("resourceNames") or []]
                if names and shared_pod not in names:
                    continue
                out.append("%s/%s" % (kind, name))
        elif kind in ("RoleBinding", "ClusterRoleBinding"):
            # Un binding large (role d'édition ou joker) donne exec sans
            # nommer la ressource : il est refusé quel que soit son sujet.
            role = str((doc.get("roleRef") or {}).get("name") or "")
            if role in ("admin", "edit", "cluster-admin"):
                out.append("%s/%s (%s)" % (kind, name, role))
    return out


class ControlSixTest(unittest.TestCase):
    """Contrôle 6, partie statique : aucun `pods/exec` sur le pod partagé.

    Le contrôle complet du design §5.3 (un `SelfSubjectAccessReview` nommant
    le pod partagé, le Role `resourceNames` du pod de connexion, le compte de
    A qui n'entre pas dans le pod de B) demande un cluster et la procédure de
    connexion ; il est porté par #7911 et le témoin `pod-witness`. Ici on
    prouve la moitié négative sur les charts présents : aucun Role, aucun
    binding large, et un contrôle positif qui montre que le détecteur n'est
    pas vide.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if yaml is None:
            raise unittest.SkipTest("PyYAML absent : charts non vérifiés")
        cls.docs = []
        for directory in (POD_SOCLE, POD_SHARED, POD_WITNESS):
            kustomization = os.path.join(directory, "kustomization.yaml")
            if os.path.exists(kustomization):
                with open(kustomization, encoding="utf-8") as fh:
                    manifest = yaml.safe_load(fh)
                for name in manifest.get("resources") or ():
                    path = os.path.join(directory, name)
                    if os.path.exists(path):
                        with open(path, encoding="utf-8") as fh:
                            cls.docs.extend(d for d in yaml.safe_load_all(fh) if d)
                continue
            for name in sorted(os.listdir(directory)):
                if name.endswith(".yaml"):
                    with open(os.path.join(directory, name), encoding="utf-8") as fh:
                        cls.docs.extend(d for d in yaml.safe_load_all(fh) if d)

    def test_aucun_pods_exec_sur_le_pod_partage(self):
        self.assertEqual(pods_exec_violations(self.docs, SHARED_POD), [])

    def test_aucun_binding_du_compte_de_service_du_pod_partage(self):
        # Le compte de service du pod partagé ne reçoit aucun droit Kubernetes
        # (design §6.1) : un binding qui le nommerait rouvrirait la porte,
        # même pour une ressource autre que pods/exec. Le chart de connexion
        # de #7911 aura son propre compte de service et son propre pod.
        shared = {"kind": "ServiceAccount", "name": "ameesh-pod"}
        for doc in self.docs:
            if doc.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            self.assertNotIn(shared, doc.get("subjects") or [], doc)

    def test_controle_positif_le_detecteur_voit_un_exec_large(self):
        broad = {"kind": "Role", "metadata": {"name": "large"},
                 "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                            "verbs": ["create"]}]}
        star = {"kind": "ClusterRole", "metadata": {"name": "joker"},
                "rules": [{"apiGroups": ["*"], "resources": ["*"],
                           "verbs": ["*"]}]}
        admin = {"kind": "RoleBinding", "metadata": {"name": "b-admin"},
                 "roleRef": {"name": "admin"}}
        self.assertEqual(pods_exec_violations([broad]), ["Role/large"])
        self.assertEqual(pods_exec_violations([star]), ["ClusterRole/joker"])
        self.assertEqual(pods_exec_violations([admin]), ["RoleBinding/b-admin (admin)"])

    def test_controle_positif_un_pod_de_connexion_borne_est_admis(self):
        login = {"kind": "Role", "metadata": {"name": "login-alice"},
                 "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                            "resourceNames": ["pod-login-alice"],
                            "verbs": ["create"]}]}
        self.assertEqual(pods_exec_violations([login]), [])
        shared = {"kind": "Role", "metadata": {"name": "login-partage"},
                  "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                             "resourceNames": [SHARED_POD],
                             "verbs": ["create"]}]}
        # Même nommé, le pod partagé reste interdit : ses autres conteneurs
        # sont accessibles par le paramètre de requête.
        self.assertEqual(pods_exec_violations([shared]), ["Role/login-partage"])
        mixed = {"kind": "Role", "metadata": {"name": "login-mixte"},
                 "rules": [{"apiGroups": [""], "resources": ["pods/exec"],
                            "resourceNames": ["pod-login-alice", SHARED_POD],
                            "verbs": ["create"]}]}
        self.assertEqual(pods_exec_violations([mixed]), ["Role/login-mixte"])
