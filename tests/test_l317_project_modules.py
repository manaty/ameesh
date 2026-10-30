# SPDX-License-Identifier: AGPL-3.0-only
"""Project isolation and real execution evidence, including storage bypasses."""
from __future__ import annotations

from pathlib import Path
import io
import json
import sys
import tempfile
import threading
import types as types_mod
import unittest
from unittest import mock

from tests import support  # isolate host config before module tests
from tests.support import PgTestCase
from ameesh import canon, canon_sync, db as db_mod, identity, modules, storage, work
from ameesh.modules import it
from ameesh.modules.it.execution import OUTPUT_BYTES, digest, execute
from ameesh.modules.storage import ModuleStore


def federation(project="acme", names=None):
    return {"extensions": {"ameesh": {"projects": {
        project: {"modules": ["it"] if names is None else names}}}}}


FIVE_DOCUMENTARY = ("docs", "investigation", "governance-tracking",
                    "promotion", "human_operation")


class FakeDistribution:
    def __init__(self, name="ameesh-sample-module", version="0.1.0"):
        self.name = name
        self.version = version


class FakeEntryPoint:
    group = "ameesh.modules"

    def __init__(self, module, name="sample", dist=None):
        self.name = name
        self.module = module
        self.value = "sample_module"
        self.dist = dist if dist is not None else FakeDistribution()

    def load(self):
        if isinstance(self.module, Exception):
            raise self.module
        return self.module


class FakeEntryPoints(list):
    def select(self, group=None):
        return [point for point in self if group in (None, point.group)]


def external_sample(types=None, name="sample", descriptor=None):
    """Un descripteur externe de test, installé hors du dépôt public."""
    module = types_mod.ModuleType("sample_module")
    declared = tuple(types or FIVE_DOCUMENTARY)
    module.NAME = name
    module.TYPES = declared
    if descriptor is not None:
        module.descriptor = descriptor
    else:
        def compile_descriptor(options=None):
            options = dict(options or {})
            if options:
                raise ValueError("unknown sample module option(s): %s"
                                 % ", ".join(sorted(options)))
            return {"types": list(declared), "schemas": {}, "rules": []}
        module.descriptor = compile_descriptor
    return module


def external_points(*points):
    return mock.patch("ameesh.modules.metadata.entry_points",
                      return_value=FakeEntryPoints(points))


class ModuleConfigurationTest(unittest.TestCase):
    def setUp(self):
        # Aucun descripteur externe réel de l'hôte ne doit fuiter dans les tests.
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)

    def test_module_refusal_is_per_lot_but_other_database_errors_propagate(self):
        store = mock.Mock()
        # #3022 : la garde de porte de production lit la marque du lot sous
        # son verrou de ligne ; un lot sans marque ne lit rien d'autre.
        store.work.lock.return_value = {"id": 1, "state": "qa", "app": "acme"}
        store.progress.lot_hold_events.return_value = []
        # MagicMock : la fermeture ouvre sa transaction (et prend le verrou de
        # graphe) avant l'écriture du stockage.
        with mock.patch.object(work, "get", return_value={"id": 1, "state": "qa"}), \
                mock.patch.object(work.storage, "of", return_value=store):
            store.work.close_merged.side_effect = db_mod.DbError("AMEESH_MODULE_RULE: evidence missing")
            self.assertEqual(work.close_merged(mock.MagicMock(), 1)["result"], "refused")
            store.work.close_merged.side_effect = db_mod.DbError("database connection lost")
            with self.assertRaises(db_mod.DbError):
                work.close_merged(mock.MagicMock(), 1)
    def test_it_cli_parses_artifact_options_after_lot_and_requests_isolated_execution(self):
        bound = identity.Binding(name="tester", source="runner", runner_id="r1", epoch=2, ok=True)
        with mock.patch.object(it, "item", return_value={"app": "acme"}), \
                mock.patch("ameesh.identity.require_bound", return_value=bound), \
                mock.patch("ameesh.modules.it.proof_service.request", return_value={"id": 1}) as request:
            it.main(mock.Mock(), ["proof", "123", "--artifact", "/artifact", "--runner", "python3",
                                 "--", "--help", "quoted argument"], actor="tester")
        request.assert_called_once_with("acme", 123, "/artifact", runner="python3",
                                        arguments=["--help", "quoted argument"], timeout=60,
                                        identity={"agent": "tester", "runner_id": "r1",
                                                  "lease_epoch": 2})

    @support.needs_builtin_modules
    def test_only_named_project_is_enabled(self):
        entries = modules.declarations(federation())
        self.assertEqual([(e["project"], e["module"]) for e in entries], [("acme", "it")])
        self.assertEqual(entries[0]["types"], ["release", "publication"])
        self.assertEqual(modules.declarations({}), [])
        self.assertEqual(modules.declarations(federation(names=[])), [])

    @support.needs_builtin_modules
    def test_dev_descriptor_without_a_type_leaves_project_policies_unchanged(self):
        """L101 T4: `dev` is built in, always active and stores no work type.

        The project declarations (and the SQL policy rows they compile) are
        unchanged; selecting `dev` is not an activation (L98 T2), so the canon
        refuses it instead of creating an empty policy row."""
        self.assertEqual(modules.installed()["dev"].descriptor({}),
                         {"types": [], "schemas": {}, "rules": []})
        entries = modules.declarations(federation())
        self.assertEqual([(e["project"], e["module"]) for e in entries],
                         [("acme", "it")])
        with self.assertRaisesRegex(ValueError, "types must be unique non-empty"):
            modules.declarations(federation(names=["it", "dev"]))

    def test_unknown_duplicate_and_malformed_modules_refused(self):
        for names in (["not.installed"], ["it", "it"], "it", [3]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                modules.declarations(federation(names=names))
        for raw in ({"extensions": []}, {"extensions": {"ameesh": []}},
                    {"extensions": {"ameesh": {"projects": []}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                modules.declarations(raw)

    def test_external_descriptor_is_discovered_and_compiled(self):
        fake = external_sample()
        with external_points(FakeEntryPoint(fake)):
            self.assertIs(modules.installed()["sample"], fake)
            entries = modules.declarations(federation(names=["sample"]))
        self.assertEqual([(e["project"], e["module"]) for e in entries],
                         [("acme", "sample")])
        self.assertEqual(entries[0]["types"], list(FIVE_DOCUMENTARY))
        # Q1 #1915 : aucun schéma ni règle métier inventé pour ces types.
        self.assertEqual(entries[0]["schemas"], {})
        self.assertEqual(entries[0]["rules"], [])
        with self.assertRaisesRegex(ValueError, "unknown sample module option"):
            fake.descriptor({"require_qa": ["promoted"]})

    @support.needs_builtin_modules
    def test_external_descriptor_and_it_coexist_with_disjoint_types(self):
        fake = external_sample()
        with external_points(FakeEntryPoint(fake)):
            entries = modules.declarations(federation(names=["it", "sample"]))
        self.assertEqual([(e["project"], e["module"]) for e in entries],
                         [("acme", "it"), ("acme", "sample")])
        it_types = set(next(e["types"] for e in entries if e["module"] == "it"))
        self.assertEqual(set(fake.TYPES), set(FIVE_DOCUMENTARY))
        self.assertFalse(it_types & set(fake.TYPES))

    @support.needs_builtin_modules
    def test_external_module_refusals(self):
        mismatch = external_sample(name="other")
        no_descriptor = types_mod.ModuleType("broken_module")
        no_descriptor.NAME = "sample"
        malformed = external_sample(
            descriptor=lambda options=None: {"types": [], "schemas": {}, "rules": []})
        wrong_keys = external_sample(
            descriptor=lambda options=None: {"types": ["docs"], "schemas": {}})
        bad_types = external_sample(types=["docs", "docs"])
        cases = (
            ("load", (FakeEntryPoint(RuntimeError("boom")),), "cannot be loaded"),
            ("name", (FakeEntryPoint(mismatch),), "declares NAME"),
            ("descriptor", (FakeEntryPoint(no_descriptor),), "no descriptor"),
            ("types", (FakeEntryPoint(malformed),), "types must be unique"),
            ("duplicate-types", (FakeEntryPoint(bad_types),), "types must be unique"),
            ("keys", (FakeEntryPoint(wrong_keys),), "descriptor keys"),
            ("duplicates", (FakeEntryPoint(external_sample()),
                            FakeEntryPoint(external_sample())),
             "duplicate installed module"),
            ("builtin", (FakeEntryPoint(it, name="it"),), "duplicate installed module"),
        )
        for label, points, message in cases:
            with self.subTest(label=label), external_points(*points), \
                    self.assertRaisesRegex(ValueError, message):
                modules.installed()
        inconsistent = external_sample()
        inconsistent.TYPES = ("docs",)
        with external_points(FakeEntryPoint(inconsistent)), \
                self.assertRaisesRegex(ValueError, "TYPES differ"):
            modules.installed()

    def test_external_evidence_exposes_installation_proof(self):
        with external_points(FakeEntryPoint(external_sample())):
            rows = modules.evidence()
        row = next(item for item in rows if item["name"] == "sample")
        self.assertTrue(row["ok"])
        self.assertEqual(row["source"], "external")
        self.assertEqual(row["distribution"], "ameesh-sample-module")
        self.assertEqual(row["version"], "0.1.0")
        self.assertEqual(len(row["descriptor_sha256"]), 64)
        broken = external_sample(name="other")
        with external_points(FakeEntryPoint(broken)):
            row = next(item for item in modules.evidence()
                       if item["name"] == "sample")
        self.assertFalse(row["ok"])
        self.assertIn("declares NAME", row["error"])

    @support.needs_builtin_modules
    def test_external_evidence_reports_duplicates_as_unusable(self):
        points = (FakeEntryPoint(external_sample()),
                  FakeEntryPoint(external_sample()))
        with external_points(*points):
            rows = [row for row in modules.evidence() if row["name"] == "sample"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row["ok"] for row in rows))
        self.assertTrue(all("duplicate installed module" in row["error"]
                            for row in rows))
        with external_points(*points), \
                self.assertRaisesRegex(ValueError, "duplicate installed module"):
            modules.installed()
        with external_points(FakeEntryPoint(it, name="it")):
            row = next(item for item in modules.evidence()
                       if item["name"] == "it" and item["source"] == "external")
        self.assertFalse(row["ok"])
        self.assertIn("duplicate installed module", row["error"])

    def test_external_types_are_rechecked_with_project_options(self):
        def descriptor(options=None):
            options = dict(options or {})
            return {"types": ["promotion"] if options else ["docs"],
                    "schemas": {}, "rules": []}

        declared = external_sample(types=["docs"], descriptor=descriptor)
        with external_points(FakeEntryPoint(declared)), \
                self.assertRaisesRegex(ValueError, "TYPES differ"):
            modules.declarations(federation(names={"sample": {"typed": True}}))
        flexible = external_sample(types=["docs"], descriptor=descriptor)
        del flexible.TYPES
        with external_points(FakeEntryPoint(flexible)):
            entries = modules.declarations(
                federation(names={"sample": {"typed": True}}))
        self.assertEqual(entries[0]["types"], ["promotion"])

    def test_external_types_cover_the_five_non_code_process_types(self):
        from ameesh.process_verifiers import (CODE_TYPES, INSTALLED_TYPES,
                                              completion_policy)
        self.assertEqual(set(FIVE_DOCUMENTARY),
                         set(INSTALLED_TYPES) - set(CODE_TYPES))
        for kind in FIVE_DOCUMENTARY:
            with self.subTest(kind=kind):
                policy = completion_policy(kind)
                self.assertFalse(policy.code)
                self.assertTrue(policy.requires_review)

    def test_module_cli_list_prints_installation_evidence(self):
        with external_points(FakeEntryPoint(external_sample())), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(modules.main(["--list"]), 0)
        rows = json.loads(out.getvalue())
        self.assertTrue(any(row["name"] == "sample" and row["ok"]
                            for row in rows))

    def test_canon_check_reports_module_errors(self):
        c = canon.Canon(root=".", readable=True, federation=federation(names=["missing"]))
        self.assertTrue(any(f.code == "project-modules-invalid" for f in canon.validate(c)))

    def test_refused_canon_does_not_apply_module_policies(self):
        c = canon.Canon(root=".", readable=True, federation=federation())
        lock = mock.Mock()
        with mock.patch.object(canon_sync, "last_applied", return_value=None), \
                mock.patch.object(canon_sync, "_verify", side_effect=canon_sync._Refused("bad ref")), \
                mock.patch.object(modules, "sync") as apply:
            result = canon_sync._sync_locked(lock, c, "main", "", "host")
        self.assertEqual(result.status, canon_sync.AUTH_REFUSED)
        apply.assert_not_called()

    @support.needs_builtin_modules
    def test_partial_canon_preserves_policies_and_trusted_complete_canon_applies(self):
        c = canon.Canon(root=".", readable=True, federation=federation())
        lock = mock.Mock()
        for partial in (True, False):
            with self.subTest(partial=partial), \
                    mock.patch.object(canon_sync, "last_applied", return_value=None), \
                    mock.patch.object(canon_sync, "_verify", return_value=("main", "trusted", [])), \
                    mock.patch.object(canon_sync, "_lag", return_value=([], [])), \
                    mock.patch.object(canon_sync, "_partial", return_value=partial), \
                    mock.patch.object(canon_sync.receipts, "_apply_authenticators", return_value={}), \
                    mock.patch.object(canon_sync, "_record"), \
                    mock.patch.object(modules, "sync") as apply:
                result = canon_sync._sync_locked(lock, c, "main", "", "host")
                self.assertEqual(result.status, canon_sync.AUTH_SYNCED)
                if partial:
                    apply.assert_not_called()
                else:
                    apply.assert_called_once_with(lock.db, c)


class ExternalDiscoveryTest(unittest.TestCase):
    """Un paquet réellement présent dans l'environnement est découvert."""

    module_name = "ameesh_sample_module_2847"

    def test_a_real_installed_distribution_is_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "%s.py" % self.module_name)
            source.write_text(
                "NAME = 'sample'\n"
                "TYPES = ('docs', 'investigation', 'governance-tracking',\n"
                "         'promotion', 'human_operation')\n"
                "def descriptor(options=None):\n"
                "    options = dict(options or {})\n"
                "    if options:\n"
                "        raise ValueError('unknown option')\n"
                "    return {'types': list(TYPES), 'schemas': {}, 'rules': []}\n")
            info = Path(folder, "ameesh_sample_module-0.1.0.dist-info")
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: ameesh-sample-module\nVersion: 0.1.0\n")
            (info / "entry_points.txt").write_text(
                "[ameesh.modules]\nsample = %s\n" % self.module_name)
            sys.path.insert(0, folder)
            try:
                registry = modules.installed()
                rows = modules.evidence()
            finally:
                sys.path.remove(folder)
                sys.modules.pop(self.module_name, None)
        self.assertEqual(registry["sample"].NAME, "sample")
        row = next(item for item in rows if item["name"] == "sample")
        self.assertTrue(row["ok"])
        self.assertEqual(row["distribution"], "ameesh-sample-module")
        self.assertEqual(row["version"], "0.1.0")
        self.assertEqual(len(row["descriptor_sha256"]), 64)

    def test_the_repository_test_module_is_discovered(self):
        folder = str(Path(__file__).resolve().parent / "fixtures" / "ameesh_modules")
        sys.path.insert(0, folder)
        try:
            registry = modules.installed()
            rows = modules.evidence()
        finally:
            sys.path.remove(folder)
            sys.modules.pop("sample_module", None)
        self.assertEqual(registry["sample"].NAME, "sample")
        self.assertEqual(set(registry["sample"].TYPES), set(FIVE_DOCUMENTARY))
        row = next(item for item in rows if item["name"] == "sample")
        self.assertTrue(row["ok"])
        self.assertEqual(row["distribution"], "ameesh-sample-module")
        self.assertEqual(row["version"], "0.1.0")


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.artifact = Path(self.folder.name, "delivered.py")

    def write(self, source):
        self.artifact.write_text(source)
        return self.artifact

    def test_real_execution_binds_observed_digest_and_argv(self):
        self.write("import sys\nprint('started:', sys.argv[1])\n")
        evidence = execute(self.artifact, runner=sys.executable, arguments=["smoke"])
        self.assertEqual(evidence["sha256"], digest(self.artifact)[1])
        self.assertEqual(evidence["command"], [sys.executable, evidence["executed_artifact"], "smoke"])
        self.assertNotEqual(evidence["executed_artifact"], str(self.artifact))
        self.assertEqual(evidence["artifact"], str(self.artifact))
        self.assertFalse(Path(evidence["executed_artifact"]).exists())
        self.assertEqual(evidence["output"], "started: smoke")
        self.assertEqual(evidence["exit_code"], 0)

    def test_failed_silent_and_timeout_execution_are_not_proof(self):
        for source, error in (("raise SystemExit(2)", "failed"), ("pass", "no output"),
                              ("import time; time.sleep(5)", "timed out")):
            with self.subTest(error=error):
                self.write(source)
                with self.assertRaisesRegex(ValueError, error):
                    execute(self.artifact, runner=sys.executable,
                            timeout=.1 if error == "timed out" else 5)

    def test_changed_artifact_is_not_proof(self):
        self.write("from pathlib import Path\np=Path(__file__); p.chmod(0o755); p.write_text('changed')\nprint('ok')\n")
        with self.assertRaisesRegex(ValueError, "changed during"):
            execute(self.artifact, runner=sys.executable)

    def test_source_A_B_A_substitution_executes_only_the_hashed_snapshot(self):
        original = "print('real artifact A')"
        replacement = "print('trivial artifact B')"
        self.write(original)
        sha = digest(self.artifact)[1]
        import subprocess
        launch = subprocess.Popen

        def swap_source(command, **options):
            self.artifact.write_text(replacement)
            try:
                self.assertNotEqual(command[1], str(self.artifact))
                self.assertEqual(Path(command[1]).read_text(), original)
                return launch(command, **options)
            finally:
                self.artifact.write_text(original)

        with mock.patch("ameesh.modules.it.execution.subprocess.Popen", side_effect=swap_source):
            evidence = execute(self.artifact, runner=sys.executable, expected_sha=sha)
        self.assertEqual(evidence["output"], "real artifact A")
        self.assertEqual(evidence["sha256"], sha)
        self.assertEqual(self.artifact.read_text(), original)

    def test_changed_source_during_run_does_not_rebind_the_snapshot(self):
        self.write("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('changed source')\nprint('snapshot ran')")
        sha = digest(self.artifact)[1]
        evidence = execute(self.artifact, runner=sys.executable, arguments=[str(self.artifact)], expected_sha=sha)
        self.assertEqual(evidence["sha256"], sha)
        self.assertEqual(evidence["output"], "snapshot ran")
        self.assertEqual(self.artifact.read_text(), "changed source")

    def test_symlink_artifact_is_refused_without_execution(self):
        self.write("print('real')")
        link = Path(self.folder.name, "symlink.py")
        link.symlink_to(self.artifact)
        with mock.patch("ameesh.modules.it.execution.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "symlink"):
                execute(link, runner=sys.executable)
        launch.assert_not_called()

    def test_output_is_drained_with_bounded_memory(self):
        self.write("print('x' * 2000000)\n")
        self.assertEqual(len(execute(self.artifact, runner=sys.executable)["output"]), OUTPUT_BYTES)

    def test_invalid_timeout_and_nonfile_are_refused(self):
        self.write("print('ok')")
        for timeout in (0, -1, float("nan"), float("inf"), 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                execute(self.artifact, runner=sys.executable, timeout=timeout)
        with self.assertRaises(ValueError):
            digest(self.folder.name)

    def test_proof_requires_declaration_before_executing(self):
        self.write("print('ok')")
        with mock.patch.object(it, "item"), mock.patch.object(ModuleStore, "latest", return_value=None), \
                mock.patch("ameesh.modules.it.execution.execute") as run:
            with self.assertRaisesRegex(ValueError, "declare"):
                it.prove(mock.Mock(), 1, self.artifact, actor="test")
        run.assert_not_called()

    def test_wrong_digest_is_refused_before_executing(self):
        self.write("print('ok')")
        with mock.patch.object(it, "item"), \
                mock.patch.object(ModuleStore, "latest", return_value={"id": 1, "subject": "0" * 64}), \
                mock.patch("ameesh.modules.it.execution.subprocess.Popen") as run:
            with self.assertRaisesRegex(ValueError, "nothing executed"):
                it.prove(mock.Mock(), 1, self.artifact, actor="test")
        run.assert_not_called()


@support.needs_builtin_modules
class LifecycleGateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # Git identity/ancestry is exercised separately in #1286; this class
        # isolates the IT gate after a valid manual merge declaration.
        discover = external_points()
        discover.start()
        self.addCleanup(discover.stop)
        proof = mock.patch.object(work, "_declared_merge_commit",
                                  side_effect=lambda item, sha, **kw: sha)
        proof.start()
        self.addCleanup(proof.stop)
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation()))
        self.artifact = Path(self.tmp, "artifact.py")
        self.artifact.write_text("print('delivered executable smoke passed')\n")
        self.lot = work.add(self.db, title="Publication", type="release", app="acme")["id"]
        # Ce témoin isole la porte IT après un gel réel : la tête gelée est
        # posée au stockage (la porte d'écriture du gel a ses propres témoins,
        # #2962).
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="a" * 40, actor="auteur", verdict=None, note="")
        self.store = ModuleStore(self.db)

    def prove(self):
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        return it.prove(self.db, self.lot, self.artifact, actor="tester", runner=sys.executable)

    def test_project_isolation_and_unknown_type(self):
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="Other project", type="release", app="other")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("INSERT INTO work_items(title,type,app) VALUES ('raw','release','other')")
        core = work.add(self.db, title="Core", app="other")["id"]
        self.assertEqual(work.merged(self.db, core, sha="abcdef1234")["result"], "merged")

    def test_missing_proof_blocks_manual_and_automatic_closure_and_go(self):
        with self.assertRaises(work.WorkError):
            work.merged(self.db, self.lot, sha="abcdef1234")
        self.assertEqual(work.close_merged(self.db, self.lot, source="sync-github")["result"], "refused")
        with self.assertRaises(db_mod.DbError):
            work.milestone(self.db, self.lot, "verdict", verdict="ok",
                           sha="a" * 40)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        self.assertFalse(any(m["kind"] == "merged" for m in work.milestones(self.db, self.lot)))
        work.milestone(self.db, self.lot, "verdict", verdict="blocked",
                       sha="a" * 40)

    def test_delivery_without_proof_still_blocks_go(self):
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        with self.assertRaisesRegex(db_mod.DbError, "proof"):
            work.milestone(self.db, self.lot, "verdict", verdict="ok",
                           sha="a" * 40)

    def test_matching_execution_allows_go_merge_and_promotion(self):
        proof = self.prove()
        self.assertEqual(proof["payload"]["exit_code"], 0)
        work.milestone(self.db, self.lot, "verdict", verdict="ok",
                       sha="a" * 40)
        self.assertEqual(work.merged(self.db, self.lot, sha="a" * 40)["result"], "merged")
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "delivered")

    def test_work_move_and_raw_storage_cannot_bypass_gate(self):
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "merged")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s", (self.lot,))
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_new_declaration_invalidates_proof_even_for_same_bytes(self):
        self.prove()
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        self.assertEqual(work.close_merged(self.db, self.lot)["result"], "refused")
        it.prove(self.db, self.lot, self.artifact, actor="tester", runner=sys.executable)
        self.assertEqual(work.close_merged(self.db, self.lot)["result"], "merged")

    def test_closure_rechecks_after_waiting_for_concurrent_declaration(self):
        self.prove()
        result = []
        entered = threading.Event()

        def close_from_other_connection():
            other = db_mod.connect(self.cfg)
            try:
                entered.set()
                result.append(work.close_merged(other, self.lot, source="concurrent-sync")["result"])
            finally:
                other.close()

        with self.db.transaction() as tx:
            it.declare(tx, self.lot, self.artifact, actor="tester")
            thread = threading.Thread(target=close_from_other_connection, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(timeout=15))
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["refused"])
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_independent_lots_do_not_block_each_other_with_or_without_module(self):
        for project, kind in (("acme", "release"), ("plain", "evolution")):
            first = work.add(self.db, title="one", type=kind, app=project)["id"]
            second = work.add(self.db, title="two", type=kind, app=project)["id"]
            finished = threading.Event()
            errors = []

            def update_other():
                other = db_mod.connect(self.cfg)
                try:
                    other.execute("UPDATE work_items SET title = 'other' WHERE id = %s", (second,))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    other.close()
                    finished.set()

            with self.db.transaction() as tx:
                tx.execute("UPDATE work_items SET title = 'first' WHERE id = %s", (first,))
                thread = threading.Thread(target=update_other, daemon=True)
                thread.start()
                self.assertTrue(finished.wait(5), "independent lot blocked on project lock")
                tx.execute("UPDATE work_items SET title = 'then second' WHERE id = %s", (second,))
            thread.join(10)
            self.assertEqual(errors, [])

    def test_changed_delivery_blocks_promotion(self):
        self.prove()
        work.merged(self.db, self.lot, sha="abcdef1234")
        self.artifact.write_text("print('new artifact')")
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")

    def test_wrong_and_stale_binding_are_refused_atomically(self):
        self.prove()
        previous = self.store.latest(self.lot, "it", "delivery")
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        evidence = execute(self.artifact, runner=sys.executable)
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", evidence["sha256"], evidence,
                              actor="tester", binding_id=previous["id"])
        latest = self.store.latest(self.lot, "it", "delivery")
        evidence["sha256"] = "f" * 64
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", "f" * 64, evidence,
                              actor="tester", binding_id=latest["id"])

    def test_module_milestones_are_immutable_and_failed_exit_rejected(self):
        proof = self.prove()
        for query in ("DELETE FROM work_module_milestones WHERE id = %s",
                      "UPDATE work_module_milestones SET subject = 'changed' WHERE id = %s"):
            with self.assertRaises(db_mod.DbError):
                self.db.execute(query, (proof["id"],))
        evidence = execute(self.artifact, runner=sys.executable)
        evidence["exit_code"] = 1
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", evidence["sha256"], evidence,
                              actor="tester", binding_id=proof["binding_id"])

    def test_reclassification_cannot_erase_requirements(self):
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET type = 'evolution' WHERE id = %s", (self.lot,))
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET app = 'other' WHERE id = %s", (self.lot,))

    def test_another_canon_cannot_steal_project(self):
        with self.assertRaises(ValueError), self.db.transaction() as tx:
            ModuleStore(tx).replace("other", modules.declarations(federation()))
        self.assertTrue(self.store.enabled("acme", "it"))

    def test_cli_types_and_structured_milestones_are_visible(self):
        self.prove()
        proc = self.mesh("work", "show", str(self.lot), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        self.assertEqual(len(json.loads(proc.stdout)["module_milestones"]), 2)
        proc = self.mesh("work", "add", "--title", "Store upload", "--type", "publication",
                         "--app", "acme", "--estimate", "1h", "--actor", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class DocumentaryTypesGateTest(PgTestCase):
    """Q1 #1915 : un descripteur externe peut déclarer les cinq types.

    Le descripteur les rend installables ; l'activation reste une sélection de
    canon par projet. Un projet sans module continue de les refuser, côté CLI
    comme par écriture SQL directe.
    """

    def setUp(self):
        super().setUp()
        self.external = external_sample()
        with external_points(FakeEntryPoint(self.external)):
            declarations = modules.declarations(federation(names=["sample"]))
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", declarations)

    def test_each_declared_type_is_admitted_for_the_project(self):
        for kind in self.external.TYPES:
            with self.subTest(kind=kind):
                lot = work.add(self.db, title="Documentary", type=kind, app="acme")
                self.assertEqual((lot["type"], lot["app"]), (kind, "acme"))
                self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
                self.assertTrue(ModuleStore(self.db).admits("acme", kind))

    def test_a_project_without_the_module_still_refuses_each_type(self):
        for kind in self.external.TYPES:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(work.WorkError, "enable its module"):
                    work.add(self.db, title="No module", type=kind, app="other")
                with self.assertRaisesRegex(db_mod.DbError, "AMEESH_MODULE_RULE"):
                    self.db.execute(
                        "INSERT INTO work_items(title,type,app) "
                        "VALUES ('raw',%s,'other')", (kind,))

    def test_declaration_does_not_admit_other_module_types(self):
        core = work.add(self.db, title="Core", app="other")["id"]
        self.assertEqual(work.get(self.db, core)["type"], "evolution")
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="Release", type="release", app="acme")
        self.assertFalse(ModuleStore(self.db).admits("acme", "release"))
        self.assertTrue(ModuleStore(self.db).admits("acme", "docs"))

    def test_cli_adds_a_documentary_lot_when_the_module_is_active(self):
        proc = self.mesh("work", "add", "--title", "Read the contract",
                         "--type", "docs", "--app", "acme", "--new-project",
                         "--actor", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Read the contract", proc.stdout)
