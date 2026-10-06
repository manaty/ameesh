# SPDX-License-Identifier: AGPL-3.0-only
"""Admission (ex-placement) au canon et règles de placement (lot L31, 0029) :
hôtes admis ou étiquettes d'hôtes, racines de travail de l'hôte, lecture
transitoire de l'ancien `cwd`, et « ne jamais déplacer un agent dont l'hôte
courant est admis »."""
from __future__ import annotations

import os
import unittest

from ameesh import canon, placement, registry

from . import test_canon
from .test_canon import write

ATELIER = """---
type: Host
title: atelier
responsible: human:bruno
%s
policy:
  harnesses: [claude, codex, deepseek]
  providers: [anthropic, openai, deepseek]
  credential_modes: [api-key, subscription]
  max_agents: 4
%s
---

# atelier
"""

BANC = """---
type: Host
title: banc
responsible: human:alice
%s
policy:
  harnesses: [deepseek, codex]
  providers: [deepseek, openai]
  credential_modes: [api-key]
  max_agents: 2
%s
---

# banc
"""


def set_host(root, name, body, tags="", work="  work_roots: {acme-web: /srv/acme/acme-web}"):
    tags_line = "tags: [%s]\n" % ", ".join(tags) if tags else ""
    write(root, "hotes/%s.md" % name, body % (tags_line, work))


class AdmissionUnitTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def codes(self, loaded, severity=None):
        return [f.code for f in canon.validate(loaded) if severity is None or f.severity == severity]

    def admission(self, agent, corps):
        write(self.root, "placements/ouvrier-banc.md", corps)

    def test_admission_multihotes(self):
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "hosts: [atelier, banc]\n---\n")
        loaded = self.load()
        adm = placement.admission_of(loaded, "ouvrier")
        self.assertEqual(placement.admitted_hosts(loaded, adm), ["atelier", "banc"])
        self.assertTrue(placement.evaluate(loaded, "ouvrier", "atelier").ok)
        self.assertTrue(placement.evaluate(loaded, "ouvrier", "banc").ok)

    def test_admission_par_etiquette(self):
        set_host(self.root, "banc", BANC, tags=["test", "partage"])
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "host_tags: [test]\n---\n")
        loaded = self.load()
        adm = placement.admission_of(loaded, "ouvrier")
        self.assertEqual(placement.admitted_hosts(loaded, adm), ["banc"])
        self.assertTrue(placement.evaluate(loaded, "ouvrier", "banc").ok)
        self.assertFalse(placement.evaluate(loaded, "ouvrier", "atelier").ok)

    def test_etiquette_inconnue_avertissement(self):
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "host_tags: [nulle-part]\n---\n")
        self.assertIn("admission-tag-unknown", self.codes(self.load(), canon.WARNING))

    def test_hote_inconnu_erreur(self):
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "hosts: [nulle-part]\n---\n")
        self.assertIn("placement-host-unknown", self.codes(self.load(), canon.ERROR))

    def test_admission_sans_cible_erreur(self):
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "credential_mode: api-key\n---\n")
        self.assertIn("placement-incomplete", self.codes(self.load(), canon.ERROR))

    def test_cwd_ancien_ignore(self):
        self.admission("ouvrier", "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
                                  "hosts: [banc]\ncwd: /srv/acme/ouvrier\n---\n")
        self.assertIn("admission-cwd-ignored", self.codes(self.load(), canon.WARNING))

    def test_racine_de_travail_par_defaut(self):
        from ameesh import canon as canon_mod
        policy = canon_mod.HostPolicy(work_root="/srv/wt")
        self.assertEqual(policy.work_dir("acme-web"), "/srv/wt/acme-web")
        self.assertEqual(policy.work_dir(None), "/srv/wt")
        policy = canon_mod.HostPolicy(work_roots={"acme-web": "/x/acme"})
        self.assertEqual(policy.work_dir("acme-web"), "/x/acme")
        self.assertIsNone(canon_mod.HostPolicy().work_dir("acme-web"))


class _Visible:
    """Façade de forge de test : tout le monde a accès."""

    mode = "gh"
    hosts = frozenset({"github.com"})

    def visible(self, repo, humans, logins):
        return True, "ok"


class AdmissionDbTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM visibility_checks")

    def test_sync_ecrit_admission_et_depot_de_memoire(self):
        chemin = os.path.join(self.root, "agents/ouvrier.md")
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        write(self.root, "agents/ouvrier.md", texte.replace(
            "credential_mode: api-key",
            "credential_mode: api-key\nmemory:\n  mode: neutral\n"
            "  repository: git@forge.example:equipe/ouvrier.git"))
        write(self.root, "placements/ouvrier-banc.md",
              "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
              "hosts: [banc]\nhost_tags: [test]\ncredential_mode: api-key\n---\n")
        set_host(self.root, "banc", BANC, tags=["test"])
        report = self.sync("banc", forge=_Visible())
        self.assertEqual({a.agent: a.action for a in report.actions}["ouvrier"], "créé")
        row = registry.get(self.db, "ouvrier")
        self.assertEqual(row["admitted_hosts"], ["banc"])
        self.assertEqual(row["admitted_tags"], ["test"])
        self.assertEqual(row["memory_repository"], "git@forge.example:equipe/ouvrier.git")
        self.assertEqual(row["cwd"], "/srv/acme/acme-web")   # work_roots de l'hôte
        self.assertEqual(row["host"], "banc")
        # inchangé au passage suivant
        self.assertEqual({a.agent: a.action for a in self.sync("banc", forge=_Visible())
                          .actions}["ouvrier"], "inchangé")

    def test_ne_deplace_pas_un_agent_dont_l_hote_courant_est_admis(self):
        write(self.root, "placements/ouvrier-banc.md",
              "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
              "hosts: [atelier, banc]\ncredential_mode: api-key\n---\n")
        self.sync("atelier")                          # ouvrier admis sur atelier, pas encore de ligne
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")
        report = self.sync("banc")                    # autre hôte admis : on ne bouge pas
        self.assertEqual({a.agent: a.action for a in report.actions}["ouvrier"], "laissé")
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")

    def test_deplace_vers_un_autre_hote_admis(self):
        write(self.root, "placements/ouvrier-banc.md",
              "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
              "hosts: [atelier]\ncredential_mode: api-key\n---\n")
        self.sync("atelier")
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")
        write(self.root, "placements/ouvrier-banc.md",
              "---\ntype: Placement\ntitle: ouvrier\nagent: ouvrier\n"
              "hosts: [banc]\ncredential_mode: api-key\n---\n")
        report = self.sync("atelier")
        self.assertEqual({a.agent: a.action for a in report.actions}["ouvrier"], "déplacé")
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "banc")
