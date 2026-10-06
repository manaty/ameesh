# SPDX-License-Identifier: AGPL-3.0-only
"""Canon OKF (L2) : lecture par git, YAML, validation, sync, éphémères, règle R14."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import canon, canon_sync, config as config_mod, db as db_mod, fil, receipts, registry
from ameesh import runner as runner_mod

from .support import REPO, PgTestCase
from .webauthn_soft import SoftWebAuthn

EXAMPLE = os.path.join(REPO, "examples", "canon")
HAS_PYYAML = canon._yaml is not None


# --------------------------------------------------------------------------
# utilitaires : dépôts git jetables (une « branche canonique distante » = un
# dépôt nu local), copies du canon d'exemple
# --------------------------------------------------------------------------

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


def write(root: str, relpath: str, text: str | None) -> None:
    path = os.path.join(root, relpath)
    if text is None:
        if os.path.exists(path):
            os.unlink(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def fiche(**fields) -> str:
    """Un fichier OKF minimal : frontmatter (YAML simple) + un titre."""
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            value = "[%s]" % ", ".join(value)
        lines.append("%s: %s" % (key, value))
    lines += ["---", "", "# fiche", ""]
    return "\n".join(lines)


def publish(workspace: str, name: str, content_from: str | None = None,
            branch: str = "main") -> tuple[str, str]:
    """Dépôt nu `<name>.git` + clone de travail `<name>` ; renvoie (nu, clone)."""
    bare = os.path.join(workspace, "remotes", name + ".git")
    os.makedirs(bare)
    git(bare, "init", "-q", "--bare", "-b", branch)
    clone = os.path.join(workspace, name)
    os.makedirs(clone)
    git(clone, "init", "-q", "-b", branch)
    if content_from:
        shutil.copytree(content_from, clone, dirs_exist_ok=True)
    else:
        write(clone, "index.md", "---\ntype: Index\ntitle: x\n---\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "canon initial")
    git(clone, "remote", "add", "origin", bare)
    git(clone, "push", "-q", "origin", branch)
    git(clone, "fetch", "-q", "origin")
    git(clone, "remote", "set-head", "origin", branch)
    return bare, clone


def commit_all(clone: str, message: str = "changement", push: bool = True) -> str:
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", message)
    if push:
        git(clone, "push", "-q", "origin", "HEAD")
        git(clone, "fetch", "-q", "origin")
    return git(clone, "rev-parse", "HEAD")


def codes(findings, severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


class _TmpMixin:
    def make_tmp(self) -> str:
        path = tempfile.mkdtemp(prefix="ameesh-canon-")
        self.addCleanup(shutil.rmtree, path, True)
        return os.path.realpath(path)

    def example_copy(self) -> str:
        """Le canon d'exemple, hors git (mode non approuvé)."""
        root = os.path.join(self.make_tmp(), "acme")
        shutil.copytree(EXAMPLE, root)
        if canon.git_toplevel(root):
            self.skipTest("le dossier temporaire est dans un dépôt git")
        return root


# ==========================================================================
# YAML
# ==========================================================================

SNIPPETS = [
    "type: Agent\ntitle: deepseek7\ncapabilities: [read, report-drift, propose]\n",
    "tools: [git, \"mcp:transport-readonly\", 'a, b']\nbudget: 20\nratio: 1.5\nok: true\nnul: ~\n",
    "policy:\n  harnesses: [claude, codex]   # absent = tous\n  max_agents: 12\n"
    "  nested:\n    deep: x\n",
    "roles:\n  - a\n  - b\nmembers:\n- id: home\n  ref: main\n  bundle: .\n- id: autre\n",
    "generated: { by: \"x/y\", at: \"2026-10-04T00:00:00+02:00\" }\n"
    "sources:\n  - { resource: \"a.md\", title: \"A, B\" }\n",
    "description: 'l''agent dit \"oui\"'\nautre: \"tab\\tet \\u00e9\"\ntitre: l'agent # commentaire\n",
    "texte: |\n  ligne 1\n  ligne 2\nplie: >\n  un\n  deux\n\n  trois\nfin: x\n",
    "migration: &mig\n  owner: role:x\n  target: \"2026-12-31\"\nautre:\n  migration: *mig\n",
    "url: https://example.invalid/a#b\nvide:\nliste_vide: []\nmap_vide: {}\n",
    "long: [a,\n  b, c]\n",
]


class YamlTest(unittest.TestCase):
    def test_lecteur_minimal_valeurs(self):
        mini = lambda text: canon.load_yaml(text, use_pyyaml=False)  # noqa: E731
        self.assertEqual(mini(SNIPPETS[0])["capabilities"], ["read", "report-drift", "propose"])
        data = mini(SNIPPETS[1])
        self.assertEqual(data["tools"], ["git", "mcp:transport-readonly", "a, b"])
        self.assertEqual((data["budget"], data["ratio"], data["ok"], data["nul"]),
                         (20, 1.5, True, None))
        self.assertEqual(mini(SNIPPETS[2])["policy"]["nested"], {"deep": "x"})
        self.assertEqual(mini(SNIPPETS[3])["members"][0],
                         {"id": "home", "ref": "main", "bundle": "."})
        self.assertEqual(mini(SNIPPETS[4])["sources"][0]["title"], "A, B")
        data = mini(SNIPPETS[5])
        self.assertEqual(data["description"], "l'agent dit \"oui\"")
        self.assertEqual(data["autre"], "tab\tet é")
        self.assertEqual(data["titre"], "l'agent")
        data = mini(SNIPPETS[6])
        self.assertEqual(data["texte"], "ligne 1\nligne 2\n")
        self.assertEqual(data["plie"], "un deux\ntrois\n")
        self.assertEqual(mini(SNIPPETS[7])["autre"]["migration"]["owner"], "role:x")
        data = mini(SNIPPETS[8])
        self.assertEqual((data["url"], data["vide"], data["liste_vide"], data["map_vide"]),
                         ("https://example.invalid/a#b", None, [], {}))
        self.assertEqual(mini(SNIPPETS[9])["long"], ["a", "b", "c"])

    @unittest.skipUnless(HAS_PYYAML, "PyYAML absent : seul le lecteur minimal est testé")
    def test_les_deux_lecteurs_concordent(self):
        for text in SNIPPETS:
            with self.subTest(text=text[:30]):
                self.assertEqual(canon.load_yaml(text, use_pyyaml=False),
                                 canon.load_yaml(text, use_pyyaml=True))
        for current, _dirs, files in os.walk(EXAMPLE):
            for name in files:
                with open(os.path.join(current, name), encoding="utf-8") as fh:
                    block = canon.split_frontmatter(fh.read())
                with self.subTest(fichier=name):
                    self.assertEqual(canon.load_yaml(block, use_pyyaml=False),
                                     canon.load_yaml(block, use_pyyaml=True))

    def test_erreurs_explicites(self):
        for use in ([False, True] if HAS_PYYAML else [False]):
            for text in ("a: 1\na: 2\n",                 # clé en double : ambigu
                         "a:\n\t- x\n",                 # tabulation
                         "a: [x, y\n",                  # liste non fermée
                         "a: \"non terminée\n",
                         "a:\n  b: 1\n c: 2\n"):        # indentation incohérente
                with self.subTest(pyyaml=use, text=text):
                    with self.assertRaises(canon.YamlError):
                        canon.load_yaml(text, use_pyyaml=use)

    def test_entrees_hostiles(self):
        # chaîne d'alias : les objets restent partagés, rien ne se déplie
        lignes = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
        lignes += ["a%d: &a%d [*a%d, *a%d, *a%d, *a%d, *a%d, *a%d, *a%d, *a%d]"
                   % ((i, i) + (i - 1,) * 8) for i in range(1, 30)]
        text = "\n".join(lignes) + "\n"
        for use in ([False, True] if HAS_PYYAML else [False]):
            with self.subTest(pyyaml=use):
                data = canon.load_yaml(text, use_pyyaml=use)
                self.assertIs(data["a29"][0], data["a29"][1])
                with self.assertRaises(canon.YamlError):
                    canon.load_yaml("a: " + "[" * 5000 + "]" * 5000 + "\n", use_pyyaml=use)

    def test_lecteur_par_defaut(self):
        """Sans PyYAML importable, le lecteur minimal prend le relais."""
        with mock.patch.object(canon, "_yaml", None):
            self.assertEqual(canon.load_yaml("a: [1, 2]\n"), {"a": [1, 2]})
            with self.assertRaises(canon.YamlError):
                canon.load_yaml("a: 1\n", use_pyyaml=True)

    def test_frontmatter(self):
        self.assertIsNone(canon.split_frontmatter("# pas de frontmatter\n"))
        self.assertEqual(canon.split_frontmatter("---\na: 1\n---\ncorps\n"), "a: 1")
        self.assertEqual(canon.split_frontmatter("\ufeff---\r\na: 1\r\n...\r\n"), "a: 1")
        with self.assertRaises(canon.YamlError):
            canon.split_frontmatter("---\na: 1\n")


# ==========================================================================
# politique d'hôte
# ==========================================================================

class PlacementPolicyTest(unittest.TestCase):
    AGENT = {"harness": "claude", "provider": "anthropic", "model": "claude-opus",
             "credential_mode": "subscription"}

    def test_cle_absente_admet_tout(self):
        self.assertEqual(canon.placement_violations(self.AGENT, {"policy": {}}), [])
        self.assertEqual(canon.placement_violations(self.AGENT, {"policy": None}), [])

    def test_liste_vide_n_admet_rien(self):
        out = canon.placement_violations(self.AGENT, {"title": "h", "policy": {"harnesses": []}})
        self.assertEqual(len(out), 1)
        self.assertIn("harnais claude", out[0])

    def test_chaque_cle(self):
        host = {"title": "banc", "policy": {
            "harnesses": ["deepseek", "codex"], "providers": ["deepseek"],
            "credential_modes": ["api-key"], "models": ["deepseek-*"]}}
        out = canon.placement_violations(self.AGENT, host)
        self.assertEqual(len(out), 4, out)
        ok = {"harness": "deepseek", "provider": "deepseek", "model": "deepseek-chat",
              "credential_mode": "api-key"}
        self.assertEqual(canon.placement_violations(ok, host), [])

    def test_valeur_non_declaree_refusee(self):
        out = canon.placement_violations({"harness": "codex"},
                                         {"policy": {"providers": ["openai"]}})
        self.assertEqual(len(out), 1)
        self.assertIn("non déclaré", out[0])

    def test_le_mode_du_placement_prime(self):
        host = {"policy": {"credential_modes": ["api-key"]}}
        self.assertEqual(canon.placement_violations(
            self.AGENT, host, {"credential_mode": "api-key"}), [])
        self.assertEqual(len(canon.placement_violations(self.AGENT, host)), 1)

    def test_objets_du_canon(self):
        policy = canon.HostPolicy(harnesses=["codex"])
        fiche_ = canon.Fiche("Host", "h", "m", "p", "r", {})
        host = canon.Host("h", "human:x", policy, fiche_)
        self.assertEqual(len(canon.placement_violations(self.AGENT, host)), 1)


# ==========================================================================
# validation (mode non approuvé : dossiers temporaires hors git)
# ==========================================================================

class ValidationTest(_TmpMixin, unittest.TestCase):
    def check(self, root: str, **kwargs):
        loaded = canon.load(root, untrusted=True, **kwargs)
        return loaded, canon.validate(loaded)

    def assertFinding(self, findings, code, severity=canon.ERROR, **subject):
        matches = [f for f in findings if f.code == code]
        self.assertTrue(matches, "%s absent de %s" % (code, sorted(codes(findings))))
        self.assertTrue(any(f.severity == severity for f in matches), matches)
        for key, value in subject.items():
            self.assertTrue(any(getattr(f, key) == value for f in matches), (key, matches))
        return matches

    def test_exemple_valide_avec_les_deux_lecteurs(self):
        root = self.example_copy()
        uses = [False, True] if HAS_PYYAML else [False]
        dumps = []
        for use in uses:
            loaded, findings = self.check(root, use_pyyaml=use)
            self.assertEqual(codes(findings), {"canon-untrusted"}, findings)
            self.assertTrue(all(f.untrusted for f in findings))
            self.assertEqual(sorted(a.title for a in loaded.agents),
                             ["orchestre", "ouvrier", "relecteur"])
            self.assertEqual(sorted(h.title for h in loaded.hosts), ["atelier", "banc"])
            self.assertEqual(len(loaded.placements), 3)
            self.assertEqual(sorted(m.title for m in loaded.members), ["alice", "bruno"])
            dumps.append(loaded.to_dict())
        self.assertEqual(dumps[0], dumps[-1])

    def test_hors_git_refuse_sans_drapeau(self):
        root = self.example_copy()
        loaded = canon.load(root)
        self.assertFalse(loaded.readable)
        self.assertEqual(loaded.agents, [])
        self.assertIn("canon-untrusted-refused", codes(canon.validate(loaded), canon.ERROR))

    def test_racine_absente(self):
        loaded = canon.load(os.path.join(self.make_tmp(), "absent"), untrusted=True)
        self.assertIn("canon-missing", codes(canon.validate(loaded), canon.ERROR))

    def test_responsable_absent(self):
        root = self.example_copy()
        write(root, "agents/ouvrier.md", fiche(type="Agent", title="ouvrier",
                                               harness="deepseek", provider="deepseek"))
        self.assertFinding(self.check(root)[1], "agent-responsible-missing", agent="ouvrier")

    def test_capacite_approve(self):
        root = self.example_copy()
        write(root, "agents/ouvrier.md", fiche(
            type="Agent", title="ouvrier", responsible="human:bruno", harness="deepseek",
            provider="deepseek", credential_mode="api-key", capabilities=["read", "Approve"]))
        self.assertFinding(self.check(root)[1], "agent-approve-capability", agent="ouvrier")

    def test_responsable_non_resolu(self):
        for valeur in ("human:inconnu", "bruno", "agent:orchestre", "human:orchestre"):
            with self.subTest(responsible=valeur):
                root = self.example_copy()
                write(root, "agents/ouvrier.md", fiche(
                    type="Agent", title="ouvrier", responsible=valeur, harness="deepseek",
                    provider="deepseek", credential_mode="api-key"))
                self.assertFinding(self.check(root)[1], "agent-responsible-unresolved",
                                   agent="ouvrier")

    def test_placement_vers_agent_ou_hote_inconnu(self):
        root = self.example_copy()
        write(root, "placements/x.md", fiche(type="Placement", agent="fantome", host="atelier"))
        write(root, "placements/ouvrier-banc.md", fiche(
            type="Placement", agent="ouvrier", host="nulle-part", credential_mode="api-key"))
        findings = self.check(root)[1]
        self.assertFinding(findings, "placement-agent-unknown", agent="fantome")
        self.assertFinding(findings, "placement-host-unknown", agent="ouvrier")

    def test_deux_placements(self):
        root = self.example_copy()
        write(root, "placements/ouvrier-atelier.md", fiche(
            type="Placement", agent="ouvrier", host="atelier", credential_mode="api-key"))
        matches = self.assertFinding(self.check(root)[1], "placement-duplicate", agent="ouvrier")
        self.assertEqual(len(matches), 2)

    def test_placement_viole_la_politique(self):
        root = self.example_copy()
        # orchestre (claude, anthropic, forfait) sur le banc (deepseek/codex, clé d'API)
        write(root, "placements/orchestre-atelier.md", fiche(
            type="Placement", agent="orchestre", host="banc", credential_mode="subscription"))
        matches = self.assertFinding(self.check(root)[1], "placement-policy-violation",
                                     agent="orchestre")
        textes = " ".join(f.message for f in matches)
        for attendu in ("harnais claude", "fournisseur anthropic",
                        "mode d'identifiants subscription"):
            self.assertIn(attendu, textes)

    def test_hote_sans_responsable_ou_non_resolu(self):
        root = self.example_copy()
        write(root, "hotes/banc.md", fiche(type="Host", title="banc"))
        write(root, "hotes/atelier.md", fiche(type="Host", title="atelier",
                                               responsible="human:personne"))
        findings = self.check(root)[1]
        self.assertFinding(findings, "host-responsible-missing", host="banc")
        self.assertFinding(findings, "host-responsible-unresolved", host="atelier")

    def test_avertissements_sans_placement(self):
        root = self.example_copy()
        write(root, "placements/ouvrier-banc.md", None)
        findings = self.check(root)[1]
        self.assertFinding(findings, "agent-unplaced", canon.WARNING, agent="ouvrier")
        self.assertFinding(findings, "host-unplaced", canon.WARNING, host="banc")
        self.assertEqual(codes(findings, canon.ERROR), set())

    def test_frontmatter_illisible(self):
        root = self.example_copy()
        write(root, "agents/casse.md", "---\ntype: Agent\ntitle: casse\nresponsible: [x\n---\n")
        write(root, "notes/libre.md", "---\ntitle: note\nliste: [x\n---\n")
        findings = self.check(root)[1]
        typed = [f for f in findings if f.code == "frontmatter-invalid"
                 and f.path == "agents/casse.md"]
        libre = [f for f in findings if f.code == "frontmatter-invalid"
                 and f.path == "notes/libre.md"]
        self.assertEqual([f.severity for f in typed], [canon.ERROR])
        self.assertEqual([f.severity for f in libre], [canon.WARNING])
        # une fiche du profil illisible n'a pas de sujet : elle bloque tout
        self.assertTrue(canon.blocking(findings).global_errors)

    def test_doublons_et_conflits(self):
        root = self.example_copy()
        shutil.copy(os.path.join(root, "agents/ouvrier.md"), os.path.join(root, "agents/bis.md"))
        write(root, "membres/orchestre.md", fiche(type="Member", title="orchestre"))
        write(root, "hotes/petit.md", fiche(type="Host", title="petit",
                                             responsible="human:bruno",
                                             policy="{max_agents: 0}"))
        write(root, "placements/relecteur-atelier.md", fiche(
            type="Placement", agent="relecteur", host="petit"))
        findings = self.check(root)[1]
        self.assertEqual(len([f for f in findings if f.code == "agent-duplicate"]), 2)
        self.assertFinding(findings, "member-agent-clash", agent="orchestre")
        self.assertFinding(findings, "host-max-agents", host="petit")

    def test_champs_invalides(self):
        root = self.example_copy()
        write(root, "agents/sans-titre.md", "---\ntype: Agent\nresponsible: human:bruno\n---\n")
        write(root, "agents/mauvais.md", fiche(type="Agent", title="'nom invalide'",
                                                responsible="human:bruno"))
        write(root, "agents/caps.md", "---\ntype: Agent\ntitle: caps\nresponsible: human:bruno\n"
                                      "capabilities: {read: true}\n---\n")
        write(root, "hotes/policy.md", fiche(type="Host", title="pol", responsible="human:bruno",
                                              policy="[a]"))
        write(root, "placements/vide.md", fiche(type="Placement", host="atelier"))
        findings = self.check(root)[1]
        for code in ("fiche-title-missing", "agent-title-invalid", "agent-capabilities-invalid",
                     "host-policy-invalid", "placement-incomplete"):
            self.assertFinding(findings, code)

    def test_blocage_par_sujet(self):
        root = self.example_copy()
        write(root, "agents/ouvrier.md", fiche(type="Agent", title="ouvrier",
                                               harness="deepseek", provider="deepseek",
                                               credential_mode="api-key"))
        write(root, "hotes/atelier.md", fiche(type="Host", title="atelier"))
        block = canon.blocking(self.check(root)[1])
        self.assertEqual(block.global_errors, [])
        self.assertTrue(block.reasons("ouvrier", ["banc"]))
        self.assertTrue(block.reasons("orchestre", ["atelier"]))     # hôte sans responsable
        self.assertEqual(block.reasons("relecteur", ["banc"]), [])

    def test_resolution_stricte(self):
        loaded = self.check(self.example_copy())[0]
        self.assertEqual(loaded.resolve_human("human:alice"), "human:alice")
        for valeur in (None, "", "alice", "agent:alice", "human:", "human:inconnu"):
            self.assertIsNone(loaded.resolve_human(valeur))


# ==========================================================================
# source approuvée : lecture par les objets git
# ==========================================================================

class GitSourceTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        self.main = git(self.clone, "rev-parse", "origin/main")

    def test_lecture_au_commit_canonique(self):
        loaded = canon.load(self.clone)
        findings = canon.validate(loaded)
        self.assertEqual(findings, [])
        self.assertEqual([s.mode for s in loaded.sources], ["git"])
        self.assertEqual(loaded.sources[0].commit, self.main)
        self.assertEqual(loaded.sources[0].rev, "origin/main")
        ref = loaded.agent("orchestre").fiche.ref
        self.assertEqual(ref, "canon:agents/orchestre.md@%s" % self.main)

    def test_modification_locale_ignoree(self):
        # retrait du responsable, sans commit ; et une fiche nouvelle non suivie
        write(self.clone, "agents/ouvrier.md", fiche(type="Agent", title="ouvrier"))
        write(self.clone, "agents/intrus.md", fiche(type="Agent", title="intrus",
                                                     responsible="human:alice"))
        loaded = canon.load(self.clone)
        findings = canon.validate(loaded)
        self.assertEqual(codes(findings, canon.ERROR), set())
        self.assertEqual(loaded.agent("ouvrier").responsible, "human:bruno")
        self.assertIsNone(loaded.agent("intrus"))
        self.assertIn("canon-worktree-dirty", codes(findings, canon.WARNING))

    def test_commit_local_non_pousse_ignore(self):
        write(self.clone, "agents/ouvrier.md", fiche(
            type="Agent", title="ouvrier", responsible="human:bruno", harness="deepseek",
            provider="deepseek", credential_mode="api-key", capabilities=["read", "approve"]))
        local = commit_all(self.clone, "élargit les droits", push=False)
        loaded = canon.load(self.clone)
        findings = canon.validate(loaded)
        self.assertEqual(loaded.sources[0].commit, self.main)
        self.assertNotEqual(local, self.main)
        self.assertEqual(codes(findings, canon.ERROR), set())
        self.assertIn("canon-local-divergence", codes(findings, canon.WARNING))
        # une fois fusionné sur la branche canonique, le changement est lu
        git(self.clone, "push", "-q", "origin", "HEAD")
        git(self.clone, "fetch", "-q", "origin")
        findings = canon.validate(canon.load(self.clone))
        self.assertIn("agent-approve-capability", codes(findings, canon.ERROR))

    def test_fetch(self):
        other = os.path.join(self.workspace, "autre")
        git(self.workspace, "clone", "-q", self.bare, other)
        write(other, "agents/ouvrier.md", None)
        write(other, "placements/ouvrier-banc.md", None)
        commit_all(other, "retire ouvrier")
        self.assertIsNotNone(canon.load(self.clone).agent("ouvrier"))
        self.assertIsNone(canon.load(self.clone, fetch=True).agent("ouvrier"))

    def test_revision_explicite_introuvable(self):
        loaded = canon.load(self.clone, ref="origin/inexistante")
        self.assertFalse(loaded.readable)
        self.assertIn("canon-ref-missing", codes(canon.validate(loaded), canon.ERROR))

    def test_branche_du_manifeste(self):
        write(self.clone, "federation.yaml",
              "federation: \"0.1\"\nid: acme\nroot: acme\nmembers:\n"
              "  - id: acme\n    ref: canon\n    bundle: .\n    workspace_path: acme\n")
        commit_all(self.clone, "manifeste")
        git(self.clone, "push", "-q", "origin", "HEAD:canon")
        write(self.clone, "agents/ouvrier.md", None)
        commit_all(self.clone, "main seulement")     # absent de la branche `canon`
        git(self.clone, "fetch", "-q", "origin")
        loaded = canon.load(self.clone)
        self.assertEqual(loaded.sources[0].rev, "origin/canon")
        self.assertIsNotNone(loaded.agent("ouvrier"))
        self.assertEqual(loaded.sources[0].member, "acme")

    def test_sous_dossier_du_depot(self):
        """Le bundle peut être un sous-dossier : seul ce préfixe est lu."""
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "mono")
        shutil.copytree(EXAMPLE, os.path.join(clone, "docs", "canon"))
        write(clone, "ailleurs/agent.md", fiche(type="Agent", title="hors-bundle"))
        commit_all(clone)
        loaded = canon.load(os.path.join(clone, "docs", "canon"))
        self.assertEqual(loaded.sources[0].prefix, "docs/canon")
        self.assertIsNone(loaded.agent("hors-bundle"))
        self.assertEqual(loaded.agent("ouvrier").fiche.path, "docs/canon/agents/ouvrier.md")
        # un bundle présent dans l'arbre de travail mais absent du commit canonique
        shutil.copytree(EXAMPLE, os.path.join(clone, "neuf"))
        findings = canon.validate(canon.load(os.path.join(clone, "neuf")))
        self.assertIn("canon-bundle-missing", codes(findings, canon.ERROR))


class FederationTest(_TmpMixin, unittest.TestCase):
    MANIFEST = (
        "federation: \"0.1\"\nid: acme-fed\nroot: home\nmembers:\n"
        "  - id: home\n    repository: https://example.invalid/home\n    ref: main\n"
        "    bundle: .\n    entrypoint: index.md\n    workspace_path: home\n"
        "    enforcement: required\n"
        "  - id: equipe\n    repository: __EQUIPE__\n    ref: main\n    bundle: knowledge\n"
        "    workspace_path: equipe\n"
        "  - id: absente\n    ref: main\n    bundle: .\n    workspace_path: absente\n"
        "roles:\n  project-maintainers:\n    resolver:\n      kind: literal\n"
        "      members: [human:alice]\n"
        "review_policies:\n  self_approval: forbidden\n"
    )

    def setUp(self) -> None:
        self.workspace = self.make_tmp()
        _b, self.home = publish(self.workspace, "home")
        for sub in ("membres", "hotes"):
            shutil.copytree(os.path.join(EXAMPLE, sub), os.path.join(self.home, sub))
        # le dépôt d'equipe est déclaré par son chemin, avec un `/` final toléré
        self.manifest = self.MANIFEST.replace(
            "__EQUIPE__", os.path.join(self.workspace, "remotes", "equipe.git/"))
        write(self.home, "federation.yaml", self.manifest)
        commit_all(self.home)
        _b, self.equipe = publish(self.workspace, "equipe")
        for sub in ("agents", "placements"):
            shutil.copytree(os.path.join(EXAMPLE, sub),
                            os.path.join(self.equipe, "knowledge", sub))
        commit_all(self.equipe)
        self.equipe_main = git(self.equipe, "rev-parse", "origin/main")

    def test_membres_presents_lus_a_leur_commit(self):
        loaded = canon.load(self.home)
        findings = canon.validate(loaded)
        self.assertEqual(codes(findings, canon.ERROR), set(), findings)
        self.assertEqual(codes(findings, canon.WARNING),
                         {"member-absent", "member-repository-mismatch"})
        # seul home déclare un dépôt (fictif) qui n'est pas son origin
        self.assertEqual([f.member for f in findings
                          if f.code == "member-repository-mismatch"], ["home"])
        self.assertEqual(sorted(s.member for s in loaded.sources), ["equipe", "home"])
        ref = loaded.agent("ouvrier").fiche.ref
        self.assertEqual(ref, "equipe:knowledge/agents/ouvrier.md@%s" % self.equipe_main)
        self.assertEqual(loaded.roles["project-maintainers"]["resolver"]["members"],
                         ["human:alice"])
        self.assertEqual(loaded.review_policies["self_approval"], "forbidden")

    def test_urls_de_depot_comparables(self):
        forms = ("git@github.com:acme/home.git", "https://github.com/acme/home",
                 "ssh://git@github.com/acme/home.git/", "HTTPS://GitHub.com/acme/home/")
        self.assertEqual(len({canon._norm_url(u) for u in forms}), 1)
        self.assertNotEqual(canon._norm_url("https://github.com/acme/home"),
                            canon._norm_url("https://github.com/autre/home"))

    def test_chemin_qui_sort_du_dossier_de_travail(self):
        write(self.home, "federation.yaml", self.manifest.replace(
            "workspace_path: equipe", "workspace_path: ../../etc"))
        commit_all(self.home)
        findings = canon.validate(canon.load(self.home))
        self.assertIn("member-path-escape", codes(findings, canon.ERROR))
        self.assertTrue(canon.blocking(findings).global_errors)

    def test_manifeste_illisible(self):
        write(self.home, "federation.yaml", "members: [x\n")
        commit_all(self.home)
        loaded = canon.load(self.home)
        self.assertIn("federation-invalid", codes(canon.validate(loaded), canon.ERROR))


# ==========================================================================
# sync, éphémères, réclamation (base réelle)
# ==========================================================================

class _CanonDbCase(_TmpMixin, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.root = self.example_copy()

    def load(self) -> canon.Canon:
        return canon.load(self.root, untrusted=True)

    def sync(self, host: str = "atelier", *, forge=None) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load(), host, forge=forge)

    def actions(self, report) -> dict[str, str]:
        return {a.agent: a.action for a in report.actions}

    def row(self, name: str) -> dict:
        return registry.get(self.db, name)


class SyncTest(_CanonDbCase):
    def test_cree_les_agents_places_sur_l_hote(self):
        report = self.sync()
        self.assertEqual(self.actions(report), {"orchestre": "créé", "relecteur": "créé"})
        self.assertIsNone(self.row("ouvrier"))           # placé sur le banc
        row = self.row("orchestre")
        self.assertEqual(row["host"], "atelier")
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["model"], "claude-opus")
        self.assertEqual(float(row["budget_usd"]), 30.0)
        self.assertEqual(row["responsible"], "human:alice")
        self.assertEqual(row["team"], "acme-web")
        self.assertEqual(row["provider"], "anthropic")
        self.assertEqual(row["credential_mode"], "subscription")
        self.assertEqual(row["capabilities"], ["read", "report-drift", "propose"])
        self.assertEqual(row["cwd"], os.path.expanduser("~/acme/acme-web"))
        self.assertTrue(row["canon_ref"].startswith("canon:agents/orchestre.md@untrusted:"))
        self.assertFalse(row["ephemeral"])
        self.assertEqual(row["status"], "idle")
        # le mode d'identifiants vient du placement
        self.assertEqual(self.row("relecteur")["credential_mode"], "api-key")
        self.assertEqual(self.actions(self.sync()),
                         {"orchestre": "inchangé", "relecteur": "inchangé"})

    def test_colonnes_d_etat_intactes(self):
        registry.upsert(self.db, "orchestre", harness="other", host="atelier", cwd="/ancien",
                        session_id="sess-42", model="vieux")
        registry.set_pending_prompt(self.db, "orchestre", "consigne en attente")
        lease = registry.claim(self.db, "orchestre", "runner-x", 600)
        self.db.execute("UPDATE agent_registry SET spent_usd = 3.5, turns = 7, "
                        "current_prompt = 'en cours', last_error = 'e', "
                        "status_text = 'occupé' WHERE name = 'orchestre'")
        avant = self.row("orchestre")
        report = self.sync()
        self.assertEqual(self.actions(report)["orchestre"], "mis à jour")
        apres = self.row("orchestre")
        for colonne in ("session_id", "status", "status_text", "spent_usd", "turns",
                        "pending_prompt", "current_prompt", "last_error", "lease_owner",
                        "lease_epoch", "lease_expires_ts", "chantier"):
            self.assertEqual(apres[colonne], avant[colonne], colonne)
        self.assertEqual(int(apres["lease_epoch"]), int(lease["lease_epoch"]))
        self.assertEqual((apres["harness"], apres["cwd"], apres["model"]),
                         ("claude", os.path.expanduser("~/acme/acme-web"), "claude-opus"))

    def test_retrait_arrete_puis_reintegration(self):
        self.sync()
        sauvegarde = {}
        for path in ("agents/orchestre.md", "placements/orchestre-atelier.md"):
            with open(os.path.join(self.root, path), encoding="utf-8") as fh:
                sauvegarde[path] = fh.read()
        for path in sauvegarde:
            write(self.root, path, None)
        report = self.sync()
        self.assertEqual(self.actions(report)["orchestre"], "arrêté")
        row = self.row("orchestre")
        self.assertEqual(row["status"], "stopped")
        self.assertTrue(row["status_text"].startswith(canon_sync.STOP_MARK))
        self.assertEqual(registry.claimable(self.db, "atelier", ["orchestre"],
                                            require_responsible=False), [])
        for path, text in sauvegarde.items():
            write(self.root, path, text)
        report = self.sync()
        self.assertIn(("orchestre", "réintégré"), [(a.agent, a.action) for a in report.actions])
        self.assertEqual(self.row("orchestre")["status"], "idle")

    def test_arret_manuel_jamais_leve_par_sync(self):
        self.sync()
        registry.set_status(self.db, "orchestre", "stopped", status_text="arrêté à la main")
        self.sync()
        self.assertEqual(self.row("orchestre")["status"], "stopped")

    def test_retrait_en_plein_tour_ne_tue_rien(self):
        self.sync()
        lease = registry.claim(self.db, "orchestre", "runner-x", 600)
        epoch = int(lease["lease_epoch"])
        self.assertTrue(registry.begin_turn(self.db, "orchestre", "runner-x", epoch, "tour"))
        write(self.root, "agents/orchestre.md", None)
        write(self.root, "placements/orchestre-atelier.md", None)
        report = self.sync()
        self.assertEqual(self.actions(report)["orchestre"], "arrêt demandé")
        row = self.row("orchestre")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_owner"], "runner-x")
        self.assertEqual(int(row["lease_epoch"]), epoch)
        self.assertTrue(row["status_text"].startswith(canon_sync.PENDING_MARK))
        # fin du tour (l'exécuteur, non modifié), puis la synchronisation suivante arrête
        self.assertTrue(registry.end_turn(self.db, "orchestre", "runner-x", epoch,
                                          status="idle", status_text="tour fini"))
        self.assertEqual(self.actions(self.sync())["orchestre"], "arrêté")
        self.assertEqual(self.row("orchestre")["status"], "stopped")

    def test_agent_inscrit_a_la_main_hors_canon(self):
        registry.upsert(self.db, "manuel", harness="codex", host="atelier")
        report = self.sync()
        self.assertEqual(self.actions(report)["manuel"], "hors canon")
        row = self.row("manuel")
        self.assertEqual(row["status"], "idle")
        self.assertIsNone(row["responsible"])

    def test_membre_non_lu_pas_de_retrait(self):
        registry.upsert(self.db, "lointain", harness="codex", host="atelier")
        self.db.execute("UPDATE agent_registry SET canon_ref = 'autre:agents/l.md@abc' "
                        "WHERE name = 'lointain'")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice' "
                        "WHERE name = 'lointain'")
        self.assertEqual(self.actions(self.sync())["lointain"], "laissé")
        row = self.row("lointain")
        self.assertEqual(row["status"], "idle")          # pas d'arrêt sur un retrait non constaté
        self.assertIsNone(row["responsible"])            # mais plus réclamable

    def test_erreur_bloquante_vide_le_responsable(self):
        self.sync()
        write(self.root, "agents/relecteur.md", fiche(
            type="Agent", title="relecteur", responsible="human:inconnu", harness="codex",
            provider="openai", credential_mode="api-key"))
        report = self.sync()
        blocked = {a.agent: a.blocked for a in report.actions}
        self.assertTrue(blocked["relecteur"])
        self.assertFalse(blocked["orchestre"])
        # une erreur propre à un agent ne ferme pas tout l'hôte : état ok, diagnostic
        self.assertEqual(report.status, "ok")
        self.assertIn("agent-responsible-unresolved", report.diagnostic)
        self.assertEqual(canon_sync.state(self.db, "atelier")["status"], "ok")
        self.assertIsNone(self.row("relecteur")["responsible"])
        self.assertEqual(self.row("orchestre")["responsible"], "human:alice")
        noms = [r["name"] for r in registry.claimable(self.db, "atelier",
                                                      require_responsible=True)]
        self.assertEqual(noms, ["orchestre"])

    def test_fiche_avec_approve_bloquee_sans_ecrire_approve(self):
        write(self.root, "agents/orchestre.md", fiche(
            type="Agent", title="orchestre", responsible="human:alice", harness="claude",
            provider="anthropic", credential_mode="subscription",
            capabilities=["read", "approve"], budget_usd_per_day="1e12"))
        report = self.sync()
        self.assertTrue({a.agent: a.blocked for a in report.actions}["orchestre"])
        row = self.row("orchestre")
        self.assertEqual(row["capabilities"], ["read"])
        self.assertIsNone(row["responsible"])
        self.assertIsNone(row["budget_usd"])
        self.assertIn("agent-budget-invalid", codes(report.findings, canon.WARNING))

    def test_erreur_globale_bloque_tout_et_differe_les_retraits(self):
        self.sync()
        write(self.root, "agents/casse.md", "---\ntype: Agent\ntitle: [x\n---\n")
        write(self.root, "agents/orchestre.md", None)
        write(self.root, "placements/orchestre-atelier.md", None)
        report = self.sync()
        self.assertEqual(self.actions(report)["orchestre"], "laissé")
        self.assertEqual(self.row("orchestre")["status"], "idle")
        self.assertIsNone(self.row("relecteur")["responsible"])
        self.assertEqual(registry.claimable(self.db, "atelier", require_responsible=True), [])

    def test_deplacement_vers_un_autre_hote(self):
        self.sync()
        write(self.root, "placements/relecteur-atelier.md", fiche(
            type="Placement", agent="relecteur", host="banc", credential_mode="api-key",
            cwd="/srv/acme/relecture"))
        self.assertEqual(self.actions(self.sync())["relecteur"], "déplacé")
        row = self.row("relecteur")
        self.assertEqual(row["host"], "banc")
        self.assertIsNone(row["responsible"])        # pas avant la synchronisation du banc
        self.sync("banc")
        row = self.row("relecteur")
        self.assertEqual((row["host"], row["responsible"], row["cwd"]),
                         ("banc", "human:alice", "/srv/acme/acme-web"))

    def test_canon_illisible_registre_inchange(self):
        registry.upsert(self.db, "manuel", harness="codex", host="atelier")
        with self.assertRaises(canon.CanonError):
            canon_sync.sync(self.db, canon.load(self.root), "atelier")   # hors git, refusé
        self.assertEqual([r["name"] for r in registry.overview(self.db)], ["manuel"])
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual(state["status"], "unreadable")
        self.assertIn("canon-untrusted-refused", state["diagnostic"])

    def test_sync_depuis_git_enregistre_le_commit(self):
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "acme", EXAMPLE)
        main = git(clone, "rev-parse", "origin/main")
        canon_sync.sync(self.db, canon.load(clone), "atelier")
        self.assertEqual(self.row("orchestre")["canon_ref"],
                         "canon:agents/orchestre.md@%s" % main)

    def test_contrainte_approve_en_base(self):
        registry.upsert(self.db, "x", host="atelier")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE agent_registry SET capabilities = ARRAY['read', ' Approve'] "
                            "WHERE name = 'x'")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE agent_registry SET ephemeral = true WHERE name = 'x'")


class CanonStateTest(_CanonDbCase):
    """§4.1, fail closed : un canon illisible ou invalide ferme toute NOUVELLE
    réclamation des agents qu'il gouverne (et de leurs éphémères), sans toucher
    aux baux, sessions ni tours en cours ; les agents inscrits à la main ne
    sont pas concernés."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        self.main = git(self.clone, "rev-parse", "origin/main")

    def git_sync(self, **kwargs) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, canon.load(self.clone, **kwargs), "atelier")

    def break_canon(self) -> canon_sync.CanonUnreadable:
        with self.assertRaises(canon_sync.CanonUnreadable) as ctx:
            self.git_sync()
        return ctx.exception

    def claimable(self, **kwargs) -> list[str]:
        return sorted(r["name"] for r in registry.claimable(self.db, "atelier", **kwargs))

    def snapshot(self) -> list[dict]:
        return self.db.query("SELECT * FROM agent_registry ORDER BY name")

    def manual(self, name: str = "manuel") -> None:
        registry.upsert(self.db, name, harness="codex", host="atelier")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice' "
                        "WHERE name = %s", (name,))

    def test_etat_ok_enregistre(self):
        report = self.git_sync()
        self.assertEqual((report.status, report.diagnostic), ("ok", ""))
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual((state["status"], state["last_good_commit"], state["diagnostic"]),
                         ("ok", self.main, ""))
        self.assertIn(self.main[:12], state["source"])
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE canon_state SET status = 'peut-être'")

    def test_racine_supprimee_bail_et_session_intacts(self):
        self.git_sync()
        registry.set_session(self.db, "orchestre", "sess-orchestre")
        lease = registry.claim(self.db, "relecteur", "runner-x", 600)
        epoch = int(lease["lease_epoch"])
        registry.set_session(self.db, "relecteur", "sess-relecteur")
        self.assertTrue(registry.begin_turn(self.db, "relecteur", "runner-x", epoch, "tour"))
        avant = self.snapshot()
        shutil.rmtree(self.clone)
        exc = self.break_canon()
        self.assertIsInstance(exc, canon.CanonError)
        self.assertIn("canon-missing", exc.diagnostic)
        # seule écriture : l'état du canon (dernier commit valide conservé)
        self.assertEqual(self.snapshot(), avant)
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual((state["status"], state["last_good_commit"]), ("unreadable", self.main))
        self.assertIn("canon-missing", state["diagnostic"])
        # plus aucune nouvelle réclamation, quel que soit le réglage R14…
        self.assertEqual(self.claimable(require_responsible=True), [])
        self.assertEqual(self.claimable(require_responsible=False), [])
        self.assertIsNone(registry.claim(self.db, "orchestre", "runner-y", 600))
        # … mais rien n'est arrêté : le bail, la session et le tour continuent
        row = self.row("relecteur")
        self.assertEqual((row["lease_owner"], int(row["lease_epoch"]), row["status"],
                          row["session_id"]), ("runner-x", epoch, "running", "sess-relecteur"))
        self.assertEqual(registry.lease_matches(self.db, "relecteur", "runner-x", epoch),
                         (True, ""))
        self.assertIsNotNone(registry.renew(self.db, "relecteur", "runner-x", epoch, 600))
        self.assertTrue(registry.end_turn(self.db, "relecteur", "runner-x", epoch,
                                          status="idle", status_text="tour fini"))
        self.assertTrue(registry.release(self.db, "relecteur", "runner-x", epoch))
        row = self.row("orchestre")
        self.assertEqual((row["session_id"], row["responsible"], row["status"]),
                         ("sess-orchestre", "human:alice", "idle"))
        self.assertEqual(self.claimable(require_responsible=True), [])
        # canon réparé, puis sync : de nouveau réclamables
        git(self.workspace, "clone", "-q", self.bare, self.clone)
        self.assertEqual(self.git_sync().status, "ok")
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])
        self.assertIsNotNone(registry.claim(self.db, "orchestre", "runner-y", 600))
        self.assertEqual(self.row("orchestre")["session_id"], "sess-orchestre")

    @unittest.skipIf(os.geteuid() == 0, "root ignore les permissions")
    def test_permission_refusee_ferme_la_reclamation(self):
        # revue codex2 (B1 bis) : une PermissionError réelle sur la racine du
        # canon doit fermer la réclamation comme une racine absente.
        self.git_sync()
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])
        mode = os.stat(self.clone).st_mode
        os.chmod(self.clone, 0)
        try:
            exc = self.break_canon()
        finally:
            os.chmod(self.clone, mode)
        self.assertIsInstance(exc, canon.CanonError)
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual(state["status"], "unreadable")
        self.assertEqual(self.claimable(require_responsible=True), [])
        self.assertEqual(self.claimable(require_responsible=False), [])
        self.assertIsNone(registry.claim(self.db, "orchestre", "runner-y", 600))
        # permissions rétablies + sync : de nouveau réclamables
        self.assertEqual(self.git_sync().status, "ok")
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])

    def test_revision_introuvable(self):
        self.git_sync()
        with self.assertRaises(canon_sync.CanonUnreadable):
            self.git_sync(ref="origin/inexistante")
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual(state["status"], "unreadable")
        self.assertIn("canon-ref-missing", state["diagnostic"])
        self.assertEqual(self.claimable(require_responsible=False), [])
        self.git_sync()
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])

    def test_depot_casse(self):
        self.git_sync()
        shutil.rmtree(os.path.join(self.clone, ".git"))
        exc = self.break_canon()
        self.assertIn("canon-untrusted-refused", exc.diagnostic)
        self.assertEqual(self.claimable(require_responsible=False), [])

    def test_agent_manuel_non_affecte(self):
        self.manual()
        self.assertEqual(self.claimable(require_responsible=True), ["manuel"])   # aucun état
        self.git_sync()
        shutil.rmtree(self.clone)
        self.break_canon()
        self.assertEqual(self.claimable(require_responsible=True), ["manuel"])
        self.assertEqual(self.claimable(require_responsible=False), ["manuel"])
        self.assertIsNotNone(registry.claim(self.db, "manuel", "runner-y", 600))

    def test_ephemeres_d_un_createur_canonique_bloques(self):
        self.git_sync()
        self.manual()
        canon_sync.spawn(self.db, "aide", "orchestre", 3600)
        canon_sync.spawn(self.db, "petit", "aide", 3600)        # petit → aide → orchestre
        canon_sync.spawn(self.db, "aidem", "manuel", 3600)
        self.assertEqual(self.claimable(require_responsible=True),
                         ["aide", "aidem", "manuel", "orchestre", "petit", "relecteur"])
        shutil.rmtree(self.clone)
        self.break_canon()
        self.assertEqual(self.claimable(require_responsible=True), ["aidem", "manuel"])
        self.assertIsNone(registry.claim(self.db, "petit", "runner-y", 600))
        overview = {r["name"]: r for r in registry.overview(self.db)}
        self.assertEqual({n for n, r in overview.items() if r["canon_governed"]},
                         {"aide", "petit", "orchestre", "relecteur"})
        self.assertEqual({n for n, r in overview.items() if r["canon_claim_ok"]},
                         {"aidem", "manuel"})
        self.assertEqual({r["canon_status"] for r in overview.values()}, {"unreadable"})
        # lignée rompue (créateur disparu) : tenu pour gouverné
        self.db.execute("UPDATE agent_registry SET created_by = 'disparu' WHERE name = 'aidem'")
        self.assertEqual(self.claimable(require_responsible=True), ["manuel"])

    def test_canon_invalide_bloque_aussi_les_ephemeres(self):
        self.git_sync()
        canon_sync.spawn(self.db, "aide", "orchestre", 3600)
        write(self.clone, "agents/casse.md", "---\ntype: Agent\ntitle: [x\n---\n")
        head = commit_all(self.clone, "fiche cassée")
        report = self.git_sync()
        self.assertEqual(report.status, "invalid")
        self.assertIn("frontmatter-invalid", report.diagnostic)
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual((state["status"], state["last_good_commit"]), ("invalid", self.main))
        # l'éphémère garde son responsable copié, mais n'est plus réclamable
        self.assertEqual(self.row("aide")["responsible"], "human:alice")
        self.assertEqual(self.claimable(require_responsible=False), [])
        write(self.clone, "agents/casse.md", None)
        repaired = commit_all(self.clone, "réparation")
        self.assertNotEqual(repaired, head)
        self.assertEqual(self.git_sync().status, "ok")
        self.assertEqual(canon_sync.state(self.db, "atelier")["last_good_commit"], repaired)
        self.assertEqual(self.claimable(require_responsible=True),
                         ["aide", "orchestre", "relecteur"])

    def test_sans_etat_pas_de_reclamation(self):
        self.git_sync()
        self.db.execute("DELETE FROM canon_state")
        self.assertEqual(self.claimable(require_responsible=False), [])
        self.assertIsNone(registry.claim(self.db, "orchestre", "runner-y", 600))

    def test_etat_par_hote(self):
        self.git_sync()
        canon_sync.sync(self.db, canon.load(self.clone), "banc")
        canon_sync.record_state(self.db, "banc", canon_sync.CANON_UNREADABLE, "panne")
        self.assertEqual(self.claimable(require_responsible=True), ["orchestre", "relecteur"])
        self.assertEqual(registry.claimable(self.db, "banc", require_responsible=False), [])


class SpawnTest(_CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync()

    def test_responsable_herite_et_capacites_limitees(self):
        row = canon_sync.spawn(self.db, "aide1", "orchestre", 7200)
        self.assertEqual(row["responsible"], "human:alice")
        self.assertEqual(row["created_by"], "orchestre")
        self.assertEqual(row["capabilities"], ["read", "propose"])
        self.assertAlmostEqual(row["ephemeral_expires_ts"], time.time() + 7200, delta=120)
        full = self.row("aide1")
        self.assertTrue(full["ephemeral"])
        self.assertEqual((full["host"], full["harness"], full["team"]),
                         ("atelier", "claude", "acme-web"))
        # relecteur n'a pas `propose` : l'enfant n'en reçoit pas davantage
        row = canon_sync.spawn(self.db, "aide2", "relecteur", 600)
        self.assertEqual(row["capabilities"], ["read"])
        # le responsable est copié : il ne suit pas le créateur ensuite
        self.db.execute("UPDATE agent_registry SET responsible = NULL WHERE name = 'orchestre'")
        self.assertEqual(self.row("aide1")["responsible"], "human:alice")
        self.assertEqual([r["name"] for r in registry.claimable(
            self.db, "atelier", ["aide1"], require_responsible=True)], ["aide1"])

    def test_echeance_bornee_par_le_createur_ephemere(self):
        parent = canon_sync.spawn(self.db, "parent", "orchestre", 600)
        child = canon_sync.spawn(self.db, "enfant", "parent", 7200)
        self.assertAlmostEqual(child["ephemeral_expires_ts"], parent["ephemeral_expires_ts"],
                               delta=1)

    def test_refus(self):
        registry.upsert(self.db, "sansresp", harness="codex", host="atelier")
        cas = [
            ("aide", "inconnu", 600, "inconnu"),
            ("aide", "sansresp", 600, "responsable"),
            ("orchestre", "relecteur", 600, "fiche dans le canon"),
            ("sansresp", "orchestre", 600, "existe déjà"),
            ("orchestre", "orchestre", 600, "lui-même"),
            ("nom invalide", "orchestre", 600, "invalide"),
        ]
        loaded = self.load()
        for name, creator, ttl, attendu in cas:
            with self.subTest(name=name, creator=creator):
                with self.assertRaises(canon_sync.SpawnError) as ctx:
                    canon_sync.spawn(self.db, name, creator, ttl, canon=loaded)
                self.assertIn(attendu, str(ctx.exception))
        canon_sync.spawn(self.db, "vieux", "orchestre", 600)
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at = now() - interval '1 s' "
                        "WHERE name = 'vieux'")
        with self.assertRaises(canon_sync.SpawnError):
            canon_sync.spawn(self.db, "petit", "vieux", 600)
        registry.set_status(self.db, "relecteur", "stopped")
        with self.assertRaises(canon_sync.SpawnError):
            canon_sync.spawn(self.db, "aide3", "relecteur", 600)

    def test_echeance_obligatoire_et_bornee(self):
        for texte in (None, "", "demain", "0", "8d"):
            with self.subTest(ttl=texte):
                with self.assertRaises(canon_sync.SpawnError):
                    canon_sync.parse_ttl(texte)
        self.assertEqual(canon_sync.parse_ttl("2h"), 7200)
        self.assertEqual(canon_sync.parse_ttl("90"), 90)

    def test_ephemere_echu_jamais_reclamable(self):
        canon_sync.spawn(self.db, "court", "orchestre", 600)
        self.assertTrue(registry.claimable(self.db, "atelier", ["court"],
                                           require_responsible=False))
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at = now() - interval '1 s' "
                        "WHERE name = 'court'")
        self.assertEqual(registry.claimable(self.db, "atelier", ["court"],
                                            require_responsible=False), [])

    def test_sync_ignore_les_ephemeres(self):
        canon_sync.spawn(self.db, "aide1", "orchestre", 600)
        report = self.sync()
        self.assertNotIn("aide1", self.actions(report))
        self.assertEqual(self.row("aide1")["status"], "idle")


class ClaimRuleTest(_TmpMixin, PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")

    def test_regle_explicite(self):
        registry.upsert(self.db, "avec", harness="claude", host="h1")
        registry.upsert(self.db, "sans", harness="claude", host="h1")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice' "
                        "WHERE name = 'avec'")
        noms = lambda **kw: sorted(r["name"] for r in registry.claimable(self.db, "h1", **kw))  # noqa
        self.assertEqual(noms(require_responsible=True), ["avec"])
        self.assertEqual(noms(require_responsible=False), ["avec", "sans"])
        self.assertEqual(noms(), ["avec", "sans"])       # banc sans canon : compatibilité
        strict = db_mod.connect(dataclasses.replace(self.cfg, require_responsible=True))
        try:
            self.assertEqual([r["name"] for r in registry.claimable(strict, "h1")], ["avec"])
        finally:
            strict.close()
        overview = {r["name"]: r for r in registry.overview(self.db)}
        self.assertTrue(overview["avec"]["responsible_ok"])
        self.assertFalse(overview["sans"]["responsible_ok"])

    def test_defaut_suit_le_canon(self):
        absent = os.path.join(self.make_tmp(), "absent.json")
        base = {"AMEESH_CONFIG": absent, "AGENT_MESH_CONFIG": absent}
        load = lambda **kw: config_mod.load(env={**base, **kw})  # noqa: E731
        self.assertFalse(load().responsible_required)
        self.assertTrue(load(AMEESH_CANON="/x").responsible_required)
        self.assertFalse(load(AMEESH_CANON="/x",
                              AMEESH_REQUIRE_RESPONSIBLE="0").responsible_required)
        self.assertTrue(load(AMEESH_REQUIRE_RESPONSIBLE="oui").responsible_required)
        cfg = load(AMEESH_CANON="~/c", AMEESH_CANON_REF="origin/canon",
                   AMEESH_CANON_UNTRUSTED="1")
        self.assertEqual((cfg.canon, cfg.canon_ref, cfg.canon_untrusted),
                         (os.path.expanduser("~/c"), "origin/canon", True))
        for bad in ({"AMEESH_REQUIRE_RESPONSIBLE": "peut-être"},
                    {"AMEESH_CANON_REF": "--upload-pack=x"}):
            with self.assertRaises(SystemExit):
                load(**bad)

    def test_l_executeur_respecte_la_regle(self):
        """De bout en bout, sans toucher à runner.py : claimable filtre."""
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        env = self.env(AMEESH_HOST="h-r14", AMEESH_REQUIRE_RESPONSIBLE="1")
        proc = self.runner("register", "sansresp", "claude", "--cwd", cwd,
                           "--prompt", "tour", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.runner("--once", "--agents", "sansresp", env=env)
        self.assertEqual(self.turns(), [])
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice' "
                        "WHERE name = 'sansresp'")
        proc = self.runner("--once", "--agents", "sansresp", env=env)
        self.assertEqual([t["harness"] for t in self.turns()], ["claude"], proc.stdout)

    def test_l_executeur_respecte_l_etat_du_canon(self):
        """§4.1 de bout en bout, sans toucher à runner.py : sans état `ok` du canon
        de son hôte, un agent du canon n'est pas réclamé."""
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        env = self.env(AMEESH_HOST="h-canon")
        proc = self.runner("register", "canonique", "claude", "--cwd", cwd,
                           "--prompt", "tour", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # placement admis pour son profil, comme l'écrirait `canon sync` (C4, L3) :
        # seul l'état du canon est en jeu ici (test_placement couvre le placement)
        self.db.execute("UPDATE agent_registry SET canon_ref = 'canon:agents/c.md@abc', "
                        "responsible = 'human:alice', placement_ok = true, "
                        "placement_profile = ameesh_placement_profile("
                        "host, harness, provider, model, credential_mode) "
                        "WHERE name = 'canonique'")
        self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual(self.turns(), [])                    # aucun état : jamais vérifié
        canon_sync.record_state(self.db, "h-canon", canon_sync.CANON_UNREADABLE,
                                "canon-missing (canon) : racine du canon introuvable")
        self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual(self.turns(), [])
        canon_sync.record_state(self.db, "h-canon", canon_sync.CANON_OK, "")
        proc = self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual([t["harness"] for t in self.turns()], ["claude"], proc.stdout)


class ReviewPolicyTest(_TmpMixin, unittest.TestCase):
    """`canon check` valide `review_policies.classes` (L10, R19)."""

    def canon_dir(self, manifest: str) -> str:
        root = self.make_tmp()
        write(root, "federation.yaml", manifest)
        return root

    def test_politique_valide_sans_constat(self):
        root = self.canon_dir(
            "id: test\nroot: test\nmembers: []\nreview_policies:\n"
            "  self_approval: forbidden\n  risk_classes:\n    default: normal\n"
            "    rules:\n      - paths: ['**/*.sql']\n        class: sensitive\n")
        canon_obj = canon.load(root, untrusted=True)
        self.assertTrue(canon_obj.readable)
        self.assertEqual([f.code for f in canon.validate(canon_obj)
                          if f.code.startswith("review-policies")], [])
        self.assertEqual(canon_obj.review_policies["self_approval"], "forbidden")

    def test_defaut_manquant_avertit_sans_invalider(self):
        root = self.canon_dir(
            "id: test\nroot: test\nmembers: []\nreview_policies:\n"
            "  risk_classes:\n    rules:\n"
            "      - paths: ['src/**']\n        class: normal\n")
        findings = [f for f in canon.validate(canon.load(root, untrusted=True))
                    if f.code.startswith("review-policies")]
        self.assertEqual([f.code for f in findings], ["review-policies-default-missing"])
        self.assertEqual(findings[0].severity, canon.WARNING)
        self.assertEqual(findings[0].path, "federation.yaml")

    def test_politique_invalide_rapportee(self):
        root = self.canon_dir(
            "id: test\nroot: test\nmembers: []\nreview_policies:\n"
            "  risk_classes:\n    default: urgent\n    rules:\n"
            "      - paths: []\n        class: normal\n")
        canon_obj = canon.load(root, untrusted=True)
        found = codes(canon.validate(canon_obj))
        self.assertIn("review-policies-class-unknown", found)
        self.assertIn("review-policies-rule-invalid", found)

    def test_chemins_changes_depuis_une_reference(self):
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "home")
        write(clone, "a.txt", "un\n")
        commit_all(clone)
        ref = git(clone, "rev-parse", "HEAD")
        write(clone, "b.sql", "deux\n")          # non suivi
        write(clone, "a.txt", "un bis\n")        # modifié
        self.assertEqual(canon.git_changed_paths(clone, ref), ["a.txt", "b.sql"])
        self.assertEqual(canon.git_changed_paths(clone), ["a.txt", "b.sql"])

    def test_reference_inconnue_refusee_au_lieu_d_un_diff_vide(self):
        """Une erreur git ne vaut jamais une liste vide (revue codex2).

        Sans cela, `review-class --diff <ref inconnue>` rendait un succès sur
        les seuls fichiers non suivis : une classe partielle, donc rassurante.
        """
        workspace = self.make_tmp()
        _bare, clone = publish(workspace, "home")
        with self.assertRaises(canon.GitError):
            canon.git_changed_paths(clone, "ref-qui-n-existe-pas")
        # une référence valide sans ancêtre commun reste un diff légitime :
        # le repli sur la référence elle-même, pas une erreur.
        git(clone, "checkout", "-q", "--orphan", "autre")
        write(clone, "c.txt", "trois\n")
        commit_all(clone, "orphelin", push=False)
        self.assertIn("c.txt", canon.git_changed_paths(clone, "main"))


class CanonCliTest(_CanonDbCase):
    def cli_env(self, **extra) -> dict:
        return self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1", **extra)

    def test_check(self):
        proc = self.mesh("canon", "check", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("canon valide", proc.stdout)
        self.assertIn("[NON APPROUVÉ]", proc.stdout)
        write(self.root, "agents/ouvrier.md", fiche(type="Agent", title="ouvrier"))
        proc = self.mesh("canon", "check", "--json", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertTrue(data["untrusted"])
        trouve = [f for f in data["findings"] if f["code"] == "agent-responsible-missing"]
        self.assertEqual(trouve[0]["severity"], "error")
        self.assertEqual(trouve[0]["path"], "agents/ouvrier.md")
        self.assertTrue(trouve[0]["message"])
        # sans le drapeau, un dossier hors git est refusé
        proc = self.mesh("canon", "check", env=self.env(AMEESH_CANON=self.root))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("canon-untrusted-refused", proc.stdout)

    def test_review_class(self):
        write(self.root, "federation.yaml",
              "id: test\nroot: test\nmembers: []\nreview_policies:\n"
              "  risk_classes:\n    default: normal\n    rules:\n"
              "      - paths: ['**/*.sql']\n        class: sensitive\n"
              "      - paths: ['docs/**']\n        class: light\n")
        env = self.cli_env()
        proc = self.mesh("review-class", "db/x.sql", "docs/a.md", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sensible", proc.stdout)
        self.assertIn("léger", proc.stdout)
        proc = self.mesh("review-class", "--json", "docs/a.md", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["class"], "light")
        self.assertIn("policy_ref", data)      # le commit du canon lu (vide ici : non approuvé)
        # ni fichier ni --diff : usage
        proc = self.mesh("review-class", env=env)
        self.assertEqual(proc.returncode, 2)
        # chemin hors dépôt : refusé
        proc = self.mesh("review-class", "../secret.txt", env=env)
        self.assertEqual(proc.returncode, 1)
        # --diff REF : dépôt git requis…
        proc = self.mesh("review-class", "--diff", "HEAD", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("dépôt git", proc.stderr)
        # …puis les fichiers changés sont classés
        git(self.tmp, "init", "-q")
        write(self.tmp, "db/x.sql", "select 0;\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-q", "-m", "base")
        write(self.tmp, "db/x.sql", "select 1;\n")     # modifié
        write(self.tmp, "docs/a.md", "note\n")         # non suivi
        proc = self.mesh("review-class", "--diff", "HEAD", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sensible", proc.stdout)
        self.assertIn("docs/a.md", proc.stdout)
        # une déclaration invalide refuse de classer
        write(self.root, "federation.yaml",
              "id: test\nroot: test\nmembers: []\nreview_policies:\n"
              "  risk_classes:\n    default: urgent\n")
        proc = self.mesh("review-class", "docs/a.md", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("review-policies-class-unknown", proc.stderr)

    def test_review_class_chemin_avec_saut_de_ligne(self):
        """De bout en bout : un dossier au nom contenant un LF reste sensible."""
        write(self.root, "federation.yaml",
              "id: test\nroot: test\nmembers: []\nreview_policies:\n"
              "  risk_classes:\n    default: light\n    rules:\n"
              "      - paths: ['**/*.sql']\n        class: sensitive\n")
        env = self.cli_env()
        git(self.tmp, "init", "-q")
        write(self.tmp, "db/x.sql", "select 0;\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-q", "-m", "base")
        write(self.tmp, "db/line\nbreak/new.sql", "select 1;\n")   # non suivi
        proc = self.mesh("review-class", "--diff", "HEAD", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sensible", proc.stdout)
        self.assertIn("new.sql", proc.stdout)

    def test_review_class_diff_reference_inconnue(self):
        """`--diff` sur une référence inconnue : erreur, jamais un succès partiel."""
        write(self.root, "federation.yaml",
              "id: test\nroot: test\nmembers: []\nreview_policies:\n"
              "  risk_classes:\n    default: light\n    rules:\n"
              "      - paths: ['**/*.sql']\n        class: sensitive\n")
        env = self.cli_env()
        git(self.tmp, "init", "-q")
        write(self.tmp, "db/x.sql", "select 0;\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-q", "-m", "base")
        write(self.tmp, "note.md", "non suivi\n")
        proc = self.mesh("review-class", "--diff", "ref-qui-n-existe-pas", env=env)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("référence", proc.stderr)
        self.assertNotIn("classe :", proc.stdout)   # aucune classe partielle

    def test_sans_canon_configure(self):
        proc = self.mesh("canon", "check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("AMEESH_CANON", proc.stderr)

    def test_show(self):
        proc = self.mesh("canon", "show", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("orchestre", proc.stdout)
        self.assertIn("human:alice", proc.stdout)
        data = json.loads(self.mesh("canon", "show", "--json", env=self.cli_env()).stdout)
        self.assertEqual(sorted(a["title"] for a in data["agents"]),
                         ["orchestre", "ouvrier", "relecteur"])
        self.assertEqual(data["sources"][0]["mode"], "untrusted")

    def test_sync_puis_show_et_list(self):
        proc = self.mesh("canon", "sync", "--host", "banc", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ouvrier", proc.stdout)
        self.assertIn("créé", proc.stdout)
        proc = self.mesh("show", "ouvrier")
        self.assertIn("responsable : human:bruno", proc.stdout)
        self.assertIn("canon    : canon:agents/ouvrier.md@", proc.stdout)
        rows = {r["name"]: r for r in json.loads(self.mesh("list", "--json").stdout)}
        self.assertTrue(rows["ouvrier"]["responsible_ok"])
        data = json.loads(self.mesh("canon", "sync", "--host", "banc", "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual(data["actions"][0]["action"], "inchangé")
        self.assertEqual(data["canon_status"], "ok")

    def test_canon_illisible_diagnostic(self):
        proc = self.mesh("canon", "sync", "--host", "atelier", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("état du canon pour atelier : ok", proc.stdout)
        registry.upsert(self.db, "manuel", harness="codex", host="atelier")
        shutil.rmtree(self.root)
        proc = self.mesh("canon", "sync", "--host", "atelier", env=self.cli_env())
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        for attendu in ("ILLISIBLE", "registre inchangé", "canon-missing",
                        "état du canon pour atelier : unreadable", "NON réclamables"):
            self.assertIn(attendu, proc.stdout)
        data = json.loads(self.mesh("canon", "sync", "--host", "atelier", "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual((data["canon_status"], data["actions"]), ("unreadable", []))
        self.assertIn("canon-missing", data["diagnostic"])
        proc = self.mesh("canon", "check", "--host", "atelier", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("état du canon pour atelier : unreadable", proc.stdout)
        self.assertIn("registre  : unreadable pour atelier", proc.stdout)
        data = json.loads(self.mesh("canon", "check", "--host", "atelier", "--json",
                                    env=self.cli_env()).stdout)
        self.assertEqual((data["canon_status"], data["registry_state"]["status"]),
                         ("unreadable", "unreadable"))
        rows = {r["name"]: r for r in json.loads(self.mesh("list", "--json").stdout)}
        orchestre = rows["orchestre"]
        self.assertEqual((orchestre["canon_governed"], orchestre["canon_status"],
                          orchestre["canon_claim_ok"]), (True, "unreadable", False))
        self.assertIn("canon-missing", orchestre["canon_diagnostic"])
        self.assertTrue(orchestre["responsible_ok"])          # rien d'autre n'a changé
        self.assertEqual((rows["manuel"]["canon_governed"], rows["manuel"]["canon_claim_ok"]),
                         (False, True))

    def test_spawn(self):
        self.sync()
        proc = self.mesh("agent", "spawn", "aide", "--by", "orchestre", "--ttl", "1h",
                         "--prompt", "premier tour")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("responsable human:alice", proc.stdout)
        self.assertEqual(self.row("aide")["pending_prompt"], "premier tour")
        proc = self.mesh("agent", "spawn", "aide2", "--by", "orchestre")
        self.assertEqual(proc.returncode, 2)                    # --ttl obligatoire
        # une session d'agent ne crée qu'en son propre nom
        proc = self.mesh("agent", "spawn", "aide3", "--by", "orchestre", "--ttl", "1h",
                         env=self.env(AGENT_MAIL_NAME="relecteur"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refus", proc.stderr)
        self.assertIsNone(self.row("aide3"))
        proc = self.mesh("agent", "spawn", "aide3", "--by", "relecteur", "--ttl", "1h",
                         env=self.env(AGENT_MAIL_NAME="relecteur"))
        self.assertEqual(proc.returncode, 0, proc.stderr)



# ==========================================================================
# canon sync → registre des authentificateurs (§8.2, L6)
# ==========================================================================

def member_fiche(title: str, entries=None, *, raw: str | None = None) -> str:
    """Fiche Member ; `entries` : liste d'entrées (JSON en flux YAML), None : clé absente."""
    lines = ["---", "type: Member", "title: %s" % title, "roles: [reviewer]"]
    if raw is not None:
        lines.append("authenticators: %s" % raw)
    elif entries is not None:
        lines.append("authenticators: %s" % json.dumps(entries))
    lines += ["---", "", "# %s" % title, ""]
    return "\n".join(lines)


class _AuthenticatorCase(_TmpMixin, PgTestCase):
    """Un canon dans un dépôt (le dépôt nu local tient lieu de branche
    canonique distante), un registre des authentificateurs vide."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE authenticators, standing_approvals, standing_reservations, "
                        "authenticator_syncs RESTART IDENTITY CASCADE")
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        self.alice = SoftWebAuthn()
        self.alice2 = SoftWebAuthn()
        self.bruno = SoftWebAuthn()

    def commit_members(self, clone: str | None = None, *, message: str = "membres",
                       push: bool = True, **fiches) -> str:
        clone = clone or self.clone
        for title, text in fiches.items():
            write(clone, "membres/%s.md" % title, text)
        return commit_all(clone, message, push=push)

    def sync(self, clone: str | None = None, *, bootstrap_ref: str = "main",
             trusted_ref: str = "", **kwargs) -> canon_sync.SyncReport:
        """`canon sync` ; le premier amorçage du registre se fait sur main
        (--bootstrap-ref, ignoré une fois le registre amorcé)."""
        return canon_sync.sync(self.db, canon.load(clone or self.clone, **kwargs), "atelier",
                               bootstrap_ref=bootstrap_ref, trusted_ref=trusted_ref)

    def rows(self, include_revoked: bool = False) -> list[dict]:
        return receipts.list_authenticators(self.db, include_revoked=include_revoked)

    def live(self) -> dict:
        return {(r["approver"], r["credential_id"]): r["canon_ref"] for r in self.rows()}

    def journal(self) -> list[dict]:
        """Le journal des synchronisations appliquées (dernier commit appliqué en fin)."""
        return self.db.query("SELECT id, root_member, root_commit, branch, trust, host "
                             "FROM authenticator_syncs ORDER BY id")


class AuthenticatorSyncTest(_AuthenticatorCase):
    """`canon sync` remplit le registre de confiance depuis Member.authenticators,
    à la révision canonique seulement, avec les règles de L6."""

    def test_ajout_mise_a_jour_et_retrait_par_le_canon(self):
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]),
                                    bruno=member_fiche("bruno", [self.bruno.entry()]))
        report = self.sync()
        done = report.authenticators
        self.assertEqual((done.status, done.errors, done.partial), ("synced", [], []))
        self.assertEqual(sorted(done.result["added"]), sorted([
            "human:alice/webauthn/%s" % self.alice.credential_id,
            "human:bruno/webauthn/%s" % self.bruno.credential_id]))
        self.assertEqual(self.live(), {
            ("human:alice", self.alice.credential_id): "canon:membres/alice.md@%s" % first,
            ("human:bruno", self.bruno.credential_id): "canon:membres/bruno.md@%s" % first})
        self.assertEqual(report.to_dict()["authenticators"]["status"], "synced")
        # même commit : rien ne change
        self.assertEqual(self.sync().authenticators.result["unchanged"], 2)
        # niveau relevé et deuxième passkey, par PR fusionnée
        self.commit_members(alice=member_fiche(
            "alice", [self.alice.entry(level="eleve"), self.alice2.entry()]))
        done = self.sync().authenticators
        self.assertEqual(done.result["added"],
                         ["human:alice/webauthn/%s" % self.alice2.credential_id])
        self.assertIn("human:alice/webauthn/%s" % self.alice.credential_id,
                      done.result["updated"])
        levels = {r["credential_id"]: r["level"] for r in self.rows()}
        self.assertEqual(levels[self.alice.credential_id], "eleve")
        # retrait : la passkey quitte la liste (révoquée), bruno quitte le canon
        third = self.commit_members(alice=member_fiche("alice", [self.alice2.entry()]),
                                    bruno=None)
        done = self.sync().authenticators
        self.assertEqual(sorted(done.result["revoked"]), sorted([
            "human:alice/webauthn/%s" % self.alice.credential_id,
            "human:bruno/webauthn/%s" % self.bruno.credential_id]))
        self.assertEqual(set(self.live()), {("human:alice", self.alice2.credential_id)})
        # la révocation note le commit qui l'a constatée
        revoked = {r["credential_id"]: r for r in self.rows(include_revoked=True)
                   if r.get("revoked_ts")}
        self.assertEqual(revoked[self.alice.credential_id]["canon_ref"],
                         "canon:membres/alice.md@%s" % third)
        self.assertEqual(revoked[self.bruno.credential_id]["canon_ref"],
                         "canon:membres/bruno.md@%s" % third)
        self.assertEqual(revoked[self.bruno.credential_id]["revoked_reason"], "absent du canon")

    def test_entree_invalide_gele_le_membre(self):
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        before = self.rows()
        broken = dict(self.alice2.entry(), public_key="pas-une-cle")
        self.commit_members(alice=member_fiche("alice", [broken]))   # retire alice, ajoute du faux
        done = self.sync().authenticators
        self.assertEqual(done.result["frozen"], ["human:alice"])
        self.assertTrue(done.errors)
        self.assertEqual(self.rows(), before)            # ni révocation, ni mise à jour
        # clé absente, ou liste illisible : gelé aussi
        for text in (member_fiche("alice"), member_fiche("alice", raw="{a: 1}")):
            self.commit_members(alice=text)
            done = self.sync().authenticators
            self.assertEqual(done.result["frozen"], ["human:alice"])
            self.assertEqual(self.rows(), before)
        # la CLI le dit et rend 1
        proc = self.mesh("canon", "sync", "--host", "atelier",
                         env=self.env(AMEESH_CANON=self.clone))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("GELÉS human:alice", proc.stdout)

    def test_une_entree_ne_choisit_pas_sa_provenance(self):
        entry = dict(self.alice.entry(), canon_ref="ailleurs:x.md@" + "f" * 40)
        commit = self.commit_members(alice=member_fiche("alice", [entry]))
        self.sync()
        self.assertEqual(list(self.live().values()), ["canon:membres/alice.md@%s" % commit])

    def test_canon_non_approuve_ou_illisible_n_ecrit_rien(self):
        root = os.path.join(self.make_tmp(), "acme")
        shutil.copytree(EXAMPLE, root)
        write(root, "membres/alice.md", member_fiche("alice", [self.alice.entry()]))
        done = canon_sync.sync(self.db, canon.load(root, untrusted=True), "atelier").authenticators
        self.assertEqual((done.status, done.errors), ("skipped", []))
        self.assertIn("NON APPROUVÉ", done.reason)
        self.assertEqual(self.rows(include_revoked=True), [])
        # un registre rempli n'est pas vidé par un canon illisible
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        before = self.rows()
        with self.assertRaises(canon_sync.CanonUnreadable):
            self.sync(ref="origin/inexistante")
        self.assertEqual(self.rows(), before)
        proc = self.mesh("canon", "sync", "--json", "--ref", "origin/inexistante",
                         env=self.env(AMEESH_CANON=self.clone, AMEESH_HOST="atelier"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["authenticators"]["status"], "skipped")
        self.assertEqual(self.rows(), before)

    def test_revision_hors_branche_canonique_refusee(self):
        """Un commit local jamais fusionné, lu par --ref : aucune écriture (§4.1)."""
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        before = self.rows()
        local = self.commit_members(alice=member_fiche("alice", [self.alice2.entry()]),
                                    bruno=member_fiche("bruno", [self.bruno.entry()]),
                                    push=False, message="non revu")
        done = self.sync(ref=local).authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("absent de la branche canonique", done.reason)
        self.assertEqual(self.rows(), before)
        proc = self.mesh("canon", "sync", "--host", "atelier", "--ref", local,
                         env=self.env(AMEESH_CANON=self.clone))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("authentificateurs : REFUSÉ", proc.stdout)
        self.assertEqual(self.rows(), before)
        # une branche de PR poussée sur le dépôt, pas fusionnée : refusée aussi
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/pr-1")
        git(self.clone, "fetch", "-q", "origin")
        done = self.sync(ref="origin/pr-1").authenticators
        self.assertEqual(done.status, "refused")
        self.assertEqual(self.rows(), before)
        # fusionnée sur la branche canonique : lue
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(self.sync(ref="origin/pr-1").authenticators.status, "synced")
        self.assertEqual({r["credential_id"] for r in self.rows()},
                         {self.alice2.credential_id, self.bruno.credential_id})

    def test_hote_en_retard_ne_reactive_rien(self):
        """Deux hôtes, une base : celui qui n'a pas vu le retrait ne le défait pas."""
        other = os.path.join(self.workspace, "hote-b")
        git(self.workspace, "clone", "-q", self.bare, other)
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        git(other, "fetch", "-q", "origin")
        self.assertEqual(self.sync(other).authenticators.result["added"],
                         ["human:alice/webauthn/%s" % self.alice.credential_id])
        # la seule passkey du registre est retirée par PR ; l'hôte A synchronise
        self.commit_members(alice=member_fiche("alice", []))
        self.assertEqual(len(self.sync().authenticators.result["revoked"]), 1)
        # l'hôte B, pas encore rafraîchi, lit l'ancien commit : refus, rien réactivé
        done = self.sync(other).authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("en retard", done.reason)
        self.assertEqual(self.rows(), [])
        # une fois rafraîchi, il suit
        done = self.sync(other, fetch=True).authenticators
        self.assertEqual((done.status, done.result["added"]), ("synced", []))
        self.assertEqual(self.rows(), [])

    def test_canon_lu_en_partie_ne_revoque_rien_pour_absence(self):
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]),
                            bruno=member_fiche("bruno", [self.bruno.entry()]))
        self.sync()
        # la fiche de bruno devient illisible : il pourrait encore y être déclaré
        self.commit_members(alice=member_fiche("alice", []),
                            bruno="---\ntype: Member\ntitle: [bruno\n---\n")
        done = self.sync().authenticators
        self.assertTrue(done.partial)
        self.assertEqual(done.result["revoked"],
                         ["human:alice/webauthn/%s" % self.alice.credential_id])
        self.assertEqual(set(self.live()), {("human:bruno", self.bruno.credential_id)})

    def test_ligne_ecrite_a_la_main_revoquee(self):
        """Une ligne hors canon (insérée en base) ne bloque rien : elle est révoquée."""
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        intruder = SoftWebAuthn()
        raw = receipts.b64u_decode(intruder.public_key())
        self.db.query(
            "INSERT INTO authenticators (approver, facade, credential_id, public_key, "
            "key_fingerprint, level, canon_ref) VALUES ('human:alice', 'webauthn', %s, %s, "
            "%s, 'standard', %s) RETURNING id",
            (intruder.credential_id, intruder.public_key(), hashlib.sha256(raw).hexdigest(),
             "membres/alice.md@" + "f" * 40))
        done = self.sync().authenticators
        self.assertEqual(done.status, "synced")
        self.assertEqual(done.result["revoked"],
                         ["human:alice/webauthn/%s" % intruder.credential_id])
        self.assertEqual(set(self.live()), {("human:alice", self.alice.credential_id)})


# ==========================================================================
# revue L9b (B1, B2) : synchronisations sérialisées, branche de confiance
# ==========================================================================

def federation(ref: str) -> str:
    """Manifeste à un seul membre — la racine `canon`, ce clone — sur la branche `ref`."""
    return ('federation: "0.1"\nid: acme\nroot: canon\nmembers:\n'
            '  - id: canon\n    ref: %s\n    bundle: .\n    workspace_path: acme\n' % ref)


class AuthenticatorRaceTest(_AuthenticatorCase):
    """B1 : contrôle d'antériorité et écritures dans UNE transaction, sous un
    verrou du registre pris AVANT le contrôle. Deux synchronisations réelles,
    sur deux connexions, ne s'entrelacent jamais."""

    def blocked_on_lock(self, thread: threading.Thread, timeout: float = 10.0) -> bool:
        """Vrai dès qu'une session attend un verrou consultatif ; faux si
        `thread` s'est terminé sans en attendre."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = self.db.query(
                "SELECT count(*)::int AS n FROM pg_locks WHERE locktype = 'advisory' "
                "AND NOT granted AND database = (SELECT oid FROM pg_database "
                "WHERE datname = current_database())")[0]["n"]
            if waiting:
                return True
            if not thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def race(self, first, second):
        """`first(db)` s'arrête juste avant ses écritures (à l'entrée de
        receipts._apply_authenticators, ses contrôles faits) ; `second(db)` est
        lancé pendant cet arrêt, sur une autre connexion. Rend (résultat du
        premier, résultat du second, le second a-t-il attendu le verrou)."""
        original = receipts._apply_authenticators
        paused, resume = threading.Event(), threading.Event()
        results: dict = {}

        def gate(lock, members, **kwargs):
            if threading.current_thread().name == "premier":
                paused.set()
                resume.wait(60)
            return original(lock, members, **kwargs)

        def run(name, job):
            conn = self.connect()
            try:
                results[name] = job(conn)
            except BaseException as exc:        # rendu au fil principal
                results[name] = exc
            finally:
                conn.close()

        with mock.patch.object(receipts, "_apply_authenticators", side_effect=gate):
            one = threading.Thread(target=run, args=("premier", first), name="premier")
            one.start()
            if not paused.wait(30):
                resume.set()
                one.join(60)
                self.fail("la première synchronisation n'a pas atteint ses écritures : %r"
                          % (results.get("premier"),))
            two = threading.Thread(target=run, args=("second", second), name="second")
            two.start()
            blocked = self.blocked_on_lock(two)
            resume.set()
            one.join(60)
            two.join(60)
        for name in ("premier", "second"):
            if isinstance(results.get(name), BaseException):
                raise results[name]
        return results["premier"], results["second"], blocked

    def test_revocation_concurrente_jamais_defaite(self):
        """Scénario de la revue : une synchronisation d'un ANCIEN canon a passé
        ses contrôles ; une autre, d'un canon plus récent, révoque une clé ; les
        écritures de l'ancienne ne la réactivent pas à l'ancien commit."""
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        old = canon.load(self.clone)
        second = self.commit_members(alice=member_fiche("alice", []))
        fresh = canon.load(self.clone)
        stale, revoke, blocked = self.race(
            lambda db: canon_sync.sync_authenticators(db, old),
            lambda db: canon_sync.sync_authenticators(db, fresh))
        self.assertEqual(self.live(), {})                  # jamais réactivée
        self.assertEqual([r["canon_ref"] for r in self.rows(include_revoked=True)],
                         ["canon:membres/alice.md@%s" % second])
        self.assertTrue(blocked, "la révocation n'a pas attendu le verrou du registre")
        self.assertEqual((stale.status, revoke.status), ("synced", "synced"))
        self.assertEqual(revoke.result["revoked"],
                         ["human:alice/webauthn/%s" % self.alice.credential_id])
        self.assertEqual([j["root_commit"] for j in self.journal()], [first, second])
        # l'ancien canon, rejoué ensuite : refusé (monotonie), rien réactivé
        late = canon_sync.sync_authenticators(self.db, old)
        self.assertEqual(late.status, "refused")
        self.assertIn("ne descend pas du dernier commit appliqué %s" % second[:12], late.reason)
        self.assertEqual(self.rows(), [])

    def test_ancien_canon_apres_la_revocation_refuse(self):
        """Ordre inverse : la révocation tient le verrou ; l'ancien canon attend,
        puis relit le dernier commit appliqué sous le verrou et est refusé."""
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        old = canon.load(self.clone)
        second = self.commit_members(alice=member_fiche("alice", []))
        fresh = canon.load(self.clone)
        revoke, stale, blocked = self.race(
            lambda db: canon_sync.sync_authenticators(db, fresh),
            lambda db: canon_sync.sync_authenticators(db, old))
        self.assertEqual(stale.status, "refused")
        self.assertIn("ne descend pas du dernier commit appliqué %s" % second[:12], stale.reason)
        self.assertEqual(revoke.status, "synced")
        self.assertEqual(self.rows(), [])
        self.assertTrue(blocked, "l'ancien canon n'a pas attendu le verrou du registre")

    def test_deux_clones_du_meme_depot_se_serialisent(self):
        """Le verrou ne dépend ni du chemin du clone, ni de l'hôte : deux clones
        distincts du même dépôt nu qui synchronisent en même temps passent l'un
        après l'autre, et le second voit ce que le premier a écrit."""
        other = os.path.join(self.workspace, "hote-b")
        git(self.workspace, "clone", "-q", self.bare, other)
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        second = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]),
                                     bruno=member_fiche("bruno", [self.bruno.entry()]))
        git(other, "fetch", "-q", "origin")
        here, there = canon.load(self.clone), canon.load(other)
        self.assertNotEqual(here.root, there.root)
        self.assertEqual([s.commit for s in there.sources], [second])
        one, two, blocked = self.race(
            lambda db: canon_sync.sync_authenticators(db, here, host="atelier"),
            lambda db: canon_sync.sync_authenticators(db, there, host="hote-b"))
        self.assertTrue(blocked, "le second clone n'a pas attendu le verrou du registre")
        self.assertEqual((one.status, one.result["added"]),
                         ("synced", ["human:bruno/webauthn/%s" % self.bruno.credential_id]))
        self.assertEqual((two.status, two.result["added"], two.result["unchanged"]),
                         ("synced", [], 2))
        self.assertEqual([(j["root_commit"], j["host"]) for j in self.journal()],
                         [(first, "atelier"), (second, "atelier")])


class AuthenticatorTrustTest(_AuthenticatorCase):
    """B2 : la branche canonique de confiance ne vient jamais du commit lu —
    configuration de l'hôte, sinon manifeste du dernier commit appliqué, sinon
    amorçage explicite (--bootstrap-ref), sinon refus."""

    def push_pr(self, branch: str = "pr-unmerged", **fiches) -> str:
        """Commit NON fusionné, poussé sur une branche de PR du dépôt nu."""
        commit = self.commit_members(push=False, message="PR non revue", **fiches)
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/%s" % branch)
        git(self.clone, "fetch", "-q", "origin")
        self.assertNotEqual(git(self.clone, "rev-parse", "origin/main"), commit)
        return commit

    def test_branche_de_pr_qui_s_autorise_elle_meme_refusee(self):
        """Sonde de la revue : un commit de PR non fusionnée qui fait de sa propre
        branche la branche canonique (federation.yaml) et ajoute une passkey."""
        write(self.clone, "federation.yaml", federation("pr-unmerged"))
        pr = self.push_pr(alice=member_fiche("alice", [self.alice.entry()]))
        candidate = canon.load(self.clone, ref="origin/pr-unmerged")
        self.assertEqual(candidate.sources[0].commit, pr)
        # registre vierge, sans branche de confiance : refus d'amorçage
        done = canon_sync.sync_authenticators(self.db, candidate)
        self.assertEqual(done.status, "refused")
        self.assertIn("--bootstrap-ref", done.reason)
        # ni l'amorçage sur main, ni la configuration de l'hôte n'en font un canon
        for kwargs in ({"bootstrap_ref": "main"}, {"trusted_ref": "origin/main"},
                       {"trusted_ref": "main"}):
            with self.subTest(**kwargs):
                done = canon_sync.sync_authenticators(self.db, candidate, **kwargs)
                self.assertEqual(done.status, "refused")
                self.assertIn("absent de la branche canonique origin/main", done.reason)
        self.assertEqual((self.rows(include_revoked=True), self.journal()), ([], []))
        # registre amorcé sur main : la PR reste refusée, par la CLI aussi
        self.assertEqual(self.sync(ref="origin/main").authenticators.status, "synced")
        done = canon_sync.sync_authenticators(
            self.db, canon.load(self.clone, ref="origin/pr-unmerged"))
        self.assertEqual(done.status, "refused")
        self.assertIn("absent de la branche canonique origin/main", done.reason)
        proc = self.mesh("canon", "sync", "--host", "atelier", "--ref", "origin/pr-unmerged",
                         env=self.env(AMEESH_CANON=self.clone))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("authentificateurs : REFUSÉ", proc.stdout)
        self.assertEqual(self.rows(include_revoked=True), [])

    def test_premier_amorcage_refuse_sans_bootstrap_ref(self):
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        done = canon_sync.sync(self.db, canon.load(self.clone), "atelier").authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("premier amorçage", done.reason)
        self.assertIn("--bootstrap-ref", done.reason)
        self.assertEqual((self.rows(include_revoked=True), self.journal()), ([], []))
        env = self.env(AMEESH_CANON=self.clone)
        proc = self.mesh("canon", "sync", "--host", "atelier", env=env)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("authentificateurs : REFUSÉ — premier amorçage", proc.stdout)
        self.assertEqual(self.rows(), [])
        # --bootstrap-ref explicite : appliqué, journalisé
        proc = self.mesh("canon", "sync", "--host", "atelier", "--bootstrap-ref", "main",
                         env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("AMORÇAGE", proc.stdout)
        self.assertIn("branche canonique de confiance origin/main (amorçage --bootstrap-ref)",
                      proc.stdout)
        self.assertEqual(set(self.live()), {("human:alice", self.alice.credential_id)})
        self.assertEqual([(j["root_commit"], j["branch"], j["trust"], j["host"])
                          for j in self.journal()],
                         [(first, "origin/main", "bootstrap", "atelier")])
        # ensuite le dernier commit appliqué fait foi ; --bootstrap-ref est ignoré
        second = self.commit_members(alice=member_fiche(
            "alice", [self.alice.entry(), self.alice2.entry()]))
        done = self.sync(bootstrap_ref="autre").authenticators
        self.assertEqual((done.status, done.trust, done.branch),
                         ("synced", "applied", "origin/main"))
        self.assertIn("--bootstrap-ref autre ignoré : registre déjà amorcé", done.notes)
        self.assertEqual(self.journal()[-1]["root_commit"], second)
        # la configuration de l'hôte (AMEESH_CANON_REF) suffit à un premier amorçage
        self.db.execute("TRUNCATE authenticators, authenticator_syncs RESTART IDENTITY CASCADE")
        proc = self.mesh("canon", "sync", "--host", "atelier", "--json",
                         env=self.env(AMEESH_CANON=self.clone, AMEESH_CANON_REF="origin/main"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)["authenticators"]
        self.assertEqual((data["status"], data["trust"], data["branch"]),
                         ("synced", "config", "origin/main"))
        self.assertEqual([j["trust"] for j in self.journal()], ["config"])

    def test_changement_de_branche_canonique_seulement_apres_fusion(self):
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()                                         # amorçage sur main
        # PR : le manifeste fait de « release » la branche canonique et ajoute
        # la passkey de bruno ; poussée sur release, PAS fusionnée sur main
        write(self.clone, "federation.yaml", federation("release"))
        manifest = self.push_pr("release", bruno=member_fiche("bruno", [self.bruno.entry()]))
        done = self.sync().authenticators                   # main : rien de neuf
        self.assertEqual((done.status, done.result["added"]), ("synced", []))
        done = self.sync(ref="origin/release").authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("absent de la branche canonique origin/main", done.reason)
        self.assertEqual(set(self.live()), {("human:alice", self.alice.credential_id)})
        # fusionnée sur main : lue, et encore vérifiée contre main
        git(self.clone, "push", "-q", "origin", "%s:refs/heads/main" % manifest)
        git(self.clone, "fetch", "-q", "origin")
        done = self.sync().authenticators
        self.assertEqual((done.status, done.trust, done.branch),
                         ("synced", "applied", "origin/main"))
        self.assertEqual(done.result["added"],
                         ["human:bruno/webauthn/%s" % self.bruno.credential_id])
        # désormais release fait foi : un commit de release seule est appliqué…
        later = self.commit_members(push=False, alice=member_fiche(
            "alice", [self.alice.entry(), self.alice2.entry()]))
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/release")
        git(self.clone, "fetch", "-q", "origin")
        done = self.sync().authenticators
        self.assertEqual((done.status, done.branch), ("synced", "origin/release"))
        self.assertEqual(done.result["added"],
                         ["human:alice/webauthn/%s" % self.alice2.credential_id])
        # … et un commit de main seule ne l'est plus
        git(self.clone, "reset", "-q", "--hard", manifest)
        write(self.clone, "note.md", "sur main seulement\n")
        commit_all(self.clone, "main seule", push=False)
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.clone, "fetch", "-q", "origin")
        done = self.sync(ref="origin/main").authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("absent de la branche canonique origin/release", done.reason)
        self.assertEqual(self.journal()[-1]["root_commit"], later)

    def test_commit_non_descendant_refuse(self):
        """Monotonie : un commit atteignable depuis la branche canonique mais qui
        ne descend pas du dernier commit appliqué n'est jamais écrit, même quand
        aucune ligne du registre ne trahit le retard."""
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        self.sync()
        write(self.clone, "note.md", "sans effet sur le registre\n")
        second = commit_all(self.clone, "note")
        self.assertEqual(self.sync().authenticators.status, "synced")
        before = self.rows(include_revoked=True)
        # un ancien commit de main, lu par --ref
        done = self.sync(ref=first).authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("ne descend pas du dernier commit appliqué %s" % second[:12],
                      done.reason)
        # historique réécrit (poussé en force sur main) qui ajoute bruno
        git(self.clone, "reset", "-q", "--hard", first)
        write(self.clone, "membres/bruno.md", member_fiche("bruno", [self.bruno.entry()]))
        commit_all(self.clone, "historique réécrit", push=False)
        git(self.clone, "push", "-q", "--force", "origin", "HEAD:main")
        git(self.clone, "fetch", "-q", "origin")
        done = self.sync().authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("ne descend pas du dernier commit appliqué %s" % second[:12],
                      done.reason)
        self.assertEqual(self.rows(include_revoked=True), before)
        self.assertEqual(self.journal()[-1]["root_commit"], second)


# ==========================================================================
# revue L9b (codex2, codex3) : branche de confiance relayée par sync lui-même,
# erreurs jamais silencieuses, écriture du registre gardée par son verrou
# ==========================================================================

class AuthenticatorRelayTest(_AuthenticatorCase):
    """codex2 : l'exécuteur appelle `canon_sync.sync(db, canon, host)` sans
    `trusted_ref` et ignore `report.authenticators` ; sync prend donc la
    branche de confiance dans la configuration de la connexion, et inscrit
    toute erreur d'authentificateurs dans canon_state et dans le fil."""

    def configured(self, **changes):
        """Connexion dont la configuration de l'hôte porte `changes`."""
        conn = db_mod.connect(dataclasses.replace(self.cfg, **changes))
        self.addCleanup(conn.close)
        return conn

    def fil_entries(self) -> list:
        return fil.transport_for(self.cfg).read(fil.ThreadRef(fil.project_for(self.cfg)))

    def runner_pass(self, **changes) -> bool:
        """Une passe de `Runner.canon_sync_once` (code de l'exécuteur, non
        modifié) avec une configuration d'hôte qui pose `changes`."""
        fake = types.SimpleNamespace(
            cfg=dataclasses.replace(self.cfg, canon=self.clone, **changes), host="atelier")
        return runner_mod.Runner.canon_sync_once(fake)

    def test_sync_sans_trusted_ref_prend_la_configuration(self):
        first = self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        # registre vierge, ni --bootstrap-ref ni trusted_ref : la configuration suffit
        conn = self.configured(canon_ref="origin/main")
        report = canon_sync.sync(conn, canon.load(self.clone), "atelier")
        done = report.authenticators
        self.assertEqual((done.status, done.trust, done.branch),
                         ("synced", "config", "origin/main"))
        self.assertEqual([(j["root_commit"], j["trust"]) for j in self.journal()],
                         [(first, "config")])
        # la configuration PRIME sur le manifeste du dernier commit appliqué
        git(self.clone, "push", "-q", "origin", "%s:refs/heads/release" % first)
        self.commit_members(alice=member_fiche("alice", [self.alice.entry(),
                                                         self.alice2.entry()]))
        conn = self.configured(canon_ref="release")
        done = canon_sync.sync(conn, canon.load(self.clone, fetch=True), "atelier").authenticators
        self.assertEqual(done.status, "refused")
        self.assertIn("absent de la branche canonique origin/release", done.reason)
        self.assertEqual(set(self.live()), {("human:alice", self.alice.credential_id)})
        # "" explicite : aucune configuration, le dernier commit appliqué fait foi
        done = canon_sync.sync(conn, canon.load(self.clone), "atelier",
                               trusted_ref="").authenticators
        self.assertEqual((done.status, done.trust), ("synced", "applied"))
        # l'exécuteur lui-même (aucun trusted_ref passé) suit la configuration
        self.db.execute("TRUNCATE authenticators, authenticator_syncs RESTART IDENTITY CASCADE")
        self.assertTrue(self.runner_pass(canon_ref="origin/main"))
        self.assertEqual([j["trust"] for j in self.journal()], ["config"])
        self.assertEqual(len(self.live()), 2)

    def test_erreur_d_authentificateurs_dans_canon_state_et_le_fil(self):
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        # premier amorçage sans branche de confiance : refusé — l'exécuteur ne le
        # dit pas (il ne lit que le statut du canon), sync l'inscrit lui-même
        self.assertTrue(self.runner_pass())
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual((state["status"], state["auth_status"]), ("ok", "error"))
        self.assertIn("premier amorçage", state["auth_diagnostic"])
        self.assertIsNotNone(state["auth_checked_ts"])
        entries = self.fil_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].author, canon_sync.FIL_SENDER)
        self.assertIn("registre des authentificateurs EN ERREUR", entries[0].text)
        self.assertIn("premier amorçage", entries[0].text)
        self.assertEqual((entries[0].meta["event"], entries[0].meta["host"]),
                         ("authenticators_error", "atelier"))
        # la même erreur, passe suivante : état rafraîchi, pas de doublon au fil
        report = canon_sync.sync(self.db, canon.load(self.clone), "atelier")
        self.assertEqual((report.status, report.auth_status), ("ok", "error"))
        self.assertEqual(report.to_dict()["auth_status"], "error")
        self.assertEqual(len(self.fil_entries()), 1)
        # une erreur d'une autre nature (membre gelé) : nouvelle entrée
        self.sync()                                          # amorçage sur main
        self.assertEqual(canon_sync.state(self.db, "atelier")["auth_status"], "ok")
        self.assertIn("de nouveau sans erreur", self.fil_entries()[-1].text)
        self.commit_members(alice=member_fiche("alice", raw="{a: 1}"))
        report = self.sync()
        self.assertEqual(report.auth_status, "error")
        state = canon_sync.state(self.db, "atelier")
        self.assertEqual(state["auth_status"], "error")
        self.assertIn("GELÉS : human:alice", state["auth_diagnostic"])
        self.assertIn("GELÉS : human:alice", self.fil_entries()[-1].text)
        self.assertEqual(len(self.fil_entries()), 3)
        # visible par `canon check`
        proc = self.mesh("canon", "check", "--host", "atelier",
                         env=self.env(AMEESH_CANON=self.clone))
        self.assertIn("authentificateurs : EN ERREUR — error", proc.stdout)


class AuthenticatorWriteGuardTest(_AuthenticatorCase):
    """codex3 : le registre n'est écrit que par `canon_sync.sync_authenticators`
    (lecture du canon, contrôles, verrou) ; l'écriture interne exige le jeton
    du verrou et revérifie dans pg_locks que la session le détient."""

    def members(self) -> list:
        return [{"title": "alice", "canon_ref": "canon:membres/alice.md@" + "a" * 40,
                 "authenticators": [self.alice.entry()]}]

    def test_ecriture_hors_du_chemin_complet_refusee(self):
        self.assertFalse(hasattr(receipts, "sync_authenticators"))   # plus de point d'entrée
        with self.assertRaises(receipts.RegistryLockError):
            receipts._apply_authenticators(self.db, self.members())   # pas un jeton
        with self.assertRaises(receipts.RegistryLockError):
            receipts.RegistryLock(self.db)                            # jeton forgé
        # verrou pris hors transaction : relâché aussitôt, jeton refusé
        lock = receipts.lock_registry(self.db)
        self.assertFalse(receipts.registry_lock_held(self.db))
        with self.assertRaises(receipts.RegistryLockError):
            receipts._apply_authenticators(lock, self.members())
        # jeton d'une transaction close
        with self.db.transaction() as tx:
            stale = receipts.lock_registry(tx)
            self.assertTrue(receipts.registry_lock_held(tx))
        with self.assertRaises(receipts.RegistryLockError):
            receipts._apply_authenticators(stale, self.members())
        self.assertEqual(self.rows(include_revoked=True), [])

    def test_verrou_tenu_ailleurs_le_chemin_complet_attend(self):
        """Une autre connexion tient le verrou : l'écriture directe est refusée
        aussitôt ; le chemin complet attend (pg_blocking_pids) et n'écrit
        qu'après la libération."""
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        loaded = canon.load(self.clone)
        holder = self.connect()
        self.addCleanup(holder.close)
        results: dict = {}

        def run():
            conn = self.connect()
            try:
                results["done"] = canon_sync.sync_authenticators(conn, loaded,
                                                                 bootstrap_ref="main")
            except BaseException as exc:        # rendu au fil principal
                results["done"] = exc
            finally:
                conn.close()

        with holder.transaction() as htx:
            receipts.lock_registry(htx)
            holder_pid = int(htx.query("SELECT pg_backend_pid() AS pid")[0]["pid"])
            # écriture directe (sans le jeton du verrou) : refusée sans attendre
            with self.assertRaises(receipts.RegistryLockError):
                receipts._apply_authenticators(self.db, self.members())
            worker = threading.Thread(target=run, name="chemin-complet")
            worker.start()
            blockers = None
            deadline = time.monotonic() + 15
            while blockers is None and time.monotonic() < deadline and worker.is_alive():
                rows = self.db.query(
                    "SELECT pg_blocking_pids(pid) AS blockers FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted AND database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())")
                blockers = [int(p) for p in rows[0]["blockers"]] if rows else None
                if blockers is None:
                    time.sleep(0.05)
            self.assertEqual(blockers, [holder_pid], results.get("done"))
            self.assertEqual(self.rows(include_revoked=True), [])     # rien pendant l'attente
            self.assertEqual(self.journal(), [])
        worker.join(60)
        done = results["done"]
        if isinstance(done, BaseException):
            raise done
        self.assertEqual(done.status, "synced")
        self.assertEqual(set(self.live()), {("human:alice", self.alice.credential_id)})

    def test_verrou_reentrant_dans_la_meme_transaction(self):
        """Via canon sync : pas d'auto-blocage. Le verrou repris dans la même
        transaction est accordé aussitôt ; une transaction imbriquée (même
        session psql, point de sauvegarde psycopg) le détient encore."""
        with self.db.transaction() as tx:
            lock = receipts.lock_registry(tx)
            receipts.lock_registry(tx)
            with tx.transaction() as inner:
                self.assertTrue(receipts.registry_lock_held(inner))
                result = receipts._apply_authenticators(receipts.lock_registry(inner),
                                                        self.members())
            self.assertEqual(len(result["added"]), 1)
            self.assertTrue(receipts.registry_lock_held(lock.db))
        self.assertFalse(receipts.registry_lock_held(self.db))
        self.assertEqual(len(self.live()), 1)
        # le chemin complet, de bout en bout, ne s'attend pas lui-même
        self.db.execute("TRUNCATE authenticators, authenticator_syncs RESTART IDENTITY CASCADE")
        self.commit_members(alice=member_fiche("alice", [self.alice.entry()]))
        started = time.monotonic()
        self.assertEqual(self.sync().authenticators.status, "synced")
        self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
