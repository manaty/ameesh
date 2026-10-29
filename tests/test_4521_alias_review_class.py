# SPDX-License-Identifier: AGPL-3.0-only
"""#4521 : un alias de module ne peut pas affaiblir la classe de relecture.

Témoins de la garde ``scripts/garde-alias-classe.py`` : la déclaration lue dans
``tests/test_generic_core.py`` (dict littéral et compréhension de #4385), les
alias ``sys.modules`` du cœur et des connecteurs, et le verdict — un alias
couvert passe, un alias dont l'implémentation tombe en classe légère est
refusé. Le branchement dans ``scripts/freeze-checks.py`` est verrouillé ici :
une garde non branchée ne refuserait aucun gel.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ameesh import review

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "garde_alias_classe", REPO / "scripts" / "garde-alias-classe.py")
script = importlib.util.module_from_spec(_spec)
# dataclasses lit `sys.modules[cls.__module__]` à la création des classes
sys.modules[_spec.name] = script
_spec.loader.exec_module(script)

#: le dict littéral d'avant #4385, avec un motif non module-import
LITERAL = """\
from collections import Counter


def test_core_does_not_import_module_implementations(self):
    exceptions = Counter({("plan_git.py", "module-import"): 1,
                          ("review.py", "module-import"): 1,
                          ("work.py", "vocabulary"): 1})
"""

#: la compréhension de #4385
COMPREHENSION = """\
from collections import Counter


def test_core_does_not_import_module_implementations(self):
    exceptions = Counter({(name + ".py", "module-import"): 1
                          for name in ("plan_github", "plan_git", "merge_pr")})
"""

ALIAS = """\
import sys

from .modules.dev import {module} as _implementation

sys.modules[__name__] = _implementation
"""

CONNECTOR_ALIAS = """\
import sys

from ..modules.dev import {module} as _implementation

sys.modules[__name__] = _implementation
"""


def policy(default="light", rules=()):
    """Une politique de revue valide pour les fixtures."""
    parsed, problems = review.parse({"default": default, "rules": list(rules)})
    assert not problems, problems
    return parsed


def classifier(policy):
    """``class_of`` tel que la CLI le construit à partir du canon."""
    def class_of(path):
        cls, rule = policy.class_of(path)
        return cls, tuple(rule.paths) if rule else None
    return class_of


class Tree:
    """Un arbre de dépôt jetable : déclaration + fichiers d'alias."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-4521-")
        os.makedirs(os.path.join(self.root, "tests"), exist_ok=True)

    def write(self, relative, text):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(text))

    def declare(self, text=LITERAL):
        self.write("tests/test_generic_core.py", text)

    def declare_paths(self, *paths):
        entries = ", ".join('("%s", "module-import"): 1' % path for path in paths)
        self.declare(
            "from collections import Counter\n\n"
            "def test_core_does_not_import_module_implementations(self):\n"
            "    exceptions = Counter({%s})\n" % entries)


class Fixture(unittest.TestCase):
    def tree(self) -> Tree:
        tree = Tree()
        self.addCleanup(shutil.rmtree, tree.root, True)
        return tree


class DeclarationTest(Fixture):
    def test_dict_litteral_ne_garde_que_module_import(self):
        tree = self.tree()
        tree.declare()
        self.assertEqual(script.declared_paths(tree.root),
                         ["plan_git.py", "review.py"])

    def test_comprehension_de_4385(self):
        tree = self.tree()
        tree.declare(COMPREHENSION)
        self.assertEqual(script.declared_paths(tree.root),
                         ["merge_pr.py", "plan_git.py", "plan_github.py"])

    def test_forme_inconnue_refusee(self):
        tree = self.tree()
        tree.declare("""\
            from collections import Counter

            def test_core_does_not_import_module_implementations(self):
                exceptions = Counter({key: 1 for key in ("a.py",)})
            """)
        with self.assertRaises(script.GuardError):
            script.declared_paths(tree.root)

    def test_test_absent_refuse(self):
        tree = self.tree()
        tree.declare("def autre(self):\n    pass\n")
        with self.assertRaises(script.GuardError):
            script.declared_paths(tree.root)

    def test_declaration_absente_refuse(self):
        tree = self.tree()
        with self.assertRaises(script.GuardError):
            script.declared_paths(tree.root)


class DiscoveryTest(Fixture):
    def test_alias_du_coeur(self):
        tree = self.tree()
        tree.declare_paths("plan_git.py")
        tree.write("src/ameesh/plan_git.py", ALIAS.format(module="plan_git"))
        tree.write("src/ameesh/modules/dev/plan_git.py", "VALUE = 1\n")
        self.assertEqual(script.aliases(tree.root), [
            script.Alias("src/ameesh/plan_git.py",
                         "src/ameesh/modules/dev/plan_git.py")])

    def test_alias_non_declare_est_vu(self):
        """Un sys.modules oublié dans les exceptions ne passe pas au gel."""
        tree = self.tree()
        tree.declare_paths()
        tree.write("src/ameesh/extra.py", ALIAS.format(module="extra"))
        tree.write("src/ameesh/modules/dev/extra.py", "VALUE = 1\n")
        self.assertEqual(script.aliases(tree.root), [
            script.Alias("src/ameesh/extra.py",
                         "src/ameesh/modules/dev/extra.py")])

    def test_alias_d_un_connecteur_import_relatif(self):
        tree = self.tree()
        tree.declare_paths("connectors/git_merge.py")
        tree.write("src/ameesh/connectors/git_merge.py",
                   CONNECTOR_ALIAS.format(module="git_merge"))
        tree.write("src/ameesh/modules/dev/git_merge.py", "VALUE = 1\n")
        self.assertEqual(script.aliases(tree.root), [
            script.Alias("src/ameesh/connectors/git_merge.py",
                         "src/ameesh/modules/dev/git_merge.py")])

    def test_import_sans_alias_sys_modules_ignore(self):
        """Une exception de module-import n'est pas un alias : pas de comparaison."""
        tree = self.tree()
        tree.declare_paths("plan_git.py")
        tree.write("src/ameesh/plan_git.py",
                   "from .modules.dev import plan_git\n")
        self.assertEqual(script.aliases(tree.root), [])

    def test_alias_vers_un_module_du_coeur_ignore(self):
        """L148 (menage/grand_livre) pointe hors de ameesh.modules : hors garde."""
        tree = self.tree()
        tree.declare_paths("plan_git.py")
        tree.write("src/ameesh/plan_git.py", """\
            import sys

            from . import housekeeping as _housekeeping

            sys.modules[__name__] = _housekeeping
            """)
        tree.write("src/ameesh/housekeeping.py", "VALUE = 1\n")
        self.assertEqual(script.aliases(tree.root), [])

    def test_implementation_absente_refusee(self):
        tree = self.tree()
        tree.declare_paths("plan_git.py")
        tree.write("src/ameesh/plan_git.py", ALIAS.format(module="plan_git"))
        with self.assertRaises(script.GuardError):
            script.aliases(tree.root)

    def test_cible_d_alias_non_resolue_refusee(self):
        tree = self.tree()
        tree.declare_paths("plan_git.py")
        tree.write("src/ameesh/plan_git.py", """\
            import sys

            sys.modules[__name__] = _inconnu
            """)
        with self.assertRaises(script.GuardError):
            script.aliases(tree.root)

    def test_ancien_chemin_declare_absent_refuse(self):
        tree = self.tree()
        tree.declare_paths("review.py")
        with self.assertRaises(script.GuardError):
            script.aliases(tree.root)


class CheckTest(Fixture):
    def declared(self) -> list:
        return [script.Alias("src/ameesh/review.py",
                             "src/ameesh/modules/dev/review.py")]

    def test_alias_couvert_passe(self):
        rules = [{"paths": ["src/ameesh/review.py",
                            "src/ameesh/modules/dev/review.py"],
                  "class": "sensitive"}]
        self.assertEqual(
            script.check(self.declared(), classifier(policy(rules=rules))), [])

    def test_implementation_light_refusee(self):
        rules = [{"paths": ["src/ameesh/review.py"], "class": "sensitive"}]
        findings = script.check(self.declared(), classifier(policy(rules=rules)))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].old_class, "sensitive")
        self.assertEqual(findings[0].new_class, "light")
        rendered = findings[0].render()
        self.assertIn("src/ameesh/modules/dev/review.py", rendered)
        self.assertIn("src/ameesh/review.py", rendered)
        self.assertIn("sensitive", rendered)
        self.assertIn("ajouter", rendered)

    def test_defaut_normal_vers_light_refuse(self):
        rules = [{"paths": ["src/ameesh/review.py"], "class": "normal"}]
        findings = script.check(self.declared(), classifier(policy(rules=rules)))
        self.assertEqual(len(findings), 1)

    def test_deux_chemins_light_passent(self):
        self.assertEqual(script.check(self.declared(), classifier(policy())), [])

    def test_classe_inconnue_refusee(self):
        with self.assertRaises(script.GuardError):
            script.check(self.declared(), lambda _path: ("inconnue", None))


class MainTest(Fixture):
    def run_main(self, root):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = script.main(["--root", root])
        return code, out.getvalue(), err.getvalue()

    def test_refus_rend_un_et_nomme_le_chemin(self):
        tree = self.tree()
        tree.declare_paths("review.py")
        tree.write("src/ameesh/review.py", ALIAS.format(module="review"))
        tree.write("src/ameesh/modules/dev/review.py", "VALUE = 1\n")
        rules = [{"paths": ["src/ameesh/review.py"], "class": "sensitive"}]
        with mock.patch.object(
                script, "configured_class_of",
                return_value=(classifier(policy(rules=rules)), [""])):
            code, out, _err = self.run_main(tree.root)
        self.assertEqual(code, 1)
        self.assertIn("src/ameesh/modules/dev/review.py", out)

    def test_vert_rend_zero(self):
        tree = self.tree()
        tree.declare_paths("review.py")
        tree.write("src/ameesh/review.py", ALIAS.format(module="review"))
        tree.write("src/ameesh/modules/dev/review.py", "VALUE = 1\n")
        rules = [{"paths": ["src/ameesh/review.py",
                            "src/ameesh/modules/dev/review.py"],
                  "class": "sensitive"}]
        with mock.patch.object(
                script, "configured_class_of",
                return_value=(classifier(policy(rules=rules)), [""])):
            code, out, _err = self.run_main(tree.root)
        self.assertEqual(code, 0)
        self.assertIn("classe de relecture conservée", out)

    def test_politique_illisible_rend_deux(self):
        tree = self.tree()
        tree.declare_paths("review.py")
        tree.write("src/ameesh/review.py", ALIAS.format(module="review"))
        tree.write("src/ameesh/modules/dev/review.py", "VALUE = 1\n")

        def refuse(_project):
            raise script.GuardError("canon illisible")
        with mock.patch.object(script, "configured_class_of", side_effect=refuse):
            code, _out, err = self.run_main(tree.root)
        self.assertEqual(code, 2)
        self.assertIn("canon illisible", err)

    def test_aucun_alias_rend_zero(self):
        tree = self.tree()
        tree.declare("""\
            from collections import Counter

            def test_core_does_not_import_module_implementations(self):
                exceptions = Counter()
            """)
        code, out, _err = self.run_main(tree.root)
        self.assertEqual(code, 0)
        self.assertIn("aucun alias", out)


class RealTreeTest(unittest.TestCase):
    """La déclaration vivante et le branchement au gel ne se perdent pas."""

    ALIASES = {
        ("src/ameesh/merge_pr.py", "src/ameesh/modules/dev/merge_pr.py"),
        ("src/ameesh/plan_git.py", "src/ameesh/modules/dev/plan_git.py"),
        ("src/ameesh/plan_github.py", "src/ameesh/modules/dev/plan_github.py"),
        ("src/ameesh/connectors/git_merge.py",
         "src/ameesh/modules/dev/git_merge.py"),
    }

    def test_les_alias_declares_du_depot(self):
        found = {(alias.old, alias.new) for alias in script.aliases(str(REPO))}
        self.assertLessEqual(self.ALIASES, found)

    def test_couverts_sous_la_politique_de_la_porte(self):
        """Le canon #4520 : les quatre implémentations gardent leur classe."""
        rules = [{"paths": ["src/ameesh/merge_pr.py", "src/ameesh/plan_git.py",
                            "src/ameesh/modules/dev/merge_pr.py",
                            "src/ameesh/modules/dev/plan_git.py"],
                  "class": "sensitive"}]
        self.assertEqual(
            script.check(script.aliases(str(REPO)), classifier(policy(rules=rules))), [])

    def test_branchee_dans_les_controles_de_gel(self):
        import importlib.util as loader
        spec = loader.spec_from_file_location(
            "freeze_checks_4521", REPO / "scripts" / "freeze-checks.py")
        freeze = loader.module_from_spec(spec)
        sys.modules[spec.name] = freeze
        spec.loader.exec_module(freeze)
        commands = [args for _name, args in freeze.CHECKS]
        self.assertIn(["scripts/garde-alias-classe.py", "--root", "."], commands)
        self.assertIn(["scripts/garde-alias-classe.py", "--root", ".",
                       "--check-policy", "scripts/alias-review-classes.json"],
                      commands)


class BranchementCITest(unittest.TestCase):
    """#4521 B/P2 (cx2) : la garde est requise en CI, pas seulement au gel.

    Le gel local et le composeur ne couvrent pas une CI verte : la garde doit
    tourner dans un job dont l'échec rougit l'agrégat requis `tests`, sur
    l'arbre testé, et refuser sans canon lisible.
    """

    WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"

    def job_block(self, name):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        start = text.index("\n  %s:" % name)
        end = len(text)
        for marker in ("\n  psycopg:", "\n  psql:", "\n  platform-macos:",
                       "\n  doc-readers:", "\n  fs-class:", "\n  version:",
                       "\n  tests:"):
            position = text.find(marker, start + 1)
            if position != -1:
                end = min(end, position)
        return text[start:end]

    def test_la_garde_tourne_dans_le_job_plan_requis(self):
        plan = self.job_block("plan")
        # le canon est privé : la CI lit la politique épinglée du dépôt
        self.assertIn(
            "run: python3 scripts/garde-alias-classe.py --root . --project ameesh "
            "--policy scripts/alias-review-classes.json", plan)
        self.assertNotIn("https://github.com/manaty/home.git", plan)
        self.assertNotIn("CANON_REF", plan)
        # `plan` est une dépendance de l'agrégat requis `tests` : son échec
        # rougit le check requis (`scripts/ci-agregat.py`, plan != success).
        self.assertIn("needs: [plan,", self.job_block("tests"))

    def test_la_politique_epinglee_couvre_les_alias_du_depot(self):
        path = REPO / "scripts" / "alias-review-classes.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(policy.get("project"), "ameesh")
        classes = policy.get("classes") or {}
        for alias in script.aliases(str(REPO)):
            self.assertIn(alias.old, classes)
            self.assertIn(alias.new, classes)


#: un git de banc, sans la configuration de l'hôte
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "banc",
    "GIT_AUTHOR_EMAIL": "banc@example.invalid",
    "GIT_COMMITTER_NAME": "banc",
    "GIT_COMMITTER_EMAIL": "banc@example.invalid",
}


class CanonReelTest(unittest.TestCase):
    """#4521 B cx2 : la garde sous un canon git réel, pas une politique simulée.

    La commande de la CI (`--root . --project ameesh --policy FILE`) est
    rejouée dans l'environnement nu d'un runner, sans canon ni config d'hôte
    (B/P1 auditeur) ; les témoins de génération et de vérification utilisent
    un canon git jetable. Une implémentation plus légère est refusée, une
    politique absente, invalide ou périmée aussi.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-4521-canon-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.canon = os.path.join(self.root, "canon")
        os.makedirs(self.canon)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_")}
        env.update(GIT_ENV)
        proc = subprocess.run(["git", *args], cwd=self.canon, env=env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))

    def write_canon(self, rules):
        """Un canon git minimal : les chemins de ``rules`` sont sensibles."""
        text = (
            'federation: "0.1"\n'
            "id: test\n"
            "members: []\n"
            "review_policies:\n"
            "  risk_classes:\n"
            "    default: normal\n"
            "    projects:\n"
            "      ameesh:\n"
            "        default: light\n"
            "        rules:\n")
        for paths in rules:
            text += "          - paths:\n"
            for path in paths:
                text += '              - "%s"\n' % path
            text += "            class: sensitive\n"
        with open(os.path.join(self.canon, "federation.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
        self.git("init", "-q", "-b", "main")
        self.git("add", "federation.yaml")
        self.git("commit", "-qm", "canon")

    def config(self, *, canon=None):
        path = os.path.join(self.root, "config.json")
        payload = {}
        if canon is not None:
            payload["canons"] = [{"path": canon, "ref": "main"}]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def run_guard(self, config, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AMEESH_CANON", "AMEESH_CANONS",
                                       "AGENT_MESH_CONFIG", "AGENT_MESH_CANON"))}
        env["AMEESH_CONFIG"] = config
        env["PYTHONPATH"] = str(REPO / "src")
        env["HOME"] = os.path.join(self.root, "home")
        os.makedirs(env["HOME"], exist_ok=True)
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "garde-alias-classe.py"),
             "--root", str(REPO), "--project", "ameesh", *args],
            env=env, capture_output=True, text=True)

    def run_guard_policy(self, policy, *args):
        """L'étape de CI : politique épinglée, sans canon ni config d'hôte.

        L'environnement ne porte que ``PATH``, ``PYTHONPATH`` et un ``HOME``
        vide (équivalent de ``env -i``) : ni ``AMEESH_CONFIG``, ni canon de
        l'hôte. C'est l'environnement d'un runner.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(REPO / "src"),
            "HOME": os.path.join(self.root, "home-ci"),
        }
        os.makedirs(env["HOME"], exist_ok=True)
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "garde-alias-classe.py"),
             "--root", str(REPO), "--project", "ameesh",
             "--policy", str(policy), *args],
            env=env, capture_output=True, text=True)

    def generate_policy(self, name="policy.json"):
        path = os.path.join(self.root, name)
        proc = self.run_guard(self.config(canon=self.canon),
                              "--write-policy", path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return path

    def test_refuse_une_implementation_light_sous_un_canon_reel(self):
        self.write_canon([["src/ameesh/merge_pr.py"]])
        proc = self.run_guard(self.config(canon=self.canon))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("src/ameesh/modules/dev/merge_pr.py", proc.stdout)

    def test_couvre_les_deux_chemins_sous_un_canon_reel(self):
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py"]])
        proc = self.run_guard(self.config(canon=self.canon))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_politique_epinglee_lue_sans_canon_d_hote(self):
        """Witness B/P1 auditeur : l'étape CI rejouée sans canon ni config."""
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py"]])
        policy = self.generate_policy()
        proc = self.run_guard_policy(policy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_politique_epinglee_refuse_une_implementation_light(self):
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py"]])
        policy = self.generate_policy()
        with open(policy, encoding="utf-8") as fh:
            data = json.load(fh)
        data["classes"]["src/ameesh/modules/dev/merge_pr.py"]["class"] = "light"
        with open(policy, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        proc = self.run_guard_policy(policy)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("src/ameesh/modules/dev/merge_pr.py", proc.stdout)

    def test_politique_epinglee_invalide_refusee(self):
        path = os.path.join(self.root, "policy-vide.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        proc = self.run_guard_policy(path)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("politique épinglée invalide", proc.stderr)

    def test_politique_epinglee_ignore_une_regle_sans_changement_de_classe(self):
        """B/P2 auditeur : ajouter un chemin à une règle ne change pas la classe."""
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py"]])
        policy = self.generate_policy()
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py",
                           "src/ameesh/exemple_4521.py"]])
        proc = self.run_guard(self.config(canon=self.canon),
                              "--check-policy", policy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("à jour", proc.stdout)

    def test_politique_epinglee_refuse_une_classe_plus_faible(self):
        """Une classe épinglée plus faible que le canon refuse le gel."""
        self.write_canon([["src/ameesh/exemple_4521.py"]])
        policy = self.generate_policy()
        self.write_canon([["src/ameesh/merge_pr.py"],
                          ["src/ameesh/exemple_4521.py"]])
        proc = self.run_guard(self.config(canon=self.canon),
                              "--check-policy", policy)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("AFFAIBLIE", proc.stdout)
        self.assertIn("src/ameesh/merge_pr.py", proc.stdout)
        self.assertIn("régénérer", proc.stdout)

    def test_politique_epinglee_sans_chemin_refusee(self):
        self.write_canon([["src/ameesh/merge_pr.py",
                           "src/ameesh/modules/dev/merge_pr.py"]])
        policy = self.generate_policy()
        with open(policy, encoding="utf-8") as fh:
            data = json.load(fh)
        del data["classes"]["src/ameesh/merge_pr.py"]
        with open(policy, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        proc = self.run_guard(self.config(canon=self.canon),
                              "--check-policy", policy)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("src/ameesh/merge_pr.py", proc.stdout)
        self.assertIn("absent", proc.stdout)

    def test_canon_epingle_absent_refuse(self):
        missing = os.path.join(self.root, "canon-absent")
        proc = self.run_guard(self.config(), "--canon", missing, "--ref", "HEAD")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("canon épinglé illisible", proc.stderr)

    def test_sans_canon_refuse(self):
        proc = self.run_guard(self.config())
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("aucun canon", proc.stderr)

    def test_politique_illisible_refuse(self):
        missing = os.path.join(self.root, "absent")
        proc = self.run_guard(self.config(canon=missing))
        self.assertEqual(proc.returncode, 2, proc.stdout)


if __name__ == "__main__":
    unittest.main()
