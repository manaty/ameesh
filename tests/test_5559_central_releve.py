# SPDX-License-Identifier: AGPL-3.0-only
"""#5559 : le serveur du mesh (manaty-mesh-1, `max_agents: 0`) ne publiait
aucun relevé : `host_disk_soon_full` (#2022) était aveugle sur le disque de la
base (#5543, vu à 830 Mo libres). Témoins : le minuteur `ameesh-releve-hote`
est posé par l'installation et la mise à jour du serveur ; `--publish` ne
double pas un exécuteur qui publie déjà ; le silence de l'échantillonneur
`publish@` d'un hôte à `max_agents: 0` est une alerte `host_unsampled`, alors
que le retrait d'un hôte sans échantillonneur reste muet (#2341).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import time
import unittest
from unittest import mock

from ameesh import config as config_mod, mesh_cli, resources, storage
from tests import test_2341_host_unsampled as t2341

DAY, HOST, NOW = t2341.DAY, t2341.HOST, t2341.NOW

ROOT = pathlib.Path(__file__).resolve().parent.parent


class PublisherSilenceTest(unittest.TestCase):
    alerts = t2341.HostUnsampledTest.alerts

    def test_a_silent_publisher_of_a_retired_host_alerts(self):
        found = self.alerts({HOST: {"sampled_ts": NOW - DAY,
                                    "runner_id": "publish@%s" % HOST}}, max_agents=0)
        self.assertEqual([a["type"] for a in found], ["host_unsampled"])
        self.assertIn("ameesh-releve-hote.timer", found[0]["detail"])
        self.assertNotIn("declare in its Host card", found[0]["detail"])

    def test_a_retired_host_left_by_its_runner_stays_silent(self):
        found = self.alerts({HOST: {"sampled_ts": NOW - DAY,
                                    "runner_id": "runner-deepseek1@%s" % HOST}},
                            max_agents=0)
        self.assertEqual(found, [])

    def test_a_fresh_publisher_reading_feeds_the_disk_warning(self):
        reading = {"sampled_ts": NOW - 30, "runner_id": "publish@%s" % HOST,
                   "disks": [{"path": "/", "total_bytes": 40 * 1024 ** 3,
                              "free_bytes": 830 * 1024 ** 2}],
                   "disk_free_bytes": 830 * 1024 ** 2,
                   "disk_total_bytes": 40 * 1024 ** 3}
        types = [a["type"] for a in self.alerts({HOST: reading}, max_agents=0)]
        self.assertIn("host_disk_soon_full", types)


class PublishGuardTest(unittest.TestCase):
    def args(self):
        return argparse.Namespace(host=None, json=False, hold=False, release=False,
                                  publish=True, until=None, reason=None)

    def run_publish(self, latest):
        cfg = config_mod.Config(host=HOST)
        store = mock.MagicMock()
        store.hosts.current.return_value = latest
        out = io.StringIO()
        with mock.patch.object(mesh_cli, "_open", return_value=mock.Mock()), \
                mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(resources, "collect",
                                  return_value={"disk_free_bytes": 1}) as collect, \
                contextlib.redirect_stdout(out):
            self.assertEqual(mesh_cli.cmd_hosts(cfg, self.args()), 0)
        return collect.called, out.getvalue()

    def test_a_runner_publishing_this_host_is_not_doubled(self):
        called, text = self.run_publish([{"host": HOST, "runner_id": "runner-a@x",
                                          "sampled_ts": time.time() - 30}])
        self.assertFalse(called)
        self.assertIn("runner runner-a@x already publishes", text)

    def test_publishes_after_its_own_reading_a_stale_runner_or_none(self):
        for latest in ([{"host": HOST, "runner_id": "publish@%s" % HOST,
                         "sampled_ts": time.time() - 30}],
                       [{"host": HOST, "runner_id": "runner-a@x",
                         "sampled_ts": time.time() - DAY}],
                       []):
            called, _ = self.run_publish(latest)
            self.assertTrue(called, latest)


class ServerUnitsTest(unittest.TestCase):
    def test_the_server_install_and_update_enable_the_timer(self):
        service = (ROOT / "deploy/serveur/ameesh-releve-hote.service").read_text()
        self.assertIn("User=ameesh", service)
        self.assertIn("ExecStart=/opt/ameesh/venv/bin/ameesh hosts --publish", service)
        self.assertIn("OnUnitActiveSec=5min",
                      (ROOT / "deploy/serveur/ameesh-releve-hote.timer").read_text())
        for script in ("deploy/serveur/installer-serveur.sh", "deploy/mise-a-jour/vm.sh"):
            self.assertIn("systemctl enable --now ameesh-releve-hote.timer",
                          (ROOT / script).read_text(), script)


if __name__ == "__main__":
    unittest.main()
