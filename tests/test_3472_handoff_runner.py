# SPDX-License-Identifier: AGPL-3.0-only
"""H5: the runner binds the pinned handoff factory before either use.

The factory source is fixed; an absent source or a refused enrollment leaves
the same named prerequisites in ``readiness`` and ``advance_host``; a bound
simulated adapter drives the runner's drain, return and loss passes, and the
elected roles stay with their election.
"""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import handoff_factory, host_lifecycle, host_preflight, registry, resources, storage
from ameesh.runner import Runner, AgentWorker

from . import test_l232e_drain_handoffs as drain_fixtures
from .test_l232e_loss_recovery import RecoveryAdapter
from .support import PgTestCase

REPO = Path(__file__).resolve().parents[1]


class FactorySourceTest(unittest.TestCase):
    """The loader pins this tree's factory; no message or document picks it."""

    def test_source_is_derived_from_this_tree(self):
        self.assertEqual(handoff_factory.source_path(),
                         REPO / "deploy/mise-a-jour/fabrique-handoff.py")

    def test_wheel_source_uses_its_interpreter_prefix(self):
        with mock.patch.object(handoff_factory, "__file__",
                               "/opt/venv/lib/python3.12/site-packages/ameesh/handoff_factory.py"), \
                mock.patch.object(handoff_factory.sys, "prefix", "/opt/venv"):
            self.assertEqual(handoff_factory.source_path(),
                             Path("/opt/venv/share/ameesh/mise-a-jour/fabrique-handoff.py"))

    def test_missing_source_is_a_named_prerequisite(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(handoff_factory, "source_path",
                                   return_value=Path(tmp) / "absent.py"):
                with self.assertRaises(handoff_factory.FactoryUnavailable) as caught:
                    handoff_factory.load(object())
        self.assertEqual(caught.exception.gap, "helper:L232b")
        self.assertEqual(str(caught.exception), "helper:L232b")
        self.assertNotIn(tmp, str(caught.exception))

    def test_symlink_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.py"
            real.write_text("class HandoffFactory:\n    pass\n")
            link = Path(tmp) / "link.py"
            link.symlink_to(real)
            with mock.patch.object(handoff_factory, "source_path", return_value=link):
                with self.assertRaises(handoff_factory.FactoryUnavailable):
                    handoff_factory.load(object())

    def test_writable_source_is_refused(self):
        for mode in (0o664, 0o666):
            with self.subTest(mode=oct(mode)):
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / "fabrique.py"
                    source.write_text("class HandoffFactory:\n    pass\n")
                    source.chmod(mode)
                    with mock.patch.object(handoff_factory, "source_path", return_value=source):
                        with self.assertRaises(handoff_factory.FactoryUnavailable):
                            handoff_factory.load(object())

    def test_loads_the_pinned_factory_with_its_controller_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fabrique.py"
            source.write_text(
                "class HandoffFactory:\n"
                "    def __init__(self, db):\n"
                "        self.db = db\n"
                "        self.adapter = object()\n"
                "        self.requirements_for = lambda agent, host: None\n")
            source.chmod(0o644)
            controller = object()
            with mock.patch.object(handoff_factory, "source_path", return_value=source):
                factory = handoff_factory.load(controller)
        self.assertIs(factory.db, controller)
        self.assertIsNotNone(factory.adapter)
        self.assertTrue(callable(factory.requirements_for))

    def test_a_broken_factory_refuses_by_category_never_its_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fabrique.py"
            source.write_text(
                "class HandoffFactory:\n"
                "    def __init__(self, db):\n"
                "        raise ValueError('document body secret')\n")
            source.chmod(0o644)
            with mock.patch.object(handoff_factory, "source_path", return_value=source):
                with self.assertRaises(handoff_factory.FactoryUnavailable) as caught:
                    handoff_factory.load(object())
        self.assertEqual(str(caught.exception), "helper:L232b")
        self.assertNotIn("secret", str(caught.exception))


class ReadinessTest(unittest.TestCase):
    def test_a_refused_requirements_identity_is_a_named_gap(self):
        def refuse(agent, host):
            raise handoff_factory.FactoryUnavailable("identity:account")

        result = host_lifecycle.readiness(
            None, {"name": "dev", "harness": "codex"}, "target",
            adapter=object(), requirements_for=refuse)
        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], ["requirements:L232c"])


class RunnerBindingTest(PgTestCase):
    def test_runner_binds_adapter_and_requirements_before_any_use(self):
        adapter = SimpleNamespace(inventory=lambda host: {}, repository=lambda agent: "")
        requirements = lambda agent, host: None
        factory = SimpleNamespace(adapter=adapter, requirements_for=requirements)
        with mock.patch.object(handoff_factory, "load", return_value=factory) as load:
            runner = Runner(self.cfg, self.db, once=True)
        self.assertIs(runner._lifecycle_factory, factory)
        self.assertIs(runner._lifecycle_adapter, adapter)
        self.assertIs(runner._lifecycle_requirements, requirements)
        self.assertIsNone(runner._lifecycle_gap)
        self.assertEqual(load.call_count, 1)

    def test_absent_enrollment_leaves_the_same_named_gaps_on_both_paths(self):
        with mock.patch.object(handoff_factory, "load",
                               side_effect=handoff_factory.FactoryUnavailable("helper:L232b")):
            runner = Runner(self.cfg, self.db, once=True)
        self.assertIsNone(runner._lifecycle_adapter)
        self.assertIsNone(runner._lifecycle_requirements)
        self.assertEqual(runner._lifecycle_gap, "helper:L232b")
        agent = {"name": "dev", "harness": "codex", "session_account": ""}
        ready = host_lifecycle.readiness(
            self.db, agent, "target", adapter=runner._lifecycle_adapter,
            requirements_for=runner._lifecycle_requirements)
        self.assertFalse(ready["ready"])
        self.assertIn("helper:L232b", ready["missing"])
        self.assertIn("requirements:L232c", ready["missing"])


class RunnerLifecycleTest(PgTestCase):
    """The bound adapter is the one the runner's lifecycle pass uses."""

    def setUp(self):
        # The H2/H3 drain fixture (hosts, canon, credentials, limits), copied
        # so the inherited scenarios are not re-run in this file.
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources,credential_health CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'] WHERE name='dev'")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        fixtures = drain_fixtures.fixtures
        for host in ("source", "target"):
            self.api.canon.record_state(host, "ok", root="/canon", source="local", commit=fixtures.COMMIT,
                                        good=True, diagnostic="")
            for _ in range(3):
                self.api.hosts.record({"host": host, "mem_available_bytes": 24*fixtures.GIB,
                                      "swap_used_bytes": 0, "cpu_count": 8, "load1": 0.0,
                                      "disk_free_bytes": 100*fixtures.GIB, "gate_state": "available"})
            for harness in ("codex", "gh", "gcloud"):
                self.api.credentials.report(host, harness, "", "healthy", source="manual")
        self.limits = dict(resources.thresholds({"resources": {"min_mem_available": "4GiB",
                                                               "min_disk_free": "1GiB"}}), max_agents=4)
        self.adapter = drain_fixtures.Adapter()

    def requirements(self, agent, target):
        return host_preflight.Requirements(target, agent["harness"], secret_refs=("mesh/api",))

    def step(self):
        return host_lifecycle.advance_host(self.db, "source", limits_for=lambda host: self.limits,
                                           adapter=self.adapter, requirements_for=self.requirements)

    def runner(self):
        runner = object.__new__(Runner)
        runner.db = self.db
        runner.runner_id = "runner-mty-h5@source"
        runner.cfg = self.cfg
        runner.handoff_limits_for = lambda host: self.limits
        runner._lifecycle_adapter = self.adapter
        runner._lifecycle_requirements = self.requirements
        return runner

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_pass_drains_and_enables_with_the_bound_adapter(self):
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        for _ in range(9):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertGreaterEqual(self.adapter.calls.count("enable"), 1)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_pass_returns_the_agent(self):
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        for _ in range(9):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertIsNotNone(registry.claim(self.db, "dev", "target-runner", ttl_seconds=300))
        self.api.host_lifecycle.undrain("source", actor="orch")
        for _ in range(9):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_pass_recovers_a_lost_prepared_drain(self):
        self.adapter = RecoveryAdapter()
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        self.step()
        self.step()
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE name='dev'")
        self.db.execute(
            "UPDATE host_resources SET sampled_at=clock_timestamp()-interval '20 minutes' "
            "WHERE host='source'")
        self.api.host_lifecycle.detect_losses(actor="remote", older_than=180, critical=("dev",))
        for _ in range(6):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertIn("recover_source", self.adapter.calls)
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_post_switch_readiness_uses_the_frozen_plan_account(self):
        # A live session carries both values; the switch clears them.
        self.db.execute(
            "UPDATE agent_registry SET session_id='sess-dev', session_account='devacct' "
            "WHERE name='dev'")
        self.api.credentials.report("target", "codex", "devacct", "healthy", source="manual")

        def requirements(agent, target):
            # The switch clears session_account; only plan.account keeps it.
            self.assertEqual(agent.get("session_account"), "devacct")
            return host_preflight.Requirements(target, agent["harness"], "devacct",
                                               secret_refs=("mesh/api",))

        self.requirements = requirements
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        for _ in range(9):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_elected_roles_stay_with_their_election(self):
        registry.upsert(self.db, "gov", harness="codex", host="source", cwd="/work/gov")
        self.db.execute(
            "UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'], "
            "releve='election' WHERE name='gov'")
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev", "gov"))
        for _ in range(9):
            Runner.host_lifecycle_once(self.runner(), self.db)
        self.assertEqual(registry.get(self.db, "gov")["host"], "source")


class BindingRecoveryTest(unittest.TestCase):
    def test_gap_is_logged_once_and_cleared_when_enrollment_arrives(self):
        runner = object.__new__(Runner)
        runner.cfg = object()
        runner._lifecycle_gap = None
        factory = SimpleNamespace(adapter=object(), requirements_for=lambda a, h: None)
        with mock.patch("ameesh.scoped_db.ScopedDb"), \
                mock.patch.object(handoff_factory, "load", side_effect=[
                    handoff_factory.FactoryUnavailable(),
                    handoff_factory.FactoryUnavailable(), factory]), \
                mock.patch("ameesh.runner.log_async") as log:
            runner._bind_lifecycle()
            runner._bind_lifecycle()
            self.assertEqual(log.call_count, 1)
            runner._bind_lifecycle()
        self.assertIsNone(runner._lifecycle_gap)
        self.assertIs(runner._lifecycle_adapter, factory.adapter)

    def test_lifecycle_pass_retries_an_empty_binding(self):
        runner = object.__new__(Runner)
        runner.db = object()
        runner._lifecycle_factory = None
        with mock.patch.object(runner, "_bind_lifecycle") as bind, \
                mock.patch("ameesh.migrations.require_compatible_writer") as admission, \
                mock.patch("ameesh.runner.storage.of", side_effect=ValueError), \
                mock.patch("ameesh.runner.log_async"):
            runner.host_lifecycle_once(object())
        bind.assert_called_once_with()
        admission.assert_called_once_with(runner.db)

    def test_lifecycle_pass_keeps_an_active_binding(self):
        runner = object.__new__(Runner)
        runner.db = object()
        runner._lifecycle_factory = object()
        with mock.patch.object(runner, "_bind_lifecycle") as bind, \
                mock.patch("ameesh.migrations.require_compatible_writer") as admission, \
                mock.patch("ameesh.runner.storage.of", side_effect=ValueError), \
                mock.patch("ameesh.runner.log_async"):
            runner.host_lifecycle_once(object())
        bind.assert_not_called()
        admission.assert_called_once_with(runner.db)

    def test_relocation_readiness_value_error_keeps_source(self):
        worker = SimpleNamespace(
            runner=SimpleNamespace(mediated=False), db=object(), name="canary",
            agent={"host": "source"})
        with mock.patch("ameesh.runner.storage.of", side_effect=ValueError("private detail")), \
                mock.patch("ameesh.runner.log") as log:
            self.assertFalse(AgentWorker.maybe_relocate(worker, {}))
        self.assertNotIn("private detail", str(log.call_args))
