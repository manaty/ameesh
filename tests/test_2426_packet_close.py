# SPDX-License-Identifier: AGPL-3.0-only
"""#2426 — fermer les lots unitaires d'un paquet à sa fusion (manifeste git).

Le paquet est composé en git seulement : pas de fiche, pas de `package_id`
sur les unités. Le manifeste fusionné sur la cible est la liste des unités ;
le relevé `packet_close` examine le dernier gel de chacune et la ferme par
le contenu, sans balayer tous les lots ouverts.
"""
from __future__ import annotations

import json
import os
import unittest

from ameesh import packet_close, work

from .test_canon import git, write
from .test_plan import _PlanDb


class PacketCloseTest(_PlanDb):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.make_tmp(), "depot")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.commit("README", "acme\n", "initial")

    # -- utilitaires -------------------------------------------------------
    def commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as fh:
            fh.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def item(self, title: str) -> dict:
        row = work.add(self.db, title=title)
        for state in ("build", "qa"):
            work.move(self.db, row["id"], state)
        return row

    def freeze(self, item: dict, sha: str) -> None:
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="ouvrier")

    def package(self, items: list[dict], *, withdrawn: set | None = None,
                manifest: bool = True, trailer: bool = True,
                branch: str = "integration/paquet-test") -> list[dict]:
        """Compose un paquet de `len(items)` unités, chacune depuis `main`, puis
        fusionne la branche du paquet dans `main`. Rend `[{lot, source, unit}]`.

        `withdrawn` : lots inscrits dans la section « Écartés » du manifeste
        (la ligne de composition reste, le retrait fait foi)."""
        withdrawn = set(withdrawn or ())
        git(self.repo, "checkout", "-q", "-b", branch, "main")
        units = []
        for item in items:
            lot = item["id"]
            source_branch = "lot/%d-test" % lot
            git(self.repo, "checkout", "-q", "-b", source_branch, "main")
            source = self.commit("src/%d.py" % lot, "value = %d\n" % lot,
                                 "lot %d: unit content" % lot)
            git(self.repo, "checkout", "-q", branch)
            message = "vehicule test : #%d %s" % (lot, source[:12])
            if trailer:
                message += "\n\nameesh-work: %d" % lot
            git(self.repo, "merge", "-q", "--no-ff", "-m", message, source_branch)
            units.append({"lot": lot, "source": source,
                          "unit": git(self.repo, "rev-parse", "HEAD")})
        if manifest:
            rows = ["# Paquet test — composition", "",
                    "## Composition (%d unités)" % len(units), "",
                    "| unité | lot | source | commit | note |",
                    "|---|---|---|---|---|"]
            for index, unit in enumerate(units, start=1):
                rows.append("| %d | #%d | `%s` | `%s` | composition propre |" % (
                    index, unit["lot"], unit["source"], unit["unit"]))
            rows.append("")
            if withdrawn:
                rows.extend(["## Écartés (ce véhicule)", "", "| lot | motif |",
                             "|---|---|"])
                rows.extend("| #%d | retiré du paquet |" % lot for lot in sorted(withdrawn))
                rows.append("")
            write(self.repo, "reviews/integration-paquet-test.md", "\n".join(rows))
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", "manifeste — %d unités" % len(units))
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "Merge pull request #1 from manaty/%s" % branch, branch)
        return units

    def sync(self, **kwargs) -> dict:
        return packet_close.sync(self.db, self.repo, target="main", **kwargs)

    @staticmethod
    def by_lot(report: dict) -> dict:
        return {row["work_item"]: row for row in report["results"]}

    # -- témoins -----------------------------------------------------------
    def test_un_paquet_de_deux_unites_ferme_les_deux(self):
        first, second = self.item("première unité"), self.item("seconde unité")
        units = self.package([first, second])
        for item, unit in zip((first, second), units):
            self.freeze(item, unit["source"])
        dry = self.by_lot(self.sync(dry_run=True))
        self.assertEqual({dry[first["id"]]["result"], dry[second["id"]]["result"]},
                         {"would-merge"})
        self.assertEqual(work.get(self.db, first["id"])["state"], "qa")
        report = self.sync()
        self.assertEqual(report["merged"], 2)
        results = self.by_lot(report)
        self.assertEqual({results[first["id"]]["result"], results[second["id"]]["result"]},
                         {"merged"})
        for item, unit in zip((first, second), units):
            row = work.get(self.db, item["id"])
            self.assertEqual(row["state"], "merged")
            merged = [m for m in work.milestones(self.db, item["id"])
                      if m["kind"] == "merged"][0]
            self.assertEqual(merged["sha"], unit["source"])
            journal = " ".join(event.get("note") or ""
                               for event in work.events(self.db, item["id"]))
            self.assertIn("packet manifest", journal)
        # idempotent : les lots sont déjà fusionnés, plus rien à examiner
        self.assertEqual(self.sync()["results"], [])

    def test_un_lot_regelé_apres_la_composition_reste_ouvert(self):
        first = self.item("unité")
        [unit] = self.package([first])
        self.freeze(first, unit["source"])
        # nouveau travail sur une branche non fusionnée : le dernier gel est
        # celui-là, le paquet ne l'a pas livré
        git(self.repo, "checkout", "-q", "-b", "lot/%d-refreeze" % first["id"], "main")
        newer = self.commit("src/%d.py" % first["id"], "value = 2\n", "nouveau travail")
        git(self.repo, "checkout", "-q", "main")
        self.freeze(first, newer)
        report = self.by_lot(self.sync())
        self.assertEqual(report[first["id"]]["result"], "open")
        self.assertEqual(work.get(self.db, first["id"])["state"], "qa")
        # livré ensuite : le relevé le ferme
        git(self.repo, "merge", "-q", "--no-ff", "-m", "livraison", "lot/%d-refreeze"
            % first["id"])
        self.assertEqual(self.sync()["results"][0]["result"], "merged")

    def test_une_ligne_de_retrait_ne_ferme_pas(self):
        first, second = self.item("livrée"), self.item("reportée")
        units = self.package([first, second], withdrawn={second["id"]})
        for item, unit in zip((first, second), units):
            self.freeze(item, unit["source"])
        results = self.by_lot(self.sync())
        self.assertEqual(results[first["id"]]["result"], "merged")
        self.assertNotIn(second["id"], results)
        self.assertEqual(work.get(self.db, second["id"])["state"], "qa")

    def test_un_lot_sans_gel_est_ferme_par_le_trailer_de_composition(self):
        unit = self.item("sans gel")
        units = self.package([unit])
        report = self.by_lot(self.sync())
        self.assertEqual(report[unit["id"]]["result"], "merged")
        self.assertEqual(report[unit["id"]]["how"], "trailer")
        self.assertEqual(report[unit["id"]]["ref"], units[0]["unit"])
        self.assertEqual(work.get(self.db, unit["id"])["state"], "merged")

    def test_un_lot_sans_gel_dont_l_unite_est_reverte_n_est_pas_ferme(self):
        unit = self.item("sans gel, unité annulée")
        units = self.package([unit])
        git(self.repo, "revert", "-m", "1", "--no-edit", units[0]["unit"])
        report = self.by_lot(self.sync())
        self.assertEqual(report[unit["id"]]["result"], "no-freeze")
        self.assertEqual(work.get(self.db, unit["id"])["state"], "qa")

    def test_un_lot_sans_gel_ni_trailer_est_laisse_aux_autres_relevés(self):
        unit = self.item("sans gel ni trailer")
        self.package([unit], trailer=False)
        report = self.by_lot(self.sync())
        self.assertEqual(report[unit["id"]]["result"], "no-freeze")
        self.assertEqual(work.get(self.db, unit["id"])["state"], "qa")

    def test_un_paquet_reverte_ne_ferme_pas(self):
        unit = self.item("unité")
        units = self.package([unit])
        self.freeze(unit, units[0]["source"])
        # le paquet est fusionné puis son unité est annulée par un revert
        git(self.repo, "revert", "-m", "1", "--no-edit", units[0]["unit"])
        report = self.by_lot(self.sync())
        self.assertEqual(report[unit["id"]]["result"], "reverted")
        self.assertEqual(work.get(self.db, unit["id"])["state"], "qa")

    def test_releve_cible_fraiche_sur_le_projet_radar(self):
        from ameesh.radar import Project

        [unit] = [self.item("unité")]
        project = Project(name="banc", repo=self.repo, target="main")
        # premier passage : rien n'est encore composé, le clone privé est créé
        self.assertEqual(packet_close.sync_project(
            self.cfg, self.db, project)["results"], [])
        units = self.package([unit])
        self.freeze(unit, units[0]["source"])
        report = packet_close.sync_project(self.cfg, self.db, project)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(work.get(self.db, unit["id"])["state"], "merged")

    def test_cli_sync_packages(self):
        [unit] = [self.item("unité")]
        units = self.package([unit])
        self.freeze(unit, units[0]["source"])
        out = self.mesh("work", "sync-packets", "--git-dir", self.repo,
                        "--target", "main", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(work.get(self.db, unit["id"])["state"], "merged")


class ManifestParseTest(unittest.TestCase):
    def test_table_de_composition_et_retrait(self):
        text = """# Paquet test

## Composition (2 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #10, #11 | `aaaaaaaa`, re-gel `bbbbbbbb` | `cccccccc` | composition |
| 2 | #12 | `dddddddd` | `eeeeeeee` | sans retrait |

## Écartés

| lot | motif |
|---|---|
| #13 | reporté au paquet suivant |
"""
        entries = packet_close.parse_manifest(text)
        self.assertEqual([entry["lots"] for entry in entries], [[10, 11], [12]])
        self.assertEqual(entries[0]["sources"], ["aaaaaaaa", "bbbbbbbb"])
        self.assertEqual(entries[1]["sources"], ["dddddddd"])
        self.assertFalse(entries[0]["withdrawn"])
        self.assertFalse(entries[1]["withdrawn"])      # « sans retrait » n'est pas un retrait
        self.assertEqual([entry["line_no"] for entry in entries], [7, 8])

    def test_un_fichier_sans_table_de_composition_ne_casse_rien(self):
        self.assertEqual(packet_close.parse_manifest("# Paquet\n\nAucune table.\n"), [])
        self.assertEqual(packet_close.parse_manifest(""), [])

    def test_une_section_ecartes_retire_meme_sans_colonne_commit(self):
        text = """## Composition (1 unité)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #30 | `aaaaaaaa` | `bbbbbbbb` | composition |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #30 | reporté |
"""
        entries = packet_close.parse_manifest(text)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["withdrawn"])

    def test_une_table_de_retrait_avec_commit_retire_aussi(self):
        text = """## Retirés

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #20 | `aaaaaaaa` | `bbbbbbbb` | revert |

## Composition (1 unité)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #21 | `cccccccc` | `dddddddd` | composition |
"""
        entries = packet_close.parse_manifest(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["lots"], [21])


if __name__ == "__main__":
    unittest.main()
