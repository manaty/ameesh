"""#4579 — seule la passe de livraison du notificateur date les épisodes
d'attente d'empreinte (#4561) ; une passe à blanc n'écrit rien.

Les lectures (`ameesh alerts`, API) appellent `exploitation.alerts` sans
`record_footprint` : voir `tests/test_4561_slo_empreinte.py`.
"""
from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_ack, canon, exploitation, notify, notify_gap


class DeliveryPassRecordsTest(unittest.TestCase):
    def run_pass(self, dry_run):
        cfg = SimpleNamespace(host="agents-2", state_dir="/nonexistent",
                              notify={})
        notifier = notify.Notifier(cfg, SimpleNamespace(), notify.empty_state(),
                                   dry_run=dry_run, sender=mock.Mock(),
                                   log=lambda text: None,
                                   only=frozenset({"alerts"}))

        @contextlib.contextmanager
        def cached_canons():
            yield

        fake_store = SimpleNamespace(
            alert_acknowledgements=SimpleNamespace(clock=lambda: 1.0))
        with mock.patch.object(notify.storage, "of", return_value=fake_store), \
                mock.patch.object(notify_gap, "beat"), \
                mock.patch.object(exploitation, "alerts",
                                  return_value=[]) as observed, \
                mock.patch.object(alert_ack, "default_thresholds",
                                  return_value={}), \
                mock.patch.object(alert_ack, "reconcile", return_value=[]), \
                mock.patch.object(notify, "Router", return_value=mock.Mock()), \
                mock.patch.object(canon, "configured_cache", cached_canons):
            notifier.run_pass(object(), thresholds={})
        return observed.call_args.kwargs["record_footprint"]

    def test_la_passe_de_livraison_date_les_episodes(self):
        self.assertTrue(self.run_pass(dry_run=False))

    def test_une_passe_a_blanc_n_ecrit_rien(self):
        self.assertFalse(self.run_pass(dry_run=True))


if __name__ == "__main__":
    unittest.main()
