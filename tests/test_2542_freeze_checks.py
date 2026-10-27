# SPDX-License-Identifier: AGPL-3.0-only
"""#2542 : les contrôles statiques passent avant le gel, pas seulement en CI
de paquet.

Témoins : le script d'ameesh (`scripts/freeze-checks.py` : chaque test nommé
existe, un contrôle rouge rend 1 et montre sa sortie, une erreur de syntaxe
rougit la compilation, hors d'un arbre ameesh 2) ; le garde
(`ameesh.freeze_checks` : arbre sans script, vert, rouge, arbre du commit et
non arbre de travail, délai dépassé, résolution du dépôt) ; puis sur Postgres
la CLI `work milestone … frozen` : refus sans jalon, passage outre motivé dans
la note, vert, options refusées hors d'un gel."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import freeze_checks, prevalidation_calls, registry, work
from ameesh.deadline import Deadline

from .support import PgTestCase

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "freeze_checks_script", REPO / "scripts" / "freeze-checks.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

GREEN = "print('compilation ok')\n"
RED = "import sys\nprint('attribution : faute')\nsys.exit(1)\n"


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


class Repo:
    """Un dépôt jetable ; `commit(script=…)` écrit `scripts/freeze-checks.py`."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-2542-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True, env=ENV)
        # #2786 : un gel n'est jamais la tête de la cible (aucun commit
        # propre) ; un socle sur main, puis les commits sur une branche de lot.
        _git(self.root, "commit", "-q", "--allow-empty", "-m", "socle")
        _git(self.root, "checkout", "-q", "-b", "lot/test")

    def commit(self, *, script: str | None = None, path: str = "lot.txt",
               text: str = "lot\n") -> str:
        files = {path: text}
        if script is not None:
            files[freeze_checks.SCRIPT] = script
        for rel, content in files.items():
            full = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            _git(self.root, "add", rel)
        _git(self.root, "commit", "-q", "-m", "c")
        return _git(self.root, "rev-parse", "HEAD")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ScriptTest(unittest.TestCase):
    """Le script d'ameesh : ses contrôles, son verdict, sa sortie."""

    def tree(self) -> str:
        root = tempfile.mkdtemp(prefix="ameesh-2542-arbre-")
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("src/ameesh", "scripts", "tests"):
            os.makedirs(os.path.join(root, rel))
        return root

    def main(self, root: str, checks=None) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(script.os, "getcwd", return_value=root), \
                mock.patch.object(script, "CHECKS", checks or script.CHECKS), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = script.main([])
        return code, out.getvalue()

    def test_chaque_test_nomme_existe(self):
        """Un contrôle renommé ou retiré de la suite ne passe pas en silence."""
        nommes = [arg for _nom, args in script.CHECKS if "unittest" in args
                  for arg in args if arg.startswith("tests.")]
        self.assertTrue(nommes)
        for ident in nommes:
            module, classe = ident.rsplit(".", 1)
            fichier = REPO.joinpath(*module.split(".")).with_suffix(".py")
            self.assertTrue(fichier.is_file(), ident)
            classes = {n.name for n in ast.parse(fichier.read_text(encoding="utf-8")).body
                       if isinstance(n, ast.ClassDef)}
            self.assertIn(classe, classes, ident)

    def test_rouge_rend_un_et_montre_sa_sortie(self):
        code, out = self.main(self.tree(), (
            ("vert", ["-c", "pass"]),
            ("rouge", ["-c", "import sys; print('la faute'); sys.exit(3)"])))
        self.assertEqual(code, 1)
        self.assertIn("ok  vert", out)
        self.assertIn("RED rouge", out)
        self.assertIn("    la faute", out)

    def test_tous_verts_rend_zero(self):
        code, out = self.main(self.tree(), (("vert", ["-c", "pass"]),))
        self.assertEqual(code, 0)
        self.assertNotIn("RED", out)

    def test_erreur_de_syntaxe_rougit_la_compilation(self):
        root = self.tree()
        pathlib.Path(root, "tests", "test_casse.py").write_text("def (:\n", encoding="utf-8")
        compilation = [c for c in script.CHECKS if c[0] == "compilation"]
        code, out = self.main(root, tuple(compilation))
        self.assertEqual(code, 1)
        self.assertIn("RED compilation", out)
        self.assertIn("test_casse.py", out)

    def test_hors_d_un_arbre_ameesh(self):
        root = tempfile.mkdtemp(prefix="ameesh-2542-vide-")
        self.addCleanup(shutil.rmtree, root, True)
        code, _out = self.main(root)
        self.assertEqual(code, 2)

    def test_garde_mecanique_refuse_un_litteral_et_montre_le_constat(self):
        """#2809 : le vrai garde du projet tourne sur l'arbre contrôlé."""
        root = self.tree()
        shutil.copyfile(REPO / "scripts/garde-mecanique.py",
                        pathlib.Path(root, "scripts/garde-mecanique.py"))
        checks = tuple(c for c in script.CHECKS
                       if "scripts/garde-mecanique.py" in c[1])
        self.assertEqual(len(checks), 1)
        source = pathlib.Path(root, "src/ameesh/example.py")
        source.write_text("def f(*, distinct_families=False):\n    pass\n",
                          encoding="utf-8")
        code, out = self.main(root, checks)
        self.assertEqual(code, 1, out)
        self.assertIn("RED garde mécanique", out)
        self.assertIn("src/ameesh/example.py", out)
        self.assertIn("reviews.distinct_families", out)
        # Une lecture dynamique du canon est acceptée par le même contrôle.
        source.write_text("distinct_families = policy.distinct_families\n",
                          encoding="utf-8")
        code, out = self.main(root, checks)
        self.assertEqual(code, 0, out)
        self.assertIn("ok  garde mécanique", out)


class GuardTest(unittest.TestCase):
    """`freeze_checks` : le script du commit gelé, sur l'arbre de ce commit."""

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def test_sans_script_rien_a_passer(self):
        sha = self.repo.commit()
        self.assertEqual(freeze_checks.run(self.repo.root, sha), {"result": "none"})

    def test_vert(self):
        sha = self.repo.commit(script=GREEN)
        result = freeze_checks.run(self.repo.root, sha)
        self.assertEqual(result["result"], "green")
        self.assertIn("compilation ok", result["output"])

    def test_rouge_refuse_avec_la_sortie_et_l_echappatoire(self):
        sha = self.repo.commit(script=RED)
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha)
        self.assertEqual(ctx.exception.cause, "checks-red")
        self.assertIn("attribution : faute", str(ctx.exception))
        self.assertIn("--no-static-checks", str(ctx.exception))
        self.assertIsInstance(ctx.exception, work.WorkError)

    def test_arbre_du_commit_et_non_arbre_de_travail(self):
        """Une correction non commitée ne rend pas vert le commit gelé."""
        sha = self.repo.commit(script=RED)
        pathlib.Path(self.repo.root, freeze_checks.SCRIPT).write_text(GREEN, encoding="utf-8")
        pathlib.Path(self.repo.root, "non-suivi.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha)
        self.assertEqual(ctx.exception.cause, "checks-red")
        # et le script voit l'arbre du commit, sans le fichier non suivi
        sha = self.repo.commit(script="import os, sys\n"
                                      "sys.exit(1 if os.path.exists('non-suivi.txt') else 0)\n")
        self.assertEqual(freeze_checks.run(self.repo.root, sha)["result"], "green")

    def test_delai_depasse_refuse(self):
        sha = self.repo.commit(script="import time\ntime.sleep(120)\n")
        with self.assertRaises(freeze_checks.Refused) as ctx:
            freeze_checks.run(self.repo.root, sha, timeout=1)
        self.assertEqual(ctx.exception.cause, "checks-incomplete")

    def test_resolution_du_depot(self):
        sha = self.repo.commit()
        ailleurs = tempfile.mkdtemp(prefix="ameesh-2542-ailleurs-")
        self.addCleanup(shutil.rmtree, ailleurs, True)
        with mock.patch.object(freeze_checks.os, "getcwd", return_value=ailleurs):
            # ni le répertoire courant, ni de dépôt de projet : rien à contrôler
            self.assertEqual(freeze_checks.resolve(sha), (None, None))
            # le dépôt du projet du lot porte le SHA
            self.assertEqual(freeze_checks.resolve(sha, project_repo=self.repo.root),
                             (self.repo.root, sha))
            # un SHA abrégé se lit en entier
            self.assertEqual(freeze_checks.resolve(sha[:12], project_repo=self.repo.root),
                             (self.repo.root, sha))
            self.assertEqual(freeze_checks.resolve("a" * 40, project_repo=self.repo.root),
                             (None, None))
            # un --repo nommé qui ne porte pas le SHA refuse
            with self.assertRaises(freeze_checks.Refused) as ctx:
                freeze_checks.resolve("a" * 40, repo=self.repo.root)
            self.assertEqual(ctx.exception.cause, "sha-unknown")
        with mock.patch.object(freeze_checks.os, "getcwd", return_value=self.repo.root):
            self.assertEqual(freeze_checks.resolve(sha), (self.repo.root, sha))

    def test_depuis_un_sous_repertoire(self):
        """Le gel depuis src/ voit le script et l'arbre de la racine (#2567)."""
        sha = self.repo.commit(script=GREEN)
        sub = os.path.join(self.repo.root, "src")
        os.makedirs(sub)
        self.assertTrue(freeze_checks.declared(self.repo.root, sha))
        self.assertTrue(freeze_checks.declared(sub, sha))
        self.assertEqual(freeze_checks.run(sub, sha)["result"], "green")
        with mock.patch.object(freeze_checks.os, "getcwd", return_value=sub):
            self.assertEqual(freeze_checks.resolve(sha),
                             (os.path.realpath(self.repo.root), sha))

    def test_depot_nu_reste_lisible(self):
        """La normalisation de racine ne casse pas un dépôt nu (#2567)."""
        sha = self.repo.commit(script=GREEN)
        parent = tempfile.mkdtemp(prefix="ameesh-2542-nu-")
        self.addCleanup(shutil.rmtree, parent, True)
        bare = os.path.join(parent, "repo.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo.root, bare],
                       check=True, env=ENV)
        self.assertTrue(freeze_checks.declared(bare, sha))
        self.assertEqual(freeze_checks.run(bare, sha)["result"], "green")
        self.assertEqual(freeze_checks.resolve(sha, repo=bare), (bare, sha))


class CliTest(PgTestCase):
    """Le seul appelant : `ameesh work milestone <lot> frozen`."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        registry.upsert(self.db, "author", harness="deepseek", host="pc")
        self.lot = work.add(self.db, title="un lot", assignee="author")["id"]

    def freeze(self, sha, *extra):
        return self.mesh("work", "milestone", str(self.lot), "frozen", "--sha", sha,
                         "--actor", "author", "--repo", self.repo.root, *extra,
                         env=self.env(AGENT_MAIL_NAME="author"))

    def frozen(self) -> list:
        return [m for m in work.milestones(self.db, self.lot) if m["kind"] == "frozen"]

    def test_symbolic_refs_refused_even_when_checks_skipped(self):
        self.repo.commit(script=GREEN)
        for ref in ("HEAD", "main", "HEAD~0", "refs/heads/main", ""):
            with self.subTest(ref=ref):
                proc = self.freeze(ref, "--no-static-checks", "incident")
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("head-missing", proc.stderr)
                self.assertEqual(self.frozen(), [])

    def test_abbreviation_is_stored_as_full_commit(self):
        sha = self.repo.commit(script=GREEN)
        proc = self.freeze(sha[:12].upper())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([m["sha"] for m in self.frozen()], [sha])

    def test_short_and_unknown_abbreviations_refused(self):
        sha = self.repo.commit(script=GREEN)
        for ref, cause in ((sha[:7], "head-missing"),
                           ("deadbeefdead", "head-unresolved")):
            with self.subTest(ref=ref):
                proc = self.freeze(ref)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn(cause, proc.stderr)
                self.assertEqual(self.frozen(), [])

    def test_direct_api_refuses_symbolic_ref_before_hook(self):
        self.repo.commit(script=GREEN)
        hook = mock.Mock()
        with self.assertRaisesRegex(work.WorkError, "head-missing"):
            work.milestone(self.db, self.lot, "frozen", sha="HEAD",
                           repo=self.repo.root, on_frozen=hook)
        hook.assert_not_called()
        self.assertEqual(self.frozen(), [])

    def test_direct_api_expands_abbreviation_before_hook(self):
        sha = self.repo.commit(script=GREEN)
        seen = []
        work.milestone(self.db, self.lot, "frozen", sha=sha[:12],
                       repo=self.repo.root,
                       on_frozen=lambda tx, milestone, threads: seen.append(milestone["sha"]))
        self.assertEqual(seen, [sha])
        self.assertEqual([m["sha"] for m in self.frozen()], [sha])

    def test_rouge_refuse_le_gel_sans_jalon(self):
        proc = self.freeze(self.repo.commit(script=RED))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("freeze refused (checks-red)", proc.stderr)
        self.assertIn("attribution : faute", proc.stderr)
        self.assertEqual(self.frozen(), [])

    def test_passer_outre_demande_une_raison_ecrite_dans_la_note(self):
        sha = self.repo.commit(script=RED)
        proc = self.freeze(sha, "--no-static-checks", "  ")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.frozen(), [])
        proc = self.freeze(sha, "--no-static-checks", "incident #1", "--note", "regel")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        note = self.frozen()[0]["note"]
        self.assertIn("regel — static checks skipped: incident #1", note)
        # #2786 (5) : la marque machine des branches absentes au gel suit.
        self.assertIn("ameesh-freeze-absent: main", note)

    def test_vert_gele_et_le_dit(self):
        sha = self.repo.commit(script=GREEN)
        proc = self.freeze(sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("static checks: green at %s" % sha[:12], proc.stdout)
        self.assertIn("compilation ok", proc.stdout)
        self.assertEqual([m["sha"] for m in self.frozen()], [sha])

    def test_sans_depot_qui_porte_le_sha_le_gel_passe_et_le_dit(self):
        proc = self.mesh("work", "milestone", str(self.lot), "frozen", "--sha", "a" * 40,
                         "--actor", "author",
                         env=self.env(AGENT_MAIL_NAME="author"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("static checks not run", proc.stderr)
        self.assertEqual(len(self.frozen()), 1)

    def test_la_prevalidation_est_appelee_apres_le_gel_sans_le_defaire(self):
        # #2644 : le jalon est écrit avant la prévalidation ; un rouge ou une
        # indisponibilité ne change pas le sort du gel. Le geste est un vrai
        # sous-processus : on lit sa trace au journal, pas un mock.
        sha = self.repo.commit(script=GREEN)
        proc = self.freeze(sha)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([m["sha"] for m in self.frozen()], [sha])
        notes = [e.get("note") or "" for e in work.events(self.db, self.lot)]
        self.assertTrue(any(n.startswith("prevalidation") for n in notes), notes)
        self.assertNotIn("prevalidation: ", proc.stderr)

    def test_options_du_gel_refusees_hors_gel(self):
        proc = self.mesh("work", "milestone", str(self.lot), "verdict", "ok", "--sha", "a" * 40,
                         "--actor", "rev", "--no-static-checks", "x",
                         env=self.env(AGENT_MAIL_NAME="rev"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("only apply to a freeze", proc.stderr)


class FreezeBudgetTest(unittest.TestCase):
    """#4073 : les contrôles statiques ne prennent que le temps qui reste."""

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.sha = self.repo.commit(script=GREEN)

    def before(self, deadline):
        seen = {}

        def run(where, commit, *, gate=None, timeout=freeze_checks.TIMEOUT):
            seen["timeout"] = timeout
            return {"result": "green", "seconds": 0.0, "output": ""}

        with mock.patch("ameesh.work.get", return_value={"id": 4073}), \
                mock.patch.object(freeze_checks, "gate_paths", return_value=None), \
                mock.patch.object(freeze_checks, "run", side_effect=run):
            freeze_checks.before_freeze(None, None, 4073, self.sha,
                                        repo=self.repo.root, deadline=deadline)
        return seen.get("timeout")

    def test_checks_take_only_what_the_freeze_budget_leaves(self):
        now = [100.0]
        deadline = Deadline(120.0, clock=lambda: now[0])
        self.assertEqual(self.before(deadline), 120.0)
        now[0] += 100.0
        self.assertEqual(self.before(deadline), 20.0)

    def test_an_expired_freeze_budget_refuses_before_running(self):
        deadline = Deadline(0.0)
        with mock.patch("ameesh.work.get", return_value={"id": 4073}), \
                mock.patch.object(freeze_checks, "gate_paths", return_value=None), \
                mock.patch.object(freeze_checks, "run") as run:
            with self.assertRaises(freeze_checks.Refused) as caught:
                freeze_checks.before_freeze(None, None, 4073, self.sha,
                                            repo=self.repo.root, deadline=deadline)
        self.assertEqual(caught.exception.cause, "checks-incomplete")
        run.assert_not_called()


class FreezeDeadlineWiringTest(unittest.TestCase):
    """#4073 : `work milestone frozen` partage son échéance en CLI."""

    def test_checks_and_prevalidation_share_the_freeze_budget(self):
        from ameesh import mesh_cli
        now = [1000.0]
        deadline = prevalidation_calls.freeze_deadline(clock=lambda: now[0])
        seen = {}

        def checks(cfg, db, lot, sha, *, deadline=None, **kw):
            seen["checks"] = deadline
            now[0] += 300.0  # contrôles statiques au plafond
            return ""

        def pv(cfg, db, lot, sha, *, deadline=None, **kw):
            seen["pv"] = deadline
            return {}

        args = mesh_cli.build_parser().parse_args([
            "work", "milestone", "4073", "frozen", "--sha", "a" * 40,
            "--actor", "author"])
        row = {"kind": "frozen", "sha": "a" * 40, "note": "", "verdict": None}
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(mesh_cli, "_work_actor"), \
                mock.patch.object(mesh_cli.work, "get", return_value={"id": 4073}), \
                mock.patch.object(mesh_cli.work, "freeze_head", return_value="a" * 40), \
                mock.patch.object(mesh_cli.work, "milestone", return_value=row), \
                mock.patch("ameesh.verdict_carry.carry",
                           return_value={"result": "idle"}), \
                mock.patch.object(prevalidation_calls, "freeze_deadline",
                                  return_value=deadline), \
                mock.patch.object(freeze_checks, "before_freeze",
                                  side_effect=checks), \
                mock.patch.object(prevalidation_calls, "after_freeze",
                                  side_effect=pv), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mesh_cli.cmd_work(object(), args), 0)
        self.assertIs(seen["checks"], deadline)
        self.assertLessEqual(seen["pv"].remaining(),
                             prevalidation_calls.FREEZE_BUDGET_S - 300.0)
        self.assertLessEqual(seen["pv"].remaining(),
                             prevalidation_calls.CALLER_BUDGET_S)
        self.assertLess(seen["pv"].remaining(),
                        prevalidation_calls.CALLER_BUDGET_S)
        self.assertLess(prevalidation_calls.FREEZE_BUDGET_S, 600)


if __name__ == "__main__":
    unittest.main()
