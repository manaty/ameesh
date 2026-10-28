# SPDX-License-Identifier: AGPL-3.0-only
"""L101 T1 — `policy.workspaces` lu au canon, anciennes clés en alias.

Forme canonique : `workspaces: {dirs, roots, root, housekeeping}`. Les clés
historiques `work_dirs`, `work_roots`, `work_root` et `housekeeping` restent
lues (avertissement `host-policy-legacy-key`, sans date de retrait) ; donner
les deux formes pour la même clé est ambigu et refusé (`host-policy-invalid`).
`HostPolicy` garde ses champs historiques : la normalisation est faite au
parse, donc `work_dir` et `housekeeping.effective` ne changent pas.
"""
from __future__ import annotations

import unittest

from ameesh import canon, menage

from . import test_canon
from .test_canon import codes, write

BANC = """---
type: Host
title: banc
responsible: human:alice
policy:
  harnesses: [deepseek, codex]
  providers: [deepseek, openai]
  credential_modes: [api-key]
  max_agents: 2
%s
---

# banc
"""

GIB = 1024 ** 3


class _Canon(test_canon._TmpMixin):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def host(self, extra: str = "") -> None:
        write(self.root, "hotes/banc.md", BANC % extra)

    def loaded(self):
        return canon.load(self.root, untrusted=True)

    def findings(self):
        return canon.validate(self.loaded())


class FormeNeuveTest(_Canon, unittest.TestCase):
    def test_forme_neuve_equivaut_a_l_alias(self):
        """Même HostPolicy par la forme neuve et par l'ancienne clé."""
        cases = (
            ("  workspaces:\n    root: /srv/wt/{agent}",
             "  work_root: /srv/wt/{agent}", lambda policy: policy.work_root),
            ("  workspaces:\n    roots: {acme-web: '/srv/acme-{agent}'}",
             "  work_roots: {acme-web: '/srv/acme-{agent}'}",
             lambda policy: policy.work_roots),
            ("  workspaces:\n    dirs: {ouvrier: /wt/ouvrier}",
             "  work_dirs: {ouvrier: /wt/ouvrier}",
             lambda policy: policy.work_dirs),
            ("  workspaces:\n    housekeeping: {tmp_quota: 1GiB, tmpfs_alert: 0.5}",
             "  housekeeping: {tmp_quota: 1GiB, tmpfs_alert: 0.5}",
             lambda policy: policy.housekeeping),
        )
        for new, old, get in cases:
            with self.subTest(forme=new.splitlines()[-1].strip()):
                self.host(new)
                loaded = self.loaded()
                new_policy = loaded.host("banc").policy
                found = canon.validate(loaded)
                self.assertNotIn("host-policy-legacy-key", codes(found))
                self.assertNotIn("host-policy-invalid", codes(found))

                self.host(old)
                loaded = self.loaded()
                old_policy = loaded.host("banc").policy
                found = canon.validate(loaded)
                self.assertIn("host-policy-legacy-key", codes(found, canon.WARNING))
                self.assertNotIn("host-policy-invalid", codes(found))
                self.assertEqual(get(new_policy), get(old_policy))

    def test_formes_melangees_pour_des_cles_differentes(self):
        """Seule la même clé est ambiguë : mélanger root et work_dirs est permis."""
        self.host("  workspaces:\n    root: /srv/wt\n"
                  "  work_dirs: {ouvrier: /wt/ouvrier}")
        found = self.findings()
        self.assertNotIn("host-policy-invalid", codes(found))
        self.assertIn("host-policy-legacy-key", codes(found, canon.WARNING))
        policy = self.loaded().host("banc").policy
        self.assertEqual(policy.work_root, "/srv/wt")
        self.assertEqual(policy.work_dirs, {"ouvrier": "/wt/ouvrier"})

    def test_gabarit_agent_dans_la_forme_neuve(self):
        self.host("  workspaces:\n    roots: {nexlink: '~/dev/nexlink-{agent}'}\n"
                  "    root: /wt/{agent}")
        host = self.loaded().host("banc")
        self.assertEqual(host.policy.work_dir("nexlink", "codex2"),
                         "~/dev/nexlink-codex2")
        self.assertEqual(host.policy.work_dir("acme", "codex2"), "/wt/codex2/acme")
        # sans agent, un gabarit ne devient jamais un chemin littéral
        self.assertIsNone(host.policy.work_dir("nexlink"))

    def test_gabarit_inconnu_refuse_dans_la_forme_neuve(self):
        self.host("  workspaces:\n    dirs: {ouvrier: '/srv/{projet}'}")
        messages = [f.message for f in self.findings()
                    if f.code == "host-policy-invalid"]
        self.assertTrue(messages, messages)
        self.assertIn("policy.workspaces.dirs.ouvrier", " ".join(messages))
        self.assertIn("unknown template", " ".join(messages))

    def test_menage_effective_lit_la_forme_neuve(self):
        self.host("  workspaces:\n    housekeeping: {tmp_quota: 1GiB, tmpfs_alert: 0.5}")
        loaded = self.loaded()
        self.assertEqual(loaded.host("banc").policy.housekeeping["tmp_quota"], GIB)
        self.assertEqual(menage.effective([loaded], "banc").tmpfs_alert, 0.5)


class ConflitTest(_Canon, unittest.TestCase):
    def test_les_deux_formes_pour_la_meme_cle_sont_refusees(self):
        cases = (
            ("  workspaces: {root: /srv/new}\n  work_root: /srv/old",
             "work_root", "workspaces.root"),
            ("  workspaces: {roots: {acme-web: /srv/new}}\n"
             "  work_roots: {acme-web: /srv/old}", "work_roots", "workspaces.roots"),
            ("  workspaces: {dirs: {ouvrier: /wt/new}}\n"
             "  work_dirs: {ouvrier: /wt/old}", "work_dirs", "workspaces.dirs"),
            ("  workspaces: {housekeeping: {tmp_quota: 1GiB}}\n"
             "  housekeeping: {tmp_quota: 2GiB}", "housekeeping",
             "workspaces.housekeeping"),
        )
        for extra, attr, path in cases:
            with self.subTest(cle=attr):
                self.host(extra)
                found = self.findings()
                errors = [f for f in found if f.code == "host-policy-invalid"
                          and "both declare" in f.message]
                self.assertEqual(len(errors), 1, [f.message for f in found])
                self.assertIn(path, errors[0].message)
                self.assertNotIn("host-policy-legacy-key", codes(found))
                # fail closed : la valeur ambiguë n'est pas retenue
                self.assertIsNone(getattr(self.loaded().host("banc").policy, attr))

    def test_workspaces_non_mapping(self):
        self.host("  workspaces: [dirs]")
        found = self.findings()
        self.assertIn("host-policy-invalid", codes(found))
        self.assertIn("`policy.workspaces`: mapping expected",
                      " ".join(f.message for f in found))

    def test_cle_inconnue_dans_workspaces(self):
        self.host("  workspaces: {dir: /wt}")
        found = self.findings()
        self.assertIn("host-workspaces-unknown", codes(found, canon.WARNING))
        self.assertIn("`policy.workspaces.dir`",
                      " ".join(f.message for f in found))
