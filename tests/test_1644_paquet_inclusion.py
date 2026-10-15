# SPDX-License-Identifier: AGPL-3.0-only
"""#1644 — contrôle d'inclusion d'un paquet §12 avant le feu.

Le banc monte un dépôt git jetable qui reproduit les cas du constat : un lot
composé et présent, un lot composé puis reverté sans note, un lot composé puis
adapté (contenu partiel), un lot retiré et consigné au manifeste, et un lot
admis mais jamais composé. Aucune base, aucun réseau : git local seulement.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


inclusion = _load("test_paquet_inclusion_1644", "paquet-inclusion.py")

MANIFEST = """\
# Paquet de test — manifeste de composition

## 0. Admissions de la coupure (liste attendue)

- #100 — admission pilote 05:17
- #200 — route 14/10 23:16
- #300 — admission pilote 05:43
- #400 — admission pilote 06:03
- #500 — admission pilote 05:51

## 2. Composition (ordre, tête source, commit)

| # | lot(s) | tête source | commit | note |
|---|---|---|---|---|
| 1 | #100 | `{src100}` | `{c100}` |  |
| 2 | #200 | `{src200}` | `{c200}` |  |
| 3 | #300 | `{src300}` | `{c300}` |  |
| 4 | #400 | `{src400}` | `{c400}` |  |

## 4. Retraits (exclusions)

| Lot | Échecs | Commits retirés (revert) |
|---|---|---|
| #400 | témoin rouge | `{r400}` |
"""


class PaquetInclusionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-inclusion-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "depot"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("base.txt", "ligne1\nligne2\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

        self.src100 = self.lot_file("100", "cent.txt", "alpha\nbeta\n")
        self.c100 = self.merge("100")
        self.src200 = self.lot_file("200", "deux.txt", "gamma\ndelta\n")
        self.c200 = self.merge("200")
        self.r200 = self.revert(self.c200)

        self.src300 = self.lot_file("300", "trois.txt", "epsilon\nzeta\n")
        self.c300 = self.merge("300")
        # adaptation tardive : la ligne « zeta » du lot est réécrite
        self.write("trois.txt", "epsilon\neta\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "adaptation du lot 300")

        self.src400 = self.lot_file("400", "quatre.txt", "iota\nkappa\n")
        self.c400 = self.merge("400")
        self.r400 = self.revert(self.c400)
        self.head = self.rev("HEAD")

        self.manifest = pathlib.Path(self.tmp.name) / "manifeste.md"
        self.write_manifest()

    # -- helpers -----------------------------------------------------------
    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} a échoué : "
                f"{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.decode().strip()

    def write(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.rev("HEAD")

    def lot_file(self, lot: str, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", f"lot/{lot}", "main")
        self.write(name, content)
        head = self.commit(f"#{lot} : {name}")
        self.git("switch", "-q", "main")
        return head

    def merge(self, lot: str) -> str:
        self.git("merge", "-q", "--no-ff", "-m", f"#{lot}: compose sur le paquet",
                 f"lot/{lot}")
        return self.rev("HEAD")

    def revert(self, merge: str) -> str:
        self.git("revert", "-m", "1", "--no-edit", merge)
        return self.rev("HEAD")

    def write_manifest(self, c100: str | None = None) -> None:
        self.manifest.write_text(
            MANIFEST.format(
                src100=self.src100[:12], c100=(c100 or self.c100)[:12],
                src200=self.src200[:12], c200=self.c200[:12],
                src300=self.src300[:12], c300=self.c300[:12],
                src400=self.src400[:12], c400=self.c400[:12],
                r400=self.r400[:12],
            ),
            encoding="utf-8",
        )

    def namespace(self, expected=(), expected_file=(), excused=(),
                  strict=False, head=None, manifest=None):
        return inclusion.argparse.Namespace(
            manifest=str(manifest or self.manifest),
            head=head or self.head,
            repo=str(self.repo),
            expected=list(expected),
            expected_file=list(expected_file),
            excused=list(excused),
            excused_file=[],
            json=False,
            strict=strict,
        )

    def control(self, *extra: str):
        argv = [
            "--manifest", str(self.manifest),
            "--head", self.head,
            "--repo", str(self.repo),
            *extra,
        ]
        return inclusion.main(argv)

    # -- tests -------------------------------------------------------------
    def test_omission_absente_du_manifeste_et_contenu_reverte(self) -> None:
        self.assertEqual(self.control(), 1)

    def test_rapport_distingue_les_cas(self) -> None:
        code, payload = inclusion.run(self.namespace())
        self.assertEqual(code, 1)
        manquants = {item["lot"] for item in payload["omissions"]}
        self.assertEqual(manquants, {500})  # admis, absent du manifeste
        retires = {item["lot"] for item in payload["removed"]}
        self.assertEqual(retires, {200})    # composé puis reverté sans note
        self.assertNotIn(400, manquants)    # retrait consigné au manifeste
        contenus = {item["lot"]: item["verdict"] for item in payload["contents"]}
        self.assertEqual(contenus[100], "présent")
        self.assertEqual(contenus[300], "partiel")
        verdicts = {entry["lots"][0]: entry["verdict"] for entry in payload["entries"]}
        self.assertEqual(verdicts[100], "présent")
        self.assertEqual(verdicts[200], "absent")
        self.assertEqual(verdicts[300], "partiel")
        self.assertEqual(verdicts[400], "absent")

    def test_strict_compte_le_partiel_comme_defaut(self) -> None:
        code, payload = inclusion.run(self.namespace(strict=True))
        self.assertEqual(code, 1)
        self.assertTrue(any("#300" in error for error in payload["errors"]))

    def test_liste_attendue_par_option_prime_sur_le_manifeste(self) -> None:
        code, payload = inclusion.run(self.namespace(expected=[100, 300]))
        self.assertEqual(payload["expected"], [100, 300])
        self.assertEqual(payload["expected_source"], "options")
        self.assertEqual(code, 1)  # le contenu du lot 300 est partiel
        self.assertEqual(payload["omissions"], [])
        self.assertEqual([item["lot"] for item in payload["removed"]], [200])

    def test_aucune_omission_quand_le_retrait_est_consigne(self) -> None:
        _code, payload = inclusion.run(self.namespace(expected=[400]))
        exclusions = {item["lot"] for item in payload["exclusions"]}
        self.assertIn(400, exclusions)
        self.assertEqual(payload["omissions"], [])

    def test_mention_faible_ne_leve_pas_l_omission(self) -> None:
        # Prose sous « Retraits » qui cite #500 sans le retirer (paquet 11h :
        # « retour de radar.py à l'état #1333 ») : l'omission reste bloquante.
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write("\nRetour des tests à l'état #500, conservé.\n")
        code, payload = inclusion.run(self.namespace(expected=[500]))
        self.assertEqual(code, 1)
        self.assertEqual([item["lot"] for item in payload["omissions"]], [500])
        self.assertIn("mention faible", payload["omissions"][0]["reason"])

    def test_fichier_attendu_lisible(self) -> None:
        fichier = pathlib.Path(self.tmp.name) / "attendus.txt"
        fichier.write_text(
            "# commentaire\n100  admission pilote\n300, 400\n", encoding="utf-8"
        )
        code, payload = inclusion.run(
            self.namespace(expected_file=[str(fichier)])
        )
        self.assertEqual(payload["expected"], [100, 300, 400])
        self.assertEqual(code, 1)

    def test_commit_inconnu_est_un_defaut(self) -> None:
        self.write_manifest(c100="0" * 12)
        code, payload = inclusion.run(self.namespace())
        self.assertEqual(code, 1)
        self.assertTrue(any(entry["verdict"] == "commit_inconnu"
                            for entry in payload["entries"]))

    def test_json_et_sortie_texte(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "paquet-inclusion.py"),
             "--manifest", str(self.manifest), "--head", self.head,
             "--repo", str(self.repo), "--json"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "ameesh-paquet-inclusion/1")
        self.assertTrue(payload["errors"])
        texte = inclusion.render(payload)
        self.assertIn("INCLUSIONS MANQUANTES", texte)
        self.assertIn("#500", texte)

    def test_suppression_seule_n_est_pas_binaire(self) -> None:
        self.git("switch", "-q", "-c", "lot/600", "main")
        self.write("base.txt", "ligne1\n")
        source = self.commit("#600 : retire une ligne")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "#600: compose", "lot/600")
        head = self.rev("HEAD")
        manifest = pathlib.Path(self.tmp.name) / "manifeste-600.md"
        manifest.write_text(
            textwrap.dedent(
                """\
                # Manifeste

                | # | lot(s) | tête source | commit | note |
                |---|---|---|---|---|
                | 1 | #600 | `%s` | `%s` |  |
                """
            ) % (source[:12], head[:12]),
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(head=head, manifest=manifest)
        )
        self.assertEqual(code, 0)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "présent")
        self.assertTrue(all(check["verdict"] != "binaire"
                            for check in entry["paths"]))

    def test_sans_liste_attendue_le_controle_le_dit(self) -> None:
        self.manifest.write_text(
            textwrap.dedent(
                """\
                # Manifeste sans section d'admissions

                | # | lot(s) | tête source | commit | note |
                |---|---|---|---|---|
                | 1 | #100 | `%s` | `%s` |  |
                """
            ) % (self.src100[:12], self.c100[:12]),
            encoding="utf-8",
        )
        code, payload = inclusion.run(self.namespace())
        self.assertEqual(code, 0)
        self.assertIsNone(payload["expected_source"])
        self.assertIn("NON effectué", inclusion.render(payload))


if __name__ == "__main__":
    unittest.main()
