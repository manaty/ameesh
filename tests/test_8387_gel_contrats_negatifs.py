# SPDX-License-Identifier: AGPL-3.0-only
"""#8387 : les contrats négatifs statiques passent au gel, pas seulement en CI.

Constat de l'auditeur (27/11) : `test_4259_client_literals` (#8340) et le
classement des colonnes d'identité de `test_4299` (#8344) ne se sont vus
qu'en CI de paquet, après le jalon ; deux retraits et plus de deux heures
perdues. Ce module tient les deux témoins demandés au lot :

* une fixture qui ajoute « IMA » dans une docstring fait rendre rouge le
  script de gel, donc `Refused("checks-red")` par `ameesh.freeze_checks` ;
* une migration `ADD COLUMN approved_by text` non classée fait de même ;
* une migration conforme (`work_items.planned_by`) reste verte, et un diff
  qui ne touche pas `src/ameesh/migrations/` ne lance pas la garde.

Les deux gardes sont réelles ; le reste des `CHECKS` est remplacé par des
stubs qui passent, pour que le rouge vienne bien du contrat visé. Les
constantes de classement du stub sont recopiées de `agent_rename` à
l'exécution, jamais maintenues à la main.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import agent_rename, freeze_checks

REPO = pathlib.Path(__file__).resolve().parent.parent
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "banc",
    "GIT_AUTHOR_EMAIL": "banc@example.invalid",
    "GIT_COMMITTER_NAME": "banc",
    "GIT_COMMITTER_EMAIL": "banc@example.invalid",
}
KEEP = ("test_4259_client_literals", "garde-colonnes-identite")


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_script("garde_colonnes_identite_8387",
                    "scripts/garde-colonnes-identite.py")
freeze_script = load_script("freeze_checks_script_8387",
                            "scripts/freeze-checks.py")


class ParserTest(unittest.TestCase):
    """Le parseur statique : CREATE TABLE et ADD COLUMN, sans deviner."""

    def columns(self, sql: str) -> list[tuple[str, str]]:
        return [(item.table, item.column) for item in guard.text_columns(sql)]

    def test_create_table_et_add_column(self):
        sql = ("CREATE TABLE demo (id bigint, approved_by text, "
               "note varchar(32)[]);\n"
               "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS planned_by text;\n")
        self.assertEqual(self.columns(sql),
                         [("demo", "approved_by"), ("demo", "note"),
                          ("work_items", "planned_by")])

    def test_les_commentaires_et_chaines_ne_mordent_pas(self):
        sql = ("-- CREATE TABLE t (approved_by text);\n"
               "/* ALTER TABLE t ADD COLUMN approved_by text; */\n"
               "SELECT 'ADD COLUMN approved_by text';\n"
               "CREATE TABLE t (note text DEFAULT 'approved_by text');\n")
        self.assertEqual(self.columns(sql), [("t", "note")])

    def test_un_corps_dollar_quote_ne_mord_pas(self):
        sql = ("DO $$\nBEGIN\n  EXECUTE 'ALTER TABLE t ADD COLUMN approved_by text';\n"
               "END $$;\n")
        self.assertEqual(self.columns(sql), [])

    def test_les_types_non_lus_par_4299_sont_ignores(self):
        sql = ("CREATE TABLE t (approved_by bigint, planned_by uuid, "
               "reviewed_by timestamp, failed_by bigint[]);\n")
        self.assertEqual(self.columns(sql), [("t", "failed_by")])

    def test_chaque_add_est_lu_seul_dans_l_alter(self):
        # #8387 B1 : un type de tableau suivi d'un `ADD` ne doit pas être
        # ignoré parce que le dernier type de l'ALTER est numérique.
        sql = ("ALTER TABLE demo ADD COLUMN approved_by bigint[], "
               "ADD COLUMN note integer;\n")
        self.assertEqual(self.columns(sql), [("demo", "approved_by")])

    def test_un_type_numerique_ne_devient_pas_identite_par_le_voisin(self):
        # #8387 B1 : le `text[]` du second `ADD` ne doit pas faire passer le
        # `bigint` du premier pour un tableau ; seul `note` est de type lu.
        sql = ("ALTER TABLE demo ADD COLUMN approved_by bigint, "
               "ADD COLUMN note text[];\n")
        self.assertEqual(self.columns(sql), [("demo", "note")])

    def test_les_virgules_des_parentheses_ne_coupent_pas_l_action(self):
        sql = ("ALTER TABLE demo ADD COLUMN amount numeric(10, 2), "
               "ADD COLUMN reviewed_by text[];\n")
        self.assertEqual(self.columns(sql), [("demo", "reviewed_by")])

    def test_les_contraintes_ne_sont_pas_des_colonnes(self):
        sql = ("CREATE TABLE t (id bigint, CONSTRAINT approved_by CHECK (id > 0));\n"
               "ALTER TABLE t ADD CONSTRAINT planned_by CHECK (id > 0);\n"
               "ALTER TABLE t ADD PRIMARY KEY (id);\n")
        self.assertEqual(self.columns(sql), [])

    def test_un_identifiant_cite_garde_sa_casse(self):
        sql = 'CREATE TABLE "T" ("Approved_By" text, approved_by text);\n'
        self.assertEqual(self.columns(sql), [("T", "Approved_By"), ("T", "approved_by")])

    def test_la_ligne_est_celle_du_fichier(self):
        sql = "CREATE TABLE t (\n  id bigint,\n  approved_by text\n);\n"
        self.assertEqual([item.line for item in guard.text_columns(sql)], [3])

    def test_une_dollar_quote_ouverte_est_un_doute(self):
        with self.assertRaises(guard.GuardError):
            guard.text_columns("DO $$ BEGIN NULL;")

    def test_classification_lue_dans_agent_rename(self):
        identity, known = guard.classification(str(REPO))
        self.assertIs(identity, agent_rename.IDENTITY_COLUMNS)
        self.assertIn("work_items.planned_by", known)
        self.assertIn("external_executors.name", known)
        self.assertIn("agent_registry.name", known)


class PathsTest(unittest.TestCase):
    """L'entrée du gel : une liste JSON, jamais une forme devinée."""

    def test_absent_hors_du_gel(self):
        self.assertIsNone(guard.read_paths({}))

    def test_liste_valide(self):
        self.assertEqual(
            guard.read_paths({guard.PATHS_ENV: '["src/ameesh/x.py"]'}),
            ["src/ameesh/x.py"])

    def test_formes_refusees(self):
        for raw in ("pas du json", "{}", '"x"', '["x", 1]', "[null]"):
            with self.subTest(raw=raw):
                with self.assertRaises(guard.GuardError):
                    guard.read_paths({guard.PATHS_ENV: raw})

    def test_seules_les_migrations_du_diff(self):
        root = tempfile.mkdtemp(prefix="ameesh-8387-paths-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "src", "ameesh", "migrations"))
        for name in ("0001_a.sql", "0002_b.sql"):
            pathlib.Path(root, "src", "ameesh", "migrations", name).write_text(
                "", encoding="utf-8")
        paths = ["src/ameesh/x.py", "src/ameesh/migrations/0002_b.sql"]
        self.assertEqual(guard.migration_files(root, paths),
                         ["src/ameesh/migrations/0002_b.sql"])
        self.assertEqual(len(guard.migration_files(root, None)), 2)

    def test_un_chemin_de_migration_douteux_refuse(self):
        for path in ("src/ameesh/migrations/../../etc/passwd.sql",
                     "src/ameesh/migrations//x.sql"):
            with self.subTest(path=path):
                with self.assertRaises(guard.GuardError):
                    guard.migration_files("/tmp", [path])


class WiringTest(unittest.TestCase):
    """Une garde non branchée ne refuserait aucun gel."""

    def test_les_deux_contrats_sont_dans_les_controles_de_gel(self):
        commands = [args for _name, args in freeze_script.CHECKS]
        self.assertIn(["scripts/garde-colonnes-identite.py", "--root", "."],
                      commands)
        self.assertIn(["-m", "unittest", "-q",
                       "tests.test_4259_client_literals.ClientLiteralsGuard"],
                      commands)

    def test_chaque_controle_nomme_un_test_ou_un_script_reel(self):
        # `test_2542` vérifie déjà chaque identifiant `unittest` ; ce témoin
        # couvre le script, absent de cette vérification.
        self.assertTrue((REPO / "scripts" / "garde-colonnes-identite.py").is_file())


class FreezeTree:
    """Un dépôt jetable : le vrai script de gel, les deux gardes réelles.

    Tous les autres contrôles sont des stubs qui passent, construits depuis
    la liste `CHECKS` du script réel : le rouge observé vient du contrat
    visé, et un contrôle ajouté plus tard ne rend pas ce banc menteur.
    """

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-8387-")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update(GIT_ENV)
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "socle")
        self.git("checkout", "-q", "-b", "lot")
        self.copy("scripts/freeze-checks.py")
        self.stubs()

    def git(self, *args) -> str:
        proc = subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, relative: str, text: str) -> None:
        path = pathlib.Path(self.root, *relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def copy(self, relative: str) -> None:
        path = pathlib.Path(self.root, *relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, path)

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", "lot")
        return self.git("rev-parse", "HEAD")

    def stubs(self) -> None:
        for _name, args in freeze_script.CHECKS:
            joined = " ".join(args)
            if any(keep in joined for keep in KEEP):
                continue
            if "compileall" in args:
                continue
            if args and args[0].startswith("scripts/"):
                self.write(args[0], "import sys\nsys.exit(0)\n")
            elif "unittest" in args:
                for ident in args:
                    if ident.startswith("tests."):
                        self._stub_test(ident)
            else:  # un contrôle non reconnu doit faire échouer le banc
                raise AssertionError("contrôle de gel inconnu : %r" % (args,))
        self.write("scripts/test-hermeticity-lint.py", "import sys\nsys.exit(0)\n")
        self.write("tests/__init__.py", "")
        self.write("src/ameesh/__init__.py", "")
        self.copy("scripts/garde-colonnes-identite.py")
        self.copy("tests/test_4259_client_literals.py")
        self.copy("tests/client_literals.txt")

    def _stub_test(self, ident: str) -> None:
        parts = ident.split(".")
        module = "tests/" + parts[1] + ".py"
        klass = parts[2] if len(parts) > 2 else "StubTest"
        method = parts[3] if len(parts) > 3 else None
        path = pathlib.Path(self.root, *module.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else \
            "import unittest\n"
        if "class %s(" % klass not in text:
            text += ("\nclass %s(unittest.TestCase):\n" % klass)
            text += ("    def %s(self):\n        pass\n" % method
                     if method else "    def test_stub(self):\n        pass\n")
        path.write_text(text, encoding="utf-8")

    def agent_rename_stub(self) -> None:
        """Le classement réel, recopié dans l'arbre jetable."""
        self.write("src/ameesh/agent_rename.py", (
            "import re\n"
            "IDENTITY_COLUMNS = re.compile(%r)\n"
            "REWRITTEN = %r\n"
            "CASCADED = %r\n"
            "KEPT = %r\n"
            "IGNORED = %r\n"
            "NOT_AGENTS = %r\n")
            % (agent_rename.IDENTITY_COLUMNS.pattern,
               agent_rename.REWRITTEN, agent_rename.CASCADED,
               agent_rename.KEPT, agent_rename.IGNORED,
               agent_rename.NOT_AGENTS))

    def client_literal(self) -> None:
        self.write("src/ameesh/bad.py",
                   'def f():\n    """Lit le depot IMA."""\n    return 1\n')

    def migration(self, text: str) -> None:
        self.write("src/ameesh/migrations/0099_test.sql", text)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class FreezeTest(unittest.TestCase):
    """Les deux témoins du lot, au niveau du garde du gel."""

    def setUp(self) -> None:
        self.tree = FreezeTree()
        self.addCleanup(self.tree.cleanup)

    def run_freeze(self, sha: str, paths=None):
        return freeze_checks.run(self.tree.root, sha, gate=[], paths=paths)

    def test_litteral_ima_dans_une_docstring_refuse_le_gel(self):
        self.tree.client_literal()
        sha = self.tree.commit()
        with self.assertRaises(freeze_checks.Refused) as caught:
            self.run_freeze(sha, ["src/ameesh/bad.py"])
        self.assertEqual(caught.exception.cause, "checks-red")
        self.assertIn("src/ameesh/bad.py:2", str(caught.exception))
        self.assertIn("IMA", str(caught.exception))

    def test_add_column_non_classee_refuse_le_gel(self):
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN planned_by text;\n"
            "ALTER TABLE work_items ADD COLUMN approved_by text;\n")
        sha = self.tree.commit()
        with self.assertRaises(freeze_checks.Refused) as caught:
            self.run_freeze(sha, ["src/ameesh/migrations/0099_test.sql"])
        self.assertEqual(caught.exception.cause, "checks-red")
        self.assertIn("work_items.approved_by", str(caught.exception))
        self.assertNotIn("work_items.planned_by", str(caught.exception))

    def test_un_tableau_suivi_d_un_add_numerique_reste_vu(self):
        # #8387 B1, sens faux négatif : le `bigint[]` reste une identité,
        # même si le dernier `ADD` de l'ALTER est numérique.
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN approved_by bigint[], "
            "ADD COLUMN planned_by text;\n")
        sha = self.tree.commit()
        with self.assertRaises(freeze_checks.Refused) as caught:
            self.run_freeze(sha, ["src/ameesh/migrations/0099_test.sql"])
        self.assertEqual(caught.exception.cause, "checks-red")
        self.assertIn("work_items.approved_by", str(caught.exception))

    def test_un_add_numerique_ne_blanchit_pas_le_suivant(self):
        # #8387 B1, sens faux positif : le `bigint` n'est pas une identité,
        # et le `text[]` qui suit ne doit pas le faire passer pour une.
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN approved_by bigint, "
            "ADD COLUMN planned_by text[];\n")
        sha = self.tree.commit()
        result = self.run_freeze(sha, ["src/ameesh/migrations/0099_test.sql"])
        self.assertEqual(result["result"], "green")

    def test_une_migration_classee_reste_verte(self):
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN planned_by text;\n")
        sha = self.tree.commit()
        result = self.run_freeze(sha, ["src/ameesh/migrations/0099_test.sql"])
        self.assertEqual(result["result"], "green")

    def test_un_diff_sans_migration_ne_lance_pas_la_garde(self):
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN approved_by text;\n")
        sha = self.tree.commit()
        result = self.run_freeze(sha, ["src/ameesh/other.py"])
        self.assertEqual(result["result"], "green")

    def test_sans_chemins_du_gel_toutes_les_migrations_sont_lues(self):
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN approved_by text;\n")
        sha = self.tree.commit()
        with self.assertRaises(freeze_checks.Refused) as caught:
            self.run_freeze(sha)
        self.assertEqual(caught.exception.cause, "checks-red")
        self.assertIn("work_items.approved_by", str(caught.exception))

    def test_des_chemins_illisibles_refusent(self):
        self.tree.agent_rename_stub()
        self.tree.migration(
            "ALTER TABLE work_items ADD COLUMN approved_by text;\n")
        with mock.patch.dict(os.environ, {guard.PATHS_ENV: "pas du json"}), \
                contextlib.redirect_stderr(io.StringIO()):
            code = guard.main(["--root", self.tree.root])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
