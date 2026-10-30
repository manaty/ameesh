# SPDX-License-Identifier: AGPL-3.0-only
"""#2202 S2 : blocs `sandbox` des cartes Agent et Host, canon check et admission.

Le profil (Agent) et le placement (Host) sont des cartes du canon, pas une
seconde autorité : le bureau relit le canon PR, `admitted_ref` en est la
preuve, `status: revoked` révoque sans seconde signature. Ces tests couvrent
la lecture des blocs, les contrôles de `canon check`, l'instantané d'admission
(références, empreintes, limites) et la fraîcheur/relecture du placement.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import canon, placement

DIGEST = "a" * 64
IMAGE = "ghcr.io/thepackengers/ima-reviewer@sha256:" + DIGEST

MEMBER = """---
type: Member
title: smichea
authenticators: []
---
"""

#: limites du profil de la qualification IMA #837 (exemples, pas constantes)
AGENT_TEMPLATE = """---
type: Agent
title: tpk-reviewer
responsible: human:smichea
team: ameesh
capabilities: [read, propose]
harness: codex
provider: openai
model: gpt-6.1-sol
credential_mode: api-key
tools: [repository.read, github.checks.read, review.report]
sandbox:
  profile: {profile}
  admitted_ref: "{admitted_ref}"
  status: {status}
  tranche: {tranche}
  credential: {credential}
  session: {session}
  capabilities: {{merge: {merge}, fix: {fix}, deploy: {deploy}, publish: {publish}}}
  limits:
    turns: {turns}
    max_attempts: {max_attempts}
    tool_calls: {tool_calls}
    turn_seconds: {turn_seconds}
    job_seconds: {job_seconds}
    context_tokens: {context_tokens}
    usd_per_turn: {usd_per_turn}
---
"""

HOST_TEMPLATE = """---
type: Host
title: tpk-stg-reviewers
responsible: human:smichea
policy:
  harnesses: [codex, deepseek]
  providers: [openai, deepseek]
  credential_modes: [api-key]
  max_agents: {max_agents}
  lease_ttl: 300
  sandbox:
    provider: {provider}
    kubeconfig: {kubeconfig}
    namespace: {namespace}
    image: {image}
    allowed_profiles: [{allowed_profiles}]
    max_concurrent_jobs: {max_concurrent_jobs}
    admitted_ref: "{host_admitted_ref}"
    job:
      activeDeadlineSeconds: {active_deadline}
      backoffLimit: {backoff}
      ttlSecondsAfterFinished: {ttl}
      automountServiceAccountToken: {automount}
---
"""

PLACEMENT = """---
type: Placement
title: tpk-reviewer-tpk-stg-reviewers
agent: tpk-reviewer
hosts: [tpk-stg-reviewers]
credential_mode: api-key
---
"""

AGENT_DEFAULTS = dict(
    profile="tpk-reviewer-2026-10", admitted_ref="thepackengers/home#239",
    status="admitted", tranche="tpk-migration-2026-10", credential="relay",
    session="none", merge="false", fix="false", deploy="false", publish="false",
    turns=1, max_attempts=2, tool_calls=200, turn_seconds=900, job_seconds=1200,
    context_tokens=131072, usd_per_turn=2)

HOST_DEFAULTS = dict(
    provider="kubernetes", kubeconfig="/etc/ameesh/kubeconfig.d/tpk-stg",
    namespace="reviewers", image=IMAGE, allowed_profiles="tpk-reviewer-2026-10",
    max_concurrent_jobs=3, host_admitted_ref="thepackengers/home#239",
    max_agents=3, active_deadline=1200, backoff=0, ttl=3600, automount="false")


def agent_card(**overrides) -> str:
    values = dict(AGENT_DEFAULTS, **overrides)
    return AGENT_TEMPLATE.format(**values)


def host_card(**overrides) -> str:
    values = dict(HOST_DEFAULTS, **overrides)
    return HOST_TEMPLATE.format(**values)


def codes(findings, severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


def _git_env() -> dict:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="banc", GIT_AUTHOR_EMAIL="banc@example.invalid",
               GIT_COMMITTER_NAME="banc", GIT_COMMITTER_EMAIL="banc@example.invalid")
    return env


def git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class SandboxCase(unittest.TestCase):
    """Un canon jetable, lu hors git (contrôles) ou par git (admission)."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-sbx-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.write("membres/smichea.md", MEMBER)
        self.write("agents/tpk-reviewer.md", agent_card())
        self.write("hotes/tpk-stg-reviewers.md", host_card())
        self.write("placements/tpk.md", PLACEMENT)

    def write(self, relpath: str, text: str | None) -> None:
        path = os.path.join(self.root, relpath)
        if text is None:
            if os.path.exists(path):
                os.unlink(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, message: str = "canon") -> str:
        if not os.path.isdir(os.path.join(self.root, ".git")):
            git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def load(self, *, trusted: bool = False, use_pyyaml: bool | None = False):
        kwargs = {"ref": "HEAD"} if trusted else {"untrusted": True}
        loaded = canon.load(self.root, use_pyyaml=use_pyyaml, **kwargs)
        return loaded, canon.validate(loaded)

    def errors(self, findings):
        return [f for f in findings if f.severity == canon.ERROR]

    def assertError(self, findings, code: str) -> None:
        matches = [f for f in findings if f.code == code]
        self.assertTrue(matches, "%s absent de %s" % (code, sorted(codes(findings))))
        self.assertTrue(any(f.severity == canon.ERROR for f in matches), matches)

    def assertNoError(self, findings) -> None:
        self.assertEqual(self.errors(findings), [], [f.to_dict() for f in
                                                     self.errors(findings)])


# ==========================================================================
# lecture et contrôles des blocs
# ==========================================================================

class SandboxParsingTest(SandboxCase):
    def test_blocs_valides_lus_sans_erreur(self):
        loaded, findings = self.load()
        self.assertNoError(findings)
        agent = loaded.agent("tpk-reviewer")
        self.assertIsNotNone(agent.sandbox)
        self.assertEqual(agent.sandbox.profile, "tpk-reviewer-2026-10")
        self.assertEqual(agent.sandbox.admitted_ref, "thepackengers/home#239")
        self.assertEqual(agent.sandbox.status, "admitted")
        self.assertEqual(agent.sandbox.tranche, "tpk-migration-2026-10")
        self.assertEqual(agent.sandbox.credential, "relay")
        self.assertEqual(agent.sandbox.session, "none")
        self.assertEqual(agent.sandbox.capabilities,
                         {"merge": False, "fix": False, "deploy": False, "publish": False})
        self.assertEqual(agent.sandbox.limits.turns, 1)
        self.assertEqual(agent.sandbox.limits.turn_seconds, 900.0)
        self.assertEqual(agent.sandbox.limits.job_seconds, 1200)
        self.assertEqual(agent.sandbox.limits.usd_per_turn, 2.0)
        host = loaded.host("tpk-stg-reviewers")
        self.assertEqual(host.policy.sandbox.provider, "kubernetes")
        self.assertEqual(host.policy.sandbox.namespace, "reviewers")
        self.assertEqual(host.policy.sandbox.image, IMAGE)
        self.assertEqual(host.policy.sandbox.image_digest, "sha256:" + DIGEST)
        self.assertEqual(host.policy.sandbox.allowed_profiles,
                         ["tpk-reviewer-2026-10"])
        self.assertEqual(host.policy.sandbox.max_concurrent_jobs, 3)
        self.assertEqual(host.policy.sandbox.job.active_deadline_seconds, 1200)
        self.assertEqual(host.policy.sandbox.job.backoff_limit, 0)
        self.assertFalse(host.policy.sandbox.job.automount_service_account_token)

    def test_to_dict_serialisable(self):
        loaded, _ = self.load()
        payload = loaded.to_dict()
        text = json.dumps(payload)          # aucun objet dataclass ne fuit
        self.assertIn("tpk-reviewer-2026-10", text)
        host = next(h for h in payload["hosts"] if h["title"] == "tpk-stg-reviewers")
        self.assertEqual(host["policy"]["sandbox"]["image"], IMAGE)
        agent = next(a for a in payload["agents"] if a["title"] == "tpk-reviewer")
        self.assertEqual(agent["sandbox"]["limits"]["job_seconds"], 1200)

    def test_les_deux_lecteurs_concordent(self):
        if canon._yaml is None:
            self.skipTest("PyYAML absent")
        mini, _ = self.load(use_pyyaml=False)
        full, _ = self.load(use_pyyaml=True)
        self.assertEqual(mini.agent("tpk-reviewer").sandbox.to_dict(),
                         full.agent("tpk-reviewer").sandbox.to_dict())
        self.assertEqual(mini.host("tpk-stg-reviewers").policy.sandbox.to_dict(),
                         full.host("tpk-stg-reviewers").policy.sandbox.to_dict())

    def test_bloc_absent(self):
        text = agent_card()
        start = text.index("sandbox:")
        end = text.index("---", start)
        self.write("agents/tpk-reviewer.md", text[:start] + text[end:])
        loaded, findings = self.load()
        self.assertIsNone(loaded.agent("tpk-reviewer").sandbox)
        self.assertNoError(findings)

    def test_bloc_non_mapping(self):
        self.write("agents/tpk-reviewer.md",
                   "---\ntype: Agent\ntitle: tpk-reviewer\nresponsible: human:smichea\n"
                   "sandbox: oui\n---\n")
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_cle_inconnue_avertissement(self):
        text = agent_card().replace("  tranche: tpk-migration-2026-10\n",
                                    "  tranche: tpk-migration-2026-10\n  couleur: bleu\n")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertIn("agent-sandbox-unknown", codes(findings, canon.WARNING))
        self.assertNoError(findings)


class SandboxProfileCheckTest(SandboxCase):
    def test_statut_revoque_est_valide(self):
        self.write("agents/tpk-reviewer.md", agent_card(status="revoked"))
        _, findings = self.load()
        self.assertNoError(findings)

    def test_statut_inconnu(self):
        self.write("agents/tpk-reviewer.md", agent_card(status="suspendu"))
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_statut_absent(self):
        text = agent_card().replace("  status: admitted\n", "")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_preuve_admission_absente(self):
        text = agent_card().replace('  admitted_ref: "thepackengers/home#239"\n', "")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_preuve_admission_faible_avertissement(self):
        self.write("agents/tpk-reviewer.md", agent_card(admitted_ref="le bureau a dit"))
        _, findings = self.load()
        self.assertIn("agent-sandbox-ref-weak", codes(findings, canon.WARNING))
        self.assertNoError(findings)

    def test_relais_seulement(self):
        self.write("agents/tpk-reviewer.md", agent_card(credential="api-key"))
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_session_none(self):
        self.write("agents/tpk-reviewer.md", agent_card(session="resume"))
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_les_quatre_capacites_fausses(self):
        for name in canon.SANDBOX_CAPABILITIES:
            with self.subTest(capability=name):
                self.write("agents/tpk-reviewer.md", agent_card(**{name: "true"}))
                _, findings = self.load()
                self.assertError(findings, "agent-sandbox-invalid")

    def test_capacite_inconnue_avertissement(self):
        text = agent_card().replace("publish: false}",
                                    "publish: false, approve: false}")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertIn("agent-sandbox-unknown", codes(findings, canon.WARNING))

    def test_capacite_non_booleenne(self):
        text = agent_card().replace("merge: false", "merge: non")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_limites_positives(self):
        cases = {"turns": 0, "max_attempts": 0, "tool_calls": -1,
                 "turn_seconds": 0, "context_tokens": 0, "usd_per_turn": 0}
        for key, value in cases.items():
            with self.subTest(limit=key):
                self.write("agents/tpk-reviewer.md", agent_card(**{key: value}))
                _, findings = self.load()
                self.assertError(findings, "agent-sandbox-invalid")

    def test_job_seconds_apres_turn_seconds(self):
        self.write("agents/tpk-reviewer.md", agent_card(job_seconds=900))
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_limite_absente(self):
        text = agent_card().replace("    job_seconds: 1200\n", "")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_limites_non_mapping(self):
        text = agent_card()
        start = text.index("  limits:")
        end = text.index("---", start)
        self.write("agents/tpk-reviewer.md", text[:start] + "  limits: 3\n" + text[end:])
        _, findings = self.load()
        self.assertError(findings, "agent-sandbox-invalid")

    def test_limite_inconnue_avertissement(self):
        text = agent_card().replace("    usd_per_turn: 2\n",
                                    "    usd_per_turn: 2\n    usd_per_attempt: 1\n")
        self.write("agents/tpk-reviewer.md", text)
        _, findings = self.load()
        self.assertIn("agent-sandbox-unknown", codes(findings, canon.WARNING))


class SandboxPlacementCheckTest(SandboxCase):
    def test_provider_inconnu(self):
        self.write("hotes/tpk-stg-reviewers.md", host_card(provider="docker"))
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")

    def test_image_doit_porter_une_empreinte(self):
        for image in ("ghcr.io/thepackengers/ima-reviewer:latest",
                      "ghcr.io/thepackengers/ima-reviewer@sha256:abc"):
            with self.subTest(image=image):
                self.write("hotes/tpk-stg-reviewers.md", host_card(image=image))
                _, findings = self.load()
                self.assertError(findings, "host-sandbox-invalid")

    def test_kubeconfig_absolu_et_prive(self):
        for path in ("relatif/kubeconfig", "~/.kube/config", "/tmp/kubeconfig"):
            with self.subTest(path=path):
                self.write("hotes/tpk-stg-reviewers.md", host_card(kubeconfig=path))
                _, findings = self.load()
                self.assertError(findings, "host-sandbox-invalid")

    def test_kubeconfig_present_doit_etre_0600(self):
        directory = tempfile.mkdtemp(prefix="ameesh-kubeconfig-",
                                     dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "config")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("apiVersion: v1\n")
        os.chmod(path, 0o644)
        self.write("hotes/tpk-stg-reviewers.md", host_card(kubeconfig=path))
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")
        os.chmod(path, 0o600)
        _, findings = self.load()
        self.assertNoError(findings)

    def test_namespace_dns(self):
        self.write("hotes/tpk-stg-reviewers.md", host_card(namespace="Reviewers_1"))
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")

    def test_max_concurrent_jobs_dans_max_agents(self):
        self.write("hotes/tpk-stg-reviewers.md",
                   host_card(max_concurrent_jobs=4, max_agents=3))
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")

    def test_max_agents_requis(self):
        text = host_card().replace("  max_agents: 3\n", "")
        self.write("hotes/tpk-stg-reviewers.md", text)
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")

    def test_job_kubernetes_borne(self):
        cases = {"backoff": 6, "automount": "true", "ttl": 0, "active_deadline": 0}
        for key, value in cases.items():
            with self.subTest(field=key):
                self.write("hotes/tpk-stg-reviewers.md", host_card(**{key: value}))
                _, findings = self.load()
                self.assertError(findings, "host-sandbox-invalid")

    def test_job_absent(self):
        text = host_card()
        start = text.index("    job:")
        end = text.index("---", start)
        self.write("hotes/tpk-stg-reviewers.md", text[:start] + text[end:])
        _, findings = self.load()
        self.assertError(findings, "host-sandbox-invalid")

    def test_cle_inconnue_avertissement(self):
        text = host_card().replace("    max_concurrent_jobs: 3\n",
                                   "    max_concurrent_jobs: 3\n    pull_secret: x\n")
        self.write("hotes/tpk-stg-reviewers.md", text)
        _, findings = self.load()
        self.assertIn("host-sandbox-unknown", codes(findings, canon.WARNING))


class SandboxAgreementTest(SandboxCase):
    def test_profil_non_admis_par_l_hote(self):
        self.write("hotes/tpk-stg-reviewers.md",
                   host_card(allowed_profiles="autre-profil"))
        _, findings = self.load()
        self.assertError(findings, "placement-sandbox-profile-not-allowed")

    def test_allowed_profiles_vide_avertissement(self):
        self.write("hotes/tpk-stg-reviewers.md", host_card(allowed_profiles=""))
        _, findings = self.load()
        self.assertIn("host-sandbox-empty", codes(findings, canon.WARNING))
        self.assertError(findings, "placement-sandbox-profile-not-allowed")

    def test_deadline_job_trop_courte(self):
        self.write("hotes/tpk-stg-reviewers.md", host_card(active_deadline=600))
        _, findings = self.load()
        self.assertError(findings, "placement-sandbox-job-too-short")

    def test_hote_sans_placement_sandbox_avertissement(self):
        text = host_card()
        start = text.index("  sandbox:")
        end = text.index("---", start)
        self.write("hotes/tpk-stg-reviewers.md", text[:start] + text[end:])
        _, findings = self.load()
        self.assertIn("placement-sandbox-missing", codes(findings, canon.WARNING))
        self.assertNoError(findings)

    def test_erreur_bloque_l_agent_et_l_hote(self):
        # un constat d'agent ferme sa réclamation ; un constat d'hôte ferme
        # les agents qui y sont placés (canon.blocking)
        self.write("agents/tpk-reviewer.md", agent_card(turns=0))
        _, findings = self.load()
        self.assertTrue(canon.blocking(findings).by_agent.get("tpk-reviewer"))
        self.write("agents/tpk-reviewer.md", agent_card())
        self.write("hotes/tpk-stg-reviewers.md", host_card(image="ghcr.io/x:tag"))
        _, findings = self.load()
        self.assertTrue(canon.blocking(findings).by_host.get("tpk-stg-reviewers"))


# ==========================================================================
# empreinte de carte
# ==========================================================================

class SandboxDigestTest(SandboxCase):
    def test_empreinte_independante_du_commit(self):
        self.commit("initial")
        first, _ = self.load(trusted=True)
        before = canon.card_digest(first.agent("tpk-reviewer").fiche)
        # une autre carte change, pas celle du profil
        self.write("membres/autre.md",
                   "---\ntype: Member\ntitle: autre\nauthenticators: []\n---\n")
        self.commit("unrelated")
        second, _ = self.load(trusted=True)
        self.assertNotEqual(first.sources[0].commit, second.sources[0].commit)
        self.assertEqual(before, canon.card_digest(second.agent("tpk-reviewer").fiche))

    def test_empreinte_suit_la_carte(self):
        self.commit("initial")
        first, _ = self.load(trusted=True)
        before = canon.card_digest(first.agent("tpk-reviewer").fiche)
        self.write("agents/tpk-reviewer.md",
                   agent_card(turns=2))
        self.commit("profile changed")
        second, _ = self.load(trusted=True)
        self.assertNotEqual(before, canon.card_digest(second.agent("tpk-reviewer").fiche))

    def test_empreinte_json_safe(self):
        self.write("hotes/tpk-stg-reviewers.md",
                   host_card().replace("responsible: human:smichea\n",
                                       "responsible: human:smichea\n"
                                       "indisponible_jusqu_au: 2026-11-01\n"))
        loaded, _ = self.load()
        self.assertTrue(canon.card_digest(loaded.host("tpk-stg-reviewers").fiche)
                        .startswith("sha256:"))


# ==========================================================================
# admission (canon lu par git, à la révision auditée)
# ==========================================================================

class SandboxAdmissionTest(SandboxCase):
    def admitted(self, **kwargs):
        self.commit("initial")
        loaded, findings = self.load(trusted=True)
        self.assertNoError(findings)
        return loaded, placement.sandbox_admission(
            loaded, "tpk-reviewer", "tpk-stg-reviewers", **kwargs)

    def test_instantane_complet(self):
        loaded, admission = self.admitted()
        self.assertTrue(admission.ok, admission.diagnostic)
        agent = loaded.agent("tpk-reviewer")
        host = loaded.host("tpk-stg-reviewers")
        self.assertEqual(admission.refusal, "")
        self.assertEqual(admission.profile, "tpk-reviewer-2026-10")
        self.assertEqual(admission.profile_ref, agent.fiche.ref)
        self.assertEqual(admission.placement_ref, host.fiche.ref)
        self.assertEqual(admission.canon_ref, loaded.sources[0].commit)
        self.assertEqual(admission.profile_digest, canon.card_digest(agent.fiche))
        self.assertEqual(admission.placement_digest, canon.card_digest(host.fiche))
        self.assertEqual(admission.admitted_ref, "thepackengers/home#239")
        self.assertEqual(admission.host_admitted_ref, "thepackengers/home#239")
        self.assertEqual(admission.tranche, "tpk-migration-2026-10")
        self.assertEqual(admission.image_digest, "sha256:" + DIGEST)
        self.assertEqual(admission.image, IMAGE)
        self.assertEqual(admission.provider, "kubernetes")
        self.assertEqual(admission.namespace, "reviewers")
        self.assertEqual(admission.active_deadline_seconds, 1200)
        self.assertEqual(admission.ttl_seconds_after_finished, 3600)
        self.assertEqual(admission.credential, "relay")
        self.assertEqual(admission.session, "none")
        self.assertEqual(admission.tools, ["repository.read", "github.checks.read",
                                           "review.report"])
        self.assertEqual(admission.capabilities,
                         {"merge": False, "fix": False, "deploy": False, "publish": False})
        self.assertEqual(admission.limits["turns"], 1)
        self.assertEqual(admission.limits["max_attempts"], 2)
        self.assertEqual(admission.limits["job_seconds"], 1200)
        self.assertEqual(admission.limits["session"], "none")

    def test_canon_non_approuve_refuse(self):
        loaded, _ = self.load()
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertEqual(admission.refusal, "profile-refused")
        self.assertIn("approved", admission.diagnostic)

    def test_revision_epinglee(self):
        self.commit("initial")
        loaded, _ = self.load(trusted=True)
        wrong = "0" * 40
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers", canon_ref=wrong)
        self.assertFalse(admission.ok)
        self.assertIn("pinned revision", admission.diagnostic)

    def test_profil_revoque_refuse(self):
        self.write("agents/tpk-reviewer.md", agent_card(status="revoked"))
        self.commit("revocation")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("revoked", admission.diagnostic)

    def test_reserve_du_compte(self):
        _, admission = self.admitted(reserve_usd=1.99)
        self.assertFalse(admission.ok)
        self.assertIn("reserve_usd", admission.diagnostic)
        _, admission = self.admitted(reserve_usd=2)
        self.assertTrue(admission.ok, admission.diagnostic)
        _, admission = self.admitted(reserve_usd=10)
        self.assertTrue(admission.ok, admission.diagnostic)
        for invalid in (float("inf"), -1, "beaucoup"):
            with self.subTest(reserve=invalid):
                _, admission = self.admitted(reserve_usd=invalid)
                self.assertFalse(admission.ok)
                self.assertIn("reserve_usd", admission.diagnostic)

    def test_profil_hors_allowed_profiles(self):
        self.write("hotes/tpk-stg-reviewers.md",
                   host_card(allowed_profiles="autre-profil"))
        self.commit("placement")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("allowed_profiles", admission.diagnostic)

    def test_placement_ordinaire_requis(self):
        self.write("placements/tpk.md", None)
        self.commit("sans placement")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("placement refused", admission.diagnostic)

    def test_politique_de_l_hote_appliquee(self):
        text = host_card().replace("harnesses: [codex, deepseek]",
                                   "harnesses: [deepseek]")
        self.write("hotes/tpk-stg-reviewers.md", text)
        self.commit("politique")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("harness codex", admission.diagnostic)

    def test_hote_sans_bloc_sandbox(self):
        text = host_card()
        start = text.index("  sandbox:")
        end = text.index("---", start)
        self.write("hotes/tpk-stg-reviewers.md", text[:start] + text[end:])
        self.commit("sans sandbox")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("policy.sandbox", admission.diagnostic)

    def test_bloc_invalide_refuse_meme_sans_canon_check(self):
        # défense en profondeur : l'admission rejoue les contrôles du bloc
        self.write("agents/tpk-reviewer.md", agent_card(turns=0))
        self.commit("limite nulle")
        loaded, _ = self.load(trusted=True)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertFalse(admission.ok)
        self.assertIn("sandbox.limits.turns", admission.diagnostic)


# ==========================================================================
# fraîcheur de canon_state
# ==========================================================================

class SandboxFreshnessTest(unittest.TestCase):
    STATE = {"status": "ok", "checked_ts": 1000.0, "last_good_commit": "a" * 40}

    def test_etat_absent(self):
        self.assertEqual(placement.canon_fresh(None), (False,
                                                       "no canon state recorded for this host"))

    def test_etat_en_erreur(self):
        state = dict(self.STATE, status="invalid")
        self.assertFalse(placement.canon_fresh(state, now=1001.0)[0])

    def test_fenetre_de_300_s(self):
        self.assertTrue(placement.canon_fresh(self.STATE, now=1300.0)[0])
        ok, reason = placement.canon_fresh(self.STATE, now=1300.1)
        self.assertFalse(ok)
        self.assertIn("freshness window", reason)

    def test_fenetre_reglable(self):
        self.assertFalse(placement.canon_fresh(self.STATE, now=1100.0, max_age=60)[0])
        self.assertTrue(placement.canon_fresh(self.STATE, now=1050.0, max_age=60)[0])

    def test_commit_absent(self):
        state = dict(self.STATE, last_good_commit="")
        self.assertFalse(placement.canon_fresh(state, now=1001.0)[0])

    def test_checked_at_illisible(self):
        state = dict(self.STATE, checked_ts="hier")
        self.assertFalse(placement.canon_fresh(state, now=1001.0)[0])


# ==========================================================================
# relecture à la resynchronisation
# ==========================================================================

class SandboxRecheckTest(SandboxCase):
    def admitted(self):
        self.commit("initial")
        loaded, findings = self.load(trusted=True)
        self.assertNoError(findings)
        admission = placement.sandbox_admission(loaded, "tpk-reviewer",
                                                "tpk-stg-reviewers")
        self.assertTrue(admission.ok, admission.diagnostic)
        return loaded, admission

    def test_inchange(self):
        loaded, admission = self.admitted()
        self.assertEqual(placement.sandbox_recheck(loaded, admission).verdict, "same")

    def test_changement_cosmetique(self):
        _, admission = self.admitted()
        text = agent_card().replace("---\n", "---\ndescription: un relecteur borné\n", 1)
        self.write("agents/tpk-reviewer.md", text)
        self.commit("description")
        loaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(loaded, admission)
        self.assertEqual(drift.verdict, "cosmetic", drift.diagnostic)

    def test_borne_modifiee(self):
        _, admission = self.admitted()
        self.write("agents/tpk-reviewer.md", agent_card(tool_calls=300))
        self.commit("limite")
        loaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(loaded, admission)
        self.assertEqual(drift.verdict, "changed", drift.diagnostic)
        self.assertIn("limits", drift.changed)

    def test_revocation(self):
        _, admission = self.admitted()
        self.write("agents/tpk-reviewer.md", agent_card(status="revoked"))
        self.commit("revocation")
        loaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(loaded, admission)
        self.assertEqual(drift.verdict, "revoked")
        self.assertIn("revoked", drift.diagnostic)

    def test_job_modifie(self):
        _, admission = self.admitted()
        self.write("hotes/tpk-stg-reviewers.md", host_card(ttl=600))
        self.commit("ttl")
        loaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(loaded, admission)
        self.assertEqual(drift.verdict, "changed", drift.diagnostic)
        self.assertIn("limits", drift.changed)

    def test_hote_revoque(self):
        _, admission = self.admitted()
        text = host_card()
        start = text.index("  sandbox:")
        end = text.index("---", start)
        self.write("hotes/tpk-stg-reviewers.md", text[:start] + text[end:])
        self.commit("host sans placement")
        loaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(loaded, admission)
        self.assertEqual(drift.verdict, "revoked")
        self.assertIn("policy.sandbox", drift.diagnostic)

    def test_sans_instantane(self):
        drift = placement.sandbox_recheck(None, placement.SandboxAdmission(
            "a", "h", False))
        self.assertEqual(drift.verdict, "revoked")


if __name__ == "__main__":
    unittest.main()
