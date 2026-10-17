# SPDX-License-Identifier: AGPL-3.0-only
"""#1333 : le radar lit la cible **publiée** (`origin/<branche>`) quelle que
soit l'écriture de la cible — `develop`, `origin/develop`,
`refs/remotes/origin/develop`, `refs/heads/develop`, `release/1.x`,
`origin/HEAD` —, pas une ref locale périmée, et ne signale que les migrations
du delta propre du lot — pas celles que la cible porte déjà. Un SHA figé reste
un cas distinct, lu tel quel sans certification (R1 B1).

Incident : un clone partagé gardait `develop` local en retard de 266 commits
sur `origin/develop` ; chaque branche ayant fusionné une develop fraîche
paraissait « ajouter » les migrations 334/355/…, et 19 lots recevaient un faux
« migration number N is duplicated ». La vraie collision (438 de #692 face à
`438_minors_age_policy.sql` de develop) passait inaperçue, faute de cible
fraîche."""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import migration_reservations, radar, registry, work

from .support import PgTestCase
from .test_l160b_radar import MIG, _git, _write

DOC_A = "lot/10-doc-a"
DOC_B = "lot/11-doc-b"
OPTIN = "lot/692-optin"
MEDIA = "lot/1223-media"
FIRST = "lot/20-first"
SECOND = "lot/21-second"
MEDIA_435 = "lot/30-media435"
NEW = "lot/12-new"

#: les migrations de `origin/develop` que l'incident voyait à tort sur chaque
#: branche ; 435 et 438 servent de vraies collisions de numéro.
DEVELOP_MIGRATIONS = ("334_circles_actor_contract.sql", "355_actor_move_t6b.sql",
                      "435_minors_age_policy.sql", "438_minors_age_policy.sql")

EDITS = {
    DOC_A: {"docs/a.md": "a\n"},
    DOC_B: {"docs/b.md": "b\n"},
    OPTIN: {MIG + "/438_partner_loyalty_optin_write.sql": "-- 692\n"},
    MEDIA: {MIG + "/438_media_at_rest_cid_guard.sql": "-- 1223\n"},
    FIRST: {MIG + "/460_first.sql": "-- first\n"},
    SECOND: {MIG + "/460_second.sql": "-- second\n"},
    MEDIA_435: {MIG + "/435_media_at_rest_cid_guard.sql": "-- 435\n"},
    NEW: {MIG + "/900_new_guard.sql": "-- 900\n"},
}

LOTS = [{"id": 10, "branch": DOC_A, "title": "docs a"},
        {"id": 11, "branch": DOC_B, "title": "docs b"},
        {"id": 692, "branch": OPTIN, "title": "optin"},
        {"id": 1223, "branch": MEDIA, "title": "media"},
        {"id": 20, "branch": FIRST, "title": "first"},
        {"id": 21, "branch": SECOND, "title": "second"},
        {"id": 30, "branch": MEDIA_435, "title": "media 435"},
        {"id": 12, "branch": NEW, "title": "new number"}]


def make_remote(root) -> tuple[str, str, str, str]:
    """Un clone dont `origin/develop` a avancé et dont la branche locale
    `develop` reste en arrière : `(dépôt, origin, ancienne, nouvelle)`."""
    origin = os.path.join(root, "origin.git")
    _git(root, "init", "-q", "--bare", "--initial-branch=develop", origin)
    repo = os.path.join(root, "repo")
    _git(root, "clone", "-q", origin, repo)
    _write(repo, "README.md", "base\n")
    _write(repo, MIG + "/100_base.sql", "-- base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    old = _git(repo, "rev-parse", "HEAD")
    for name in DEVELOP_MIGRATIONS:
        _write(repo, MIG + "/" + name, "-- %s\n" % name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "develop avance")
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "develop")
    # le clone a été fait sur un dépôt vide : la pointe par défaut du remote
    # n'existe qu'après la première poussée (état normal d'un clone d'agent)
    _git(repo, "remote", "set-head", "origin", "develop")
    _git(repo, "reset", "--hard", old)           # la branche locale reste périmée
    return repo, origin, old, new


def lot_branch(repo, name, edits, *, merge=True) -> str:
    """Une branche de lot née de `develop` local (périmé) ; avec `merge`, elle
    a fusionné `origin/develop` : son delta face à la ref périmée contient les
    migrations de develop, son delta face à la ref publiée n'a que ses propres
    fichiers."""
    _git(repo, "checkout", "-q", "-B", name, "develop")
    for path, text in edits.items():
        _write(repo, path, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", name)
    if merge:
        _git(repo, "merge", "-q", "--no-edit", "origin/develop")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "develop")
    return tip


def advance_remote(root, origin, name="470_remote.sql") -> str:
    """Une poussée depuis un AUTRE clone : `origin/develop` avance sans que le
    dépôt du radar ne récupère — sa ref de suivi reste en arrière, comme dans
    un clone partagé que personne ne fetch."""
    other = os.path.join(root, "other")
    _git(root, "clone", "-q", origin, other)
    _write(other, MIG + "/" + name, "-- %s\n" % name)
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "avance distante")
    ahead = _git(other, "rev-parse", "HEAD")
    _git(other, "push", "-q", "origin", "develop")
    return ahead


#: la branche qui porte la vraie collision 435 n'a PAS fusionné develop : sa
#: collision ne tient qu'à une cible fraîche, sinon elle est invisible.
NO_MERGE = {MEDIA_435}


class CertifiedTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-radar-1333-")
        self.repo, self.origin, self.old, self.new = make_remote(self.tmp)
        for row in LOTS:
            lot_branch(self.repo, row["branch"], EDITS[row["branch"]],
                       merge=row["branch"] not in NO_MERGE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, **kw):
        return radar.scan(self.repo, LOTS, default_target="develop", migrations=MIG, **kw)

    def test_the_published_ref_is_read_not_the_stale_local_branch(self):
        report = self.scan()
        self.assertEqual({b["target_ref"] for b in report["branches"]},
                         {"origin/develop"})
        self.assertEqual({b["target_head"] for b in report["branches"]}, {self.new})
        self.assertEqual(report["uncertified_targets"], [])

    def test_docs_only_lots_keep_no_inherited_migration(self):
        report = self.scan()
        dups = {d["number"]: d for d in report["migrations"]}
        self.assertEqual(sorted(dups), ["435", "438", "460"])
        holders = {h["work_item"] for d in report["migrations"] for h in d["lots"]}
        self.assertFalse(holders & {10, 11, 12})

    def test_real_number_collisions_are_reported(self):
        dups = {d["number"]: d for d in self.scan()["migrations"]}
        self.assertEqual([h["work_item"] for h in dups["438"]["lots"]], [692, 1223])
        self.assertEqual(dups["438"]["in_target"], MIG + "/438_minors_age_policy.sql")
        # une seule branche face à la cible : la cible fraîche la révèle
        self.assertEqual([h["work_item"] for h in dups["435"]["lots"]], [30])
        self.assertEqual(dups["435"]["in_target"], MIG + "/435_minors_age_policy.sql")
        # deux lots, même numéro, noms différents, cible muette
        self.assertEqual([h["path"] for h in dups["460"]["lots"]],
                         [MIG + "/460_first.sql", MIG + "/460_second.sql"])
        self.assertIsNone(dups["460"]["in_target"])

    def test_every_branch_spelling_is_normalized_and_certified(self):
        """#1333 R1 B1 : `origin/develop`, `refs/remotes/origin/develop`,
        `refs/heads/develop` et la cible par défaut (`origin/HEAD`) nomment la
        même branche publiée que `develop` ; la certification doit les
        protéger toutes, pas seulement le nom nu."""
        for spelling in ("develop", "origin/develop", "refs/remotes/origin/develop",
                         "refs/heads/develop", None):
            with self.subTest(spelling=spelling):
                report = radar.scan(self.repo, LOTS, default_target=spelling,
                                    migrations=MIG, verify=radar.published_tip)
                self.assertEqual({b["target_ref"] for b in report["branches"]},
                                 {"origin/develop"})
                self.assertEqual({b["target_head"] for b in report["branches"]}, {self.new})
                self.assertEqual(report["uncertified_targets"], [])
                self.assertEqual(sorted(d["number"] for d in report["migrations"]),
                                 ["435", "438", "460"])

    def test_a_lagging_published_tip_uncertifies_every_spelling(self):
        """La pointe publiée avance sans fetch : la ref de suivi est périmée,
        quelle que soit l'écriture de la cible — certification et silence,
        jamais les migrations héritées de develop."""
        ahead = advance_remote(self.tmp, self.origin)
        for spelling in ("develop", "origin/develop", "refs/remotes/origin/develop",
                         "refs/heads/develop", None):
            with self.subTest(spelling=spelling):
                report = radar.scan(self.repo, LOTS, default_target=spelling,
                                    migrations=MIG, verify=radar.published_tip)
                self.assertEqual(report["migrations"], [])
                self.assertEqual([t["ref"] for t in report["uncertified_targets"]],
                                 ["origin/develop"])
                self.assertIn("not fetched", report["uncertified_targets"][0]["reason"])
                self.assertNotIn(ahead, {b["target_head"] for b in report["branches"]})

    def test_the_stale_local_ref_reproduces_the_incident_without_verify(self):
        """Témoin négatif conservé (auditeur) : sans certification, une cible
        qui ne peut être lue que sur la branche locale périmée — pas de ref de
        suivi — fait entrer les migrations de develop dans le delta de chaque
        branche, et la collision réelle 435 n'est pas rattachée."""
        _git(self.repo, "update-ref", "-d", "refs/remotes/origin/develop")
        stale = radar.scan(self.repo, LOTS, default_target="refs/heads/develop",
                           migrations=MIG)
        dups = {d["number"]: d for d in stale["migrations"]}
        self.assertIn("334", dups)
        self.assertIn("355", dups)
        self.assertEqual(len(dups["334"]["lots"]), 7)   # les 7 branches qui ont fusionné
        self.assertTrue({10, 11} <= {h["work_item"] for h in dups["334"]["lots"]})
        self.assertNotIn(30, {h["work_item"] for h in dups["334"]["lots"]})
        self.assertIsNone(dups["334"]["in_target"])
        self.assertIsNone(dups["435"]["in_target"])
        inherited = [h for h in dups["435"]["lots"]
                     if h["path"] == MIG + "/435_minors_age_policy.sql"]
        self.assertGreaterEqual(len(inherited), 2)

    def test_certification_recovers_a_target_read_on_a_stale_local_branch(self):
        """Même cas, avec `verify` : la pointe publiée est déjà présente
        localement, la cible est lue dessus et la sonde redevient juste."""
        _git(self.repo, "update-ref", "-d", "refs/remotes/origin/develop")
        report = radar.scan(self.repo, LOTS, default_target="refs/heads/develop",
                            migrations=MIG, verify=radar.published_tip)
        self.assertEqual(report["uncertified_targets"], [])
        self.assertEqual({b["target_head"] for b in report["branches"]}, {self.new})
        self.assertEqual(sorted(d["number"] for d in report["migrations"]),
                         ["435", "438", "460"])

    def test_origin_head_is_resolved_to_its_branch(self):
        report = radar.scan(self.repo, LOTS, default_target="origin/HEAD",
                            migrations=MIG, verify=radar.published_tip)
        self.assertEqual({b["target_ref"] for b in report["branches"]}, {"origin/develop"})
        self.assertEqual({b["target_head"] for b in report["branches"]}, {self.new})
        self.assertEqual(report["uncertified_targets"], [])

    def test_a_pinned_sha_is_trusted_without_certification(self):
        """Cas distinct (R1) : un SHA figé n'est pas une branche publiée, il se
        lit tel quel — aucune certification n'est tentée."""
        with mock.patch.object(radar, "published_tip") as tip:
            report = radar.scan(self.repo, LOTS, default_target=self.old,
                                migrations=MIG, verify=radar.published_tip)
        tip.assert_not_called()
        self.assertEqual({b["target_ref"] for b in report["branches"]}, {self.old})
        self.assertEqual(report["uncertified_targets"], [])

    def test_a_branch_name_with_a_slash_is_not_a_remote(self):
        """`release/1.x` : le premier segment n'est un remote que s'il est
        déclaré. La cible est lue sur `origin/release/1.x` et certifiée."""
        _git(self.repo, "checkout", "-q", "-B", "release/1.x", "origin/develop")
        _write(self.repo, MIG + "/470_release.sql", "-- 470\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "release 1.x")
        release = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "release/1.x")
        _git(self.repo, "checkout", "-q", "-B", "lot/40-doc-release", "release/1.x")
        _write(self.repo, "docs/release.md", "r\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "docs release")
        _git(self.repo, "checkout", "-q", "develop")
        lots = [dict(id=40, branch="lot/40-doc-release", title="docs release")]
        for spelling in ("release/1.x", "origin/release/1.x", "refs/heads/release/1.x"):
            with self.subTest(spelling=spelling):
                report = radar.scan(self.repo, lots, default_target=spelling,
                                    migrations=MIG, verify=radar.published_tip)
                self.assertEqual({b["target_ref"] for b in report["branches"]},
                                 {"origin/release/1.x"})
                self.assertEqual({b["target_head"] for b in report["branches"]}, {release})
                self.assertEqual(report["uncertified_targets"], [])
                # le lot n'ajoute qu'une doc : la migration 470 de sa cible
                # n'est à personne
                self.assertEqual(report["migrations"], [])

    def test_an_uncertified_target_silences_the_probe(self):
        report = self.scan(verify=lambda repo, remote, branch: None)
        self.assertEqual(report["migrations"], [])
        self.assertEqual([t["target"] for t in report["uncertified_targets"]], ["develop"])
        self.assertIn("unreadable", report["uncertified_targets"][0]["reason"])
        self.assertEqual(radar.summary(report)["uncertified_targets"], 1)
        self.assertIn("cible non certifiée", "\n".join(radar.render(report)))

    def test_a_published_tip_not_fetched_is_uncertified(self):
        report = self.scan(verify=lambda repo, remote, branch: "0" * 40)
        self.assertEqual(report["migrations"], [])
        self.assertIn("not fetched", report["uncertified_targets"][0]["reason"])

    def test_a_shallow_history_silences_the_probe(self):
        """Un clone superficiel rend `merge-base` suspect (frontière fausse) :
        la sonde se tait au lieu d'accuser des migrations héritées."""
        _write(self.repo, ".git/shallow", "")       # marqueur de clone superficiel
        report = self.scan()
        self.assertEqual(report["migrations"], [])
        self.assertEqual(report["migration_probe"],
                         "suspended: shallow repository (history is incomplete)")
        self.assertEqual({t["reason"] for t in report["uncertified_targets"]},
                         {"shallow repository (history is incomplete)"})
        self.assertIn("sonde des migrations suspended", "\n".join(radar.render(report)))

    def test_a_published_tip_present_locally_is_used(self):
        _git(self.repo, "checkout", "-q", "-B", "tmp-avance", self.new)
        _write(self.repo, MIG + "/461_remote_only.sql", "-- 461\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "avance distante")
        advanced = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        report = self.scan(verify=lambda repo, remote, branch: advanced)
        self.assertEqual(report["uncertified_targets"], [])
        self.assertEqual({b["target_head"] for b in report["branches"]}, {advanced})

    def test_reservations_ignore_uncertified_deltas_but_keep_pinned_targets(self):
        # The stale target makes a docs-only lot inherit 334/355/… in added.
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.old)
        project = radar.Project(name="p", repo=self.repo, migrations=MIG,
                                github="acme/product")
        selected = [dict(LOTS[0], branch_target="develop"),
                    dict(LOTS[-1], branch_target=self.new)]
        registry = mock.Mock()
        registry.work.migration_holder.return_value = None
        for published in (None, "0" * 40):
            with self.subTest(published=published), mock.patch.object(
                    migration_reservations.storage, "of", return_value=registry):
                report = radar.scan(
                    self.repo, selected, migrations=MIG,
                    verify=lambda *_: published,
                    reservations=lambda raw: migration_reservations.probe(None, project, raw))
            self.assertEqual([r["number"] for r in report["unreserved_migrations"]], ["900"])
            self.assertEqual(len(report["uncertified_targets"]), 1)
        _write(self.repo, ".git/shallow", "")
        with mock.patch.object(migration_reservations.storage, "of", return_value=registry):
            report = radar.scan(
                self.repo, selected, migrations=MIG,
                reservations=lambda raw: migration_reservations.probe(None, project, raw))
        self.assertEqual(report["unreserved_migrations"], [])

    def test_published_tip_reads_the_remote_and_fails_closed(self):
        self.assertEqual(radar.published_tip(self.repo, "origin", "develop"), self.new)
        self.assertIsNone(radar.published_tip(self.repo, "origin", "absente"))
        local = os.path.join(self.tmp, "sans-origin")
        os.makedirs(local)
        _git(local, "init", "-q", "-b", "develop")
        _write(local, "a.txt", "a\n")
        _git(local, "add", "-A")
        _git(local, "commit", "-qm", "a")
        self.assertIsNone(radar.published_tip(local, "origin", "develop"))


class RunPassTargetTest(PgTestCase):
    """Le passage de l'exécuteur : cible publiée, courrier juste."""

    def setUp(self):
        super().setUp()
        self.repo, _origin, _old, self.new = make_remote(self.tmp)
        for row in LOTS:
            if row["id"] in (10, 11, 692, 1223, 30):
                lot_branch(self.repo, row["branch"], EDITS[row["branch"]],
                           merge=row["branch"] not in NO_MERGE)
        self.cfg = dataclasses.replace(self.cfg, radar={"projects": {"ameesh": {
            "repo": self.repo, "migrations": MIG, "target": "develop"}}})
        for name in ("a1", "a2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)

    def lot(self, row, assignee):
        return work.add(self.db, title=row["title"], branch=row["branch"],
                        assignee=assignee)["id"]

    def row(self, work_item):
        return next(r for r in LOTS if r["id"] == work_item)

    def test_docs_only_lots_get_no_migration_mail(self):
        for row in LOTS:
            if row["id"] in (10, 11):
                self.lot(row, "a1")
        lines = radar.run_pass(self.cfg, self.db)
        self.assertIsNone(lines[0]["error"])
        self.assertEqual(lines[0]["summary"]["migration_duplicates"], 0)
        self.assertEqual(lines[0]["summary"]["uncertified_targets"], 0)
        self.assertEqual(lines[0]["mail"], [])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)

    def test_real_collisions_reach_their_owners(self):
        optin = self.lot(self.row(692), "a1")
        media = self.lot(self.row(1223), "a2")
        lines = radar.run_pass(self.cfg, self.db)
        self.assertEqual(lines[0]["summary"]["migration_duplicates"], 1)
        self.assertEqual({(m["lot"], m["to"], m["level"]) for m in lines[0]["mail"]},
                         {(optin, "a1", "passive"), (media, "a2", "passive")})

    def test_an_unreadable_publication_sends_nothing(self):
        self.lot(self.row(692), "a1")
        self.lot(self.row(1223), "a2")
        with mock.patch.object(radar, "published_tip", return_value=None):
            lines = radar.run_pass(self.cfg, self.db)
        self.assertEqual(lines[0]["summary"]["migration_duplicates"], 0)
        self.assertEqual(lines[0]["summary"]["uncertified_targets"], 1)
        self.assertEqual(lines[0]["mail"], [])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
