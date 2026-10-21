# SPDX-License-Identifier: AGPL-3.0-only
"""#2803 — sur un véhicule, les auteurs sont ceux du delta relu, pas du paquet.

#2766 fait relire à la porte du véhicule son seul **delta propre** (résolution
non mécanique, adaptation) ; les membres ont leurs avis sur leur propre tête.
#2803 aligne la lecture des auteurs sur ce contenu : les familles des auteurs
des lots membres ne sont plus exclues une seconde fois, et le composeur n'est
pas une référence d'office — il ne compte que s'il a écrit un commit du delta.
Le commit de manifeste (`reviews/integration-*.md`) reste hors lecture.
"""
from __future__ import annotations

import os
import tempfile
from unittest import TestCase, mock

from ameesh import merge_queue, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

HEAD_HEX = "a" * 40
FAMILIES = ("claude", "codex", "deepseek")
PACKET = {"id": 1, "assignee": "composer", "branch": "integration/paquet-test"}
NORMAL = {"id": 1, "assignee": "composer", "branch": "lot/1-review"}


def mark(number: int, actor: str, *, sha: str = HEAD_HEX) -> dict:
    return {"kind": "verdict", "id": number, "actor": actor, "verdict": "ok",
            "sha": sha, "at_ts": 100.0 + number}


class ReferenceTest(TestCase):
    def test_the_composer_of_a_packet_is_not_the_reference_author(self):
        self.assertEqual(review_authors.packet_composer(PACKET), "composer")
        self.assertEqual(review_authors.packet_composer(NORMAL), "")
        self.assertEqual(
            review_authors.reference_author(None, PACKET, None, FAMILIES),
            {"name": "", "family": ""},
            "the packet composer must not be seated as the reference author")

    def test_an_ordinary_lot_keeps_its_reference_author(self):
        self.assertEqual(
            review_authors.reference_author(None, NORMAL, None, FAMILIES),
            {"name": "composer", "family": ""})


class ReceiptProvider:
    """A receipt source that names the commit's actor and harness exactly."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def receipt_for_commit(self, _db, sha):
        return self.rows.get(sha)


class ReadingTest(TestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        self.repo = _Repo(self.root)
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.lot = self.repo.commit("lot", agent="impl")
        self.repo.write("reviews/integration-paquet-test.md", "# manifest\n")
        self.manifest = self.repo.commit("manifest", agent="composer")
        self.provider = ReceiptProvider({
            self.lot: {"actor": "impl", "harness": "deepseek"},
            self.manifest: {"actor": "composer", "harness": "claude"},
        })

    def reading(self, item, commits):
        with mock.patch.object(review_authors, "designated_reviewers",
                               return_value=set()):
            return review_authors.reading(
                None, item, None, commits, self.repo.root, FAMILIES,
                provider=self.provider)

    def test_a_packet_manifest_commit_does_not_make_the_composer_an_author(self):
        authors, unknown, _correctors = self.reading(
            PACKET, [self.lot, self.manifest])
        self.assertEqual([row["name"] for row in authors], ["impl"])
        self.assertEqual(unknown, [])

    def test_an_ordinary_lot_still_reads_its_manifest_commit(self):
        authors, _unknown, _correctors = self.reading(
            NORMAL, [self.lot, self.manifest])
        self.assertEqual(sorted(row["name"] for row in authors),
                         ["composer", "impl"])


class VehicleDeltaTest(PgTestCase):
    """The gate with #2766's own delta: only that delta's authors count."""

    def setUp(self):
        super().setUp()
        for name, harness in (("composer", "claude"), ("impl", "codex"),
                              ("resolver", "deepseek"), ("r_claude", "claude"),
                              ("r_codex", "codex"), ("r_other", "codex"),
                              ("r_deepseek", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.repo = _Repo(os.path.join(self.tmp, "vehicle"))
        self.repo.write("src/ameesh/vehicle.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/member.py", "member\n")
        self.member = self.repo.commit("member", agent="impl")
        self.repo.write("reviews/integration-paquet-test.md", "# manifest\n")
        self.manifest = self.repo.commit("manifest", agent="composer")
        self.repo.write("src/ameesh/vehicle.py", "resolution\n")
        self.resolution = self.repo.commit("resolution", agent="resolver")
        self.receipts = {
            self.member: ("impl", "codex"),
            self.manifest: ("composer", "claude"),
            self.resolution: ("resolver", "deepseek"),
        }
        for sha, (actor, harness) in self.receipts.items():
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness))

    def item(self, branch="integration/paquet-test"):
        row = work.add(self.db, title="vehicle authors", app="ameesh",
                       branch=branch, branch_target=self.base)
        lot = int(row["id"])
        self.db.execute("UPDATE work_items SET assignee = 'composer' WHERE id = %s",
                        (lot,))
        return work.get(self.db, lot)

    def marks(self, *actors, head=None):
        head = head or self.resolution
        return ([{"kind": "frozen", "id": 1, "sha": head,
                  "actor": "composer", "at_ts": 90.0}]
                + [mark(2 + index, actor, sha=head)
                   for index, actor in enumerate(actors)])

    def gate(self, commits, marks, *, required=1, head=None, item=None,
             authors=None):
        head = head or self.resolution
        vehicle = {"ok": None, "required": required,
                   "klass": {"class": "sensitive",
                             "files": [{"path": "src/ameesh/vehicle.py"}]},
                   "commits": list(commits),
                   "own_paths": ["src/ameesh/vehicle.py"]}
        return merge_queue.review_gate(
            self.db, marks, head, repo=self.repo.root,
            klass={"class": "sensitive", "project": "ameesh",
                   "base_sha": self.base,
                   "files": [{"path": "src/ameesh/vehicle.py"}]},
            item=item or self.item(), families=FAMILIES, vehicle=vehicle,
            authors=authors)

    def test_a_member_author_family_is_eligible_for_the_vehicle_delta(self):
        # Le membre est codex ; le delta propre est écrit par resolver
        # (deepseek). Un avis codex est indépendant du delta, même si le
        # membre est codex : la famille du membre n'est plus exclue ici.
        out = self.gate([self.resolution], self.marks("r_codex"))
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([row["name"] for row in out["authors"]], ["resolver"])
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_codex"])

    def test_the_composer_is_not_an_author_when_they_did_not_write_the_delta(self):
        # Le delta est écrit par resolver (deepseek) ; le composeur (claude)
        # n'est pas une référence : un avis claude est indépendant.
        out = self.gate([self.resolution], self.marks("r_claude"))
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([row["name"] for row in out["authors"]], ["resolver"])

    def test_a_composer_named_by_the_caller_is_dropped_when_the_delta_is_read(self):
        # #1515 : un appelant qui sème les détenteurs du journal (dont le
        # composeur, claude) ne peut pas le faire passer pour un auteur du
        # delta relu, écrit par resolver (deepseek) : l'avis claude reste
        # indépendant, comme dans le paquet #2791.
        out = self.gate(
            [self.resolution], self.marks("r_claude"),
            authors=[{"name": "composer", "family": "claude"}])
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([row["name"] for row in out["authors"]], ["resolver"])
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_claude"])

    def test_the_manifest_commit_is_not_an_author_of_the_delta(self):
        # Même si un appelant cite le manifeste dans les commits du delta, la
        # lecture l'écarte : le composeur n'y est pas auteur.
        out = self.gate([self.manifest, self.resolution],
                        self.marks("r_claude"))
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([row["name"] for row in out["authors"]], ["resolver"])

    def test_two_member_families_do_not_consume_the_two_reviews(self):
        # Delta écrit par le composeur (claude) : les deux familles codex et
        # deepseek restent éligibles, même si les membres les portent déjà.
        self.repo.write("src/ameesh/resolution.py", "by composer\n")
        by_composer = self.repo.commit("resolution by composer", agent="composer")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("commit:" + by_composer, by_composer, "composer", "s-claude", "t",
             "claude"))
        out = self.gate([by_composer],
                        self.marks("r_codex", "r_deepseek", head=by_composer),
                        required=2, head=by_composer)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual([row["name"] for row in out["authors"]], ["composer"])
        self.assertEqual([row["actor"] for row in out["accepted"]],
                         ["r_codex", "r_deepseek"])

    def test_the_resolution_author_family_still_cannot_review(self):
        out = self.gate([self.resolution], self.marks("r_deepseek"))
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["accepted"], [])

    def test_a_reviewer_correction_on_the_delta_is_still_excluded(self):
        # Auditeur #2766 : le klass d'un véhicule (class_of_paths) ne porte ni
        # base_sha ni target ; la lecture du delta (vehicle["commits"]) doit
        # quand même rendre le correcteur du relecteur (#2739), sans dépendre
        # de la plage gelée complète. #2888 : ce correcteur est un auteur du
        # delta, sa famille (codex) est donc exclue elle aussi.
        item = self.item()
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r_codex'] WHERE id = %s",
            (int(item["id"]),))
        item = work.get(self.db, int(item["id"]))
        self.repo.write("src/ameesh/vehicle.py", "resolution\nsmall fix\n")
        fix = self.repo.commit("r_codex: small fix", agent="r_codex")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("commit:" + fix, fix, "r_codex", "s-codex", "t", "codex"))
        out = self.gate([self.resolution, fix],
                        self.marks("r_codex", "r_other", "r_claude", head=fix),
                        required=2, head=fix, item=item)
        self.assertEqual(out["corrections"], ["r_codex"], out)
        self.assertFalse(out["ok"], out["why"])
        accepted = [row["actor"] for row in out["accepted"]]
        self.assertNotIn("r_codex", accepted)
        self.assertNotIn("r_other", accepted)   # same codex family, author now
        self.assertEqual(accepted, ["r_claude"])


class NoExactReadingTest(TestCase):
    """Sans delta de véhicule lisible, la porte garde la référence historique.

    Elle ne peut pas prouver que l'assigné (le composeur) n'est pas un auteur :
    elle le garde — direction stricte, sa famille reste exclue.
    """

    def test_a_packet_without_a_read_range_keeps_the_composer_as_author(self):
        out = merge_queue.review_gate(
            None, [mark(1, "r1")], HEAD_HEX,
            klass={"class": "sensitive", "files": [{"path": "src/ameesh/work.py"}]},
            required=1, item=PACKET,
            authors=[{"name": "impl", "family": "deepseek"}],
            family_of=lambda name: {"composer": "claude", "impl": "deepseek",
                                    "r1": "claude"}.get(
                                        str(name or "").strip(), ""))
        self.assertFalse(out["ok"], out)
        self.assertEqual([row["name"] for row in out["authors"]],
                         ["composer", "impl"])
        self.assertEqual(out["accepted"], [])
