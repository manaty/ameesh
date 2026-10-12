# SPDX-License-Identifier: AGPL-3.0-only
"""L243 (#468): the default name of a new agent is `<org>-<repo>-<role>-<seq>`.

The segments come from `extensions.ameesh.naming` in the canon's
federation.yaml. `ameesh canon agent name --role R [--project P …]` prints the
next free name, checking every card of every configured canon and every
registry row (stopped agents included, so a number is never reused). A card
whose title does not follow the convention gets an `agent-name-convention`
warning when the canon declares a naming block, and nothing changes without
one. `canon agent spawn` uses the same generator when no name is given.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import canon, registry

from .test_canon import (_CanonDbCase, _TmpMixin, codes, commit_all, fiche, git,
                         publish, write)

NAMING = ("extensions:\n"
          "  ameesh:\n"
          "    naming:\n"
          "      org: mty\n"
          "      projects:\n"
          "        acme-web: amh\n"
          "        acme-api: api\n"
          "      roles:\n"
          "        implementer: impl\n"
          "        conductor: conc\n")

FEDERATION = "id: acme\nroot: acme\nmembers: []\n"


def card(title: str, **fields) -> str:
    values = dict(type="Agent", title=title, responsible="human:alice",
                  capabilities=["read", "report-drift", "propose"], harness="claude",
                  model="claude-opus", provider="anthropic",
                  credential_mode="subscription")
    values.update(fields)
    return fiche(**{k: v for k, v in values.items() if v is not None})


class NamingConfigTest(_TmpMixin, unittest.TestCase):

    def check(self, root: str):
        loaded = canon.load(root, untrusted=True)
        return loaded, canon.validate(loaded)

    def assertFinding(self, findings, code, severity=canon.ERROR, **subject):
        matches = [f for f in findings if f.code == code and f.severity == severity
                   and all(getattr(f, k) == v for k, v in subject.items())]
        self.assertTrue(matches, "%s absent from %s" % (code, findings))
        return matches

    def test_naming_read_from_the_federation(self):
        root = self.example_copy()
        write(root, "federation.yaml", FEDERATION + NAMING)
        loaded, findings = self.check(root)
        self.assertEqual(codes(findings, canon.ERROR), set(), findings)
        # les noms existants (orchestre, ouvrier, relecteur) restent muets : ils
        # ne tentent pas la convention, #423 les renommera
        self.assertNotIn("agent-name-convention", codes(findings))
        self.assertEqual(loaded.naming, canon.Naming(
            org="mty", projects={"acme-web": "amh", "acme-api": "api"},
            roles={"implementer": "impl", "conductor": "conc"}))
        shown = loaded.to_dict()["federation"]["naming"]
        self.assertEqual(shown["org"], "mty")
        self.assertEqual(shown["projects"], {"acme-web": "amh", "acme-api": "api"})
        # un canon sans bloc `naming` : aucune convention, aucun constat
        write(root, "federation.yaml", FEDERATION)
        loaded, findings = self.check(root)
        self.assertIsNone(loaded.naming)
        self.assertEqual(codes(findings), {"canon-untrusted"}, findings)

    def test_naming_invalid_is_an_error_and_generates_nothing(self):
        root = self.example_copy()
        cases = [
            "      org: MTY\n",                                    # majuscules
            "      org: m\n",                                       # trop court
            "      org: mtytoolong\n",                              # trop long
            "      org: mty\n      projects: {acme-web: toolongseg}\n",
            # deux dépôts ne partagent jamais un segment
            "      org: mty\n      projects: {acme-web: amh, acme-api: amh}\n",
            "      org: mty\n      roles: [implementer]\n",         # pas un mapping
            "      org: mty\n      colours: {red: rd}\n",           # clé inconnue
            "      projects: {acme-web: amh}\n",                    # org absent
        ]
        for block in cases:
            write(root, "federation.yaml",
                  FEDERATION + "extensions:\n  ameesh:\n    naming:\n" + block)
            loaded, findings = self.check(root)
            self.assertIsNone(loaded.naming, block)
            found = self.assertFinding(findings, "naming-invalid")
            self.assertIn("naming", found[0].message)
            self.assertTrue(found[0].message, block)

    def test_naming_segment_refuses_a_trailing_newline(self):
        """Codex r1 (#452, P2) : `$` laissait passer `"mty\\n"`."""
        root = self.example_copy()
        cases = [
            "      org: \"mty\\n\"\n",
            "      org: mty\n      projects: {acme-web: \"amh\\n\"}\n",
            "      org: mty\n      roles: {implementer: \"impl\\n\"}\n",
        ]
        for block in cases:
            write(root, "federation.yaml",
                  FEDERATION + "extensions:\n  ameesh:\n    naming:\n" + block)
            loaded, findings = self.check(root)
            self.assertIsNone(loaded.naming, block)
            self.assertFinding(findings, "naming-invalid")
        # et un segment valide reste accepté (le contrôle n'est pas trop strict)
        write(root, "federation.yaml", FEDERATION + NAMING)
        loaded, findings = self.check(root)
        self.assertEqual(canon.errors(findings), [], findings)
        self.assertEqual(loaded.naming.org, "mty")

    def test_explicit_naming_null_is_invalid(self):
        """Codex r1 (#452, P2) : `naming:` (null) était traité comme absent."""
        root = self.example_copy()
        for value in ("naming:\n", "naming: null\n", "naming: ~\n"):
            write(root, "federation.yaml",
                  FEDERATION + "extensions:\n  ameesh:\n    " + value)
            loaded, findings = self.check(root)
            self.assertIsNone(loaded.naming, value)
            found = self.assertFinding(findings, "naming-invalid")
            self.assertIn("mapping expected", found[0].message)

    def test_naming_is_a_root_key_only(self):
        """`naming` dans l'entrée d'un membre n'a pas de sens : ignoré, dit."""
        root = self.example_copy()
        write(root, "federation.yaml",
              "id: acme\nroot: acme\nmembers:\n  - id: home\nextensions:\n"
              "  ameesh:\n    members:\n      home:\n        naming:\n          org: mty\n")
        loaded, findings = self.check(root)
        self.assertIsNone(loaded.naming)
        self.assertIn("ameesh-key-unknown", codes(findings, canon.WARNING))

    def test_agent_name_convention_is_a_warning(self):
        root = self.example_copy()
        write(root, "federation.yaml", FEDERATION + NAMING)
        loaded, findings = self.check(root)
        # les noms existants hors convention (orchestre, ouvrier, relecteur)
        # restent muets tant que #423 ne les a pas renommés
        self.assertNotIn("agent-name-convention", codes(findings))
        # un titre qui TENTE la convention et la rate (préfixe de l'org) est dit
        write(root, "agents/mty-amh-impl-1.md", card("mty-amh-impl-1"))
        loaded, findings = self.check(root)
        old = self.assertFinding(findings, "agent-name-convention", canon.WARNING,
                                 agent="mty-amh-impl-1")
        self.assertIn("mty-<repo>-<role>-01", old[0].message)
        # le nom engendré par la convention ne dit rien
        write(root, "agents/mty-amh-impl-1.md", None)
        write(root, "agents/mty-amh-impl-01.md", card("mty-amh-impl-01"))
        loaded, findings = self.check(root)
        self.assertNotIn("agent-name-convention", codes(findings))
        # sans bloc `naming`, même un titre préfixé par l'org reste muet
        write(root, "federation.yaml", FEDERATION)
        write(root, "agents/mty-amh-impl-1.md", card("mty-amh-impl-1"))
        _loaded, findings = self.check(root)
        self.assertNotIn("agent-name-convention", codes(findings))


class NamingGeneratorTest(unittest.TestCase):

    def naming(self, **overrides) -> canon.Naming:
        values = dict(org="mty", projects={"acme-web": "amh", "acme-api": "api"},
                      roles={"implementer": "impl", "conductor": "conc"})
        values.update(overrides)
        return canon.Naming(**values)

    def test_single_project_name_and_free_number(self):
        naming = self.naming()
        self.assertEqual(naming.next_name("implementer", ["acme-web"], []), "mty-amh-impl-01")
        self.assertEqual(
            naming.next_name("implementer", ["acme-web"], ["mty-amh-impl-01"]),
            "mty-amh-impl-02")
        # un trou n'est pas comblé tant que le numéro est réservé
        self.assertEqual(
            naming.next_name("implementer", ["acme-web"],
                             ["mty-amh-impl-01", "mty-amh-impl-03"]),
            "mty-amh-impl-02")
        # le préfixe compte : un autre dépôt garde ses propres numéros
        self.assertEqual(
            naming.next_name("implementer", ["acme-api"], ["mty-amh-impl-01"]),
            "mty-api-impl-01")
        # deux chiffres, puis trois après 99
        self.assertEqual(
            naming.next_name("implementer", ["acme-web"],
                             ["mty-amh-impl-%02d" % n for n in range(1, 100)]),
            "mty-amh-impl-100")

    def test_multi_project_name_drops_the_repo(self):
        naming = self.naming()
        self.assertEqual(naming.next_name("conductor", ["acme-web", "acme-api"], []),
                         "mty-conc-01")
        self.assertEqual(naming.next_name("conductor", ["acme-api", "acme-web"], []),
                         "mty-conc-01")
        self.assertEqual(naming.next_name("conductor", ["acme-web", "acme-api"],
                                          ["mty-conc-01", "mty-conc-02"]),
                         "mty-conc-03")

    def test_project_without_short_name_is_truncated(self):
        naming = self.naming(projects={})
        self.assertEqual(naming.next_name("implementer", ["catalogue"], []),
                         "mty-catalo-impl-01")
        # le nom tronqué doit rester un segment : sinon on refuse au lieu de deviner
        with self.assertRaises(canon.NamingError):
            naming.next_name("implementer", ["acme-web"], [])
        # une troncature qui rejoindrait le nom court d'un AUTRE dépôt est
        # refusée, en demandant le nom court à déclarer
        clash = self.naming(projects={"acme-api": "catalo"})
        with self.assertRaises(canon.NamingError) as caught:
            clash.next_name("implementer", ["catalogue"], [])
        self.assertIn("naming.projects.catalogue", str(caught.exception))

    def test_truncation_clash_between_undeclared_repos_is_refused(self):
        """Codex r1 (#452, P2) : `catalogue` et `catalogapi` donnent `catalo`."""
        naming = self.naming(projects={})
        # sans la liste des dépôts connus, la troncature reste permise
        self.assertEqual(naming.next_name("implementer", ["catalogue"], []),
                         "mty-catalo-impl-01")
        # connus (fiches, demande), deux dépôts qui tronquent pareil : refus ;
        # `known` porte le segment de chaque dépôt (codex r2, #452)
        with self.assertRaises(canon.NamingError) as caught:
            naming.next_name("implementer", ["catalogue"], [],
                             known={"catalogapi": "catalo"})
        self.assertIn("catalogapi", str(caught.exception))
        self.assertIn("naming.projects.catalogue", str(caught.exception))
        # le nom court déclaré de l'autre dépôt lève l'ambiguïté
        declared = canon.Naming(org="mty", projects={"catalogapi": "capi"},
                                roles={"implementer": "impl"})
        self.assertEqual(declared.next_name("implementer", ["catalogue"], []),
                         "mty-catalo-impl-01")
        self.assertEqual(declared.next_name("implementer", ["catalogapi"], []),
                         "mty-capi-impl-01")

    def test_role_and_projects_are_never_guessed(self):
        naming = self.naming()
        with self.assertRaises(canon.NamingError):
            naming.next_name("unknown", ["acme-web"], [])
        with self.assertRaises(canon.NamingError):
            naming.next_name("implementer", [], [])
        with self.assertRaises(canon.NamingError):
            naming.next_name("implementer", ["acme-web", "acme-web"], [])

    def test_conforms(self):
        naming = self.naming()
        self.assertTrue(naming.conforms("mty-amh-impl-01"))
        self.assertTrue(naming.conforms("mty-conc-100"))
        self.assertFalse(naming.conforms("mty-amh-impl-1"))
        self.assertFalse(naming.conforms("mty-amh-unknown-01"))
        self.assertFalse(naming.conforms("ameesh-dev3"))
        self.assertFalse(naming.conforms("acme-mty-amh-impl-01"))


class NamingCliTest(_CanonDbCase):

    def cli_env(self, **extra) -> dict:
        return self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1", **extra)

    def naming(self) -> None:
        write(self.root, "federation.yaml", FEDERATION + NAMING)

    def test_name_lists_cards_and_the_whole_registry(self):
        self.naming()
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-01")
        # une fiche réserve le nom, même hors convention de validation
        write(self.root, "agents/mty-amh-impl-01.md", card("mty-amh-impl-01"))
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-02")
        # un agent arrêté reste réservé
        registry.upsert(self.db, "mty-amh-impl-02", harness="claude", host="atelier",
                        cwd="/tmp/impl-02")
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                        ("mty-amh-impl-02",))
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-03")
        # un agent d'un autre préfixe ne consomme pas le numéro
        registry.upsert(self.db, "mty-amh-conc-01", harness="claude", host="atelier",
                        cwd="/tmp/conc-01")
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-03")
        # plusieurs projets : le segment de dépôt disparaît, un seul numéro par préfixe
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", "--project", "acme-api", "--json",
                         env=self.cli_env())
        data = json.loads(proc.stdout)
        self.assertEqual(data["name"], "mty-impl-01")
        self.assertEqual(data["org"], "mty")
        self.assertEqual(data["projects"], ["acme-web", "acme-api"])

    def test_name_refusals(self):
        self.naming()
        # rôle sans nom court : jamais deviné
        proc = self.mesh("agent", "name", "--role", "unknown",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no short name", proc.stderr)
        # aucun projet : pas de nom
        proc = self.mesh("agent", "name", "--role", "implementer",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("at least one project", proc.stderr)
        # projet sans nom court utilisable : refusé, pas deviné
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme web", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no usable short name", proc.stderr)
        # sans bloc `naming`
        write(self.root, "federation.yaml", FEDERATION)
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("extensions.ameesh.naming", proc.stderr)

    def test_check_warns_without_blocking(self):
        self.naming()
        # un titre préfixé par l'org qui rate la convention est un avertissement
        write(self.root, "agents/mty-amh-impl-1.md", card("mty-amh-impl-1"))
        proc = self.mesh("canon", "check", "--json", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        convention = [f for f in data["findings"] if f["code"] == "agent-name-convention"]
        self.assertTrue(convention)
        self.assertEqual({f["severity"] for f in convention}, {"warning"})

    def test_spawn_uses_the_generator_when_no_name(self):
        self.naming()
        self.sync()
        proc = self.mesh("agent", "spawn", "--by", "orchestre", "--ttl", "30m",
                         "--role", "implementer", "--project", "acme-web",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mty-amh-impl-01", proc.stdout)
        row = registry.get(self.db, "mty-amh-impl-01")
        self.assertTrue(row["ephemeral"])
        self.assertEqual(row["created_by"], "orchestre")
        # le nom engendré est réservé le tour suivant
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-02")
        # un nom explicite reste possible, seul
        proc = self.mesh("agent", "spawn", "aide", "--by", "orchestre", "--ttl", "30m",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(registry.get(self.db, "aide"))
        # nom ET rôle : ambigu, refusé avant toute écriture
        proc = self.mesh("agent", "spawn", "aide2", "--by", "orchestre", "--ttl", "30m",
                         "--role", "implementer", "--project", "acme-web",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused", proc.stderr)
        self.assertIsNone(registry.get(self.db, "aide2"))
        # sans nom ni rôle : refusé
        proc = self.mesh("agent", "spawn", "--by", "orchestre", "--ttl", "30m",
                         env=self.cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--role", proc.stderr)

    def test_agent_group_is_reachable_under_canon_too(self):
        """Codex r1 (#452, P2) : `canon agent name|spawn` sortait en code 2."""
        self.naming()
        self.sync()
        proc = self.mesh("canon", "agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-01")
        proc = self.mesh("canon", "agent", "spawn", "--by", "orchestre",
                         "--ttl", "30m", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mty-amh-impl-01", proc.stdout)
        # le chemin historique reste branché, sur le même générateur
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-02")

    def test_canon_dir_keeps_the_other_canons_reservations(self):
        """Codex r1 (#452, P2) : `--canon DOSSIER` perdait les réservations."""
        self.naming()
        other = self.example_copy()
        write(other, "federation.yaml", "id: acme-other\nroot: acme\nmembers: []\n")
        write(other, "agents/employe-01.md", card("mty-amh-impl-01"))
        env = self.cli_env(AMEESH_CANONS="%s:%s" % (self.root, other))
        proc = self.mesh("agent", "name", "--canon", self.root, "--role",
                         "implementer", "--project", "acme-web", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-02")

    def test_canon_dir_reads_the_configured_revision(self):
        """Codex r2 (#452, P2) : `--canon DOSSIER` perdait la révision configurée.

        B est configuré sur `origin/stable`, qui réserve `mty-amh-impl-01` ;
        `origin/main`, la révision que lit un dossier nu, ne le connaît pas.
        Dédupliquer les canons par dossier seul perdait cette réservation."""
        workspace = self.make_tmp()
        _bare_a, clone_a = publish(workspace, "a", None)
        _bare_b, clone_b = publish(workspace, "b", None)
        write(clone_b, "federation.yaml",
              "id: acme-other\nroot: acme\nmembers: []\n" + NAMING)
        commit_all(clone_b)                     # origin/main : le nommage, sans la fiche
        git(clone_b, "checkout", "-q", "-b", "stable")
        write(clone_b, "agents/mty-amh-impl-01.md", card("mty-amh-impl-01"))
        commit_all(clone_b)                     # origin/stable : + la réservation
        git(clone_b, "checkout", "-q", "main")
        config = os.path.join(workspace, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"canons": [
                {"path": clone_a, "untrusted": True},
                {"path": clone_b, "ref": "origin/stable", "untrusted": True}]}, fh)
        proc = self.mesh("agent", "name", "--canon", clone_b, "--role", "implementer",
                         "--project", "acme-web", env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-amh-impl-02")

    def test_another_canons_declared_segment_counts(self):
        """Codex r2 (#452, P2) : comparer les SEGMENTS des autres canons, pas
        leurs noms de dépôts."""
        self.naming()
        other = self.example_copy()
        write(other, "federation.yaml",
              "id: acme-other\nroot: acme\nmembers: []\n"
              "extensions:\n  ameesh:\n    naming:\n      org: oth\n"
              "      projects:\n        otherrepo: catalo\n")
        env = self.cli_env(AMEESH_CANONS="%s:%s" % (self.root, other))
        # B donne `catalo` à `otherrepo` : le `catalogue` non déclaré de A
        # porterait le segment d'un AUTRE dépôt -> refus, même si les noms
        # ne se ressemblent pas
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "catalogue", env=env)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("otherrepo", proc.stderr)
        self.assertIn("naming.projects.catalogue", proc.stderr)
        # B donne `capi` à `catalogapi` : aucun segment commun avec le
        # `catalo` de `catalogue`, malgré des noms qui se ressemblent
        write(other, "federation.yaml",
              "id: acme-other\nroot: acme\nmembers: []\n"
              "extensions:\n  ameesh:\n    naming:\n      org: oth\n"
              "      projects:\n        catalogapi: capi\n")
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "catalogue", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-catalo-impl-01")

    def test_truncation_clash_with_a_card_project_is_refused(self):
        """Codex r1 (#452, P2) : deux dépôts non déclarés, même troncature."""
        self.naming()
        write(self.root, "agents/employe-catalogue.md",
              card("employe-catalogue", projects=["catalogue"]))
        write(self.root, "agents/employe-api.md",
              card("employe-api", projects=["catalogapi"]))
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "catalogue", env=self.cli_env())
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("naming.projects.catalogue", proc.stderr)
        # déclarer le nom court de l'AUTRE dépôt lève le refus
        write(self.root, "federation.yaml",
              FEDERATION + NAMING.replace(
                  "      projects:\n", "      projects:\n        catalogapi: capi\n"))
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "catalogue", env=self.cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "mty-catalo-impl-01")

    def test_unreadable_canon_refuses_a_generated_name(self):
        """Codex r3 (#452, P2) : un canon configuré mais ILLISIBLE (révision
        jamais récupérée) passait en silence — ses réservations étaient
        ignorées et un nom déjà pris pouvait être proposé."""
        self.naming()
        workspace = self.make_tmp()
        _bare_b, clone_b = publish(workspace, "b", None)
        write(clone_b, "federation.yaml",
              "id: acme-other\nroot: acme\nmembers: []\n" + NAMING)
        write(clone_b, "agents/mty-amh-impl-01.md", card("mty-amh-impl-01"))
        commit_all(clone_b)                     # origin/main : + la réservation
        config = os.path.join(workspace, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"canons": [
                {"path": self.root, "untrusted": True},
                {"path": clone_b, "ref": "origin/stable", "untrusted": True}]}, fh)
        # `origin/stable` n'a jamais été poussé : B est illisible, ses
        # réservations sont inconnues -> refus, jamais un nom « libre » supposé
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web", env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn(clone_b, proc.stderr)
        self.assertIn("origin/stable", proc.stderr)
        # `spawn` sans nom construit sa propre liste de canons : même refus,
        # et rien n'est écrit au registre
        proc = self.mesh("agent", "spawn", "--by", "orchestre", "--ttl", "30m",
                         "--role", "implementer", "--project", "acme-web",
                         env=self.env(AMEESH_CONFIG=config))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("origin/stable", proc.stderr)
        self.assertIsNone(registry.get(self.db, "mty-amh-impl-01"))
        # le canon CHOISI peut être l'illisible : même refus nommé, et non un
        # « pas de bloc naming » qui enverrait déclarer une convention déjà là
        config_b_first = os.path.join(workspace, "config-b-first.json")
        with open(config_b_first, "w", encoding="utf-8") as fh:
            json.dump({"canons": [
                {"path": clone_b, "ref": "origin/stable", "untrusted": True},
                {"path": self.root, "untrusted": True}]}, fh)
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "acme-web",
                         env=self.env(AMEESH_CONFIG=config_b_first))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(clone_b, proc.stderr)
        self.assertIn("origin/stable", proc.stderr)
        self.assertNotIn("no `extensions.ameesh.naming`", proc.stderr)

    def test_declared_segments_never_collide_across_canons(self):
        """Codex r3 (#452, P2) : les projets DÉCLARÉS échappaient au contrôle de
        collision — `catalogue: catalo` ici, `otherrepo: catalo` ailleurs."""
        self.naming()
        write(self.root, "federation.yaml",
              FEDERATION + NAMING.replace(
                  "      projects:\n", "      projects:\n        catalogue: catalo\n"))
        other = self.example_copy()
        write(other, "federation.yaml",
              "id: acme-other\nroot: acme\nmembers: []\n"
              "extensions:\n  ameesh:\n    naming:\n      org: oth\n"
              "      projects:\n        otherrepo: catalo\n")
        env = self.cli_env(AMEESH_CANONS="%s:%s" % (self.root, other))
        proc = self.mesh("agent", "name", "--role", "implementer",
                         "--project", "catalogue", env=env)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("otherrepo", proc.stderr)
        self.assertIn("catalo", proc.stderr)

    def test_a_repo_with_two_declared_segments_is_refused_in_any_order(self):
        """Codex r3 (#452, P2) : `setdefault` gardait le premier segment d'un
        dépôt (`otherrepo: other` chez B, `otherrepo: catalo` chez C) ; le
        verdict dépendait donc de l'ordre des canons."""
        self.naming()
        second = self.example_copy()
        write(second, "federation.yaml",
              "id: acme-second\nroot: acme\nmembers: []\n"
              "extensions:\n  ameesh:\n    naming:\n      org: two\n"
              "      projects:\n        otherrepo: other\n")
        third = self.example_copy()
        write(third, "federation.yaml",
              "id: acme-third\nroot: acme\nmembers: []\n"
              "extensions:\n  ameesh:\n    naming:\n      org: tre\n"
              "      projects:\n        otherrepo: catalo\n")
        errors = []
        for order in ((second, third), (third, second)):
            env = self.cli_env(AMEESH_CANONS=":".join([self.root, *order]))
            proc = self.mesh("agent", "name", "--role", "implementer",
                             "--project", "catalogue", env=env)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")
            self.assertIn("otherrepo", proc.stderr)
            self.assertIn("'other'", proc.stderr)
            self.assertIn("'catalo'", proc.stderr)
            errors.append(proc.stderr.strip())
        self.assertEqual(errors[0], errors[1])
