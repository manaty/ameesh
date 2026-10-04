# SPDX-License-Identifier: AGPL-3.0-only
"""Canon OKF (L2) : lecture par git, YAML, validation, sync, éphémères, règle R14."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import canon, canon_sync, config as config_mod, db as db_mod, registry

from .support import REPO, PgTestCase

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

    def sync(self, host: str = "atelier") -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load(), host)

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
                         ("banc", "human:alice", "/srv/acme/relecture"))

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
        self.db.execute("UPDATE agent_registry SET canon_ref = 'canon:agents/c.md@abc', "
                        "responsible = 'human:alice' WHERE name = 'canonique'")
        self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual(self.turns(), [])                    # aucun état : jamais vérifié
        canon_sync.record_state(self.db, "h-canon", canon_sync.CANON_UNREADABLE,
                                "canon-missing (canon) : racine du canon introuvable")
        self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual(self.turns(), [])
        canon_sync.record_state(self.db, "h-canon", canon_sync.CANON_OK, "")
        proc = self.runner("--once", "--agents", "canonique", env=env)
        self.assertEqual([t["harness"] for t in self.turns()], ["claude"], proc.stdout)


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


if __name__ == "__main__":
    unittest.main()
