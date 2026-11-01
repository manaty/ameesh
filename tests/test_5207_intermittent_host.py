# SPDX-License-Identifier: AGPL-3.0-only
"""#5207 : un poste qu'on éteint (`policy.intermittent`, canon §14) levait
`host_unsampled` sans fin (pc-smichea, 93 h) et `docker_storage_unavailable`.
Témoin : muet 4 jours, aucune alerte ; au premier relevé frais, tout revient."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon as canon_mod, exploitation, resources, storage

from .test_l43_perimetre_hote import HOST as CARD_HOST, _Canons, codes, host

HOST = "pc-smichea"
NOW = 10_000_000.0
DAY = 86400.0
OFF = {"sampled_ts": NOW - 4 * DAY, "tmp_fstype": "tmpfs", "tmp_used_bytes": 99,
       "tmp_size_bytes": 100}
ON = dict(OFF, sampled_ts=NOW - 30)


def card(**policy):
    return SimpleNamespace(policy=SimpleNamespace(**policy))


def canon_with(**policy):
    return SimpleNamespace(id="c", host=lambda _name: card(**policy))


def alert_types(readings, *, intermittent=True) -> list[str]:
    store = mock.MagicMock()
    store.hosts.current.return_value = [dict(r, host=HOST) for r in readings]
    store.hosts.disk_history.return_value = []
    store.hosts.runners.return_value = []
    store.turn_resources.orphans.return_value = [
        {"turn_id": "t", "agent": "a", "host": HOST, "started_ts": NOW - DAY,
         "status": "orphan", "pgid": 1}]
    store.turn_resources.stale_running.return_value = []
    store.housekeeping.last_measure.return_value = [
        {"host": HOST, "at_ts": NOW - 4 * DAY,
         "data": {"docker_storage": {"sampled_ts": NOW - 4 * DAY}}}]
    store.housekeeping.worktrees.return_value = []
    store.housekeeping.latest_by_path.return_value = []
    canon = canon_with(max_agents=27, intermittent=intermittent)
    with mock.patch.object(storage, "of", return_value=store), \
            mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
            mock.patch.object(exploitation.time, "time", return_value=NOW):
        return [a["type"] for a in exploitation._host_alerts(None, None, now=NOW)]


class IntermittentHostTest(unittest.TestCase):
    def test_switched_off_for_four_days_raises_nothing(self):
        self.assertEqual(alert_types([OFF]), [])

    def test_a_purged_reading_changes_nothing(self):
        self.assertEqual(alert_types([]), [])

    def test_its_alerts_come_back_with_the_first_fresh_reading(self):
        types = alert_types([ON])
        for kind in ("orphan_resource", "tmpfs_full", "docker_storage_unavailable"):
            self.assertIn(kind, types)

    def test_an_ordinary_host_still_alerts(self):
        types = alert_types([OFF], intermittent=False)
        self.assertIn("host_unsampled", types)
        self.assertIn("docker_storage_unavailable", types)

    def test_every_card_must_agree(self):
        agree = canon_with(intermittent=True, max_agents=None)
        other = canon_with(intermittent=False, max_agents=None)
        self.assertTrue(resources.host_limits([agree, agree], HOST)["intermittent"])
        self.assertFalse(resources.host_limits([agree, other], HOST)["intermittent"])
        self.assertFalse(resources.host_limits([], HOST)["intermittent"])


class CardTest(_Canons, unittest.TestCase):
    def load(self, value: str) -> tuple:
        text = host("bob", harnesses=("codex",), resources_yaml="  intermittent: %s\n" % value)
        loaded = canon_mod.load(self.acme(host_text=text), default=False)
        return (loaded.host(CARD_HOST).policy.intermittent,
                "host-policy-invalid" in codes(canon_mod.validate(loaded), canon_mod.ERROR))

    def test_the_card_declares_a_boolean(self):
        self.assertEqual(self.load("true"), (True, False))

    def test_an_unreadable_value_keeps_the_silence_alert(self):
        self.assertEqual(self.load("oui"), (False, True))


if __name__ == "__main__":
    unittest.main()
