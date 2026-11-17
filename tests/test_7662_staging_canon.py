# SPDX-License-Identifier: AGPL-3.0-only
"""#7662 : la séquence staging se lit au canon, jamais dans le cœur.

Les fiches `Host` déclarent ``staging: {order, kind}`` (canon §14) : trois VM
d'agents toujours requises, un serveur sans exécutant et le poste reportables,
le poste en dernier. Un profil nu ne déclare aucun hôte : ni déploiement ni
attestation par défaut.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import canon, release_cutoff

from .test_canon import _TmpMixin, codes, write

HOSTE = """---
type: Host
title: %s
responsible: human:alice
staging:
  order: %d
  kind: %s
---
"""


class SequenceTest(_TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.make_tmp()

    def host(self, title: str, order: int, kind: str) -> None:
        write(self.root, "hotes/%s.md" % title, HOSTE % (title, order, kind))

    def loaded(self):
        return canon.load(self.root, untrusted=True)

    def manaty(self):
        # L'ordre des fichiers ne dit rien : c'est `staging.order` qui classe.
        self.host("pc-smichea", 50, "workstation")
        self.host("manaty-mesh-1", 40, "server")
        self.host("ameesh-agents-2", 20, "agents")
        self.host("ameesh-agents-3", 30, "agents")
        self.host("ameesh-agents-1", 10, "agents")
        return canon.staging_topology([self.loaded()])

    def test_manaty_sequence_is_computed_from_the_host_cards(self):
        topology = self.manaty()
        self.assertEqual(topology.hosts, ("ameesh-agents-1", "ameesh-agents-2",
                                          "ameesh-agents-3", "manaty-mesh-1",
                                          "pc-smichea"))
        self.assertEqual(topology.agents, ("ameesh-agents-1", "ameesh-agents-2",
                                           "ameesh-agents-3"))
        self.assertEqual(topology.server, "manaty-mesh-1")
        self.assertEqual(topology.workstation, "pc-smichea")
        self.assertEqual(topology.vms, ("ameesh-agents-1", "ameesh-agents-2",
                                        "ameesh-agents-3", "manaty-mesh-1"))
        self.assertEqual(topology.reportable, {"manaty-mesh-1", "pc-smichea"})
        # Un poste reporté retire le poste de la preuve, pas une VM d'agents.
        self.assertEqual(topology.required({"pc-smichea"}),
                         ("ameesh-agents-1", "ameesh-agents-2",
                          "ameesh-agents-3", "manaty-mesh-1"))

    def test_an_empty_sequence_is_falsy(self):
        """#7662 : un objet vide ne vaut jamais un hôte dans un `or`."""
        self.assertFalse(canon.StagingTopology())
        self.assertTrue(self.manaty())

    def test_a_bare_canon_declares_no_staging_host_at_all(self):
        topology = canon.staging_topology([self.loaded()])
        self.assertEqual(topology.hosts, ())
        self.assertEqual(topology.agents, ())
        self.assertIsNone(topology.server)
        self.assertIsNone(topology.workstation)
        # Aucune preuve n'est acceptée pour une séquence vide, même complète.
        sha = "a" * 40
        with mock.patch.dict(os.environ, {"HOME": self.root}):
            folder = os.path.dirname(release_cutoff.attestation_path(sha))
            os.makedirs(folder, exist_ok=True)
            with open(release_cutoff.attestation_path(sha), "w",
                      encoding="utf-8") as fh:
                json.dump({"sha": sha, "hotes": {"a1": {"actif": sha}}}, fh)
            self.assertIsNone(
                release_cutoff.read_attestation(sha, topology=topology))
        # Et le déploiement refuse par un nom, avant tout geste.
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=self.root, target="main")
        with self.assertRaisesRegex(release_cutoff.ReleaseError,
                                    "no staging host declared in the canon"):
            release_cutoff.run_staging(project, sha, [], state_dir=self.root)

    def test_another_configured_canon_adds_no_staging_host(self):
        # L42 : seul le canon par défaut (celui du mesh) porte la séquence.
        other_root = self.make_tmp()
        write(other_root, "hotes/autre.md", HOSTE % ("autre", 10, "agents"))
        other = canon.load(other_root, untrusted=True)
        with mock.patch.object(canon, "load_configured",
                               return_value=[self.loaded(), other]):
            topology = release_cutoff.staging_topology_of(mock.Mock())
        self.assertEqual(topology.hosts, ())
        self.assertEqual(topology.agents, ())

    def test_validator_refuses_a_sequence_without_agents_vm(self):
        self.host("manaty-mesh-1", 40, "server")
        self.host("pc-smichea", 50, "workstation")
        self.assertIn("host-staging-no-agents",
                      codes(canon.validate(self.loaded())))

    def test_validator_refuses_duplicate_server_or_workstation(self):
        self.host("a1", 10, "agents")
        self.host("s1", 40, "server")
        self.host("s2", 45, "server")
        self.host("poste", 50, "workstation")
        self.assertIn("host-staging-duplicate",
                      codes(canon.validate(self.loaded())))

    def test_validator_refuses_a_workstation_before_another_host(self):
        self.host("a1", 10, "agents")
        self.host("poste", 20, "workstation")
        self.host("s1", 40, "server")
        findings = canon.validate(self.loaded())
        self.assertIn("host-staging-order", codes(findings))

    def test_the_staging_block_shape_is_validated(self):
        write(self.root, "hotes/ordre.md", """---
type: Host
title: ordre
responsible: human:alice
staging:
  order: -1
  kind: poste
  couleur: rouge
---
""")
        findings = canon.validate(self.loaded())
        self.assertIn("host-staging-invalid", codes(findings))
        self.assertIn("host-staging-unknown", codes(findings, "warning"))

    def test_an_empty_project_sequence_falls_back_to_the_database_canon(self):
        """#7662 B : un projet au profil nu retombe sur le canon de la base."""
        topology = self.manaty()
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=self.root, target="main")
        sha = "a" * 40
        with mock.patch.object(release_cutoff, "topology_for",
                               return_value=topology) as fallback, \
             mock.patch.object(release_cutoff, "read_attestation",
                               return_value={"sha": sha}) as read, \
             mock.patch.object(release_cutoff, "staging_local_host",
                               return_value="ameesh-agents-1"):
            release_cutoff.maybe_staging(project, sha, [], self.root)
        fallback.assert_called_once()
        self.assertIs(read.call_args.args[2], topology)

    def test_the_project_carries_the_canon_sequence(self):
        topology = self.manaty()
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=self.root, target="main")
        cfg = mock.Mock()
        with mock.patch.object(canon, "load_configured",
                               return_value=[self.loaded()]):
            filled = release_cutoff.with_staging_topology(cfg, project)
        self.assertEqual(filled.staging_topology, topology)
        self.assertEqual(project.staging_topology.hosts, ())  # jamais muté


if __name__ == "__main__":
    unittest.main()
