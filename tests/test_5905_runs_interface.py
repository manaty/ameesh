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
                       [canon_of("journal")], [canon_of("none", "journal")],
                       [canon_of(host_idle.RUNS_INVALID)],
                       [canon_of("none", host_idle.RUNS_INVALID)]):
            with self.subTest(canons=canons):
                self.assertEqual(host_idle.runs_interface(cfg, canons, "vm"),
                                 (None, None))

    def load(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"runs_interfaces": value}, fh)
            return config.load({"AMEESH_CONFIG": path}, register_credentials=False)

    def test_a_lending_or_invalid_card_wins_over_the_configuration(self):
        # bureau #192975: a lending host (L159b) receives offloaded guest
        # jobs; no configuration `none` may turn them into a proven zero
        cfg = config.Config(runs_interfaces={"vm": "none"})
        self.assertEqual(host_idle.runs_interface(
            cfg, [canon_of(host_idle.RUNS_LENDING)], "vm"),
            (host_idle.RUNS_LENDING, "Host card"))
        self.assertEqual(host_idle.runs_interface(
            cfg, [canon_of("none", host_idle.RUNS_LENDING)], "vm"),
            (host_idle.RUNS_LENDING, "Host card"))
        self.assertEqual(host_idle.runs_interface(
            cfg, [canon_of(host_idle.RUNS_INVALID)], "vm"), (None, None))

    def test_configuration_refuses_an_unknown_interface(self):
        for value in ({"vm": "journal"}, {"vm": None}, ["vm"]):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.load(value)
        self.assertEqual(self.load({"vm": "none"}).runs_interfaces, {"vm": "none"})


NOW = 10000


def project(**kw):
    reading = dict(sampled_ts=NOW, turns_in_progress=0, cpu_count=8,
                   mem_total_bytes=16000, mem_available_bytes=12000, app_cpu=0.25,
                   cgroup={'container': False, 'limits_readable': True})
    agent = dict(name='worker', host='vm', status='idle', lease_live=True,
                 session_lot_state=None, actionable_lots=0, unread=0,
                 passive_unread=0, status_since_ts=7000, last_turn_ts=7000)
    history = [dict(reading, sampled_ts=t) for t in range(7600, NOW + 1, 60)]
    return host_idle.project(reading, history, [agent], [], now=NOW, **kw)


def blockers(out):
    """(auto_drain_blocker, release_blocker) of an idle burst footprint."""
    control = {"host": "vm", "requested_ts": NOW - 600}
    cfg = config.Config(resource_interval=60, host_release_settle=1,
                        host_release_drain_bound=3600)
    reading = {"sampled_ts": NOW - 10, "turns_in_progress": 0}
    out = dict(out, baseline_guard=False)
    return (host_release.auto_drain_blocker(out, None, hook="/hook"),
            host_release.release_blocker(control, [], reading, footprint=out,
                                         cfg=cfg, now=NOW))


class ProjectionTest(unittest.TestCase):
    def test_declared_none_is_a_known_zero_for_drain_and_release(self):
        out = project(runs_declared=("none", "Host card"))
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(host_release.runs_evidence(out),
                         (True, 0, "runs_interface: none (Host card)"))
        self.assertIn('runs 0 (runs_interface: none (Host card))', host_idle.line(out))
        self.assertEqual(blockers(out), (None, None))

    def test_lending_host_names_its_uncounted_guest_jobs(self):
        out = project(runs_declared=(host_idle.RUNS_LENDING, "Host card"))
        known, _runs, source = host_release.runs_evidence(out)
        self.assertFalse(known)
        self.assertIn("travaux déportés non comptés", source)
        drain, release = blockers(out)
        self.assertIn("travaux déportés", drain)
        self.assertTrue(release.startswith("declared runs unknown"), release)

    def test_undeclared_keeps_the_named_refusal(self):
        for declared in (None, (None, None), ("journal", "Host card"),
                         (host_idle.RUNS_INVALID, "Host card")):
            with self.subTest(declared=declared):
                out = project(runs_declared=declared)
                drain, release = blockers(out)
                self.assertTrue(drain.startswith("declared runs unknown"), drain)
                self.assertTrue(release.startswith("declared runs unknown"), release)


class CardTest(_Canons, unittest.TestCase):
    def canon(self, value: str, name: str = "t"):
        text = host("bob", harnesses=("codex",),
                    resources_yaml="  runs_interface: %s\n" % value)
        return canon_mod.load(self.acme(name=name, host_text=text), default=False)

    def load(self, value: str) -> tuple:
        loaded = self.canon(value)
        return (loaded.host(CARD_HOST).policy.runs_interface,
                "host-policy-invalid" in codes(canon_mod.validate(loaded), canon_mod.ERROR))

    def test_the_card_declares_none(self):
        self.assertEqual(self.load("none"), ("none", False))

    def test_an_unknown_interface_is_an_error_kept_invalid(self):
        self.assertEqual(self.load("journal"), (host_idle.RUNS_INVALID, True))

    def lending(self, value: str | None, name: str):
        extra = ("  runs_interface: %s\n" % value) if value else ""
        text = host("bob", harnesses=("codex",), resources_yaml=extra +
                    "  lend:\n    cpu_max: 2\n    memory_max: 9GiB\n"
                    "    disk_max: 40GiB\n")
        return canon_mod.load(self.acme(name=name, host_text=text), default=False)

    def test_a_lending_card_never_declares_none(self):
        for value, error in ((None, False), ("none", True)):
            with self.subTest(value=value):
                loaded = self.lending(value, "l%s" % bool(value))
                self.assertIsNotNone(loaded.host(CARD_HOST).policy.lend)
                self.assertEqual(loaded.host(CARD_HOST).policy.runs_interface,
                                 host_idle.RUNS_LENDING)
                self.assertEqual(any("lending host" in f.message
                                     for f in canon_mod.validate(loaded)), error)
                cfg = config.Config(runs_interfaces={CARD_HOST: "none"})
                declared = host_idle.runs_interface(cfg, [loaded], CARD_HOST)
                out = project(runs_declared=declared)
                drain, release = blockers(out)
                self.assertTrue(drain.startswith("declared runs unknown"), drain)
                self.assertTrue(release.startswith("declared runs unknown"), release)

    def test_a_valid_card_beside_an_invalid_one_keeps_both_gates_closed(self):
        # cx3's B on 2f8523f13cbf: two canons really loaded, one `none`, the
        # other unreadable; the invalid card must not vanish behind the valid.
        canons = [self.canon("none", "t"), self.canon("journal", "u")]
        declared = host_idle.runs_interface(config.Config(), canons, CARD_HOST)
        self.assertEqual(declared, (None, None))
        out = project(runs_declared=declared)
        drain, release = blockers(out)
        self.assertTrue(drain.startswith("declared runs unknown"), drain)
        self.assertTrue(release.startswith("declared runs unknown"), release)
        both_valid = [self.canon("none", "v"), self.canon("none", "w")]
        self.assertEqual(host_idle.runs_interface(config.Config(), both_valid, CARD_HOST),
                         ("none", "Host card"))


if __name__ == "__main__":
    unittest.main()
