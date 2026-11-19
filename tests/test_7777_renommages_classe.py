# SPDX-License-Identifier: AGPL-3.0-only
"""#7777 : un fichier déplacé garde la classe de son ancien chemin.

Témoins de la garde ``scripts/garde-renommages-classe.py`` et de son
branchement au gel : un déplacement sensible → light est refusé, avec le
chemin et la classe à ajouter au canon ; un canon qui nomme déjà le nouveau
chemin l'accepte ; une liste illisible refuse ; une liste absente (lancement
hors gel) rend le contrôle inactif sans bloquer. Le garde du gel transmet les
paires `git diff -M` du diff gelé et refuse lui-même quand il ne peut pas les
lire — jamais une liste vide rassurante.

Cas d'origine (#7663) : le moteur de release Nexlink, `sensitive` par la règle
de la porte de fusion, est passé dans `src/ameesh/modules/nexlink/` où le canon
ne le nommait pas encore ; `review-class` le rendait `light, default`. Le canon
a dû être complété à la main, après le gel.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import freeze_checks
from ameesh.modules.dev import lot_class as lot_class_mod

REPO = pathlib.Path(__file__).resolve().parent.parent


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: dataclasses lit `sys.modules[cls.__module__]` à la création des classes
guard = load_script("garde_renommages_classe_7777",
                    "scripts/garde-renommages-classe.py")
freeze_script = load_script("freeze_checks_script_7777",
                            "scripts/freeze-checks.py")

#: un git de banc, sans configuration d'hôte
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "banc",
    "GIT_AUTHOR_EMAIL": "banc@example.invalid",
    "GIT_COMMITTER_NAME": "banc",
    "GIT_COMMITTER_EMAIL": "banc@example.invalid",
}

OLD = "src/ameesh/fire.py"
NEW = "src/ameesh/modules/x.py"


def class_of(classes):
    """``class_of`` d'une politique simulée : chemin -> (classe, règle)."""
    def lookup(path):
        cls, rule = classes[path]
        return cls, tuple(rule) if rule else None
    return lookup


class ReadRenamesTest(unittest.TestCase):
    """Le format transmis par le gel : des paires, jamais une forme devinée."""

    def test_lit_et_trie_les_paires(self):
        self.assertEqual(guard.read_renames(json.dumps([[OLD, NEW]])),
                         [guard.Move(OLD, NEW)])
        text = json.dumps([[NEW, OLD], [OLD, NEW], [OLD, NEW]])
        self.assertEqual(guard.read_renames(text),
                         [guard.Move(OLD, NEW), guard.Move(NEW, OLD)])

    def test_refuse_une_forme_inconnue(self):
        for raw in ("pas du json", "{}", '"x"', json.dumps([[OLD]]),
                    json.dumps([[OLD, NEW, "x"]]),
                    json.dumps([{"old": OLD, "new": NEW}])):
            with self.subTest(raw=raw):
                with self.assertRaises(guard.GuardError):
                    guard.read_renames(raw)

    def test_refuse_un_chemin_hors_du_depot(self):
        for path in ("/etc/passwd", "../x", "src//x", "src/../x", "src\\x",
                     "./src/x.py"):
            with self.subTest(path=path):
                with self.assertRaises(guard.GuardError):
                    guard.read_renames(json.dumps([[path, NEW]]))

    def test_refuse_un_renommage_vers_lui_meme(self):
        with self.assertRaises(guard.GuardError):
            guard.read_renames(json.dumps([[OLD, OLD]]))

    def test_une_liste_vide_est_licite(self):
        self.assertEqual(guard.read_renames("[]"), [])


class CheckTest(unittest.TestCase):
    """Le verdict : la classe la plus forte des deux chemins l'emporte."""

    def test_un_deplacement_sensible_vers_light_est_refuse(self):
        findings = guard.check(
            [guard.Move(OLD, NEW)],
            class_of({OLD: ("sensitive", ["src/ameesh/fire.py"]),
                      NEW: ("light", None)}))
        self.assertEqual(len(findings), 1)
        text = findings[0].render()
        self.assertIn(NEW, text)
        self.assertIn("(light)", text)
        self.assertIn("(sensitive", text)
        self.assertIn("classe sensitive", text)

    def test_une_classe_egale_ou_plus_forte_est_acceptee(self):
        cases = [
            ({OLD: ("sensitive", ["a"]), NEW: ("sensitive", ["b"])}, 0),
            ({OLD: ("light", None), NEW: ("sensitive", ["b"])}, 0),
            ({OLD: ("normal", None), NEW: ("light", None)}, 1),
        ]
        for classes, expected in cases:
            with self.subTest(classes=classes):
                findings = guard.check([guard.Move(OLD, NEW)], class_of(classes))
                self.assertEqual(len(findings), expected)

    def test_une_classe_inconnue_refuse(self):
        with self.assertRaises(guard.GuardError):
            guard.check([guard.Move(OLD, NEW)],
                        class_of({OLD: ("urgent", None), NEW: ("light", None)}))


class WiringTest(unittest.TestCase):
    """Une garde non branchée ne refuserait aucun gel, et inversement."""

    def test_branchee_dans_les_controles_de_gel(self):
        commands = [args for _name, args in freeze_script.CHECKS]
        self.assertIn(["scripts/garde-renommages-classe.py"], commands)

    def test_la_meme_variable_des_deux_cotes(self):
        self.assertEqual(guard.RENAMES_ENV, freeze_checks.RENAMES_ENV)
        self.assertEqual(freeze_script.RENAMES_ENV, freeze_checks.RENAMES_ENV)


class Repo:
    """Un dépôt jetable : la cible porte un fichier sensible, le lot le déplace."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-7777-")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update(GIT_ENV)
        self.git("init", "-q", "-b", "main")
        self.write(OLD, "sensible\n")
        self.base = self.commit("base")
        self.git("checkout", "-q", "-b", "lot")

    def git(self, *args) -> str:
        proc = subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, relative, text):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, message) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def move(self):
        self.write("src/ameesh/modules/.keep", "")
        self.git("mv", OLD, NEW)
        return self.commit("move")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class FrozenRangeTest(unittest.TestCase):
    """Le garde du gel lit les paires dans le diff depuis le merge-base."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.item = {"id": 7777, "app": "ameesh", "branch_target": "main"}

    def test_renamed_paths_rend_les_paires_du_diff(self):
        head = self.repo.move()
        self.assertEqual(
            freeze_checks.renamed_paths(None, self.item, self.repo.root, head),
            [[OLD, NEW]])
        self.assertEqual(
            freeze_checks.renamed_paths(None, self.item, self.repo.root,
                                        self.repo.base),
            [])

    def test_renamed_paths_doute_quand_la_cible_manque(self):
        head = self.repo.move()
        item = dict(self.item, branch_target="absente")
        self.assertIsNone(
            freeze_checks.renamed_paths(None, item, self.repo.root, head))


class RunEnvTest(unittest.TestCase):
    """`freeze_checks.run` transmet la liste, et ne laisse rien hériter."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def commit_script(self, source):
        self.repo.write(freeze_checks.SCRIPT, source)
        return self.repo.commit("script")

    def test_la_liste_est_transmise(self):
        source = ("import json, os, sys\n"
                  "raw = os.environ.get('AMEESH_FREEZE_RENAMES')\n"
                  "print('renames=' + str(raw))\n"
                  "sys.exit(0 if json.loads(raw) == "
                  + repr([[OLD, NEW]]) + " else 1)\n")
        sha = self.commit_script(source)
        result = freeze_checks.run(self.repo.root, sha, renames=[[OLD, NEW]])
        self.assertEqual(result["result"], "green")
        self.assertIn(OLD, result["output"])

    def test_sans_liste_la_variable_est_retiree(self):
        sha = self.commit_script(
            "import os, sys\n"
            "sys.exit(1 if os.environ.get('AMEESH_FREEZE_RENAMES') else 0)\n")
        with mock.patch.dict(os.environ,
                             {freeze_checks.RENAMES_ENV:
                              json.dumps([[OLD, NEW]])}):
            self.assertEqual(freeze_checks.run(self.repo.root, sha)["result"],
                             "green")


class CanonReelTest(unittest.TestCase):
    """La garde sous un canon git réel, configuré comme celui de l'hôte."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-7777-canon-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.canon = os.path.join(self.root, "canon")
        os.makedirs(self.canon)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_")}
        env.update(GIT_ENV)
        subprocess.run(["git", *args], cwd=self.canon, env=env, check=True,
                       capture_output=True, text=True)

    def write_canon(self, *rules, fallback=True):
        text = (
            'federation: "0.1"\n'
            "id: test\n"
            "members: []\n"
            "review_policies:\n"
            "  risk_classes:\n")
        if fallback:
            text += "    default: normal\n"
        text += (
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
        if not os.path.isdir(os.path.join(self.canon, ".git")):
            self.git("init", "-q", "-b", "main")
        self.git("add", "federation.yaml")
        self.git("commit", "-qm", "canon")

    def config(self):
        path = os.path.join(self.root, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"canons": [{"path": self.canon, "ref": "main"}]}, fh)
        return path

    def run_guard(self, *args, renames=((OLD, NEW),)):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AMEESH_CANON", "AMEESH_CANONS",
                                       "AGENT_MESH_CONFIG", "AGENT_MESH_CANON"))}
        env["AMEESH_CONFIG"] = self.config()
        env["PYTHONPATH"] = str(REPO / "src")
        env["HOME"] = os.path.join(self.root, "home")
        env.pop(guard.RENAMES_ENV, None)
        os.makedirs(env["HOME"], exist_ok=True)
        argv = [sys.executable, str(REPO / "scripts" / "garde-renommages-classe.py"),
                "--project", "ameesh", *args]
        if renames is not None:
            argv += ["--renames", json.dumps(list(renames))]
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    def test_refuse_un_deplacement_sensible_vers_light(self):
        self.write_canon([OLD])
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("REFUSÉ", proc.stdout)
        self.assertIn(NEW, proc.stdout)
        self.assertIn("classe sensitive", proc.stdout)

    def test_accepte_quand_le_canon_nomme_le_nouveau(self):
        self.write_canon([OLD, NEW])
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("classe de relecture conservée", proc.stdout)

    def test_une_liste_illisible_refuse(self):
        self.write_canon([OLD, NEW])
        proc = self.run_guard("--renames", "pas du json", renames=None)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("illisibles", proc.stderr)

    def test_sans_liste_le_controle_est_inactif(self):
        self.write_canon([OLD])
        proc = self.run_guard(renames=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("inactif", proc.stdout)

    def test_un_projet_non_declare_refuse(self):
        self.write_canon([OLD, NEW], fallback=False)
        proc = self.run_guard("--project", "inconnu")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


class GelTest(unittest.TestCase):
    """Le refus arrive par le gel, sur l'arbre du commit gelé."""

    WRAPPER = """\
import subprocess, sys
sys.exit(subprocess.call([sys.executable, "scripts/garde-renommages-classe.py",
                          "--canon", %r, "--ref", "main"]))
"""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.canon = tempfile.mkdtemp(prefix="ameesh-7777-gel-")
        self.addCleanup(shutil.rmtree, self.canon, True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.canon], check=True)
        self.write_canon([OLD])
        self.repo.write("scripts/garde-renommages-classe.py",
                        (REPO / "scripts" / "garde-renommages-classe.py")
                        .read_text(encoding="utf-8"))
        self.repo.write(freeze_checks.SCRIPT, self.WRAPPER % self.canon)
        self.repo.write("src/ameesh/modules/.keep", "")
        self.repo.git("mv", OLD, NEW)
        self.head = self.repo.commit("scripts et déplacement")

    def write_canon(self, rule):
        text = (
            'federation: "0.1"\nid: test\nmembers: []\n'
            "review_policies:\n  risk_classes:\n    default: normal\n"
            "    projects:\n      ameesh:\n        default: light\n"
            "        rules:\n          - paths:\n")
        for path in rule:
            text += '              - "%s"\n' % path
        text += "            class: sensitive\n"
        with open(os.path.join(self.canon, "federation.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)
        env = dict(os.environ, **GIT_ENV)
        subprocess.run(["git", "-C", self.canon, "add", "federation.yaml"],
                       check=True, env=env)
        subprocess.run(["git", "-C", self.canon, "commit", "-qm", "canon"],
                       check=True, env=env)

    def run_gate(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(REPO / "src")}):
            return freeze_checks.run(self.repo.root, self.head,
                                     renames=[[OLD, NEW]])

    def test_un_renommage_sensible_vers_light_refuse_le_gel(self):
        with self.assertRaises(freeze_checks.Refused) as ctx:
            self.run_gate()
        self.assertEqual(ctx.exception.cause, "checks-red")
        self.assertIn("REFUSÉ", str(ctx.exception))
        self.assertIn(NEW, str(ctx.exception))

    def test_un_canon_etendu_accepte_le_gel(self):
        self.write_canon([OLD, NEW])
        self.assertEqual(self.run_gate()["result"], "green")


class BeforeFreezeTest(unittest.TestCase):
    """Le gel refuse un diff de renommages illisible, et transmet la liste."""

    ITEM = {"id": 7777, "app": "ameesh", "branch_target": "main"}

    def run_before(self, **patches):
        green = {"result": "green", "seconds": 0.1, "output": ""}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                freeze_checks.work, "get", return_value=self.ITEM))
            stack.enter_context(mock.patch.object(
                freeze_checks, "resolve", return_value=("/depot", "a" * 40)))
            stack.enter_context(mock.patch.object(
                freeze_checks, "changed_paths", return_value=[]))
            stack.enter_context(mock.patch.object(
                freeze_checks, "gate_paths", return_value=[]))
            stack.enter_context(mock.patch.object(
                lot_class_mod, "repository", return_value=None))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(freeze_checks, name,
                                                       **value))
            return freeze_checks.before_freeze(None, None, 7777, "a" * 40)

    def test_un_diff_illisible_refuse_le_gel(self):
        with self.assertRaises(freeze_checks.Refused) as ctx:
            self.run_before(renamed_paths={"return_value": None})
        self.assertEqual(ctx.exception.cause, "checks-incomplete")
        self.assertIn("renames", str(ctx.exception))

    def test_la_liste_est_transmise_au_script(self):
        green = {"result": "green", "seconds": 0.1, "output": ""}
        run = mock.Mock(return_value=green)
        self.run_before(renamed_paths={"return_value": [[OLD, NEW]]},
                        run={"new": run})
        self.assertEqual(run.call_args.kwargs["renames"], [[OLD, NEW]])


if __name__ == "__main__":
    unittest.main()
