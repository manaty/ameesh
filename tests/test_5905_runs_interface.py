# SPDX-License-Identifier: AGPL-3.0-only
"""#5905 (C2 of #5900): a host declares its runs explicitly (`runs_interface`).

`none` makes A's zero a known count, so the automatic drain and the
idle-drained proof of #4148 no longer refuse "declared runs unknown"; an
undeclared host keeps the named refusal, never a success by default.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from ameesh import canon as canon_mod, config, host_idle, host_release

from .test_4150_host_idle import NOW, HostIdleTest
from .test_l43_perimetre_hote import HOST as CARD_HOST, _Canons, codes, host


def canon_of(*values, title="vm"):
    return NS(hosts=[NS(title=title, policy=NS(runs_interface=v)) for v in values])


class DeclarationTest(unittest.TestCase):
    def test_card_or_configuration_declares_none(self):
        cfg = config.Config()
        self.assertEqual(host_idle.runs_interface(cfg, [canon_of("none")], "vm"),
                         ("none", "Host card"))
        cfg = config.Config(runs_interfaces={"vm": "none"})
        self.assertEqual(host_idle.runs_interface(cfg, [canon_of(None)], "vm"),
                         ("none", "configuration"))

    def test_absent_unknown_or_disagreeing_cards_declare_nothing(self):
        cfg = config.Config()
        for canons in ([], [canon_of(None)], [canon_of("none", title="other")],
                       [canon_of("journal")], [canon_of("none", "journal")]):
            with self.subTest(canons=canons):
                self.assertEqual(host_idle.runs_interface(cfg, canons, "vm"),
                                 (None, None))

    def load(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"runs_interfaces": value}, fh)
            return config.load({"AMEESH_CONFIG": path}, register_credentials=False)

    def test_configuration_refuses_an_unknown_interface(self):
        for value in ({"vm": "journal"}, {"vm": None}, ["vm"]):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.load(value)
        self.assertEqual(self.load({"vm": "none"}).runs_interfaces, {"vm": "none"})


class ProjectionTest(HostIdleTest):
    def blockers(self, out):
        control = {"host": "vm", "requested_ts": NOW - 600}
        cfg = config.Config(resource_interval=60, host_release_settle=1,
                            host_release_drain_bound=3600)
        reading = {"sampled_ts": NOW - 10, "turns_in_progress": 0}
        out = dict(out, baseline_guard=False)
        return (host_release.auto_drain_blocker(out, None, hook="/hook"),
                host_release.release_blocker(control, [], reading, footprint=out,
                                             cfg=cfg, now=NOW))

    def test_declared_none_is_a_known_zero_for_drain_and_release(self):
        out = self.project(runs_declared=("none", "Host card"))
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(host_release.runs_evidence(out),
                         (True, 0, "runs_interface: none (Host card)"))
        self.assertIn('runs 0 (runs_interface: none (Host card))', host_idle.line(out))
        self.assertEqual(self.blockers(out), (None, None))

    def test_undeclared_keeps_the_named_refusal(self):
        for declared in (None, (None, None), ("journal", "Host card")):
            with self.subTest(declared=declared):
                out = self.project(runs_declared=declared)
                drain, release = self.blockers(out)
                self.assertTrue(drain.startswith("declared runs unknown"), drain)
                self.assertTrue(release.startswith("declared runs unknown"), release)


class CardTest(_Canons, unittest.TestCase):
    def load(self, value: str) -> tuple:
        text = host("bob", harnesses=("codex",),
                    resources_yaml="  runs_interface: %s\n" % value)
        loaded = canon_mod.load(self.acme(host_text=text), default=False)
        return (loaded.host(CARD_HOST).policy.runs_interface,
                "host-policy-invalid" in codes(canon_mod.validate(loaded), canon_mod.ERROR))

    def test_the_card_declares_none(self):
        self.assertEqual(self.load("none"), ("none", False))

    def test_an_unknown_interface_is_an_error_and_stays_undeclared(self):
        self.assertEqual(self.load("journal"), (None, True))


if __name__ == "__main__":
    unittest.main()
