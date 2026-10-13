# SPDX-License-Identifier: AGPL-3.0-only
"""#953 (R5-IMAGE-APPROVE) : l'image approve se publie sous des regles etroites.

Tests sans base ni reseau : ils lisent le workflow, le Containerfile et la
politique ecrite. Ils verrouillent ce que l'arbitrage du pilote du 13/10/2026
et le canon §3 imposent : ni PR ni fourche, runner auto-heberge, permissions
minimales, delai maximal, tag immuable sha-<12>, tag de version seulement sur
main, conteneur non-root et sans secret.
"""
from __future__ import annotations

import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "image-approve.yml"
CONTAINERFILE = REPO / "deploy" / "approve-image" / "Containerfile"
POLICY = REPO / "docs" / "IMAGE-APPROVE.md"


class WorkflowTest(unittest.TestCase):
    """Declencheurs etroits, droits minimaux, preuve par digest."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_aucun_declencheur_de_pr_ni_runner_heberge(self):
        self.assertNotIn("pull_request", self.text)
        # canon §3 : nos runners auto-heberges, aucun runner heberge payant
        for heberge in ("ubuntu-", "macos-", "windows-"):
            self.assertNotIn(heberge, self.text)

    def test_declencheurs_du_lot_seulement(self):
        self.assertIn('branches: ["lot/953-image-approve"]', self.text)
        self.assertIn("workflow_dispatch:", self.text)

    def test_fourche_ignoree(self):
        self.assertIn("github.repository == 'manaty/ameesh'", self.text)

    def test_runner_auto_heberge(self):
        self.assertIn("runs-on: [self-hosted, linux, ameesh-ci]", self.text)

    def test_permissions_minimales(self):
        self.assertIn("contents: read", self.text)
        self.assertIn("packages: write", self.text)
        self.assertNotIn("contents: write", self.text)
        # le jeton du depot, jamais un jeton personnel
        self.assertIn("secrets.GITHUB_TOKEN", self.text)

    def test_chaque_job_declare_un_delai(self):
        self.assertEqual(self.text.count("runs-on:"),
                         self.text.count("timeout-minutes:"))

    def test_tag_immuable_sha12(self):
        self.assertIn("cut -c1-12", self.text)
        self.assertIn("sha-${{ steps.meta.outputs.sha12 }}", self.text)
        self.assertIn("jamais reecrit", self.text)
        self.assertIn("push.log", self.text)

    def test_tag_de_version_reserve_a_main(self):
        self.assertIn("refs/heads/main)", self.text)
        self.assertIn("refs/heads/lot/*)", self.text)
        self.assertIn("publish_version=1", self.text)
        self.assertIn("publish_version=0", self.text)
        self.assertIn("PUBLISH_VERSION: ${{ steps.meta.outputs.publish_version }}",
                      self.text)

    def test_digest_verifie_et_publie(self):
        self.assertIn("docker manifest inspect", self.text)
        self.assertIn('"$IMAGE@$digest"', self.text)
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)


class ContainerfileTest(unittest.TestCase):
    """Le conteneur : non-root, sans secret, contrat du service explicite."""

    @classmethod
    def setUpClass(cls):
        cls.text = CONTAINERFILE.read_text(encoding="utf-8")

    def test_non_root_et_entree(self):
        self.assertIn("USER 10002:10002", self.text)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--", "ameesh-approve"]',
                      self.text)
        self.assertIn('CMD ["serve"]', self.text)

    def test_aucun_secret_dans_l_image(self):
        self.assertIn("AUCUN SECRET", self.text)
        # seules les instructions comptent : un commentaire peut nommer le DSN
        corps = "\n".join(ligne for ligne in self.text.splitlines()
                          if not ligne.lstrip().startswith("#"))
        for interdit in ("AMEESH_DSN", "PASSWORD", "TOKEN="):
            self.assertNotIn(interdit, corps)

    def test_contrat_du_service(self):
        self.assertIn("AMEESH_APPROVE_CONFIG=/etc/ameesh-approve/config.json",
                      self.text)
        self.assertIn("AMEESH_APPROVE_STATE=/var/lib/ameesh-approve", self.text)
        self.assertIn("AMEESH_APPROVE_BIND=127.0.0.1", self.text)
        self.assertIn("EXPOSE 8765", self.text)
        self.assertIn(
            'org.opencontainers.image.source="https://github.com/manaty/ameesh"',
            self.text)

    def test_construite_depuis_la_source(self):
        self.assertIn("COPY src ./src", self.text)
        self.assertIn("pip wheel", self.text)


class PolicyTest(unittest.TestCase):
    """La politique est ecrite : registre, tags, digest, retour arriere."""

    def test_politique_ecrite(self):
        text = POLICY.read_text(encoding="utf-8")
        for attendu in ("ghcr.io/manaty/ameesh-approve", "sha-<12>", "digest",
                        "Retour arrière", "packages: write", "ghcr"):
            self.assertIn(attendu, text)


if __name__ == "__main__":
    unittest.main()
