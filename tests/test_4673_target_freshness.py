# SPDX-License-Identifier: AGPL-3.0-only
"""#4673 — la porte et la désignation lisent une cible fraîche.

Constat (orchestrateur Nexlink #165406) : `review_authors` lisait la ref
`origin/<cible>` d'un clone partagé, fraîche de son dernier `fetch` seulement.
Un commit déjà entré dans la cible (ici le manifeste `ad6c3982f` de Nexlink,
côté ameesh la remise `f632f729` citée par #4654) était alors compté
« auteur inconnu » dans la plage gelée, et la porte/la désignation refusait la
provenance. Un simple `git fetch origin` débloquait.

Attendu : la pointe **publiée** de la cible est relue à la forge avant toute
lecture d'ascendance, par SHA exact (comme un gel absent, #3147) ou par fetch
de la branche cible ; une forge muette laisse la ref locale (le doute ferme,
il n'ouvre jamais). Témoin : un clone en retard, plus un commit déjà dans la
cible, ne doivent pas donner « inconnu ».

Ce module couvre les deux chemins qui ont refusé : la lecture partagée
(`review_authors.reading`) et le pré-calcul de la porte (`_delta_base`,
`_complete_authors`). Il vérifie aussi que la transaction de gel
(`fetch=False`) ne touche jamais au réseau.
"""
from __future__ import annotations

import os
from unittest import mock

from ameesh import fire, merge_queue, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class FreshTargetFixture(PgTestCase):
    """Une cible qui a avancé depuis le dernier fetch du clone de lecture."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        for name, harness in (("impl", "deepseek"), ("other", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=os.path.join(self.tmp, "writer"), status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.writer = _Repo(os.path.join(self.tmp, "writer"))
        self.writer.write("base.txt", "base\n")
        self.base = self.writer.commit("base")
        self.writer.git("branch", "-M", "main")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.writer.git("init", "--bare", "-q", self.origin)
        self.writer.git("remote", "add", "origin", self.origin)
        self.writer.git("push", "-q", "origin", "main")
        # Le clone de lecture ne connaît que la cible d'alors ; il ne la
        # rafraîchira pas de lui-même.
        self.reader = _Repo(os.path.join(self.tmp, "reader"))
        self.reader.git("remote", "add", "origin", self.origin)
        self.reader.git("fetch", "-q", "origin", "main:refs/remotes/origin/main")
        self.assertEqual(self.reader.git("rev-parse", "refs/remotes/origin/main"),
                         self.base)
        # Un autre lot entre dans la cible, puis le lot lit sa propre tête.
        self.writer.write("delivered.txt", "already in the target\n")
        self.delivered = self.writer.commit("other: delivered by another lot",
                                            agent="other")
        self.writer.git("push", "-q", "origin", "main")
        self.writer.git("checkout", "-q", "-b", "lot/4673-freshness")
        self.writer.write("own.txt", "own change\n")
        self.head = self.writer.commit("impl: own change", agent="impl")
        self.writer.git("push", "-q", "origin", "HEAD")
        # Le clone de lecture reçoit la tête gelée (donc ses ancêtres, dont le
        # commit de la cible) sans déplacer `origin/main`.
        self.reader.git("fetch", "-q", "origin", self.head)
        self.assertEqual(self.reader.git("rev-parse", "refs/remotes/origin/main"),
                         self.base)
        self.item = work.add(self.db, title="cible fraîche #4673", app="ameesh",
                             assignee="impl", branch="lot/4673-freshness")
        self.lot = int(self.item["id"])
        for sha, actor, harness in ((self.head, "impl", "deepseek"),
                                    (self.delivered, "other", "claude")):
            if actor == "other":
                continue                    # le tiers n'a aucun reçu
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))
        self.marks = [{"id": 1, "kind": "frozen", "sha": self.head,
                       "actor": "impl", "at_ts": 0.0}]

    def names(self, authors):
        return {row["name"]: row["family"] for row in authors}

    def item_row(self):
        return work.get(self.db, self.lot)


class ReadingOnStaleCloneTest(FreshTargetFixture):
    def test_a_delivered_commit_in_a_stale_clone_is_not_unknown(self):
        authors, unknown, _correctors = review_authors.reading(
            self.db, self.item_row(), None, [self.head, self.delivered],
            self.reader.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target="main")
        self.assertEqual(unknown, [])
        self.assertEqual(self.names(authors), {"impl": "deepseek"})

    def test_the_published_target_is_read_before_the_local_ref(self):
        self.assertEqual(
            review_authors.target_commit(self.reader.root, "main", fetch=True),
            self.delivered)
        self.assertEqual(
            review_authors.target_commit(self.reader.root, "main"), self.base)

    def test_an_unreachable_forge_falls_back_to_the_local_ref(self):
        self.reader.git("remote", "set-url", "origin",
                        os.path.join(self.tmp, "missing.git"))
        self.assertEqual(
            review_authors.target_commit(self.reader.root, "main", fetch=True),
            self.base)

    def test_the_freeze_transaction_never_refreshes_the_target(self):
        # #3836 : la lecture du gel tourne dans sa transaction ; un fetch y
        # laisserait la session `idle in transaction`. Le doute garde le
        # commit de la cible, jamais une famille supposée.
        with mock.patch.object(review_authors, "_published_target",
                               side_effect=AssertionError("network in freeze")):
            _authors, unknown, _correctors = review_authors.reading(
                self.db, self.item_row(), None, [self.head, self.delivered],
                self.reader.root, FAMILIES, strict=True, base=self.base,
                head=self.head, target="main", fetch=False)
        self.assertIn(self.delivered[:12], unknown)


class GateOnStaleCloneTest(FreshTargetFixture):
    def test_the_gate_delta_starts_at_the_published_target(self):
        commit, _ref, _repo = merge_queue._delta_base(
            self.reader.root, self.item_row(), "main")
        self.assertEqual(commit, self.delivered)

    def test_the_complete_authors_read_the_published_target(self):
        authors, _families = merge_queue._complete_authors(
            self.db, self.item_row(), self.head, self.reader.root, "main",
            klass={"base_sha": self.base, "class": "normal"}, marks=self.marks)
        self.assertEqual(self.names(authors), {"impl": "deepseek"})

    def test_the_merge_gate_does_not_refuse_a_delivered_commit(self):
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, self.marks, self.head, repo=self.reader.root,
            klass={"base_sha": self.base, "class": "normal", "target": "main"},
            item=self.item_row(), families=FAMILIES, strict=True, target="main")
        self.assertEqual(self.names(authors), {"impl": "deepseek"})


class FireOnStaleCloneTest(FreshTargetFixture):
    def test_the_fire_gate_reads_the_published_target(self):
        # Le feu du pilote part d'un checkout partagé : sans la pointe
        # publiée, la lecture des auteurs refuse le commit déjà fusionné et
        # masque le vrai motif (avis manquants).
        row = dict(self.item_row(), branch_target="main")
        ok, why, _klass = fire.ready_verdicts(
            self.cfg, self.db, row, self.marks, repo=self.reader.root,
            project=None, target="main")
        self.assertFalse(ok)
        self.assertNotIn("provenance unknown", why)
        self.assertNotIn("complete authors", why)

    def test_a_vehicle_gate_receives_the_published_commit(self):
        # B/P1 dev1 (gel #26365) : `packet_reviews.need` résout sa base par
        # `plan_git._commit` ; lui passer « main » lirait la branche LOCALE du
        # checkout (ici volontairement divergente). La porte doit recevoir le
        # commit exact publié, pas le nom nu.
        self.reader.git("update-ref", "refs/heads/main", self.base)
        row = dict(self.item_row(), branch="integration/paquet",
                   assignee="composeur", branch_target="main")
        seen = {}

        def gate(_db, _marks, _frozen, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

        with mock.patch.object(merge_queue, "required_verdicts",
                               return_value=0), \
                mock.patch.object(merge_queue, "review_gate", side_effect=gate):
            ok, why, _klass = fire.ready_verdicts(
                self.cfg, self.db, row, self.marks, repo=self.reader.root,
                project=None, target="main")
        self.assertTrue(ok, why)
        self.assertEqual(seen["target"], self.delivered)
