# SPDX-License-Identifier: AGPL-3.0-only
"""#762 : les deux résidus P3 de la revue codex post-fusion de #550 (#530).

1. le dry-run de `menage` compte comme non supprimables les noms d'un inode
   qu'il supprimerait en réalité, et annonce une passe bloquée alors que la
   passe réelle évince le bloc (`housekeeping.py`, P2 du verdict) ;
2. quand la table des montages est redevenue lisible mais que la liste
   s'arrête à la limite de marche, l'épisode `tmp_unscanned` redémarre avec un
   nouveau `since` au lieu de continuer celui ouvert par la passe
   `mounts_unreadable` (`housekeeping.py`, `tmp_episode`).

Le troisième point — le test de crédit des liens durs qui passait aussi sur
l'ancien code — est corrigé dans `test_530_housekeeping_followups.py`, où ce
test vit.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import exploitation, menage, platform, registry

from .support import PgTestCase

HOUR = 3600.0
#: one file of one block: its size on disk is its length (sizes count blocks)
B = 4096


class _Cfg:
    def __init__(self, state_dir: str, dsn: str = "", schema: str = "public"):
        self.state_dir, self.dsn, self.schema, self.host = state_dir, dsn, schema, "pc"


def _write(path: str, size: int = B, age_s: float = 3 * HOUR) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        at = time.time() - age_s
        os.utime(path, (at, at))


class DryRunTest(unittest.TestCase):
    """Point 1 : le dry-run annonce ce que la passe ferait, liens durs compris."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="t762-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "tmp")
        menage._ensure_root(self.root)
        self.mine = os.path.join(self.root, "codex3")

    def _fields(self, result: dict) -> tuple:
        return (result["files"], result["evicted"], result["unremovable"],
                result["blocked"], result["after"])

    def test_dry_run_credits_the_names_it_would_remove(self):
        # deux noms du même inode, tous deux dans la marche : la passe les
        # retire tous les deux et crédite le bloc une fois ; le dry-run doit
        # annoncer la même chose au lieu de compter le bloc comme
        # non supprimable et bloqué (#762 P2)
        first = os.path.join(self.mine, "a")
        _write(first, size=5)                    # 5 bytes: one 4096 block
        os.link(first, os.path.join(self.mine, "b"))
        names = [os.path.join(self.mine, name) for name in ("a", "b")]
        dry = menage.evict_lru([self.mine], self.root, 0, dry_run=True)
        self.assertEqual(self._fields(dry), (1, B, 0, False, 0))
        self.assertTrue(all(os.path.exists(p) for p in names),
                        "a dry-run removes nothing")
        apply_ = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(self._fields(apply_), (1, B, 0, False, 0))
        self.assertFalse(any(os.path.exists(p) for p in names))

    def test_dry_run_does_not_credit_a_name_outside_the_walk(self):
        # un nom dans la marche, un autre hors de la racine gérée : l'inode
        # survit à la passe, rien n'est libéré ; le dry-run doit prédire la
        # comptabilité `unremovable`, pas créditer le bloc
        inside = os.path.join(self.mine, "a")
        _write(inside, size=5)
        keep = os.path.join(self.base, "keep")
        os.link(inside, keep)                    # a name outside the eviction
        dry = menage.evict_lru([self.mine], self.root, 0, dry_run=True)
        self.assertEqual(self._fields(dry), (1, 0, B, True, B))
        self.assertTrue(os.path.exists(inside))
        apply_ = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(self._fields(apply_), (1, 0, B, True, B))
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(os.path.exists(keep))


class EpisodeTest(PgTestCase):
    """Point 2 : `mounts_unreadable` → `truncated`, un seul épisode `tmp_unscanned`."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        self.pol = menage.Policy(tmp_quota=4 * B)
        registry.upsert(self.db, "codex3", harness="codex", host="pc")

    def _alerts(self, kind: str) -> list[dict]:
        return [a for a in exploitation._housekeeping_alerts([], self.db)
                if a["type"] == kind]

    def test_truncated_after_mounts_unreadable_keeps_its_since(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        many = [os.path.join(tmp, "many", "f%d" % i) for i in range(6)]
        for path in many:
            _write(path)
        with mock.patch.object(platform, "mountinfo", return_value=[]):
            menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        first = self._alerts("tmp_unscanned")
        self.assertEqual(len(first), 1)
        self.assertIn("mount table unreadable", first[0]["detail"])
        time.sleep(1.1)                    # a later `at_ts`, the same episode
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                     actor="t")
        second = self._alerts("tmp_unscanned")
        self.assertEqual(len(second), 1)
        self.assertIn("stopped at the file limit", second[0]["detail"])
        self.assertEqual(second[0]["since"], first[0]["since"])
        self.assertEqual(exploitation.alert_key(second[0]),
                         exploitation.alert_key(first[0]))
        rows = [e for e in report["entries"] if e["action"] == "truncated"]
        self.assertEqual(len(rows), 1)
        # the alert's `since` is rounded to the millisecond by `_alert`
        self.assertEqual(round(rows[0]["data"]["episode_since"], 3),
                         first[0]["since"])
        # the listing is complete again: the episode is lifted, still under its
        # first observation
        for path in many:
            os.unlink(path)
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                 actor="t")
        resolved = [e for e in report["entries"]
                    if (e["kind"], e["action"]) == ("tmp", "resolved")]
        self.assertEqual(len(resolved), 1)
        self.assertIn("truncated", resolved[0]["detail"])
        self.assertEqual(round(resolved[0]["data"]["episode_since"], 3),
                         first[0]["since"])
        self.assertEqual(self._alerts("tmp_unscanned"), [])
