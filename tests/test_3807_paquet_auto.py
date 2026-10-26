# SPDX-License-Identifier: AGPL-3.0-only
"""#3807 — composition scriptée du paquet, sans agent.

Le banc monte un dépôt git jetable : une unité propre, un candidat en conflit
(écarté sans commit), une unité propre après lui, puis le rejeu d'un manifeste
gelé et le contrôle d'inclusion sur le manifeste produit. Aucune base, aucun
réseau.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"
COMPOSE = REPO / "scripts" / "paquet-compose.py"
INCLUSION = REPO / "scripts" / "paquet-inclusion.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_paquet_auto_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_paquet_auto_script"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()


def _has_ref(ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True)
    return proc.returncode == 0


class PaquetAutoBanc(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-auto-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        self.git("config", "user.name", "banc", cwd=self.repo)
        self.git("config", "user.email", "banc@example.invalid", cwd=self.repo)
        self.git("config", "commit.gpgsign", "false", cwd=self.repo)
        self.write("base.txt", "base\n")
        self.write("shared.txt", "base\n")
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-qm", "base", cwd=self.repo)
        self.base = self.rev("HEAD")

    # -- helpers -----------------------------------------------------------
    def git(self, *args: str, cwd: pathlib.Path | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise AssertionError(
                "git %s a échoué : %s" % (" ".join(args), proc.stderr))
        return proc

    def rev(self, ref: str, cwd: pathlib.Path | None = None) -> str:
        return self.git("rev-parse", ref, cwd=cwd).stdout.strip()

    def write(self, name: str, content: str, cwd: pathlib.Path | None = None) -> None:
        path = (cwd or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str, cwd: pathlib.Path | None = None) -> str:
        where = cwd or self.repo
        self.git("add", "-A", cwd=where)
        self.git("commit", "-qm", message, cwd=where)
        return self.rev("HEAD", cwd=where)

    def lot(self, lot: int, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", "lot/%d" % lot, "main", cwd=self.repo)
        self.write(name, content)
        sha = self.commit("#%d : %s" % (lot, name))
        self.git("switch", "-q", "main", cwd=self.repo)
        return sha

    def worktree(self, name: str, head: str = None) -> pathlib.Path:
        path = pathlib.Path(self.tmp.name) / name
        branch = "integration/paquet-%s" % name
        self.git("worktree", "add", "-b", branch, str(path),
                 head or self.base, cwd=self.repo)
        self.addCleanup(self.git, "worktree", "remove", "--force", str(path),
                        cwd=self.repo, check=False)
        return path

    def fixture(self) -> tuple[str, str, str]:
        """Un candidat propre, un conflictuel, un propre après lui."""
        first = self.lot(1, "shared.txt", "un\n")
        conflict = self.lot(2, "shared.txt", "deux\n")
        third = self.lot(3, "trois.txt", "trois\n")
        return first, conflict, third

    # -- témoins -----------------------------------------------------------
    def test_conflit_ecarte_sans_rien_commiter(self) -> None:
        first, conflict, third = self.fixture()
        worktree = self.worktree("banc")
        composition = paquet_auto.compose_candidates(
            worktree,
            [paquet_auto.Candidate(1, first),
             paquet_auto.Candidate(2, conflict),
             paquet_auto.Candidate(3, third)],
            99,
            compose_script=COMPOSE,
            record_package=False,
            log=lambda _line: None,
        )
        self.assertEqual([unit.lot for unit in composition.units], [1, 3])
        self.assertEqual(len(composition.discarded), 1)
        self.assertEqual(composition.discarded[0].lot, 2)
        self.assertEqual(composition.discarded[0].reason, "conflit, coupure suivante")
        self.assertIn("shared.txt", composition.discarded[0].detail)
        # Aucun commit du candidat conflictuel, aucune fusion en cours, arbre propre.
        log = self.git("log", "--first-parent", "--format=%s",
                       cwd=worktree).stdout
        self.assertNotIn("#2", log)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=worktree).stdout, "")
        self.assertNotEqual(
            self.git("rev-parse", "--verify", "--quiet", "MERGE_HEAD",
                     cwd=worktree, check=False).returncode, 0)
        self.assertEqual(self.rev("HEAD", cwd=worktree), composition.head)

    def _fake_candidates(self, payload: dict, code: int) -> str:
        data = pathlib.Path(self.tmp.name) / "candidats.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        fake = (
            "import pathlib, sys\n"
            "print(pathlib.Path(%r).read_text(encoding='utf-8'))\n"
            "sys.exit(%d)\n" % (str(data), code)
        )
        self.write("scripts/paquet-candidats.py", fake)
        return self.commit("faux candidats")

    def test_lot_en_erreur_jamais_compose(self) -> None:
        payload = {
            "schema": "ameesh-packet-candidates/1",
            "base_source": "option",
            "ready": [7, 8],
            "candidates": [
                {"work_item": 7, "source": self.base},
                {"work_item": 8, "source": self.base},
            ],
            "errors": ["#7 : porte illisible"],
            "errors_by_lot": [{"work_item": 7, "error": "porte illisible"}],
        }
        head = self._fake_candidates(payload, 3)
        worktree = self.worktree("candidats", head)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        candidates, got = paquet_auto.compute_candidates(
            worktree, "main", self.base, ready)
        self.assertEqual([candidate.lot for candidate in candidates], [8])
        errors = paquet_auto.error_discards(got)
        self.assertEqual([item.lot for item in errors], [7])
        self.assertIn("jamais composé", errors[0].reason)
        self.assertIn("porte illisible", errors[0].detail)

    def test_erreur_globale_interrompt_la_composition(self) -> None:
        payload = {
            "schema": "ameesh-packet-candidates/1",
            "base_source": "option",
            "ready": [],
            "candidates": [],
            "errors": ["base illisible"],
            "errors_by_lot": [],
        }
        head = self._fake_candidates(payload, 1)
        worktree = self.worktree("candidats-erreur", head)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.compute_candidates(worktree, "main", self.base, ready)
        self.assertIn("base illisible", str(caught.exception))

    def test_manifeste_lu_par_le_controle_d_inclusion(self) -> None:
        first, conflict, third = self.fixture()
        worktree = self.worktree("inclusion")
        composition = paquet_auto.compose_candidates(
            worktree,
            [paquet_auto.Candidate(1, first),
             paquet_auto.Candidate(2, conflict),
             paquet_auto.Candidate(3, third)],
            99,
            compose_script=COMPOSE,
            record_package=False,
            log=lambda _line: None,
        )
        table = paquet_auto.composition_table(worktree, self.base, COMPOSE)
        manifest = paquet_auto.build_manifest(
            name="inclusion", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        rel = "reviews/integration-paquet-inclusion.md"
        self.write(rel, manifest, cwd=worktree)
        head = self.commit("manifeste", cwd=worktree)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        ready.write_text("#1\n#2\n#3\n", encoding="utf-8")
        expected = pathlib.Path(self.tmp.name) / "attendus.txt"
        expected.write_text("#1\n#3\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(INCLUSION), "--repo", str(worktree),
             "--manifest", str(worktree / rel), "--head", head,
             "--ready-file", str(ready), "--expected-file", str(expected)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT : aucun défaut", proc.stdout)
        # La table d'admissions ne cite que les lots composés : #2 est écarté
        # au manifeste, pas attendu.
        parse = self._parse_manifest(worktree / rel)
        self.assertEqual(parse.admissions, [1, 3])
        self.assertIn(2, parse.ecartes)
        self.assertIn("conflit", parse.ecartes[2])

    def _parse_manifest(self, path: pathlib.Path):
        spec = importlib.util.spec_from_file_location("test_paquet_inclusion_script",
                                                      INCLUSION)
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_paquet_inclusion_script"] = module
        spec.loader.exec_module(module)
        return module.parse_manifest(path)

    def test_rejeu_rend_le_meme_arbre(self) -> None:
        first = self.lot(11, "onze.txt", "onze\n")
        second = self.lot(12, "douze.txt", "douze\n")
        reference = self.worktree("reference")
        composition = paquet_auto.compose_candidates(
            reference,
            [paquet_auto.Candidate(11, first),
             paquet_auto.Candidate(12, second)],
            99, compose_script=COMPOSE, record_package=False,
            log=lambda _line: None)
        table = paquet_auto.composition_table(reference, self.base, COMPOSE)
        manifest = paquet_auto.build_manifest(
            name="fixture", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        self.write("reviews/integration-paquet-fixture.md", manifest, cwd=reference)
        ref = self.commit("manifeste de référence", cwd=reference)
        expected_tree = self.rev("HEAD^{tree}", cwd=reference)
        self.git("worktree", "remove", "--force", str(reference), cwd=self.repo)
        code = paquet_auto.replay(
            self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
            log=lambda _line: None)
        self.assertEqual(code, 0)
        # Le rejeu supprime son worktree et sa branche temporaire ; l'arbre
        # comparé est celui de la référence.
        branches = self.git("branch", "--list", "tmp/paquet-auto-rejeu-*",
                            cwd=self.repo).stdout
        self.assertEqual(branches.strip(), "")
        self.assertEqual(self.rev(ref + "^{tree}"), expected_tree)
        # Admission (#3813) : #12 en erreur dans la liste calculée est refusé ;
        # une liste sans erreur laisse passer le même manifeste.
        bad = pathlib.Path(self.tmp.name) / "candidats-legacy.json"
        bad.write_text(json.dumps({
            "base": self.base, "ready": [11],
            "errors": ["#12 : porte illisible"],
        }), encoding="utf-8")
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.replay(
                self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
                candidates_json=bad, log=lambda _line: None)
        self.assertIn("#12", str(caught.exception))
        self.assertIn("erreur", str(caught.exception))
        good = pathlib.Path(self.tmp.name) / "candidats-prets.json"
        good.write_text(json.dumps({
            "base": self.base, "ready": [11, 12], "errors": [],
        }), encoding="utf-8")
        self.assertEqual(paquet_auto.replay(
            self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
            candidates_json=good, log=lambda _line: None), 0)

    @unittest.skipUnless(_has_ref("b57c0380") and _has_ref("f444955b"),
                         "témoin h-31 v3 : références absentes de ce clone")
    def test_rejeu_h31_v3_refuse_le_lot_en_erreur(self) -> None:
        # Le snapshot minimal des entrées h-31 : #3506 est en erreur de porte,
        # les six autres unités de la v4 sont prêtes.
        snapshot = pathlib.Path(self.tmp.name) / "h31-candidats.json"
        snapshot.write_text(json.dumps({
            "base": "f444955b2eebcde5b096175e2c52e7a32bceda06",
            "ready": [3779, 3782, 3755, 3792, 3788, 3735],
            "errors": ["#3506 : provenance de gel illisible (cas #3781)"],
        }), encoding="utf-8")
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.replay(
                REPO, "b57c0380", "h-20261026-31", "f444955b",
                candidates_json=snapshot, log=lambda _line: None)
        self.assertIn("#3506", str(caught.exception))
        self.assertIn("erreur", str(caught.exception))

    @unittest.skipUnless(_has_ref("fc59a14e") and _has_ref("f444955b"),
                         "témoin h-31 v4 : références absentes de ce clone")
    def test_rejeu_h31_v4_arbre_identique(self) -> None:
        self.assertEqual(
            paquet_auto.main(["--repo", str(REPO), "--replay", "fc59a14e",
                              "--name", "h-20261026-31", "--base", "f444955b"]),
            0)


if __name__ == "__main__":
    unittest.main()
