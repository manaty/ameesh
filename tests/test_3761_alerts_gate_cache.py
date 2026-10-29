# SPDX-License-Identifier: AGPL-3.0-only
"""#3761 — la passe d'alertes ne relit pas la porte des avis à chaque passage.

Le profil (5 min 53, 306 s de CPU user) montrait `_review_gate_open` à 57 %
des échantillons et `_carried_from_previous` à 46 % : la survie du contenu des
correctifs était re-prouvée par `git grep` pour chaque lot, à chaque passe.

Témoins :
* `review_gate_for_item` rend le verdict mémorisé pour une signature
  identique et fraîche, sans appeler `review_gate` ;
* une signature différente (jalon, cible, canon) ou une entrée périmée relit ;
* une lecture illisible ne se mémorise pas ;
* `Notifier.run_pass` remet le cache à `exploitation.alerts` et lit les canons
  une fois dans une fenêtre `configured_cache` ;
* l'état de livraison porte le cache (rétrocompatible avec un ancien document).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_ack, canon, exploitation, lot_class, merge_queue, \
    notify, plan_git, storage

HEAD = "f" * 40
BASE = "b" * 40


def _marks(extra=()):
    return [{"id": 10, "kind": "frozen", "sha": HEAD, "actor": "author",
             "at_ts": 1.0}] + list(extra)


class GateCacheTest(unittest.TestCase):
    """Le verdict de porte est mémorisé par signature, pas par lot seul."""

    def setUp(self):
        self.item = {"id": 3761, "app": "ameesh", "assignee": "author",
                     "type": "bug"}
        self.cfg = SimpleNamespace(canon="/canon", canon_ref="")
        self.db = object()
        self.klass = {"class": "normal", "files": [{"path": "src/ameesh/x.py"}]}

    def _call(self, cache, *, marks=None, now=1000.0, target="origin/main",
              base_sha=BASE):
        with mock.patch.object(plan_git, "latest_freeze",
                               return_value={"sha": HEAD}), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", HEAD)), \
                mock.patch.object(lot_class, "default_base",
                                  return_value=target), \
                mock.patch.object(merge_queue, "_class_base",
                                  return_value=target), \
                mock.patch.object(plan_git, "_commit",
                                  return_value=base_sha), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value=self.klass), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}) as gate:
            out = merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item, marks or _marks(),
                gate_cache=cache, now=now)
        return out, gate

    def test_a_fresh_signature_returns_without_reading_git(self):
        cache: dict = {}
        first, gate = self._call(cache)
        self.assertTrue(first["ok"])
        self.assertFalse(first.get("cached"))
        self.assertEqual(gate.call_count, 1)
        second, gate = self._call(cache)
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("cached"))
        self.assertEqual(gate.call_count, 0, "la porte ne doit plus être relue")

    def test_a_new_mark_reads_again(self):
        cache: dict = {}
        with mock.patch.object(plan_git, "latest_freeze",
                               return_value={"sha": HEAD}), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", HEAD)), \
                mock.patch.object(lot_class, "default_base",
                                  return_value="origin/main"), \
                mock.patch.object(merge_queue, "_class_base",
                                  return_value="origin/main"), \
                mock.patch.object(plan_git, "_commit", return_value=BASE), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value=self.klass), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}) as gate:
            merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item, _marks(), gate_cache=cache,
                now=1000.0)
            merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item,
                _marks([{"id": 11, "kind": "verdict", "sha": HEAD,
                         "actor": "rv", "verdict": "ok", "at_ts": 2.0}]),
                gate_cache=cache, now=1000.0)
            self.assertEqual(gate.call_count, 2, "un verdict neuf relit")

    def test_a_moved_base_reads_again(self):
        cache: dict = {}
        with mock.patch.object(plan_git, "latest_freeze",
                               return_value={"sha": HEAD}), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", HEAD)), \
                mock.patch.object(lot_class, "default_base",
                                  return_value="origin/main"), \
                mock.patch.object(merge_queue, "_class_base",
                                  return_value="origin/main"), \
                mock.patch.object(plan_git, "_commit",
                                  side_effect=[BASE, "c" * 40]), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value=self.klass), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}) as gate:
            merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item, _marks(), gate_cache=cache,
                now=1000.0)
            merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item, _marks(), gate_cache=cache,
                now=1000.0)
            self.assertEqual(gate.call_count, 2, "la base de la cible a bougé")

    def test_an_expired_entry_is_read_again(self):
        cache: dict = {}
        first, gate = self._call(cache)
        self.assertTrue(first["ok"])
        self.assertEqual(gate.call_count, 1)
        cache["3761"]["at"] = 1000.0 - merge_queue.GATE_CACHE_TTL_S - 1.0
        fresh, gate = self._call(cache)
        self.assertTrue(fresh["ok"])
        self.assertEqual(gate.call_count, 1, "nouveau mock")
        # L'entrée a été remplacée : elle est de nouveau fraîche.
        self.assertEqual(cache["3761"]["at"], 1000.0)

    def test_an_inconclusive_read_is_not_cached(self):
        cache: dict = {}
        with mock.patch.object(plan_git, "latest_freeze",
                               return_value={"sha": HEAD}), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", HEAD)), \
                mock.patch.object(lot_class, "default_base",
                                  return_value="origin/main"), \
                mock.patch.object(merge_queue, "_class_base",
                                  return_value="origin/main"), \
                mock.patch.object(plan_git, "_commit", return_value=BASE), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value=self.klass), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value=None):
            out = merge_queue.review_gate_for_item(
                self.db, self.cfg, self.item, _marks(), gate_cache=cache,
                now=1000.0)
        self.assertIsNone(out)
        self.assertEqual(cache, {})

    def test_a_code_change_invalidates_the_signature(self):
        args = (self.cfg, self.item, _marks(), "/repo", HEAD, "origin/main",
                "origin/main", BASE)
        with mock.patch.object(merge_queue, "_gate_code_version",
                               return_value="ameesh 1.0+aaaa"):
            first = merge_queue._gate_cache_signature(*args)
        with mock.patch.object(merge_queue, "_gate_code_version",
                               return_value="ameesh 1.0+bbbb"):
            second = merge_queue._gate_cache_signature(*args)
        self.assertNotEqual(first, second)


class GateCodeFingerprintTest(unittest.TestCase):
    """#4526 : le code de la porte est celui des modules déplacés, pas l'alias."""

    def test_the_moved_plan_git_module_is_part_of_the_fingerprint(self):
        self.assertIn("modules/dev/plan_git.py", merge_queue._GATE_CODE_FILES)
        root = tempfile.mkdtemp(prefix="ameesh-gate-code-")
        self.addCleanup(shutil.rmtree, root, True)
        for name in merge_queue._GATE_CODE_FILES:
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("v1")
        before = merge_queue._gate_code_digest(root)
        with open(os.path.join(root, "modules/dev/plan_git.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("v2")
        self.assertNotEqual(before, merge_queue._gate_code_digest(root),
                            "modifier le module déplacé doit changer l'empreinte")


class GateCacheWiringTest(unittest.TestCase):
    """Le cache traverse la passe d'alertes jusqu'à l'état de livraison."""

    def test_review_gate_open_forwards_the_cache(self):
        cache = {"3761": {"key": "k", "ok": True, "at": 1.0}}
        with mock.patch.object(merge_queue, "review_gate_for_item",
                               return_value={"ok": True}) as gate:
            out = exploitation._review_gate_open(
                SimpleNamespace(), object(), {"id": 3761}, _marks(),
                {"verdict_required": True}, gate_cache=cache)
        self.assertTrue(out)
        self.assertIs(gate.call_args.kwargs["gate_cache"], cache)

    def test_run_pass_hands_the_cache_and_reads_canons_once(self):
        state = notify.empty_state()
        state["gates"] = {"3761": {"key": "k", "ok": True, "at": 1.0}}
        cfg = SimpleNamespace(host="agents-2", state_dir="/nonexistent",
                              notify={})
        ncfg = SimpleNamespace()
        notifier = notify.Notifier(cfg, ncfg, state, dry_run=True,
                                   sender=mock.Mock(), log=lambda text: None,
                                   only=frozenset({"alerts"}))
        entered = []

        @contextlib.contextmanager
        def cached_canons():
            entered.append(True)
            yield

        fake_store = SimpleNamespace(
            alert_acknowledgements=SimpleNamespace(clock=lambda: 1.0))
        with mock.patch.object(notify.storage, "of",
                               return_value=fake_store), \
                mock.patch.object(exploitation, "alerts",
                                  return_value=[]) as observed, \
                mock.patch.object(alert_ack, "default_thresholds",
                                  return_value={}), \
                mock.patch.object(alert_ack, "reconcile", return_value=[]), \
                mock.patch.object(notify, "Router", return_value=mock.Mock()), \
                mock.patch.object(canon, "configured_cache", cached_canons):
            out = notifier.run_pass(object(), thresholds={})
        self.assertEqual(out, [])
        self.assertTrue(entered, "les canons doivent être lus dans la fenêtre")
        self.assertIs(observed.call_args.kwargs["gate_cache"], state["gates"])


class StateSchemaTest(unittest.TestCase):
    """Un ancien document reste lisible ; le cache est un champ de l'état."""

    def test_an_old_document_without_gates_loads_with_an_empty_cache(self):
        old = {"schema": notify.STATE_SCHEMA, "active": {}, "resolved": {},
               "rate": {}, "summary": {}, "last_summary": {}, "briefs": {},
               "notified": {}, "fallback": {}}
        state = notify._state_from_document(old)
        self.assertEqual(state["gates"], {})

    def test_the_gate_cache_round_trips(self):
        state = notify.empty_state()
        state["gates"]["3761"] = {"key": "k", "ok": True, "at": 1.0}
        restored = notify._state_from_document(dict(state))
        self.assertEqual(restored["gates"], state["gates"])

    def test_the_shared_document_keeps_its_gate_cache(self):
        shared = notify.empty_state()
        shared["gates"] = {"3761": {"key": "k", "ok": True, "at": 1.0}}
        notifier = notify.Notifier(
            SimpleNamespace(host="agents-2", state_dir="/nonexistent",
                            notify={}),
            SimpleNamespace(), notify.empty_state(), dry_run=True,
            sender=mock.Mock(), log=lambda text: None)
        with mock.patch.object(notify, "load_shared_state",
                               return_value=shared):
            lease, deliver = notify.enter_shared_state(
                object(), notifier, notifier.cfg, dry_run=True)
        self.assertIsNone(lease)
        self.assertTrue(deliver)
        self.assertIs(notifier.state["gates"], shared["gates"])


if __name__ == "__main__":
    unittest.main()
