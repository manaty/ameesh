# SPDX-License-Identifier: AGPL-3.0-only
"""#2988 — la tête relue et les voisins d'un lot créé depuis une revue.

Un constat né d'une ultrareview doit citer la tête exacte que la revue a lue,
et la création doit confronter le constat aux lots existants : même fichier,
titre proche, ou commit qui a déjà changé la ligne citée. Sans cela, #2981
(P1) doublonnait #2915, fusionné quatre heures plus tôt.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from ameesh import intake_neighbors, work

from .support import PgTestCase


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True,
        timeout=30).stdout


class NeighborTest(PgTestCase):
    """T1, T5 : même fichier, titre proche, fenêtre de sept jours."""

    def setUp(self):
        super().setUp()

    def lot(self, title, *, body="", app="p", **kwargs):
        return work.add(self.db, title=title, body=body, app=app, **kwargs)

    def test_t1_merged_lot_citing_the_same_file_is_a_neighbor(self):
        old = self.lot("OTA production publie sans APP_VARIANT",
                       body="Corrige `.github/workflows/mobile-build.yml:121` sur develop.")
        self.db.execute(
            "UPDATE work_items SET state = 'merged', branch_target = 'develop'"
            " WHERE id = %s", (old["id"],))
        found = intake_neighbors.neighbors(
            self.db, "p", title="mobile-build.yml:121",
            body="le constat cite la ligne APP_VARIANT")
        self.assertEqual([row["id"] for row in found["neighbors"]], [old["id"]])
        row = found["neighbors"][0]
        self.assertIn("même fichier", row["motifs"][0])
        self.assertIn("mobile-build.yml:121", row["motifs"][0])
        self.assertEqual(row["state_label"], "merged → develop")

    def test_promoted_neighbor_is_labelled_delivered(self):
        old = self.lot("correctif livré", body="x.txt:1")
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s",
                        (old["id"],))
        found = intake_neighbors.neighbors(self.db, "p", title="x.txt:1")
        self.assertEqual([row["id"] for row in found["neighbors"]], [old["id"]])
        self.assertEqual(found["neighbors"][0]["state_label"], "livré")

    def test_t5_neighbor_closed_eight_days_ago_is_excluded(self):
        old = self.lot("ancien constat", body="x.txt:1")
        recent = self.lot("constat récent", body="x.txt:1")
        self.db.execute(
            "UPDATE work_items SET state = 'closed', close_reason = 'abandoned',"
            " closed_at = now() - interval '8 days' WHERE id = %s", (old["id"],))
        self.db.execute(
            "UPDATE work_items SET state = 'closed', close_reason = 'abandoned',"
            " closed_at = now() - interval '6 days' WHERE id = %s", (recent["id"],))
        found = intake_neighbors.neighbors(self.db, "p", title="x.txt:1",
                                           body="même ligne")
        ids = [row["id"] for row in found["neighbors"]]
        self.assertNotIn(old["id"], ids)
        self.assertIn(recent["id"], ids)

    def test_title_similarity_needs_three_significant_words(self):
        lot = self.lot("l'OTA production publie sans APP_VARIANT sur develop")
        found = intake_neighbors.neighbors(
            self.db, "p",
            title="l'OTA production publie sans APP_VARIANT",
            body="pas de chemin ici")
        row = next(item for item in found["neighbors"] if item["id"] == lot["id"])
        self.assertTrue(any(motif.startswith("titre proche") for motif in row["motifs"]))
        found = intake_neighbors.neighbors(
            self.db, "p", title="OTA production", body="deux mots seulement")
        self.assertNotIn(lot["id"], [item["id"] for item in found["neighbors"]])


class GitTest(PgTestCase):
    """T2 : un commit après la tête qui touche la ligne donne le motif git."""

    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp(prefix="n2988-git-", dir=self.tmp)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        _git(self.repo, "config", "user.name", "test")
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("line1\nline2\nline3\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-qm", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD").strip()

    def test_t2_commit_touching_the_line_names_its_lot(self):
        lot = work.add(self.db, title="correction de la ligne deux", app="p")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (lot["id"],))
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("line1\nCHANGED\nline3\n")
        _git(self.repo, "commit", "-qam", "fix line two\n\nameesh-work: %d" % lot["id"])
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:2", body="le constat vise la ligne deux",
            reviewed_head=self.head, repo=self.repo)
        self.assertEqual(found["git"], "vérifié")
        row = next(item for item in found["neighbors"] if item["id"] == lot["id"])
        motif = next(m for m in row["motifs"] if "peut-être corrigé par" in m)
        self.assertIn("#%d" % lot["id"], motif)
        # la ligne non touchée ne donne aucun voisin
        untouched = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:3", body="le constat vise la ligne trois",
            reviewed_head=self.head, repo=self.repo)
        self.assertEqual(untouched["neighbors"], [])

    def _merged_lot(self, title):
        lot = work.add(self.db, title=title, app="p")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (lot["id"],))
        return lot["id"]

    def _patch(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_two_commits_touching_the_same_line_are_both_read(self):
        # B1 (#2988) : le second commit qui touche la ligne était rejeté
        # (chunk commençant par le patch précédent), et le lot qu'il citait
        # disparaissait quand le commit le plus récent ne citait rien.
        lot = self._merged_lot("correctif porté par le commit précédent")
        self._patch("f.txt", "line1\nFIXED\nline3\n")
        _git(self.repo, "commit", "-qam", "fix older\n\nameesh-work: %d" % lot)
        self._patch("f.txt", "line1\nFIXED2\nline3\n")
        _git(self.repo, "commit", "-qam", "tweak newer")
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:2", body="", reviewed_head=self.head,
            repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_line_cited_at_the_reviewed_head_is_followed_into_the_target(self):
        # B2 (#2988) : une insertion en tête décale la ligne citée ; `-L` sur
        # les coordonnées de la tête ne voyait pas le correctif de la cible.
        self._patch("shifted.txt", "one\nbug\nthree\n")
        _git(self.repo, "add", "shifted.txt")
        _git(self.repo, "commit", "-qm", "base shifted")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = self._merged_lot("correctif après insertion")
        self._patch("shifted.txt", "inserted\none\nbug\nthree\n")
        _git(self.repo, "commit", "-qam", "insert a line above")
        self._patch("shifted.txt", "inserted\none\nFIXED\nthree\n")
        _git(self.repo, "commit", "-qam", "fix bug\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="shifted.txt:2", body="le constat vise la ligne deux",
            reviewed_head=head, repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_deleted_line_names_the_lot_that_deleted_it(self):
        # B2 second tour : la ligne citée est supprimée dans la cible ; `-L`
        # ne peut pas partir d'une ligne absente, le suivi avant doit garder
        # le commit qui la supprime.
        lot = self._merged_lot("suppression de la ligne")
        self._patch("f.txt", "line1\nline3\n")
        _git(self.repo, "commit", "-qam", "delete bug line\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:2", body="", reviewed_head=self.head,
            repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_deleted_line_is_followed_across_hunks_of_one_commit(self):
        # Relecture claude de #2988 : un commit à plusieurs hunks compare
        # chaque hunk en coordonnées d'avant ce commit ; décaler la position
        # dès le premier hunk ratait la suppression (et voyait une ligne
        # voisine touchée).
        self._patch("many.txt", "".join("l%d\n" % i for i in range(1, 21)))
        _git(self.repo, "add", "many.txt")
        _git(self.repo, "commit", "-qm", "base many")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = self._merged_lot("suppression après un autre hunk")
        kept = [i for i in range(1, 21) if not (2 <= i <= 6 or i == 10)]
        self._patch("many.txt", "".join("l%d\n" % i for i in kept))
        _git(self.repo, "commit", "-qam", "drop 2-6 and 10\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="many.txt:10", body="", reviewed_head=head,
            repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])
        # l'inverse : insertion au-dessus puis suppression plus bas dans le
        # même commit ; la ligne 10 n'est pas touchée et suit sa route
        self._patch("other.txt", "".join("o%d\n" % i for i in range(1, 21)))
        _git(self.repo, "add", "other.txt")
        _git(self.repo, "commit", "-qm", "base other")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        wrong = self._merged_lot("insertion haute et suppression basse")
        lines = ["o%d\n" % i for i in range(1, 21) if i != 15]
        lines[2:2] = ["new%d\n" % i for i in range(5)]
        self._patch("other.txt", "".join(lines))
        _git(self.repo, "commit", "-qam", "insert and drop 15\n\nameesh-work: %d" % wrong)
        later = self._merged_lot("suppression de la ligne suivie")
        lines.remove("o10\n")
        self._patch("other.txt", "".join(lines))
        _git(self.repo, "commit", "-qam", "drop 10\n\nameesh-work: %d" % later)
        found = intake_neighbors.neighbors(
            self.db, "p", title="other.txt:10", body="", reviewed_head=head,
            repo=self.repo)
        hits = {item["id"] for item in found["neighbors"] if item["git"]}
        self.assertEqual(hits, {later})

    def test_deleted_file_names_the_lot_that_deleted_it(self):
        # B2 second tour : le fichier entier disparaît ; le chemin est résolu
        # dans la tête, pas seulement dans la cible.
        lot = self._merged_lot("suppression du fichier")
        os.remove(os.path.join(self.repo, "f.txt"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "delete file\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:2", body="", reviewed_head=self.head,
            repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_insert_at_head_and_deleted_line_in_one_commit(self):
        # B2 troisième tour : les hunks d'un commit sont tous relatifs au même
        # parent. Témoin [(0,0,1,1),(5,1,5,0)] : la position passait de 5 à 6
        # au premier hunk et ne voyait plus la suppression de la ligne 5.
        self._patch("f.txt", "one\ntwo\nthree\nfour\nbug\nsix\nseven\n")
        _git(self.repo, "commit", "-qam", "base witness")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = self._merged_lot("insertion et suppression dans le même commit")
        self._patch("f.txt", "insert\none\ntwo\nthree\nfour\nsix\nseven\n")
        _git(self.repo, "commit", "-qam",
             "insert head and delete bug\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:5", body="", reviewed_head=head,
            repo=self.repo)
        ids = [item["id"] for item in found["neighbors"]]
        self.assertIn(lot, ids)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_negative_shift_above_the_deleted_line(self):
        # B2 troisième tour : une suppression au-dessus décale la position vers
        # le haut ; comparée au hunk suivant, elle manquait la suppression de
        # la ligne citée, pourtant relative au parent fixe.
        self._patch("f.txt", "one\ntwo\nthree\nfour\nbug\nsix\nseven\n")
        _git(self.repo, "commit", "-qam", "base negative shift")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = self._merged_lot("deux suppressions dans le même commit")
        self._patch("f.txt", "one\nthree\nfour\nsix\nseven\n")
        _git(self.repo, "commit", "-qam",
             "delete two and bug\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:5", body="", reviewed_head=head,
            repo=self.repo)
        ids = [item["id"] for item in found["neighbors"]]
        self.assertIn(lot, ids)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_real_git_path_case_is_preserved(self):
        # B3 (#2988) : `Makefile` ne doit pas devenir `makefile` pour Git
        # (deux noms distincts sous Linux).
        self._patch("Makefile", "all:\n\techo bug\n")
        _git(self.repo, "add", "Makefile")
        _git(self.repo, "commit", "-qm", "add Makefile")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = self._merged_lot("correctif du Makefile")
        self._patch("Makefile", "all:\n\techo FIXED\n")
        _git(self.repo, "commit", "-qam", "fix makefile\n\nameesh-work: %d" % lot)
        found = intake_neighbors.neighbors(
            self.db, "p", title="Makefile:2", body="", reviewed_head=head,
            repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot)
        self.assertTrue(any("peut-être corrigé par #%d" % lot in motif
                            for motif in row["motifs"]), row["motifs"])

    def test_without_clone_git_is_unverified(self):
        found = intake_neighbors.neighbors(
            self.db, "p", title="f.txt:2", body="", reviewed_head=self.head,
            repo=os.path.join(self.tmp, "pas-un-depot"))
        self.assertEqual(found["git"], "non vérifié")

    def test_basename_finds_the_file_in_a_subdirectory(self):
        os.makedirs(os.path.join(self.repo, "sub"))
        with open(os.path.join(self.repo, "sub", "g.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\ntwo\n")
        _git(self.repo, "add", "sub/g.txt")
        _git(self.repo, "commit", "-qm", "add nested file")
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        lot = work.add(self.db, title="correction du fichier imbriqué", app="p")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (lot["id"],))
        with open(os.path.join(self.repo, "sub", "g.txt"), "w", encoding="utf-8") as fh:
            fh.write("ONE\ntwo\n")
        _git(self.repo, "commit", "-qam", "fix nested\n\nameesh-work: %d" % lot["id"])
        found = intake_neighbors.neighbors(
            self.db, "p", title="g.txt:1", body="le constat vise g.txt",
            reviewed_head=head, repo=self.repo)
        row = next(item for item in found["neighbors"] if item["id"] == lot["id"])
        self.assertTrue(any("peut-être corrigé par #%d" % lot["id"] in motif
                            for motif in row["motifs"]), row["motifs"])


class CreationTest(PgTestCase):
    """T3, T4 : marque de la tête relue, avertissement, review-after-merge."""

    def setUp(self):
        super().setUp()

    def created_note(self, title):
        item = next(row for row in work.list_items(self.db) if row["title"] == title)
        notes = [event["note"] for event in work.events(self.db, item["id"])]
        return item, notes[-1]

    def test_t3_review_without_head_is_created_and_marked_absent(self):
        title = "constat ultrareview sans tête"
        proc = self.mesh("work", "add", "--title", title,
                         "--source", "ultrareview develop→main",
                         "--app", "p", "--new-project", "--estimate", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item, note = self.created_note(title)
        self.assertIn("reviewed head: absent", note)
        self.assertIn("lot né d'une revue sans tête relue : --reviewed-head <sha>",
                      proc.stderr)
        self.assertEqual(intake_neighbors.reviewed_head_of(
            work.events(self.db, item["id"])), "absent")

    def test_t3_review_with_head_journals_the_sha(self):
        title = "constat ultrareview avec tête"
        sha = "a" * 40
        proc = self.mesh("work", "add", "--title", title,
                         "--source", "ultrareview develop→main",
                         "--reviewed-head", sha,
                         "--app", "p", "--new-project", "--estimate", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item, note = self.created_note(title)
        self.assertIn("reviewed head: " + sha, note)
        self.assertNotIn("sans tête relue", proc.stderr)
        self.assertEqual(intake_neighbors.reviewed_head_of(
            work.events(self.db, item["id"])), sha)
        shown = self.mesh("work", "show", str(item["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("reviewed : " + sha, shown.stdout)

    def test_t4_review_after_merge_gets_no_mark_and_no_warning(self):
        title = "REVUE #12 : constat après fusion"
        proc = self.mesh("work", "add", "--title", title,
                         "--source", "review-after-merge:#12",
                         "--app", "p", "--new-project", "--estimate", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, note = self.created_note(title)
        self.assertNotIn("reviewed head:", note)
        self.assertNotIn("sans tête relue", proc.stderr)
        self.assertIsNone(intake_neighbors.reviewed_head_of(
            work.events(self.db, next(
                row["id"] for row in work.list_items(self.db)
                if row["title"] == title))))

    def test_non_hexadecimal_head_is_refused_before_creation(self):
        before = [row["id"] for row in work.list_items(self.db)]
        proc = self.mesh("work", "add", "--title", "tête invalide",
                         "--reviewed-head", "pas-un-sha",
                         "--app", "p", "--new-project", "--estimate", "30m")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hexadecimal", proc.stderr)
        self.assertEqual([row["id"] for row in work.list_items(self.db)], before)

    def test_add_prints_neighbors_on_stderr_and_writes_them_in_the_note(self):
        old = work.add(self.db, title="OTA production sans APP_VARIANT",
                       body="mobile-build.yml:121", app="p")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (old["id"],))
        title = "constat sur mobile-build.yml:121"
        sha = "b" * 40
        proc = self.mesh("work", "add", "--title", title,
                         "--source", "ultrareview develop→main",
                         "--reviewed-head", sha,
                         "--app", "p", "--new-project", "--estimate", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, note = self.created_note(title)
        self.assertIn("reviewed head: " + sha, note)
        self.assertIn("neighbors: #%d" % old["id"], note)
        self.assertIn("neighbor: #%d" % old["id"], proc.stderr)
        self.assertIn("même fichier", proc.stderr)

    def test_work_neighbors_is_read_only_and_shows_the_neighbor(self):
        old = work.add(self.db, title="constat existant", body="mobile-build.yml:121",
                       app="p")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (old["id"],))
        before = len(work.list_items(self.db, limit=None))
        proc = self.mesh("work", "neighbors", "--app", "p",
                         "--title", "mobile-build.yml:121",
                         "--body", "le constat", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"id": %d' % old["id"], proc.stdout)
        self.assertEqual(len(work.list_items(self.db, limit=None)), before)


if __name__ == "__main__":
    unittest.main()
