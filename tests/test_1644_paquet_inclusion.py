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
        self.ready_empty = pathlib.Path(self.tmp.name) / "prets-vide.txt"
        self.ready_empty.write_text("", encoding="utf-8")

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
        self.git("merge", "-q", "--no-ff", "-m",
                 f"#{lot}: compose sur le paquet\n\nameesh-work: {lot}",
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
                  ready=(), ready_file=None, strict=False, head=None,
                  manifest=None, no_expected=False, no_ready=False):
        if ready_file is None:
            # Depuis #2464, le contrôle exige toujours la liste calculée : le
            # cas par défaut du banc est « aucun lot prêt » (fichier vide).
            ready_file = [str(self.ready_empty)]
        return inclusion.argparse.Namespace(
            manifest=str(manifest or self.manifest),
            head=head or self.head,
            repo=str(self.repo),
            expected=list(expected),
            expected_file=list(expected_file),
            excused=list(excused),
            excused_file=[],
            ready=list(ready),
            ready_file=list(ready_file),
            json=False,
            strict=strict,
            no_expected=no_expected,
            no_ready=no_ready,
        )

    def control(self, *extra: str):
        argv = [
            "--manifest", str(self.manifest),
            "--head", self.head,
            "--repo", str(self.repo),
            "--ready-file", str(self.ready_empty),
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

    def test_trailer_absent_stoppe_avant_la_ci(self) -> None:
        # #4240 : un merge de lot sans trailer `ameesh-work:` est un défaut
        # bloquant du contrôle d'inclusion, avant la CI complète (constat du
        # paquet Nexlink 229 : détecté au gel, après une CI annulée).
        self.git("switch", "-q", "-c", "lot/601", "main")
        self.write("sixcentun.txt", "six cent un\n")
        source = self.commit("#601 : six cent un")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#601: compose sans trailer", "lot/601")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(601, source, head)
        code, payload = inclusion.run(
            self.namespace(expected=[601], head=head, manifest=manifest)
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            [(item["lot"], item["commit"][:12])
             for item in payload["trailers_missing"]],
            [(601, head[:12])],
        )
        self.assertTrue(any("sans trailer" in error
                            for error in payload["errors"]))
        texte = inclusion.render(payload)
        self.assertIn("TRAILERS DE COMPOSITION MANQUANTS", texte)
        self.assertIn("`ameesh-work: 601`", texte)
        # Le message corrigé, à arbre identique, lève le défaut : c'est le
        # geste que le composeur doit faire avant de demander la CI.
        self.git("commit", "--amend", "-q", "-m",
                 "#601: compose\n\nameesh-work: 601")
        fixed = self.rev("HEAD")
        manifest = self.manifest_simple(601, source, fixed)
        code, payload = inclusion.run(
            self.namespace(expected=[601], head=fixed, manifest=manifest)
        )
        self.assertEqual(code, 0, payload["errors"])
        self.assertEqual(payload["trailers_missing"], [])

    def test_json_et_sortie_texte(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "paquet-inclusion.py"),
             "--manifest", str(self.manifest), "--head", self.head,
             "--repo", str(self.repo), "--json",
             "--ready-file", str(self.ready_empty)],
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
        self.git("merge", "-q", "--no-ff", "-m",
                 "#600: compose\n\nameesh-work: 600", "lot/600")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(600, source, head)
        code, payload = inclusion.run(
            self.namespace(expected=[600], head=head, manifest=manifest)
        )
        self.assertEqual(code, 0)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "présent")
        self.assertTrue(all(check["verdict"] != "binaire"
                            for check in entry["paths"]))

    def test_sans_liste_attendue_le_controle_est_refuse(self) -> None:
        # La liste prête contrôle les omissions de lots prêts, pas les
        # admissions : sauter ce second contrôle reste explicite (#1655 N1),
        # même quand la liste calculée est fournie.
        self.ecrire_manifeste_sans_admissions()
        with self.assertRaises(inclusion.InputError) as caught:
            inclusion.run(self.namespace())
        self.assertIn("--no-expected", str(caught.exception))
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "paquet-inclusion.py"),
             "--manifest", str(self.manifest), "--head", self.head,
             "--repo", str(self.repo),
             "--ready-file", str(self.ready_empty)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--no-expected", proc.stderr)

    def test_sans_liste_attendue_option_no_expected(self) -> None:
        self.ecrire_manifeste_sans_admissions()
        code, payload = inclusion.run(self.namespace(no_expected=True))
        self.assertEqual(code, 0)
        self.assertIsNone(payload["expected_source"])
        rapport = inclusion.render(payload)
        self.assertIn("NON effectué", rapport)
        self.assertIn("--no-expected", rapport)

    def test_admissions_en_table_ne_sont_pas_une_composition(self) -> None:
        # Une section d'admissions en tableau est la liste attendue, même si
        # ses colonnes s'appellent « lot » et « commit » : elle ne doit pas
        # être lue comme une composition (#1655 N2).
        manifest = pathlib.Path(self.tmp.name) / "manifeste-admissions.md"
        manifest.write_text(
            textwrap.dedent(
                """\
                # Manifeste

                ## 0. Admissions de la coupure

                | Lot | Commit | Note |
                |---|---|---|
                | #100 | `{c100}` | admission pilote |
                | #500 | — | routé, jamais composé |

                ## 2. Composition

                | # | lot(s) | tête source | commit | note |
                |---|---|---|---|---|
                | 1 | #100 | `{src100}` | `{c100}` |  |
                """
            ).format(src100=self.src100[:12], c100=self.c100[:12]),
            encoding="utf-8",
        )
        code, payload = inclusion.run(self.namespace(manifest=manifest))
        self.assertEqual(payload["expected"], [100, 500])
        self.assertEqual(payload["expected_source"], "section du manifeste")
        self.assertEqual(payload["composed"], [100])  # 500 n'est pas composée
        self.assertEqual([item["lot"] for item in payload["omissions"]], [500])
        self.assertEqual(code, 1)

    def test_ecartes_table_with_reason_explains_a_ready_lot(self) -> None:
        # #2404 : « Écartés (ce véhicule) » est la forme que le composeur
        # écrit ; un motif non vide est une explication forte.
        manifest = pathlib.Path(self.tmp.name) / "manifeste-ecartes.md"
        manifest.write_text(
            self.manifest_simple(100, self.src100, self.c100).read_text(encoding="utf-8")
            + textwrap.dedent(
                """
                ## Écartés (ce véhicule)

                | lot | motif |
                |---|---|
                | #500 | conflit à préparer, paquet suivant |
                """
            ),
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(expected=[100], ready=[500], manifest=manifest)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["ready_omissions"], [])
        self.assertIn(500, {item["lot"] for item in payload["exclusions"]})
        self.assertEqual(payload["ecartes"], {"500": "conflit à préparer, paquet suivant"})

    def test_ecarte_without_reason_does_not_lift_omission(self) -> None:
        manifest = pathlib.Path(self.tmp.name) / "manifeste-ecartes-vide.md"
        manifest.write_text(
            self.manifest_simple(100, self.src100, self.c100).read_text(encoding="utf-8")
            + textwrap.dedent(
                """
                ## Écartés (ce véhicule)

                | lot | motif |
                |---|---|
                | #500 |  |
                """
            ),
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(expected=[100], ready=[500], manifest=manifest)
        )
        self.assertEqual(code, 1)
        self.assertEqual([item["lot"] for item in payload["ready_omissions"]], [500])
        self.assertIn("sans motif", payload["ready_omissions"][0]["reason"])

    def test_ready_file_is_checked_without_admissions(self) -> None:
        manifest = self.manifest_simple(100, self.src100, self.c100)
        ready_file = pathlib.Path(self.tmp.name) / "prets.txt"
        ready_file.write_text("# commentaire\n#100  prêt\n500\n#501\n", encoding="utf-8")
        code, payload = inclusion.run(
            self.namespace(ready_file=[str(ready_file)], manifest=manifest,
                           no_expected=True)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["expected_source"], None)
        self.assertEqual(payload["ready_source"], "options")
        self.assertEqual(payload["ready"], [100, 500, 501])
        self.assertEqual([item["lot"] for item in payload["ready_omissions"]], [500, 501])
        self.assertIn("#100", inclusion.render(payload))
        self.assertIn("lot prêt ni composé ni écarté", payload["ready_omissions"][0]["reason"])

    def test_ready_list_is_required(self) -> None:
        manifest = self.manifest_simple(100, self.src100, self.c100)
        with self.assertRaises(inclusion.InputError) as caught:
            inclusion.run(self.namespace(expected=[100], manifest=manifest,
                                         ready_file=[]))
        self.assertIn("--ready", str(caught.exception))

    def test_no_ready_is_refused(self) -> None:
        # #2464 : une recomposition a utilisé --no-ready et laissé passer
        # #2384, prêt depuis 14:29, absent du manifeste sans ligne « écarté ».
        manifest = self.manifest_simple(100, self.src100, self.c100)
        with self.assertRaises(inclusion.InputError) as caught:
            inclusion.run(self.namespace(expected=[100], manifest=manifest,
                                         no_ready=True))
        message = str(caught.exception)
        self.assertIn("--no-ready est refusé", message)
        self.assertIn("#2384", message)
        self.assertIn("--ready-file", message)

    def test_no_ready_is_refused_by_the_cli(self) -> None:
        manifest = self.manifest_simple(100, self.src100, self.c100)
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "paquet-inclusion.py"),
             "--manifest", str(manifest), "--head", self.head,
             "--repo", str(self.repo), "--expected", "100", "--no-ready"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--no-ready est refusé", proc.stderr)
        self.assertIn("#2384", proc.stderr)

    def test_ready_list_required_by_the_cli(self) -> None:
        manifest = self.manifest_simple(100, self.src100, self.c100)
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "paquet-inclusion.py"),
             "--manifest", str(manifest), "--head", self.head,
             "--repo", str(self.repo), "--expected", "100"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("aucune liste de lots prêts", proc.stderr)

    def test_ready_lot_composed_is_not_an_omission(self) -> None:
        manifest = self.manifest_simple(100, self.src100, self.c100)
        code, payload = inclusion.run(
            self.namespace(expected=[100], ready=[100], manifest=manifest)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["ready_omissions"], [])

    def test_weak_prose_under_ecartes_stays_weak(self) -> None:
        manifest = pathlib.Path(self.tmp.name) / "manifeste-ecartes-faible.md"
        manifest.write_text(
            self.manifest_simple(100, self.src100, self.c100).read_text(encoding="utf-8")
            + "\n## Écartés\n\nLe lot #500 repassera au paquet suivant.\n",
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(expected=[100], ready=[500], manifest=manifest)
        )
        self.assertEqual(code, 1)
        self.assertEqual([item["lot"] for item in payload["ready_omissions"]], [500])
        self.assertIn("mention faible", payload["ready_omissions"][0]["reason"])

    def test_ecarte_reference_in_reason_stays_weak(self) -> None:
        # Relecture cx2 B1 : « - #500 dépend de #600. » sous « Écartés » ne
        # fait pas de #600 un lot écarté ; seul #500 est le sujet de la ligne.
        manifest = pathlib.Path(self.tmp.name) / "manifeste-ecartes-ref.md"
        manifest.write_text(
            self.manifest_simple(100, self.src100, self.c100).read_text(encoding="utf-8")
            + "\n## Écartés (ce véhicule)\n\n- #500 dépend de #600.\n",
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(expected=[100], ready=[600], manifest=manifest,
                           no_ready=False)
        )
        self.assertEqual(code, 1)
        self.assertEqual([item["lot"] for item in payload["ready_omissions"]], [600])
        self.assertNotIn("600", payload["explained"])
        self.assertEqual(payload["ecartes"].get("500"), "dépend de #600.")
        self.assertIn("600", payload["weak"])

    def test_retrait_reference_in_reason_stays_weak(self) -> None:
        # Même règle dans la branche EXPLAIN_RE (retrait/revert/report).
        manifest = pathlib.Path(self.tmp.name) / "manifeste-retraits-ref.md"
        manifest.write_text(
            self.manifest_simple(100, self.src100, self.c100).read_text(encoding="utf-8")
            + "\n## Retraits\n\n- #500 retiré, dépend de #600.\n",
            encoding="utf-8",
        )
        code, payload = inclusion.run(
            self.namespace(expected=[100, 500], ready=[500, 600],
                           manifest=manifest, no_ready=False)
        )
        self.assertEqual(code, 1)
        self.assertEqual([item["lot"] for item in payload["ready_omissions"]], [600])
        self.assertIn("500", payload["explained"])
        self.assertNotIn("600", payload["explained"])
        self.assertIn("600", payload["weak"])

    def test_empty_ready_file_is_a_valid_source(self) -> None:
        # Relecture cx2 B2 : une source fournie mais vide dit « aucun lot
        # prêt » ; elle ne doit pas être confondue avec l'absence de source.
        manifest = self.manifest_simple(100, self.src100, self.c100)
        ready_file = pathlib.Path(self.tmp.name) / "prets-vide.txt"
        ready_file.write_text("# aucun lot prêt\n", encoding="utf-8")
        code = inclusion.main([
            "--manifest", str(manifest), "--head", self.head,
            "--repo", str(self.repo), "--expected", "100",
            "--ready-file", str(ready_file),
        ])
        self.assertEqual(code, 0)
        _code, payload = inclusion.run(
            self.namespace(expected=[100], ready_file=[str(ready_file)],
                           manifest=manifest, no_ready=False)
        )
        self.assertEqual(payload["ready"], [])
        self.assertEqual(payload["ready_source"], "options")
        self.assertEqual(payload["ready_omissions"], [])

    def test_missing_ready_file_is_invalid_input(self) -> None:
        # Relecture cx2 B3 : un export retiré (inventaire incomplet) n'est pas
        # une liste vide ; l'inclusion doit s'arrêter en entrée invalide.
        manifest = self.manifest_simple(100, self.src100, self.c100)
        missing = pathlib.Path(self.tmp.name) / "prets-absent.txt"
        with self.assertRaises(inclusion.InputError) as caught:
            inclusion.run(
                self.namespace(expected=[100], ready_file=[str(missing)],
                               manifest=manifest, no_ready=False)
            )
        self.assertIn("illisible", str(caught.exception))

    def test_commit_sans_delta_est_vide(self) -> None:
        # Remise fraîche sans delta : le paquet 11h réel en produit quatre.
        self.git("switch", "-q", "-c", "lot/700", "main")
        self.git("commit", "-q", "--allow-empty",
                 "-m", "#700 : remise fraîche sans delta")
        source = self.rev("HEAD")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#700: compose\n\nameesh-work: 700", "lot/700")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(700, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[700], head=head, manifest=manifest)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["entries"][0]["verdict"], "vide")
        self.assertTrue(any("#700" in warning and "vide" in warning
                            for warning in payload["warnings"]))
        code, payload = inclusion.run(
            self.namespace(expected=[700], head=head, manifest=manifest,
                           strict=True)
        )
        self.assertEqual(code, 1)
        self.assertTrue(any("#700" in error and "vide" in error
                            for error in payload["errors"]))

    def test_changement_de_mode_seul_n_est_pas_binaire(self) -> None:
        # Un commit qui ne change que le bit exécutable n'émet aucun hunk :
        # ce n'est pas un binaire, et il ne doit pas faire un faux défaut
        # sous --strict (#1655 N1).
        self.git("switch", "-q", "-c", "lot/900", "main")
        self.write("neuf.sh", "#!/bin/sh\necho ok\n")
        self.commit("#900 : ajoute le script")
        (self.repo / "neuf.sh").chmod(0o755)
        source = self.commit("#900 : rend le script exécutable")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#900: compose\n\nameesh-work: 900", "lot/900")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(900, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[900], strict=True, head=head,
                           manifest=manifest)
        )
        self.assertEqual(code, 0)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "présent")
        self.assertTrue(all(check["verdict"] != "binaire"
                            for check in entry["paths"]))
        self.assertEqual([check["added"] for check in entry["paths"]], [0])

    def test_fichier_nouveau_et_vide_n_est_pas_binaire(self) -> None:
        # Un commit qui AJOUTE un fichier vide (statut A) n'émet ni hunk ni
        # en-tête ---/+++ (aucun blob parent) : le chemin est absent de
        # parse_added. Ce n'est pas un binaire, et il ne doit pas faire un
        # faux défaut sous --strict (#1684).
        self.git("switch", "-q", "-c", "lot/970", "main")
        (self.repo / "vide.txt").write_text("", encoding="utf-8")
        source = self.commit("#970 : ajoute un fichier vide")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#970: compose\n\nameesh-work: 970", "lot/970")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(970, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[970], strict=True, head=head,
                           manifest=manifest)
        )
        self.assertEqual(code, 0)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "présent")
        check = entry["paths"][0]
        self.assertEqual(check["verdict"], "identique")
        self.assertEqual(check["added"], 0)

    def test_fichier_vide_disparu_de_la_tete_reste_absent(self) -> None:
        # Le même ajout vide, mais la tête ne porte plus le fichier : le delta
        # vide ne doit pas masquer la disparition (#1684).
        self.git("switch", "-q", "-c", "lot/971", "main")
        (self.repo / "vide.txt").write_text("", encoding="utf-8")
        source = self.commit("#971 : ajoute un fichier vide")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#971: compose\n\nameesh-work: 971", "lot/971")
        self.git("rm", "-q", "vide.txt")
        head = self.commit("retrait du fichier vide")
        manifest = self.manifest_simple(971, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[971], head=head, manifest=manifest)
        )
        self.assertEqual(code, 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "absent")
        self.assertEqual([c["verdict"] for c in entry["paths"]], ["absent"])

    def test_binaire_remplace_par_du_texte_reste_binaire(self) -> None:
        # Le diff d'un binaire n'a ni hunk ni en-tête ---/+++ : le chemin est
        # absent de parse_added. La comparaison des blobs le distingue d'un
        # changement de mode seul, même quand le nouveau contenu est du texte
        # (#1655 N1).
        self.git("switch", "-q", "-c", "lot/960", "main")
        (self.repo / "mix.dat").write_bytes(b"MIX\0\x01\n")
        self.commit("#960 : ajoute un binaire")
        self.write("mix.dat", "desormais du texte\n")
        source = self.commit("#960 : remplace le binaire par du texte")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#960: compose\n\nameesh-work: 960", "lot/960")
        head = self.rev("HEAD")
        manifest = self.manifest_simple(960, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[960], head=head, manifest=manifest)
        )
        self.assertEqual(code, 0)
        entry = payload["entries"][0]
        self.assertEqual(entry["verdict"], "partiel")
        self.assertEqual([check["verdict"] for check in entry["paths"]],
                         ["binaire"])

    def test_ligne_ajoutee_commencant_par_deux_plus(self) -> None:
        # Une ligne ajoutée « ++ … » produit une ligne de diff « +++ … » :
        # elle ne doit pas être prise pour un en-tête de fichier (#1655 N3).
        self.git("switch", "-q", "-c", "lot/800", "main")
        self.write("huit.txt", "++ entete en apparence\nsimple\n")
        source = self.commit("#800 : ajoute une ligne ++")
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m",
                 "#800: compose\n\nameesh-work: 800", "lot/800")
        # la ligne « ++ » est retirée à la tête : elle doit être vue manquante
        self.write("huit.txt", "simple\n")
        head = self.commit("retrait de la ligne ++")
        manifest = self.manifest_simple(800, source, source)
        code, payload = inclusion.run(
            self.namespace(expected=[800], head=head, manifest=manifest)
        )
        self.assertEqual(code, 0)
        check = payload["entries"][0]["paths"][0]
        self.assertEqual(check["added"], 2)
        self.assertEqual(check["verdict"], "partiel")
        self.assertEqual(check["missing_sample"], ["++ entete en apparence"])

    def test_titre_qui_nie_les_admissions_est_exclu(self) -> None:
        # « Pas d'admission » (élision sans espace) nie les admissions comme
        # « aucune admission » : ses lots ne sont pas la liste attendue.
        for titre in ("Pas d'admission à la coupure", "Pas d’admissions",
                      "Aucune admission", "Sans section d'admissions"):
            manifest = pathlib.Path(self.tmp.name) / "manifeste-negation.md"
            manifest.write_text(
                f"# Manifeste\n\n## {titre}\n\n- #900\n", encoding="utf-8"
            )
            self.assertEqual(inclusion.parse_manifest(manifest).admissions, [],
                             titre)

    def test_ready_export_names_error_lots_without_inventing_ids(self) -> None:
        # #3813 : les nombres d'un motif ne sont jamais relus comme des ids.
        ready = pathlib.Path(self.tmp.name) / "prets-erreurs.txt"
        ready.write_text(
            "# paquet-candidats main f444955b2eeb code 6d89c0aeca21\n"
            "# prêts\n#100\n#200  prêt\n"
            "# erreurs\n"
            "!3506 : freeze #3506 du 26/10 20:35Z introuvable, 4096 essais\n",
            encoding="utf-8",
        )
        ids, errors = inclusion.read_ready_file(ready)
        self.assertEqual(ids, [100, 200])
        self.assertEqual(list(errors), [3506])
        self.assertIn("20:35Z", errors[3506])

    def test_ready_error_lot_absent_from_manifest_is_an_exclusion(self) -> None:
        # Témoin h-31 v4 : #3506 en erreur, absent du manifeste, n'est pas une
        # recopie manuelle ni une omission : il est consigné écarté et le
        # paquet des autres lots passe (#3813).
        ready = pathlib.Path(self.tmp.name) / "prets-erreurs.txt"
        ready.write_text(
            "# paquet-candidats main f444955b2eeb code 6d89c0aeca21\n"
            "# prêts\n#100\n# erreurs\n"
            "!3506 : cannot read the review gate: provenance unknown\n",
            encoding="utf-8",
        )
        manifest = self.manifest_simple(100, self.src100, self.c100)
        code, payload = inclusion.run(self.namespace(
            expected=[100, 3506], manifest=manifest, ready_file=[str(ready)]))
        self.assertEqual(code, 0, payload["errors"])
        self.assertEqual(payload["ready_errors"],
                         {"3506": "cannot read the review gate: provenance unknown"})
        exclusions = {item["lot"]: item for item in payload["exclusions"]}
        self.assertIn(3506, exclusions)
        self.assertIn("cannot read the review gate", exclusions[3506]["reason"])
        self.assertEqual(payload["omissions"], [])

    def test_ready_error_lot_composed_is_a_blocking_defect(self) -> None:
        # Témoin h-31 v3 : une unité que la liste calculée dit en erreur est
        # un défaut bloquant, même si son contenu semble présent (#3813).
        ready = pathlib.Path(self.tmp.name) / "prets-erreurs.txt"
        ready.write_text(
            "# prêts\n#100\n# erreurs\n"
            "!3506 : cannot read the review gate: provenance unknown\n",
            encoding="utf-8",
        )
        manifest = pathlib.Path(self.tmp.name) / "manifeste-v3.md"
        manifest.write_text(
            "# Manifeste v3\n\n"
            "| # | lot(s) | tête source | commit | note |\n"
            "|---|---|---|---|---|\n"
            "| 1 | #100 | `%s` | `%s` |  |\n"
            "| 2 | #3506 | `%s` | `%s` |  |\n"
            % (self.src100[:12], self.c100[:12],
               self.src100[:12], self.c100[:12]),
            encoding="utf-8",
        )
        code, payload = inclusion.run(self.namespace(
            expected=[100], manifest=manifest, ready_file=[str(ready)]))
        self.assertEqual(code, 1)
        self.assertTrue(any(
            "#3506" in error and "en erreur" in error
            for error in payload["errors"]), payload["errors"])

    def test_ready_error_line_without_motive_is_invalid_input(self) -> None:
        ready = pathlib.Path(self.tmp.name) / "prets-casse.txt"
        ready.write_text("!3506\n", encoding="utf-8")
        with self.assertRaises(inclusion.InputError):
            inclusion.read_ready_file(ready)

    def test_ready_error_lot_also_ready_is_invalid_input(self) -> None:
        ready = pathlib.Path(self.tmp.name) / "prets-casse.txt"
        ready.write_text("#3506\n!3506 : porte illisible\n", encoding="utf-8")
        with self.assertRaises(inclusion.InputError):
            inclusion.read_ready_file(ready)

    # -- helpers de banc ---------------------------------------------------
    def ecrire_manifeste_sans_admissions(self) -> None:
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

    def manifest_simple(self, lot: int, source: str, commit: str) -> pathlib.Path:
        """Manifeste à une entrée de composition, sans liste d'admissions."""
        manifest = pathlib.Path(self.tmp.name) / f"manifeste-{lot}.md"
        manifest.write_text(
            textwrap.dedent(
                """\
                # Manifeste

                | # | lot(s) | tête source | commit | note |
                |---|---|---|---|---|
                | 1 | #%d | `%s` | `%s` |  |
                """
            ) % (lot, source[:12], commit[:12]),
            encoding="utf-8",
        )
        return manifest


if __name__ == "__main__":
    unittest.main()
