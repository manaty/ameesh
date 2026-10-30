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
from unittest import mock

from ameesh import packet_close, work
from ameesh.storage.postgres.work import WorkItems

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
            self.assertEqual(row["state"], "delivered")
            merged = [m for m in work.milestones(self.db, item["id"])
                      if m["kind"] == "merged"][0]
            self.assertEqual(merged["sha"], unit["source"])
            journal = " ".join(event.get("note") or ""
                               for event in work.events(self.db, item["id"]))
            self.assertIn("packet manifest", journal)
        # idempotent : les lots sont déjà fusionnés, plus rien à examiner
        self.assertEqual(self.sync()["results"], [])

    def test_le_balayage_transmet_le_depot_a_la_relecture(self):
        """#3201 : la fermeture par manifeste de paquet passe le dépôt du
        relevé à `close_merged`. La classe et la relecture §2 du lot léger
        sont lues sur ce dépôt, même sur un hôte sans dossier d'assigné ni
        projet radar — le défaut laissait « dépôt inconnu » (#3011, #3105)."""
        from ameesh import review_lot, storage

        item = self.item("unité relue")
        unit = self.package([item])[0]
        self.freeze(item, unit["source"])
        with mock.patch.object(review_lot.lot_class, "of_lot",
                               return_value={"class": "light", "why": "témoin"}) as klass:
            report = self.by_lot(self.sync())
        self.assertEqual(report[item["id"]]["result"], "merged", report)
        klass.assert_called_once()
        self.assertEqual(klass.call_args.kwargs["repo"], self.repo)
        rows = storage.of(self.db).work.by_issue_ref(review_lot.ref_of(item["id"]))
        self.assertEqual(len(rows), 1, rows)

    def test_un_lot_absent_d_une_liste_d_items_tronquee_est_ferme(self):
        """#2508 : la fermeture lit les lots du manifeste par leur id, pas la
        liste globale `items(limit=1000)` ; une unité absente d'une liste
        tronquée est tout de même examinée et fermée."""
        first, second = self.item("première unité"), self.item("seconde unité")
        units = self.package([first, second])
        for item, unit in zip((first, second), units):
            self.freeze(item, unit["source"])
        original = WorkItems.items
        called = []

        def truncated(store, **kwargs):
            called.append(True)
            rows = original(store, **kwargs)
            return [row for row in rows if int(row["id"]) != second["id"]]

        with mock.patch.object(WorkItems, "items", new=truncated):
            report = self.sync()
        results = self.by_lot(report)
        self.assertEqual(results[first["id"]]["result"], "merged", report)
        self.assertEqual(results[second["id"]]["result"], "merged", report)
        self.assertEqual(work.get(self.db, second["id"])["state"], "delivered")
        self.assertEqual(called, [],
                         "#2508 : le balayage global `items()` ne doit plus être lu")

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

    def test_2744_un_lot_blocked_n_est_pas_ferme_par_le_paquet(self):
        """#2744 : une unité bloquée garde sa fermeture, le contenu du paquet
        n'est qu'une preuve de livraison."""
        first = self.item("unité bloquée")
        [unit] = self.package([first])
        self.freeze(first, unit["source"])
        work.move(self.db, first["id"], "blocked", note="passe réelle due",
                  actor="dev-a")
        report = self.by_lot(self.sync())
        self.assertEqual(report[first["id"]]["result"], "withheld")
        self.assertEqual(work.get(self.db, first["id"])["state"], "blocked")

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
        self.assertEqual(work.get(self.db, unit["id"])["state"], "delivered")

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
        self.assertEqual(work.get(self.db, unit["id"])["state"], "delivered")

    def test_cli_sync_packages(self):
        [unit] = [self.item("unité")]
        units = self.package([unit])
        self.freeze(unit, units[0]["source"])
        out = self.mesh("work", "sync-packets", "--git-dir", self.repo,
                        "--target", "main", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(work.get(self.db, unit["id"])["state"], "delivered")

    def _manifest_sans_commit(self, items: list[dict]) -> None:
        """#3629 : la forme du manifeste de #3514 — colonne « lot », aucune
        colonne « commit » — posée sur la cible après la fusion du paquet."""
        rows = ["# Paquet test", "", "## Composition", "",
                "| lot | source composée | gel | avis indépendants |",
                "|---|---|---|---|"]
        rows.extend("| #%d | `%s` | gel | deux avis |" % (item["id"], "a" * 12)
                    for item in items)
        write(self.repo, "reviews/integration-paquet-sans-commit.md", "\n".join(rows) + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "manifeste sans colonne commit")

    def test_3629_un_manifeste_sans_colonne_commit_est_signale(self):
        ouvert, ferme = self.item("unité ouverte"), self.item("unité fermée")
        units = self.package([ouvert, ferme], manifest=False)
        for item, unit in zip((ouvert, ferme), units):
            self.freeze(item, unit["source"])
        self._manifest_sans_commit([ouvert, ferme])
        work.close(self.db, ferme["id"], abandoned=True, actor="banc")
        report = self.sync()
        # rien n'est livré par ce manifeste, et le relevé le dit
        self.assertEqual((report["units"], report["results"]), (0, []))
        [warning] = report["warnings"]
        self.assertEqual(warning["manifest"], "reviews/integration-paquet-sans-commit.md")
        self.assertEqual(warning["line_no"], 5)
        self.assertEqual(warning["heading"], "Composition")
        # seul le lot encore ouvert est nommé ; le lot fermé ne relance rien
        self.assertEqual(warning["lots"], [ouvert["id"]])
        self.assertIn("'commit' column", warning["message"])
        self.assertEqual(work.get(self.db, ouvert["id"])["state"], "qa")
        out = self.mesh("work", "sync-packets", "--git-dir", self.repo,
                        "--target", "main")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("warning: reviews/integration-paquet-sans-commit.md:5 (Composition)",
                      out.stderr)
        self.assertIn("#%d" % ouvert["id"], out.stderr)
        self.assertNotIn("#%d:" % ferme["id"], out.stderr)

    def test_3629_un_manifeste_dont_tous_les_lots_sont_fermes_ne_dit_rien(self):
        ferme = self.item("unité fermée")
        units = self.package([ferme], manifest=False)
        self.freeze(ferme, units[0]["source"])
        self._manifest_sans_commit([ferme])
        work.close(self.db, ferme["id"], abandoned=True, actor="banc")
        self.assertEqual(self.sync()["warnings"], [])

    def test_3629_un_paquet_bien_forme_ne_rend_aucun_avertissement(self):
        unit = self.item("unité")
        units = self.package([unit])
        self.freeze(unit, units[0]["source"])
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        self.assertEqual(report["warnings"], [])


class ManifestParseTest(unittest.TestCase):
    def test_3629_une_table_de_lots_sans_commit_est_une_lacune(self):
        text = "\n".join([
            "# Paquet", "", "## Composition", "",
            "| lot | source composée | gel | avis indépendants |",
            "|---|---|---|---|",
            "| #2309 | `aaaaaaaaaaaa` | 19001 | cx1, auditeur |",
            "| #2882, #3067 | `bbbbbbbbbbbb` | 19002 | dev4 |", ""])
        self.assertEqual(packet_close.parse_manifest(text), [])
        self.assertEqual(packet_close.manifest_gaps(text), [
            {"line_no": 5, "heading": "Composition", "lots": [2309, 2882, 3067]}])

    def test_3629_un_manifeste_qui_livre_ne_signale_pas_ses_autres_tables(self):
        # une table d'avis à colonne « lot » à côté d'une vraie composition
        text = "\n".join([
            "## Composition", "",
            "| unité | lot | source | commit | note |", "|---|---|---|---|---|",
            "| 1 | #10 | `aaaaaaaaaaaa` | `bbbbbbbbbbbb` | composition |", "",
            "## Avis", "", "| lot | relecteur | verdict |", "|---|---|---|",
            "| #10 | auditeur | ok |", ""])
        self.assertEqual(len(packet_close.parse_manifest(text)), 1)
        self.assertEqual(packet_close.manifest_gaps(text), [])

    def test_3629_une_table_de_retrait_sans_commit_n_est_pas_une_lacune(self):
        text = "\n".join([
            "## Écartés (ce véhicule)", "", "| lot | motif |", "|---|---|",
            "| #11 | retiré du paquet |", ""])
        self.assertEqual(packet_close.parse_manifest(text), [])
        self.assertEqual(packet_close.manifest_gaps(text), [])
        self.assertEqual(packet_close.manifest_gaps(""), [])

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
