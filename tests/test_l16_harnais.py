# SPDX-License-Identifier: AGPL-3.0-only
"""L16 : descripteurs de harnais (manifeste ACP + clés `ameesh`), CLI, canon.

Aucune base n'est nécessaire pour les descripteurs et le canon : ce sont des
fichiers. Les tests qui en ont besoin (comptes, budget) utilisent Postgres réel
par la classe `PgTestCase` de `support`.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import accounts, adapters, canon, cost, harnesses
from ameesh import platform as os_layer

from .support import FAKEBIN, child_env
from .test_canon import fiche, write


def descriptor_doc(ident: str = "faux", **ameesh) -> dict:
    """Un document de descripteur valide, surchargeable par `ameesh`. """
    base = {
        "id": ident,
        "name": "Faux harnais",
        "version": "1.0.0",
        "schema_version": "1",
        "description": "Harnais de test, sans binaire réel.",
        "capabilities": {},
        "authentication": {},
        "distribution": {},
        "ameesh": {
            "protocol": "cli",
            "binary": "faux-binaire",
            "command": ["--json"],
            "stream": "text",
            **ameesh,
        },
    }
    return base


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l16-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.hosts = os.path.join(self.tmp, "harnais")
        os.makedirs(self.hosts, mode=0o700)

    def write_host(self, ident: str, doc: dict | None = None, *, name: str | None = None,
                   mode: int = 0o600) -> str:
        path = os.path.join(self.hosts, name or ("%s.json" % ident))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc if doc is not None else descriptor_doc(ident), fh)
        os.chmod(path, mode)
        return path

    def env(self, **extra) -> dict:
        base = {"AMEESH_HARNESSES_DIR": self.hosts}
        base.update(extra)
        return child_env(**base)

    def cli(self, *args, env=None, timeout=30.0):
        return subprocess.run([sys.executable, "-m", "ameesh.main", *args],
                              capture_output=True, text=True, timeout=timeout,
                              env=env or self.env())


# ==========================================================================
# Descripteurs : chargement, validation, sources
# ==========================================================================

class DescriptorTest(_Tmp):
    def test_descripteurs_du_paquet(self):
        known, findings = harnesses.scan()
        self.assertEqual(findings, [])
        self.assertEqual(sorted(known), ["claude", "codex", "deepseek"])
        for ident, d in known.items():
            with self.subTest(harness=ident):
                self.assertEqual(d.source, "paquet")
                self.assertEqual(d.protocol, "cli")
                self.assertIn(d.stream, harnesses.STREAM_FORMATS)
                self.assertEqual(len(d.sha256), 64)
                self.assertTrue(d.binary)
        self.assertEqual(known["deepseek"].launcher, ("--profile", "agent"))
        self.assertTrue(known["deepseek"].paid_per_token)
        self.assertFalse(known["claude"].paid_per_token)

    def test_check_accepte_un_document_valide(self):
        path = self.write_host("faux")
        document, findings = harnesses.check_path(path)
        self.assertIsNotNone(document)
        self.assertEqual(harnesses.errors(findings), [])

    def test_check_refuse_et_explique(self):
        cas = {
            "harness-ameesh-missing": {"id": "x", "name": "x", "version": "1",
                                       "schema_version": "1",
                                       "description": "sans espace ameesh"},
            "harness-stream-unknown": descriptor_doc("x", stream="flux-inconnu"),
            "harness-permissions-invalid": descriptor_doc(
                "x", permissions={"default": "deny", "allow_kinds": ["teleportation"]}),
            "harness-protocol-invalid": descriptor_doc("x", protocol="rpc"),
            "harness-binary-missing": descriptor_doc("x", binary=""),
        }
        for attendu, doc in cas.items():
            with self.subTest(code=attendu):
                path = os.path.join(self.tmp, "%s.json" % attendu)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
                os.chmod(path, 0o600)
                document, findings = harnesses.check_path(path)
                self.assertIsNone(document, findings)
                self.assertIn(attendu, [f.code for f in findings
                                        if f.severity == harnesses.ERROR])

    def test_check_avertit_sans_bloquer(self):
        doc = descriptor_doc("x")
        doc["clé_inconnue"] = 1
        doc["schema_version"] = "2"
        doc["ameesh"]["model"] = {"flag": ["--model", "{model}"], "mystere": True}
        path = os.path.join(self.tmp, "x.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.chmod(path, 0o600)
        document, findings = harnesses.check_path(path)
        self.assertIsNotNone(document)
        codes = {f.code for f in findings}
        self.assertIn("harness-key-unknown", codes)
        self.assertIn("harness-schema-version", codes)
        self.assertIn("harness-distribution-empty", codes)
        self.assertEqual(harnesses.errors(findings), [])

    def test_hote_prime_sur_le_paquet_et_le_signale(self):
        doc = descriptor_doc("claude", command=["-p", "--local"])
        doc["name"] = "Claude local"
        doc["version"] = "9.9.9"
        self.write_host("claude", doc)
        known, findings = harnesses.scan(host=self.hosts)
        self.assertEqual(known["claude"].source, "hôte")
        self.assertEqual(known["claude"].name, "Claude local")
        self.assertIn("harness-host-overrides", [f.code for f in findings])

    def test_disposition_du_registre_acceptee(self):
        # `<id>/agent.json`, comme le dépôt du registre ACP
        dossier = os.path.join(self.hosts, "gemini")
        os.makedirs(dossier, mode=0o700)
        path = os.path.join(dossier, "agent.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(descriptor_doc("gemini", command=["--acp"]), fh)
        os.chmod(path, 0o600)
        self.assertIn("gemini", harnesses.known_ids(host=self.hosts))

    def test_dossier_hote_non_prive_est_ignore(self):
        self.write_host("gemini")
        os.chmod(self.hosts, 0o755)
        known, findings = harnesses.scan(host=self.hosts)
        self.assertNotIn("gemini", known)
        codes = [f.code for f in findings]
        self.assertIn("harness-host-insecure", codes)

    def test_fichier_hote_modifiable_est_ignore(self):
        self.write_host("gemini", mode=0o666)
        known, findings = harnesses.scan(host=self.hosts)
        self.assertNotIn("gemini", known)
        self.assertIn("harness-host-insecure", [f.code for f in findings])

    def test_empreinte_du_fichier_hote(self):
        import hashlib
        path = self.write_host("gemini")
        d = harnesses.get("gemini", host=self.hosts)
        with open(path, "rb") as fh:
            attendu = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(d.sha256, attendu)
        self.assertEqual(d.source, "hôte")

    def test_sous_dossier_symbolique_refuse(self):
        """Un sous-dossier de l'hôte ne peut pas être un lien vers ailleurs."""
        dehors = os.path.join(self.tmp, "dehors")
        os.makedirs(dehors, mode=0o755)
        with open(os.path.join(dehors, "agent.json"), "w", encoding="utf-8") as fh:
            json.dump(descriptor_doc("gemini"), fh)
        os.symlink(dehors, os.path.join(self.hosts, "gemini"))
        known, findings = harnesses.scan(host=self.hosts)
        self.assertNotIn("gemini", known)
        self.assertIn("harness-host-insecure", [f.code for f in findings])

    def test_fichier_symbolique_refuse(self):
        dehors = os.path.join(self.tmp, "dehors.json")
        with open(dehors, "w", encoding="utf-8") as fh:
            json.dump(descriptor_doc("gemini"), fh)
        os.chmod(dehors, 0o600)
        os.symlink(dehors, os.path.join(self.hosts, "gemini.json"))
        known, findings = harnesses.scan(host=self.hosts)
        self.assertNotIn("gemini", known)
        self.assertIn("harness-host-insecure", [f.code for f in findings])

    def test_dossier_hote_symbolique_refuse(self):
        """Le dossier de l'hôte lui-même ne peut pas être un lien."""
        dehors = os.path.join(self.tmp, "ailleurs")
        os.makedirs(dehors, mode=0o700)
        lien = os.path.join(self.tmp, "lien-harnais")
        os.symlink(dehors, lien)
        self.assertNotEqual(harnesses.host_dir_problem(lien), "")
        known, findings = harnesses.scan(host=lien)
        self.assertNotIn("gemini", known)
        self.assertIn("harness-host-insecure", [f.code for f in findings])

    def test_octets_analyses_sont_ceux_empreintes(self):
        """L'empreinte porte sur les octets lus et analysés, une seule lecture."""
        import hashlib
        doc = descriptor_doc("gemini")
        path = self.write_host("gemini", doc)
        d = harnesses.get("gemini", host=self.hosts)
        with open(path, "rb") as fh:
            octets = fh.read()
        self.assertEqual(d.sha256, hashlib.sha256(octets).hexdigest())
        self.assertEqual(d.document, json.loads(octets.decode("utf-8")))

    def test_scan_hote_explicite_lit_en_securite(self):
        """`scan(host=...)` propage sa racine : la lecture sûre ne dépend plus du
        dossier de l'environnement, qui peut être différent (codex2)."""
        self.write_host("gemini")
        with mock.patch.object(harnesses, "_secure_read",
                               wraps=harnesses._secure_read) as lecture:
            known, _ = harnesses.scan(host=self.hosts)
        self.assertIn("gemini", known)
        self.assertTrue(lecture.called)

    def test_racine_hote_atteinte_par_un_lien_refusee(self):
        """Les ancêtres de la racine sont protégés : un lien les atteignant est refusé."""
        self.write_host("gemini")
        lien = os.path.join(self.tmp, "lien-hote")
        os.symlink(self.hosts, lien)
        with self.assertRaises(harnesses.DescriptorError):
            harnesses._secure_read(os.path.join(lien, "gemini.json"), lien)

    def test_secure_read_ne_fuit_pas_de_descripteur(self):
        """Le dossier parent est refermé sur chaque lecture réussie (codex2)."""
        path = self.write_host("gemini")
        with open(path, "rb") as fh:
            octets = fh.read()
        for _ in range(3):  # chauffe : rien ne doit rester ouvert
            harnesses._secure_read(path, self.hosts)
        try:  # L63 : par la couche plateforme (psutil, ou /proc en repli)
            avant = os_layer.open_fd_count()
        except os_layer.NotAvailable as exc:
            self.skipTest(str(exc))
        for _ in range(20):
            self.assertEqual(harnesses._secure_read(path, self.hosts), octets)
        apres = os_layer.open_fd_count()
        self.assertEqual(apres, avant, "descripteurs de dossiers non refermés")

    def test_type_non_regulier_refuse_sans_bloquer(self):
        """Un FIFO est refusé en temps borné : l'acquisition est non bloquante
        (`O_NONBLOCK`) avant le contrôle du type, y compris par `check_path`
        (chemin `harness_cli.cmd_show`) (codex2)."""
        fifo = os.path.join(self.hosts, "fifo.json")
        os.mkfifo(fifo, 0o600)
        debut = time.monotonic()
        with self.assertRaises(harnesses.DescriptorError):
            harnesses._secure_read(fifo, self.hosts)
        self.assertLess(time.monotonic() - debut, 5.0)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            debut = time.monotonic()
            document, findings = harnesses.check_path(fifo)
            self.assertLess(time.monotonic() - debut, 5.0)
        self.assertIsNone(document, findings)
        self.assertIn("harness-host-insecure", [f.code for f in findings])


# ==========================================================================
# Adaptateurs : plus de liste fermée
# ==========================================================================

class AdapterDescriptorTest(_Tmp):
    def test_harnais_inedit_pilote_par_descripteur(self):
        doc = descriptor_doc("gemini", command=["--json"], stream="text",
                             model={"flag": ["--model", "{model}"]},
                             session={"flag": ["--resume"]})
        path = self.write_host("gemini", doc)
        binaire = os.path.join(self.tmp, "gemini-faux")
        with open(binaire, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(binaire, 0o755)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            adapter = adapters.adapter_for("gemini", binary=binaire)
            argv = adapter.command("fais X", "s-1", model="m-1")
            self.assertEqual(argv, [binaire, "--model", "m-1", "--json",
                                    "--resume", "s-1", "fais X"])
            self.assertEqual(adapter.parse("une ligne brute")["display"], ["une ligne brute"])
            self.assertEqual(adapter.descriptor.path, path)

    def test_descripteur_acp_construit_le_pont(self):
        doc = descriptor_doc("agent-acp", protocol="acp", command=["--acp"],
                             stream="acp-json", binary=os.path.join(self.tmp, "agent"))
        with open(doc["ameesh"]["binary"], "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(doc["ameesh"]["binary"], 0o755)
        self.write_host("agent-acp", doc)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            adapter = adapters.adapter_for("agent-acp")
            argv = adapter.command("fais X", "s-1", model="m")
            self.assertEqual(argv[:4], [sys.executable, "-m", "ameesh.acp", "run"])
            self.assertIn("--descriptor", argv)
            self.assertIn("--agent", argv)
            self.assertIn(doc["ameesh"]["binary"], argv)
            self.assertEqual(argv[argv.index("--session") + 1], "s-1")
            self.assertEqual(argv[-1], "fais X")
            # l'empreinte du descripteur est épinglée pour le pont
            self.assertEqual(argv[argv.index("--descriptor-sha256") + 1],
                             adapter.descriptor.sha256)
            self.assertEqual(len(adapter.descriptor.sha256), 64)
            self.assertEqual(adapter.env(), {})
            with self.assertRaises(adapters.HarnessMissing):
                adapter.interactive_command("s-1")

    def test_harnais_inconnu_liste_les_descripteurs(self):
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            with self.assertRaises(adapters.HarnessMissing) as ctx:
                adapters.adapter_for("inconnu")
            self.assertIn("claude", str(ctx.exception))

    def test_tier_declare_par_descripteur(self):
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts,
                                          "AMEESH_BIN_DIR": FAKEBIN}):
            self.assertTrue(adapters.supports_tier("codex"))
            self.assertFalse(adapters.supports_tier("claude"))
            self.assertFalse(adapters.supports_tier("inconnu"))


# ==========================================================================
# Canon : le harnais vient des descripteurs, plus d'une liste en dur
# ==========================================================================

class CanonHarnessTest(_Tmp):
    def canon_root(self, harness: str | None, *, policy: str = "") -> str:
        root = os.path.join(self.tmp, "canon")
        write(root, "members/alice.md",
              fiche(type="Member", title="alice", roles=["reviewer"]))
        champs = {"type": "Agent", "title": "ouvrier", "responsible": "human:alice"}
        if harness is not None:
            champs["harness"] = harness
        write(root, "agents/ouvrier.md", fiche(**champs))
        write(root, "hotes/atelier.md",
              fiche(type="Host", title="atelier", responsible="human:alice", **(
                  {"policy": policy} if policy else {})))
        return root

    def check(self, root: str):
        loaded = canon.load(root, untrusted=True)
        return canon.validate(loaded)

    def test_harnais_inconnu_est_une_erreur(self):
        findings = self.check(self.canon_root("gemini"))
        codes = {f.code for f in findings if f.severity == canon.ERROR}
        self.assertIn("agent-harness-unknown", codes)

    def test_harnais_absent_est_un_avertissement(self):
        findings = self.check(self.canon_root(None))
        erreurs = {f.code for f in findings if f.severity == canon.ERROR}
        self.assertNotIn("agent-harness-unknown", erreurs)
        self.assertIn("agent-harness-missing", {f.code for f in findings})

    def test_harnais_de_l_hote_est_connu(self):
        self.write_host("gemini", descriptor_doc("gemini"))
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            findings = self.check(self.canon_root("gemini"))
        self.assertNotIn("agent-harness-unknown",
                         {f.code for f in findings if f.severity == canon.ERROR})

    def test_politique_d_hote_inconnue_est_une_erreur(self):
        root = self.canon_root("claude", policy="{harnesses: [gemini], max_agents: 2}")
        findings = self.check(root)
        codes = {f.code for f in findings if f.severity == canon.ERROR}
        self.assertIn("host-harness-unknown", codes)

    def test_dossier_hote_non_prive_est_signale_par_canon_check(self):
        self.write_host("gemini")
        os.chmod(self.hosts, 0o755)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            findings = self.check(self.canon_root("claude"))
        self.assertIn("harness-host-insecure", {f.code for f in findings})


# ==========================================================================
# Comptes et coût : les tables viennent des descripteurs
# ==========================================================================

class ComptesEtCoutTest(_Tmp):
    def test_comptes_du_descripteur(self):
        self.assertEqual(accounts.config_env_of("claude"), "CLAUDE_CONFIG_DIR")
        self.assertEqual(accounts.default_home_of("codex"), "~/.codex")
        self.assertEqual(accounts.session_store_of("claude"), "projects")
        self.assertEqual(accounts.credentials_of("codex"), "auth.json")
        self.assertEqual(accounts.default_key_env_of("deepseek"), "DEEPSEEK_API_KEY")
        self.assertEqual(accounts.config_env_of("inconnu"), "")

    def test_comptes_d_un_harnais_inedit(self):
        doc = descriptor_doc("gemini")
        doc["ameesh"]["accounts"] = {"config_env": "GEMINI_HOME", "default_home": "~/.gemini"}
        self.write_host("gemini", doc)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            self.assertEqual(accounts.config_env_of("gemini"), "GEMINI_HOME")
            self.assertEqual(accounts.default_home_of("gemini"), "~/.gemini")

    def test_comptes_harnais_inconnu_refuse(self):
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"gemini": [{"name": "a"}]})

    def test_harnais_payes_au_token_viennent_des_descripteurs(self):
        self.assertEqual(cost.paid_harnesses_of(), ("deepseek",))
        doc = descriptor_doc("gemini", cost={"source": "stream", "paid_per_token": True})
        self.write_host("gemini", doc)
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}):
            self.assertEqual(cost.paid_harnesses_of(), ("deepseek", "gemini"))

    def test_aucun_descripteur_lisible_est_une_erreur(self):
        # fail-closed : sans descripteur, le plafond horaire ne devine pas
        with mock.patch.dict(os.environ, {"AMEESH_HARNESSES_DIR": self.hosts}), \
                mock.patch.object(harnesses, "package_dir", return_value=self.tmp):
            with self.assertRaises(cost.CostError):
                cost.paid_harnesses_of()


# ==========================================================================
# CLI `ameesh harness list|show|check`
# ==========================================================================

class HarnessCliTest(_Tmp):
    def test_list(self):
        proc = self.cli("harness", "list")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for ident in ("claude", "codex", "deepseek"):
            self.assertIn(ident, proc.stdout)
        proc = self.cli("harness", "list", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual({d["id"] for d in data}, {"claude", "codex", "deepseek"})
        self.assertTrue(all(d["source"] == "paquet" for d in data))

    def test_show(self):
        proc = self.cli("harness", "show", "deepseek")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--patch", proc.stdout)
        self.assertIn("empreinte", proc.stdout)
        proc = self.cli("harness", "show", "deepseek", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["descriptor"]["id"], "deepseek")
        self.assertEqual(len(data["sha256"]), 64)
        self.assertEqual(data["source"], "paquet")
        proc = self.cli("harness", "show", "inconnu")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("inconnu", proc.stderr)

    def test_check(self):
        path = self.write_host("faux")
        proc = self.cli("harness", "check", path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("descripteur valide", proc.stdout)
        proc = self.cli("harness", "check", path, "--json")
        data = json.loads(proc.stdout)
        self.assertTrue(data["valid"])
        # un document refusé sort en code 1, sans JSON de succès
        mauvais = os.path.join(self.tmp, "mauvais.json")
        with open(mauvais, "w", encoding="utf-8") as fh:
            json.dump({"id": "x"}, fh)
        os.chmod(mauvais, 0o600)
        proc = self.cli("harness", "check", mauvais, "--json")
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertFalse(data["valid"])
        self.assertTrue(data["findings"])

    def test_list_signale_le_masquage(self):
        doc = descriptor_doc("claude")
        doc["name"] = "Claude local"
        self.write_host("claude", doc)
        proc = self.cli("harness", "list")
        self.assertIn("masque", proc.stderr)

    def test_list_signale_un_dossier_non_prive(self):
        os.chmod(self.hosts, 0o755)
        self.write_host("gemini")
        proc = self.cli("harness", "list")
        self.assertIn("harness-host-insecure", proc.stderr)
        self.assertNotIn("gemini", proc.stdout)


if __name__ == "__main__":
    unittest.main()
