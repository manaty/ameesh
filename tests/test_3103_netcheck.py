# SPDX-License-Identifier: AGPL-3.0-only
"""#3103 : `ameesh doctor` signale un pont Docker qui chevauche une route de
l'hôte (incident #3092, Wi-Fi d'hôtel en 192.168.x capté par un réseau Docker
en 192.168.0.0/20). Routes et réseaux simulés : ni `ip`, ni Docker."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from . import support  # isole l'environnement agent de l'hôte
from ameesh import netcheck

WIFI = [
    {"dst": "default", "gateway": "192.168.1.1", "dev": "wlp0s20f3", "protocol": "dhcp"},
    {"dst": "192.168.1.0/24", "dev": "wlp0s20f3", "protocol": "kernel", "scope": "link"},
    {"dst": "172.17.0.0/16", "dev": "docker0", "protocol": "kernel", "scope": "link"},
    {"dst": "192.168.0.0/20", "dev": "br-aaaaaaaaaaaa", "protocol": "kernel",
     "flags": ["linkdown"]},
]
INSPECT = [
    {"Name": "bridge", "Id": "0bd535858234ffff", "Driver": "bridge",
     "Options": {"com.docker.network.bridge.name": "docker0"},
     "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}},
    {"Name": "supabase_network_x", "Id": "aaaaaaaaaaaa0123", "Driver": "bridge",
     "Options": {}, "IPAM": {"Config": [{"Subnet": "192.168.0.0/20"}]}},
    {"Name": "host", "Id": "94655fc8930f", "Driver": "host", "IPAM": {"Config": None}},
    {"Name": "lan", "Id": "cccccccccccc", "Driver": "macvlan",
     "IPAM": {"Config": [{"Subnet": "192.168.1.0/24"}]}},
]


def fake_run(routes, inspect, *, ip_fails=False, v6_fails=False):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out, code = "", 0
        if argv[1:3] == ["network", "ls"]:
            out = "\n".join(doc["Id"][:12] for doc in inspect)
        elif argv[1:3] == ["network", "inspect"]:
            out = json.dumps(inspect)
        elif "-4" in argv:
            out, code = json.dumps(routes), (1 if ip_fails else 0)
        elif "-6" in argv:
            out, code = "[]", (1 if v6_fails else 0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
    run.calls = calls
    return run


class NetcheckTest(unittest.TestCase):
    def setUp(self):
        self.cfg = mock.Mock(container_runtime="auto", container_timeout=5.0)
        for target, value in ((netcheck.containers, "runtime_binary"),
                              (netcheck.platform, "is_linux")):
            patcher = mock.patch.object(target, value, return_value={
                "runtime_binary": "/usr/bin/docker", "is_linux": True}[value])
            patcher.start()
            self.addCleanup(patcher.stop)
        which = mock.patch.object(netcheck.shutil, "which", return_value="/usr/bin/ip")
        which.start()
        self.addCleanup(which.stop)

    def test_hotel_wifi_overlap_is_reported(self):
        lines = netcheck.doctor_lines(self.cfg, run=fake_run(WIFI, INSPECT))
        text = "\n".join(lines)
        self.assertIn("WARNING — docker network supabase_network_x (192.168.0.0/20, "
                      "br-aaaaaaaaaaaa) overlaps host route 192.168.1.0/24 dev wlp0s20f3",
                      text)
        self.assertIn("default via 192.168.1.1 dev wlp0s20f3", text)
        self.assertIn("192.168.0.0/16", text)  # plage exposée
        self.assertIn("default-address-pools", lines[-1])
        # ni le pont lui-même, ni docker0, ni le macvlan (chevauche par conception)
        self.assertNotIn("dev br-", text)
        self.assertNotIn("lan (", text)

    def test_clean_host_says_so(self):
        routes = [r for r in WIFI if r["dev"] != "br-aaaaaaaaaaaa"]
        inspect = [INSPECT[0], dict(INSPECT[1], IPAM={"Config": [{"Subnet": "10.213.0.0/24"}]})]
        self.assertEqual(netcheck.doctor_lines(self.cfg, run=fake_run(routes, inspect)),
                         ["network    : 2 docker bridge network(s), none overlaps a host route"])

    def test_exposed_range_without_overlap_warns(self):
        routes = [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0"},
                  {"dst": "10.0.0.0/24", "dev": "eth0"}]
        lines = netcheck.doctor_lines(self.cfg, run=fake_run(routes, INSPECT))
        self.assertNotIn("WARNING", "\n".join(lines))
        self.assertIn("supabase_network_x (192.168.0.0/20)", lines[0])

    def test_unreadable_sources_are_said_not_guessed(self):
        lines = netcheck.doctor_lines(self.cfg, run=fake_run(WIFI, INSPECT, ip_fails=True))
        self.assertEqual(lines, ["network    : host routes not read (`ip -j route`): "
                                 "overlap not checked"])
        # IPv6 coupé : l'IPv4 suffit
        lines = netcheck.doctor_lines(self.cfg, run=fake_run(WIFI, INSPECT, v6_fails=True))
        self.assertIn("WARNING", lines[0])

    def test_no_runtime_or_not_linux_is_silent(self):
        with mock.patch.object(netcheck.containers, "runtime_binary", return_value=None):
            self.assertEqual(netcheck.doctor_lines(self.cfg, run=fake_run(WIFI, INSPECT)), [])
        with mock.patch.object(netcheck.platform, "is_linux", return_value=False):
            self.assertEqual(netcheck.doctor_lines(self.cfg, run=fake_run(WIFI, INSPECT)), [])

    def test_podman_shape(self):
        doc = [{"name": "podman", "id": "dddd", "driver": "bridge",
                "network_interface": "podman0", "subnets": [{"subnet": "192.168.1.0/25"}]}]
        networks = netcheck.parse_networks(json.dumps(doc))
        self.assertEqual(networks, [{"name": "podman", "bridge": "podman0",
                                     "subnets": ["192.168.1.0/25"]}])
        routes = [r for r in WIFI if r["dev"] != "br-aaaaaaaaaaaa"]
        self.assertEqual([c["dev"] for c in netcheck.conflicts(routes, networks)],
                         ["wlp0s20f3", "wlp0s20f3"])

    def test_read_only_commands(self):
        run = fake_run(WIFI, INSPECT)
        netcheck.doctor_lines(self.cfg, run=run)
        for argv in run.calls:
            self.assertTrue(argv[1:3] in (["network", "ls"], ["network", "inspect"])
                            or argv[-3:] == ["show", "table", "main"], argv)


if __name__ == "__main__":
    unittest.main()
