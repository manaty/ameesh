# SPDX-License-Identifier: AGPL-3.0-only
"""#7687 (C5 of #6002): a lending host counts its live guest offload jobs.

A lending host runs jobs offloaded from other hosts, in the transient
`ameesh-job-<id>.slice` of its offload shell. The `guest` runs interface
publishes their count in the host measure, so the automatic drain and the
`idle-drained` proof know the host's runs instead of staying unknown forever
(#4147, #4148, #5905); a live job is never turned into a proven zero.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import canon as canon_mod, config, host_idle, host_release, resources
from ameesh.storage.postgres import hosts as pg_hosts

from .support import REPO, PgTestCase
from .test_l43_perimetre_hote import HOST as CARD_HOST, _Canons, codes, host

NOW = 100000.0

#: where systemd nests an offload job slice in a user manager
USER_BASE = os.path.join("user.slice", "user-1002.slice", "user@1002.service")


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def job_slice(root: str, name: str, *, populated: bool | None = False,
              age: float = 0.0, base: str = "", now: float = NOW) -> str:
    """A fake job slice; `populated=None` leaves `cgroup.events` unreadable."""
    path = os.path.join(root, base, *resources.GUEST_JOB_PARTS, name)
    os.makedirs(path)
    if populated is None:
        # a directory where the file is expected: open() fails whatever the uid
        os.makedirs(os.path.join(path, "cgroup.events"))
    else:
        write(os.path.join(path, "cgroup.events"),
              "populated %d\nfrozen 0\n" % int(populated))
    stamp = now - age
    os.utime(path, (stamp, stamp))
    return path


class GuestJobsTest(unittest.TestCase):
    """The cgroup scan: live, ambiguous and leftover job slices."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def scan(self, **kw):
        return resources.guest_jobs(self.root, now=NOW, **kw)

    def test_a_tree_without_a_job_slice_is_a_known_zero(self):
        os.makedirs(os.path.join(self.root, "ameesh.slice", "ameesh-job.slice"))
        self.assertEqual(self.scan(), 0)

    def test_a_missing_tree_is_a_known_zero(self):
        self.assertEqual(self.scan(), 0)

    def test_a_populated_slice_is_a_live_job(self):
        job_slice(self.root, "ameesh-job-20261117_054411_1639f9.slice",
                  populated=True)
        self.assertEqual(self.scan(), 1)

    def test_every_live_job_counts(self):
        for job in ("20261117_054411_1639f9", "20261117_054848_13987c"):
            job_slice(self.root, "ameesh-job-%s.slice" % job, populated=True)
        self.assertEqual(self.scan(), 2)

    def test_a_job_slice_in_a_user_manager_counts_too(self):
        job_slice(self.root, "ameesh-job-20261117_054411_1639f9.slice",
                  populated=True, base=USER_BASE)
        self.assertEqual(self.scan(), 1)

    def test_a_young_empty_slice_is_ambiguous(self):
        # the job may be between two phases: never a proven zero
        job_slice(self.root, "ameesh-job-x.slice", age=0)
        self.assertIsNone(self.scan())
        job_slice(self.root, "ameesh-job-y.slice",
                  age=resources.GUEST_JOB_EMPTY_S - 0.5)
        self.assertIsNone(self.scan())

    def test_an_old_empty_slice_is_a_leftover_and_ignored(self):
        job_slice(self.root, "ameesh-job-20261111_004106_20bfd4.slice",
                  age=resources.GUEST_JOB_EMPTY_S)
        self.assertEqual(self.scan(), 0)
        job_slice(self.root, "ameesh-job-20261111_130121_a97235.slice",
                  age=6 * 24 * 3600)
        self.assertEqual(self.scan(), 0)

    def test_an_old_populated_slice_is_still_a_live_job(self):
        # the age rule only decides an *empty* slice: a live job is never
        # ignored, however old its slice looks
        job_slice(self.root, "ameesh-job-old.slice", populated=True,
                  age=6 * 24 * 3600)
        self.assertEqual(self.scan(), 1)

    def test_a_future_mtime_is_an_activity_now_never_an_old_slice(self):
        job_slice(self.root, "ameesh-job-x.slice", age=-60)
        self.assertIsNone(self.scan())

    def test_an_unreadable_slice_is_unknown(self):
        job_slice(self.root, "ameesh-job-x.slice", populated=None)
        self.assertIsNone(self.scan())

    def test_an_unreadable_parent_is_unknown(self):
        # a file where the job-slice directory is expected: the tree is not read
        write(os.path.join(self.root, *resources.GUEST_JOB_PARTS), "")
        self.assertIsNone(self.scan())

    def test_a_positive_count_survives_an_unreadable_extra_slice(self):
        # the drain only needs to know that runs are not zero
        job_slice(self.root, "ameesh-job-live.slice", populated=True)
        job_slice(self.root, "ameesh-job-unknown.slice", populated=None)
        self.assertEqual(self.scan(), 1)

    def test_slices_of_another_kind_are_ignored(self):
        parent = os.path.join(self.root, *resources.GUEST_JOB_PARTS)
        os.makedirs(parent)
        # a scope, an unrelated unit, and a plain file are not job slices
        os.makedirs(os.path.join(parent, "ameesh-job-x.scope"))
        os.makedirs(os.path.join(parent, "ameesh-runner@x.service"))
        write(os.path.join(parent, "ameesh-job-file.slice"), "")
        self.assertEqual(self.scan(), 0)

    def test_the_reading_publishes_the_count(self):
        with mock.patch.object(resources, "guest_jobs", return_value=3) as scan, \
                mock.patch.object(resources, "runner_cgroups", return_value=[]):
            out = resources.sample("vm", now=NOW)
        self.assertEqual(out["guest_jobs"], 3)
        scan.assert_called_once_with(now=NOW)


def reading(**extra):
    out = dict(sampled_ts=NOW, turns_in_progress=0, cpu_count=8,
               mem_total_bytes=16000, mem_available_bytes=12000, app_cpu=0.25,
               cgroup={'container': False, 'limits_readable': True})
    out.update(extra)
    return out


def footprint(guest_jobs="absent", *, sampled_ts=NOW, runs_declared=None):
    """An idle burst footprint, as `host_idle.project` builds it."""
    current = reading()
    if guest_jobs != "absent":
        current["guest_jobs"] = guest_jobs
    if sampled_ts != NOW:
        current["sampled_ts"] = sampled_ts
    agent = dict(name='worker', host='vm', status='idle', lease_live=True,
                 session_lot_state=None, actionable_lots=0, unread=0,
                 passive_unread=0, status_since_ts=7000, last_turn_ts=7000)
    history = [dict(current, sampled_ts=t) for t in range(7600, int(NOW) + 1, 60)]
    if sampled_ts != NOW:
        history = []
    return host_idle.project(current, history, [agent], [], now=NOW,
                             runs_declared=runs_declared)


def blockers(out):
    control = {"host": "vm", "requested_ts": NOW - 600}
    cfg = config.Config(resource_interval=60, host_release_settle=1,
                        host_release_drain_bound=3600)
    out = dict(out, baseline_guard=False)
    return (host_release.auto_drain_blocker(out, None, hook="/hook"),
            host_release.release_blocker(control, [], reading(sampled_ts=NOW - 10),
                                         footprint=out, cfg=cfg, now=NOW))


class ProjectionTest(unittest.TestCase):
    """`runs` is an observation: only a fresh, typed count is a zero."""

    def test_a_fresh_zero_is_a_known_zero(self):
        out = footprint(0, runs_declared=("guest", "Host card"))
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(out['runs'], 0)
        self.assertEqual(host_release.runs_evidence(out),
                         (True, 0, "runs_interface: guest (Host card)"))
        self.assertEqual(blockers(out), (None, None))

    def test_live_jobs_block_the_drain_and_the_release(self):
        out = footprint(2, runs_declared=("guest", "Host card"))
        self.assertEqual(out['runs'], 2)
        drain, release = blockers(out)
        self.assertTrue(drain.startswith("2 declared run(s)"), drain)
        self.assertTrue(release.startswith("2 declared run(s)"), release)

    def test_a_missing_count_stays_unknown(self):
        out = footprint("absent", runs_declared=("guest", "Host card"))
        self.assertIsNone(out['runs'])
        self.assertIn("aucune mesure fraîche", out['runs_source'])
        drain, release = blockers(out)
        self.assertTrue(drain.startswith("declared runs unknown"), drain)
        self.assertTrue(release.startswith("declared runs unknown"), release)

    def test_a_stale_reading_stays_unknown(self):
        out = footprint(0, sampled_ts=NOW - 3600,
                        runs_declared=("guest", "Host card"))
        self.assertIsNone(out['runs'])
        self.assertIn("aucune mesure fraîche", out['runs_source'])

    def test_an_ill_typed_or_negative_count_stays_unknown(self):
        for count in (True, False, -1, 1.0, "0", [0]):
            with self.subTest(count=count):
                out = footprint(count, runs_declared=("guest", "Host card"))
                self.assertIsNone(out['runs'])

    def test_none_ignores_the_guest_measure(self):
        # a `none` card is refused on a lending host by the canon; when one
        # reaches the projection, it keeps its own declared zero (the
        # `runs_interface` precedence never downgrades a `guest` card)
        out = footprint(3, runs_declared=("none", "Host card"))
        self.assertEqual(out['runs'], 0)

    def test_an_undeclared_host_keeps_the_placeholder_source(self):
        out = footprint(0, runs_declared=None)
        self.assertFalse(host_release.runs_evidence(out)[0])
        self.assertIn("aucune interface", out['runs_source'])

    def test_a_lending_host_without_the_interface_stays_unknown(self):
        out = footprint(0, runs_declared=(host_idle.RUNS_LENDING, "Host card"))
        self.assertFalse(host_release.runs_evidence(out)[0])
        self.assertIn("travaux déportés non comptés", out['runs_source'])


def canon_of(*values, title="vm"):
    return NS(hosts=[NS(title=title, policy=NS(runs_interface=v)) for v in values])


class InterfaceTest(unittest.TestCase):
    """Which interface a host declares, and what may not downgrade it."""

    def declared(self, values=(), configured=None):
        cfg = config.Config(runs_interfaces={CARD_HOST: configured}
                            if configured else {})
        return host_idle.runs_interface(cfg, [canon_of(*values, title=CARD_HOST)],
                                        CARD_HOST)

    def test_a_guest_card_declares_the_count(self):
        self.assertEqual(self.declared(("guest",)),
                         (host_idle.RUNS_GUEST, "Host card"))

    def test_the_configuration_adds_guest(self):
        self.assertEqual(self.declared((), "guest"),
                         (host_idle.RUNS_GUEST, "configuration"))
        self.assertEqual(self.declared(("none",), "guest"),
                         (host_idle.RUNS_GUEST, "configuration"))

    def test_a_guest_card_is_never_downgraded_to_none(self):
        self.assertEqual(self.declared(("guest",), "none"),
                         (host_idle.RUNS_GUEST, "Host card"))

    def test_a_guest_card_beside_another_one_declares_nothing(self):
        for other in ("none", host_idle.RUNS_LENDING):
            with self.subTest(other=other):
                self.assertEqual(self.declared(("guest", other)), (None, None))

    def test_the_configuration_resolves_a_lending_card_to_guest_or_not(self):
        self.assertEqual(self.declared((host_idle.RUNS_LENDING,), "guest"),
                         (host_idle.RUNS_GUEST, "configuration"))
        self.assertEqual(self.declared((host_idle.RUNS_LENDING,)),
                         (host_idle.RUNS_LENDING, "Host card"))
        self.assertEqual(self.declared((host_idle.RUNS_LENDING,), "none"),
                         (host_idle.RUNS_LENDING, "Host card"))

    def test_an_invalid_card_refuses_even_beside_a_guest_one(self):
        self.assertEqual(self.declared(("guest", host_idle.RUNS_INVALID)),
                         (None, None))
        self.assertEqual(self.declared((host_idle.RUNS_INVALID,), "guest"),
                         (None, None))


class CanonTest(_Canons, unittest.TestCase):
    """The Host card: `guest` is the only counting interface of a lender."""

    def load(self, value: str | None, *, lending=True, name="t"):
        extra = ("  runs_interface: %s\n" % value) if value else ""
        if lending:
            extra += ("  lend:\n    cpu_max: 2\n    memory_max: 9GiB\n"
                      "    disk_max: 40GiB\n")
        text = host("bob", harnesses=("codex",), resources_yaml=extra)
        return canon_mod.load(self.acme(name=name, host_text=text), default=False)

    def check(self, value, *, lending=True, name="t"):
        loaded = self.load(value, lending=lending, name=name)
        found = canon_mod.validate(loaded)
        return (loaded.host(CARD_HOST).policy.runs_interface,
                "host-policy-invalid" in codes(found, canon_mod.ERROR),
                any("lending host" in f.message for f in found))

    def test_guest_is_accepted_on_a_lending_card(self):
        self.assertEqual(self.check("guest", name="g1"), ("guest", False, False))
        self.assertIsNotNone(self.load("guest", name="g2").host(CARD_HOST).policy.lend)

    def test_none_is_refused_on_a_lending_card_and_kept_lending(self):
        interface, error, lending_message = self.check("none")
        self.assertEqual(interface, host_idle.RUNS_LENDING)
        self.assertTrue(error)
        self.assertTrue(lending_message)

    def test_a_lending_card_without_the_key_stays_lending(self):
        self.assertEqual(self.check(None), (host_idle.RUNS_LENDING, False, False))

    def test_an_invalid_value_is_kept_invalid(self):
        self.assertEqual(self.check("journal"),
                         (host_idle.RUNS_INVALID, True, False))

    def test_an_unknown_value_is_refused_on_a_plain_host_too(self):
        self.assertEqual(self.check("journal", lending=False),
                         (host_idle.RUNS_INVALID, True, False))


class StorageTest(PgTestCase):
    """The count travels with the reading, and the supervisor may read it."""

    def test_guest_jobs_roundtrips_with_the_reading(self):
        store = pg_hosts.Hosts(self.db)
        store.record({"host": "vm-guest", "guest_jobs": 2})
        self.assertEqual(store.latest("vm-guest")["guest_jobs"], 2)
        store.record({"host": "vm-guest"})
        self.assertIsNone(store.latest("vm-guest")["guest_jobs"])

    def test_the_supervisor_contract_includes_guest_jobs(self):
        with open(os.path.join(REPO, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read())
        block = re.search(r"\('host_resources', ARRAY\[(.*?)\]\)", sql, re.S).group(1)
        self.assertIn("guest_jobs", set(re.findall(r"'(\w+)'", block)))

    def test_the_migration_is_additive(self):
        with open(os.path.join(REPO, "src", "ameesh", "migrations",
                               "0589_guest_jobs.sql"), encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read()).lower()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith(
            "alter table host_resources add column if not exists guest_jobs"), statements)


if __name__ == "__main__":
    unittest.main()
