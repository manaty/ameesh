# SPDX-License-Identifier: AGPL-3.0-only
"""Règle de visibilité (lot L31, décision 0029) : accès au dépôt de mémoire de
la persona pour le responsable et les administrateurs de l'hôte, cache lié au
CONTEXTE (dépôt+forge, humains, comptes), forge interrogée explicitement,
fail closed, intégration au verdict de placement."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import canon, visibility

from . import test_canon
from .test_canon import write

REPO = "git@github.com:acme/memoire-orchestre.git"


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def fake_gh(perms, appels=None):
    """`gh api` simulé : {login: permission|None} ; journalise les argv vus."""
    def run(argv, capture_output=True, text=True, timeout=None):
        if appels is not None:
            appels.append(list(argv))
        login = argv[argv.index("--jq") - 1].split("/")[-2]
        permission = perms.get(login)
        if permission is None:
            return FakeProc(1, "")
        return FakeProc(0, permission + "\n")
    return run


def fake_git(reachable):
    return lambda argv, capture_output=True, text=True, timeout=None: \
        FakeProc(0 if reachable else 1, "")


def _replace(path, old, new):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text, (path, old)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


def add_forge(root, name, login):
    _replace(os.path.join(root, "membres", "%s.md" % name),
             "authenticators: []", "authenticators: []\nforge: %s" % login)


def add_memory(root, agent, repo=REPO):
    _replace(os.path.join(root, "agents", "%s.md" % agent),
             "credential_mode:", "memory:\n  mode: neutral\n  repository: %s\n"
             "credential_mode:" % repo)


def add_admin(root, host, human="human:alice"):
    _replace(os.path.join(root, "hotes", "%s.md" % host),
             "policy:", "admins: [%s]\npolicy:" % human)


class ParseTest(unittest.TestCase):
    def test_parse_repository(self):
        cas = {
            "git@forge.example:acme/memoire.git": ("forge.example", "acme", "memoire"),
            "https://forge.example/acme/memoire.git": ("forge.example", "acme", "memoire"),
            "ssh://git@forge.example/acme/memoire": ("forge.example", "acme", "memoire"),
        }
        for url, attendu in cas.items():
            with self.subTest(url=url):
                repo = visibility.parse_repository(url)
                self.assertEqual((repo.forge, repo.owner, repo.name), attendu)
        for mauvais in (None, "", "pas une url", "git@forge:seul"):
            with self.subTest(mauvais=mauvais):
                self.assertIsNone(visibility.parse_repository(mauvais))

    def test_identite_comprend_la_forge(self):
        a = visibility.parse_repository("git@github.com:acme/memoire.git")
        b = visibility.parse_repository("git@forge.example:acme/memoire.git")
        self.assertNotEqual(a.identity, b.identity)


class ForgeTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()
        add_forge(self.root, "alice", "alice-gh")
        add_forge(self.root, "bruno", "bruno-gh")
        add_admin(self.root, "atelier", "human:alice")
        self.canon = canon.load(self.root, untrusted=True)
        self.repo = visibility.parse_repository(REPO)
        self.humans = visibility.host_humans(self.canon, "atelier")
        self.logins = {h: visibility.human_login(self.canon, h) for h in self.humans}

    def test_humains_et_comptes(self):
        self.assertEqual(self.humans, ["human:bruno", "human:alice"])
        self.assertEqual(self.logins, {"human:bruno": "bruno-gh", "human:alice": "alice-gh"})

    def test_hote_sans_responsable_resolu(self):
        add_admin(self.root, "banc", "human:inconnu")
        loaded = canon.load(self.root, untrusted=True)
        self.assertIsNone(visibility.host_humans(loaded, "banc"))
        self.assertIsNone(visibility.host_humans(loaded, "nulle-part"))

    def test_api_admet_les_deux(self):
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "maintain"}))
        visible, why = forge.visible(self.repo, self.humans, self.logins)
        self.assertTrue(visible, why)

    def test_api_refuse_sans_acces(self):
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "none"}))
        visible, why = forge.visible(self.repo, self.humans, self.logins)
        self.assertFalse(visible)
        self.assertIn("alice", why)

    def test_hostname_explicite_independant_de_gh_host(self):
        appels: list = []
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"},
                                                       appels=appels))
        with mock.patch.dict(os.environ, {"GH_HOST": "autre.example"}):
            permission = forge.permission(self.repo, "bruno-gh")
        self.assertEqual(permission, "read")
        argv = appels[0]
        self.assertIn("--hostname", argv)
        self.assertEqual(argv[argv.index("--hostname") + 1], "github.com")
        self.assertNotIn("autre.example", argv)

    def test_forge_non_prise_en_charge(self):
        repo = visibility.parse_repository("git@forge.example:acme/memoire.git")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read"}))
        # plusieurs humains : refus explicite, jamais de repli sur github.com
        visible, why = forge.visible(repo, self.humans, self.logins)
        self.assertFalse(visible)
        self.assertIn("non prise en charge", why)
        # un seul humain : la copie d'essai prévue par 0029 peut prouver l'accès
        forge = visibility.Forge(mode="auto", gh=fake_gh({}), git=fake_git(True))
        visible, why = forge.visible(repo, ["human:bruno"], {"human:bruno": None})
        self.assertTrue(visible, why)

    def test_copie_d_essai_poste_personnel(self):
        forge = visibility.Forge(mode="auto", gh=fake_gh({}), git=fake_git(True))
        visible, why = forge.visible(self.repo, ["human:bruno"], {"human:bruno": None})
        self.assertTrue(visible, why)
        visible, why = forge.visible(self.repo, self.humans,
                                     {"human:bruno": None, "human:alice": None})
        self.assertFalse(visible)

    def test_mode_git_et_desactive(self):
        forge = visibility.Forge(mode="git", git=fake_git(True))
        self.assertTrue(forge.visible(self.repo, ["human:bruno"],
                                      {"human:bruno": None})[0])
        forge = visibility.Forge(mode="none")
        self.assertFalse(forge.visible(self.repo, self.humans, self.logins)[0])


class ContextTest(test_canon._TmpMixin, unittest.TestCase):
    """B1 : l'empreinte du cache couvre tout ce qui détermine le verdict."""

    def setUp(self) -> None:
        self.root = self.example_copy()
        add_forge(self.root, "alice", "alice-gh")
        add_forge(self.root, "bruno", "bruno-gh")
        add_admin(self.root, "atelier", "human:alice")

    def empreinte(self, repo=REPO, forge=None):
        loaded = canon.load(self.root, untrusted=True)
        f = forge or visibility.Forge(mode="gh", hosts=frozenset({"github.com"}))
        return visibility.decision_context(loaded, "atelier", repo, f)["fingerprint"]

    def test_depot_et_forge(self):
        base = self.empreinte()
        self.assertNotEqual(base, self.empreinte("git@github.com:acme/memoire2.git"))
        # même owner/name, forge différente : empreinte différente
        self.assertNotEqual(base, self.empreinte("git@forge.example:acme/memoire-orchestre.git"))

    def test_mode_de_verification(self):
        self.assertNotEqual(self.empreinte(),
                            self.empreinte(forge=visibility.Forge(mode="git")))

    def test_responsable_et_admins(self):
        base = self.empreinte()
        _replace(os.path.join(self.root, "hotes/atelier.md"),
                 "responsible: human:bruno", "responsible: human:alice")
        self.assertNotEqual(base, self.empreinte())
        _replace(os.path.join(self.root, "hotes/atelier.md"),
                 "admins: [human:alice]", "admins: [human:bruno]")
        self.assertNotEqual(base, self.empreinte())

    def test_compte_de_forge(self):
        base = self.empreinte()
        _replace(os.path.join(self.root, "membres/bruno.md"),
                 "forge: bruno-gh", "forge: nouveau-bruno")
        self.assertNotEqual(base, self.empreinte())


class VisibilityDbTest(test_canon._CanonDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM visibility_checks")
        add_forge(self.root, "alice", "alice-gh")
        add_forge(self.root, "bruno", "bruno-gh")
        add_admin(self.root, "atelier", "human:alice")

    def _canon(self):
        return canon.load(self.root, untrusted=True)

    def _check(self, forge):
        return visibility.check(self._canon(), self._canon().agent("orchestre"),
                                "atelier", db=self.db, forge=forge)

    def _orchestre(self):
        return self.db.query("SELECT placement_ok, placement_diagnostic, visibility_ok, "
                             "visibility_diagnostic FROM agent_registry "
                             "WHERE name = 'orchestre'")[0]

    def test_cache_identique_et_expiration(self):
        add_memory(self.root, "orchestre")
        appels: list = []
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}, appels=appels))
        self.assertTrue(self._check(forge).ok)
        self.assertEqual(len(appels), 2)                 # un appel par humain
        self.assertTrue(self._check(forge).ok)
        self.assertEqual(len(appels), 2)                 # cache : aucun nouvel appel
        self.db.execute("UPDATE visibility_checks SET expires_at = now() - interval '1 second'")
        self.assertTrue(self._check(forge).ok)
        self.assertEqual(len(appels), 4)                 # expiré : revérifié

    def test_changement_de_depot_reverifie(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.assertTrue(self._check(forge).ok)
        _replace(os.path.join(self.root, "agents/orchestre.md"),
                 "acme/memoire-orchestre.git", "acme/memoire-autre.git")
        forge_no = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                           "alice-gh": "none"}))
        self.assertFalse(self._check(forge_no).ok)       # l'ancien OK ne vaut pas

    def test_changement_d_admin_illisible_refuse(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.assertTrue(self._check(forge).ok)
        _replace(os.path.join(self.root, "hotes/atelier.md"),
                 "admins: [human:alice]", "admins: [human:inconnu]")
        self.assertFalse(self._check(forge).ok)          # contexte illisible

    def test_changement_de_responsable_reverifie(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.assertTrue(self._check(forge).ok)
        _replace(os.path.join(self.root, "hotes/atelier.md"),
                 "responsible: human:bruno", "responsible: human:alice")
        _replace(os.path.join(self.root, "hotes/atelier.md"),
                 "admins: [human:alice]", "admins: []")
        forge_no = visibility.Forge(mode="gh", gh=fake_gh({"alice-gh": "none"}))
        self.assertFalse(self._check(forge_no).ok)

    def test_changement_de_login_reverifie(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.assertTrue(self._check(forge).ok)
        _replace(os.path.join(self.root, "membres/bruno.md"),
                 "forge: bruno-gh", "forge: nouveau-bruno")
        forge_no = visibility.Forge(mode="gh", gh=fake_gh({"nouveau-bruno": "none",
                                                           "alice-gh": "read"}))
        self.assertFalse(self._check(forge_no).ok)

    def test_depot_illisible_refuse(self):
        add_memory(self.root, "orchestre", repo="pas une url")
        self.assertFalse(self._check(visibility.Forge(mode="gh")).ok)

    def test_sync_rend_visible_et_ecrit_le_verdict(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.sync("atelier", forge=forge)
        row = self._orchestre()
        self.assertIs(row["placement_ok"], True)
        self.assertIs(row["visibility_ok"], True)
        self.assertIn("accès", row["visibility_diagnostic"])

    def test_sync_cache_par_contexte(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.sync("atelier", forge=forge)
        appels: list = []
        compte = visibility.Forge(mode="gh",
                                  gh=fake_gh({"bruno-gh": "read", "alice-gh": "read"},
                                             appels=appels))
        self.sync("atelier", forge=compte)
        self.assertEqual(appels, [])                     # cache : aucun appel

    def test_sync_cache_expire(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                        "alice-gh": "read"}))
        self.sync("atelier", forge=forge)
        self.db.execute("UPDATE visibility_checks SET expires_at = now() - interval '1 second'")
        forge_no = visibility.Forge(mode="gh", gh=fake_gh({"bruno-gh": "read",
                                                           "alice-gh": "none"}))
        self.sync("atelier", forge=forge_no)
        row = self._orchestre()
        self.assertIs(row["placement_ok"], False)
        self.assertIn("persona-hidden-from-host", row["placement_diagnostic"])
        self.assertIs(row["visibility_ok"], False)

    def test_sans_depot_de_memoire_sans_objet(self):
        forge = visibility.Forge(mode="none")
        self.sync("atelier", forge=forge)
        row = self._orchestre()
        self.assertIs(row["placement_ok"], True)
        self.assertIsNone(row["visibility_ok"])

    def test_api_indisponible_refuse(self):
        add_memory(self.root, "orchestre")
        forge = visibility.Forge(mode="gh", gh=fake_gh({}), git=fake_git(True))
        self.sync("atelier", forge=forge)
        row = self._orchestre()
        self.assertIs(row["placement_ok"], False)
        self.assertIn("persona-hidden-from-host", row["placement_diagnostic"])
