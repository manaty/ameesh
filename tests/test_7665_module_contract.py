# SPDX-License-Identifier: AGPL-3.0-only
"""#7665 (#5857): contract tests of the external module contract v1.

The witness under examples/modules/contract-witness is a real distribution
outside src/: its module half implements only the version 1 Module
interface, its client half reaches ameesh only through the installed
executables, HTTP and the named SQL views. Each element of
docs/api/module-contract-v1.md is frozen in
docs/api/module-contract-v1.surface.json and mapped to the test that guards
it in docs/api/module-contract-v1-coverage.md: a break turns ameesh's CI
red, never the production of a module.
"""
from __future__ import annotations

import ast
import configparser
import copy
from importlib import metadata, util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from tests.support import PgTestCase
from tests.test_canon import commit_all, publish, write
from ameesh import db as db_mod, serve
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore

ROOT = Path(__file__).resolve().parents[1]
WITNESS = ROOT / "examples" / "modules" / "contract-witness"
CONTRACT = ROOT / "docs" / "api" / "module-contract-v1.md"
SURFACE_FILE = ROOT / "docs" / "api" / "module-contract-v1.surface.json"
COVERAGE = ROOT / "docs" / "api" / "module-contract-v1-coverage.md"
SURFACE = json.loads(SURFACE_FILE.read_text(encoding="utf-8"))
#: The only imports the witness may use: no ameesh internals, no tests.
WITNESS_IMPORTS = {"argparse", "http.client", "json", "re", "subprocess"}
TOKEN = "w" * 43
#: A valid value per path parameter, so the request reaches the token check.
PARAMETERS = {"turn": "0" * 32}
PY = sys.executable
#: Command prefixes of the installed executables (pyproject [project.scripts]).
EXECUTABLES = {"ameesh": [PY, "-m", "ameesh"], "agent-runner": [PY, "-m", "ameesh.runner"],
               "agent-mail": [PY, "-m", "ameesh.entry"]}


def _load(name, path):
    spec = util.spec_from_file_location(name, path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load("contract_witness_client", WITNESS / "contract_witness_client.py")
witness = _load("ameesh_contract_witness", WITNESS / "ameesh_contract_witness.py")


def package():
    """(project, entry points) read from the witness pyproject (TOML subset)."""
    parser = configparser.ConfigParser()
    parser.read(WITNESS / "pyproject.toml")
    project = {key: json.loads(value) for key, value in parser["project"].items()}
    entries = {key: json.loads(value) for key, value in
               parser['project.entry-points."ameesh.modules"'].items()}
    return project, entries


def install(directory, entries=None, name=None, version=None):
    """Install a distribution's metadata in `directory`, as pip would."""
    project, declared = package()
    name, version = name or project["name"], version or project["version"]
    info = Path(directory) / ("%s-%s.dist-info" % (name.replace("-", "_"), version))
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: %s\nVersion: %s\n"
                                   % (name, version))
    (info / "entry_points.txt").write_text("[ameesh.modules]\n" + "".join(
        "%s = %s\n" % item for item in (entries or declared).items()))
    return directory


def witness_path(site):
    return os.pathsep.join([str(site), str(WITNESS), support.SRC])


def command(name):
    """`ameesh turn current` → (prefix, args) of the installed executable."""
    words = name.split()
    return EXECUTABLES[words[0]], words[1:]


class _EntryPoints(list):
    """Python 3.9 has no importlib.metadata.EntryPoints; ameesh supports 3.9."""

    def select(self, group=None):
        return [point for point in self if group is None or point.group == group]


def installed_points(site):
    return mock.patch("ameesh.modules.metadata.entry_points", return_value=_EntryPoints(
        point for dist in metadata.distributions(path=[str(site)])
        for point in dist.entry_points))


class WitnessBoundaryTest(unittest.TestCase):
    """The witness is a consumer: its own distribution, public surfaces only."""

    def test_the_witness_is_its_own_distribution_outside_the_package(self):
        self.assertNotIn(ROOT / "src", WITNESS.parents)
        self.assertNotIn(ROOT / "tests", WITNESS.parents)
        project, entries = package()
        self.assertEqual(project["name"], "ameesh-contract-witness")
        self.assertEqual(entries, {witness.NAME: "ameesh_contract_witness"})

    def test_the_witness_imports_neither_ameesh_nor_tests(self):
        for path in sorted(WITNESS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, path)
                    names.add(node.module)
                elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                        "__import__", "import_module"):
                    self.fail("%s: dynamic import" % path.name)
            self.assertLessEqual(names, WITNESS_IMPORTS, path.name)


class CompatibilityTest(unittest.TestCase):
    """Baseline surface → candidate: additive is compatible, a removal is not."""

    def test_the_surface_names_the_contract_revision(self):
        text = CONTRACT.read_text(encoding="utf-8")
        self.assertIn("Contract revision: **%s," % SURFACE["contract"], text)
        self.assertIn(SURFACE["baseline"], text)

    def test_the_served_openapi_keeps_every_baseline_operation(self):
        candidate = client.openapi_surface(serve.openapi())
        self.assertEqual(client.openapi_breaks(SURFACE["http"]["operations"], candidate), [])

    def test_the_comparator_tells_additions_from_breaks(self):
        baseline = SURFACE["http"]["operations"]
        candidate = copy.deepcopy(baseline)
        candidate["GET /api/v1/witness-new"] = candidate["GET /api/v1/me"]
        candidate["GET /api/v1/me"]["response_properties"].append("added_field")
        candidate["GET /api/v1/lots"]["parameters"].append("query:added")
        self.assertEqual(client.openapi_breaks(baseline, candidate), [])
        broken = copy.deepcopy(baseline)
        del broken["GET /api/v1/hosts"]
        broken["GET /api/v1/me"]["response_properties"].remove("human")
        broken["GET /api/v1/lots"]["required_parameters"].append("query:limit")
        broken["POST /api/v1/budget/caps"]["body_required"].append("added")
        self.assertEqual(client.openapi_breaks(baseline, broken), [
            "GET /api/v1/hosts: operation removed",
            "GET /api/v1/lots: parameter newly required: query:limit",
            "GET /api/v1/me: response field removed: human",
            "POST /api/v1/budget/caps: request field newly required: added"])


class CliContractTest(unittest.TestCase):
    """The installed executables, with the witness installed beside them."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="w7665-"))
        cls.site = install(cls.tmp / "site")
        (cls.tmp / "config.json").write_text("{}\n")
        cls.env = support.child_env(PYTHONPATH=witness_path(cls.site),
                                    AMEESH_CONFIG=str(cls.tmp / "config.json"),
                                    # no reachable database: these commands need none
                                    AMEESH_DSN="postgresql://nobody@127.0.0.1:9/none")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ameesh(self, *args, env=None):
        return client.run(EXECUTABLES["ameesh"], args, env=env or self.env)

    def test_every_documented_option_is_in_the_help(self):
        for name, options in sorted(SURFACE["cli"].items()):
            with self.subTest(name):
                if name == "ameesh --version":
                    code, out, err = self.ameesh("--version")
                    self.assertEqual(code, 0, err)
                    self.assertRegex(out, r"^ameesh \d+\.\d+\.\d+")
                    continue
                prefix, args = command(name)
                found, text = client.help_options(prefix, args, env=self.env)
                self.assertLessEqual(set(options), found)
                if name.startswith("agent-mail "):
                    self.assertIn(name, text)

    def test_modules_json_describes_the_installed_witness(self):
        code, document = client.run_json(EXECUTABLES["ameesh"], ["modules", "--no-canon",
                                                                 "--json"], env=self.env)
        self.assertEqual((code, document["refused"]), (0, []))
        row = {item["name"]: item for item in document["modules"]}[witness.NAME]
        client.require_fields(row, SURFACE["module"]["record_fields"], "modules --json")
        self.assertEqual({key: row[key] for key in ("version", "source", "capabilities",
                                                    "cli", "storage", "distribution",
                                                    "entry_point", "card_ref")},
                         {"version": "1.0.0", "source": "external",
                          "capabilities": list(witness.CAPABILITIES),
                          "cli": "ameesh module contract-witness",
                          "storage": {"types": list(witness.TYPES)},
                          "distribution": "ameesh-contract-witness",
                          "entry_point": "ameesh_contract_witness", "card_ref": None})
        self.assertRegex(row["descriptor_sha256"], r"^[0-9a-f]{64}$")

    def test_module_list_is_reserved_installation_evidence(self):
        code, rows = client.run_json(EXECUTABLES["ameesh"], ["module", "list"], env=self.env)
        self.assertEqual(code, 0)
        row = {item["name"]: item for item in rows}[witness.NAME]
        self.assertEqual((row["ok"], row["source"], row["distribution"]),
                         (True, "external", "ameesh-contract-witness"))

    def test_module_help_runs_without_a_database_and_dispatch_needs_one(self):
        code, out, err = self.ameesh("module", witness.NAME, "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("usage: ameesh module contract-witness", out)
        # Ordinary dispatch opens the core connection: refused, never db=None.
        code, out, err = self.ameesh("module", witness.NAME, "whoami")
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out, "")

    def test_the_bare_profile_keeps_installed_entry_points_only(self):
        env = dict(self.env, AMEESH_MODULES="bare")
        code, document = client.run_json(EXECUTABLES["ameesh"], ["modules", "--no-canon",
                                                                 "--json"], env=env)
        self.assertEqual(code, 0)
        self.assertEqual([row["name"] for row in document["modules"]], [witness.NAME])

    def test_a_reserved_or_mismatched_entry_point_is_refused(self):
        site = install(self.tmp / "refused", entries={
            "list": "ameesh_contract_witness", "other-name": "ameesh_contract_witness"},
            name="ameesh-witness-refused")
        env = dict(self.env, PYTHONPATH=os.pathsep.join([str(site), self.env["PYTHONPATH"]]))
        code, document = client.run_json(EXECUTABLES["ameesh"], ["modules", "--no-canon",
                                                                 "--json"], env=env)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(row["name"] for row in document["refused"]),
                         ["list", "other-name"])
        self.assertIn(witness.NAME, [row["name"] for row in document["modules"]])

    def test_canon_selection_validates_the_witness_options(self):
        _, clone = publish(str(self.tmp / "canons"), "canon7665")
        for options, expected in (("{strict: true}", 0), ("{typo: 1}", 1)):
            with self.subTest(options):
                write(clone, "federation.yaml",
                      'federation: "0.1"\nid: w7665\nmembers: []\n'
                      "extensions:\n  ameesh:\n    projects:\n      atelier:\n"
                      "        modules:\n          contract-witness: %s\n" % options)
                commit_all(clone, options)
                code, out, err = self.ameesh("canon", "check", "--canon", clone,
                                             "--ref", "HEAD", "--host", "banc")
                self.assertEqual(code != 0, bool(expected), out + err)
                if expected:
                    self.assertIn("unknown contract-witness option(s): typo", out + err)


class ModuleInterfaceTest(unittest.TestCase):
    """`discover()` and the descriptor interface, on the real distribution."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site = install(Path(tmp.name))
        self.addCleanup(sys.modules.pop, "ameesh_contract_witness", None)
        patcher = mock.patch.object(sys, "path", [str(WITNESS)] + sys.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def discover(self, **kwargs):
        with installed_points(self.site):
            return registry.discover(builtin=False, **kwargs)

    def test_discover_signature_and_registry_record(self):
        spec = SURFACE["module"]
        self.assertEqual(str(inspect.signature(registry.discover)), spec["discover"])
        found = self.discover()
        for name in spec["registry"]:
            self.assertTrue(hasattr(found, name), name)
        self.assertEqual((found.names(), found.refused), ([witness.NAME], ()))
        module = found.get(witness.NAME)
        self.assertEqual(sorted(module.as_dict()), sorted(spec["record_fields"]))
        for name in spec["record_fields"]:
            self.assertTrue(hasattr(module, name), name)
        # Two representations of the same declared types.
        self.assertEqual(module.storage, witness.TYPES)
        self.assertEqual(module.as_dict()["storage"], {"types": list(witness.TYPES)})

    def test_descriptor_keys_and_options(self):
        self.assertEqual(sorted(witness.descriptor()), sorted(SURFACE["module"]["descriptor_keys"]))
        self.assertEqual(witness.descriptor({"strict": True})["schemas"],
                         {witness.KIND: {"strings": ["note", "reviewer"]}})
        with self.assertRaisesRegex(ValueError, "unknown contract-witness option"):
            witness.descriptor({"typo": 1})
        # The core refuses a descriptor that drops a key or an external type.
        for broken in ({"types": ["x"], "schemas": {}},
                       {"types": [], "schemas": {}, "rules": []}):
            with self.subTest(broken), \
                    mock.patch.object(witness, "descriptor", lambda options=None, b=broken: b), \
                    mock.patch.dict(sys.modules, {"ameesh_contract_witness": witness}):
                found = self.discover()
                self.assertEqual(found.names(), [])
                self.assertEqual([row["name"] for row in found.refused], [witness.NAME])

    def test_capabilities_are_the_published_names_and_are_checked(self):
        self.assertEqual(list(registry.CAPABILITIES), SURFACE["module"]["capabilities"])
        with mock.patch.object(witness, "CAPABILITIES", ("lifecycle", "workspaces")), \
                mock.patch.dict(sys.modules, {"ameesh_contract_witness": witness}):
            found = self.discover()
        self.assertEqual(found.names(), [])
        self.assertIn("declares workspaces without providing it", found.refused[0]["error"])

    def test_names_follow_the_published_pattern(self):
        pattern = re.compile(SURFACE["module"]["name_pattern"])
        for name, valid in (("a" * 64, True), ("a" * 65, False), ("w.it_-1", True),
                            ("-w", False), ("w/x", False), ("list", True)):
            with self.subTest(name):
                self.assertEqual(bool(pattern.match(name)), valid)
                entries = {name: "ameesh_contract_witness"}
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                site = install(Path(tmp.name), entries=entries, name="ameesh-witness-n")
                with mock.patch.object(witness, "NAME", name), \
                        mock.patch.dict(sys.modules, {"ameesh_contract_witness": witness}), \
                        installed_points(site):
                    found = registry.discover(builtin=False)
                admitted = valid and name not in SURFACE["module"]["reserved_names"]
                self.assertEqual(found.names(), [name] if admitted else [])

    def test_callbacks_keep_their_calling_convention(self):
        for name, signature in SURFACE["module"]["callbacks"].items():
            self.assertEqual(str(inspect.signature(getattr(witness, name))), signature)

    def test_store_named_operations_keep_their_signatures(self):
        for name, signature in sorted(SURFACE["module"]["store_operations"].items()):
            with self.subTest(name):
                method = getattr(ModuleStore, name)
                parameters = list(inspect.signature(method).parameters.values())[1:]
                self.assertEqual(str(inspect.Signature(parameters)), signature)


class HttpContractTest(unittest.TestCase):
    """Discovery and OpenAPI through the witness client, on a live server."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="w7665-http-")
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = type("Cfg", (), {"approve_url": "https://approve.example",
                               "threads_dir": os.path.join(tmp, "fils"),
                               "threads_root": os.path.join(tmp, "fils")})()

        def read(fn):
            raise db_mod.Unavailable("no database in this test")
        self.server = serve.make_server(serve.Context(cfg=cfg, human="human:owner",
                                                      organisation="Witness"),
                                        token=TOKEN, read=read, port=0, rate_per_minute=1000)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_discovery_then_openapi_without_a_token(self):
        discovery, document = client.discover_api(self.port)
        self.assertEqual(discovery["api"], "/api/v1")
        self.assertEqual(document, json.loads((ROOT / "docs/api/openapi-v1.json").read_text()))
        unauthenticated = {"/.well-known/ameesh", discovery["openapi"]}
        self.assertEqual(unauthenticated, set(SURFACE["http"]["unauthenticated"]))
        self.assertEqual(client.openapi_breaks(SURFACE["http"]["operations"],
                                               client.openapi_surface(document)), [])

    def test_every_other_documented_path_requires_the_token(self):
        for method, path in sorted(client.operations(serve.openapi())):
            if path in SURFACE["http"]["unauthenticated"]:
                continue
            concrete = re.sub(r"\{([^}]+)\}", lambda m: PARAMETERS.get(m.group(1), "1"), path)
            with self.subTest("%s %s" % (method, path)):
                status, headers, _ = client.http_request(
                    self.port, concrete, method=method,
                    headers={"Host": "127.0.0.1:%d" % self.port})
                self.assertEqual((status, headers.get("WWW-Authenticate")), (401, "Bearer"))

    def test_an_authenticated_read_answers_its_documented_schema(self):
        status, _, me = client.http_json(self.port, "/api/v1/me", token=TOKEN,
                                         headers={"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        expected = SURFACE["http"]["operations"]["GET /api/v1/me"]
        client.require_fields(me, expected["response_required"], "/api/v1/me")


class StoreAndViewsTest(PgTestCase):
    """Canon activation, ordinary dispatch, named store operations and views."""

    HOST = "h7665"
    PROJECT = "atelier7665"

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM project_modules")
        self.db.execute("DELETE FROM canon_state")
        self.db.execute("TRUNCATE authenticators, standing_approvals, standing_reservations, "
                        "authenticator_syncs RESTART IDENTITY CASCADE")
        self.site = install(Path(self.tmp) / "site")

    def witness_env(self, **extra):
        return self.env(PYTHONPATH=witness_path(self.site), **extra)

    def run_ok(self, *args, **extra):
        code, out, err = client.run(EXECUTABLES["ameesh"], args, env=self.witness_env(**extra))
        self.assertEqual(code, 0, out + err)
        return out

    def activate(self):
        _, root = publish(tempfile.mkdtemp(dir=self.tmp), "canon7665")
        write(root, "federation.yaml",
              'federation: "0.1"\nid: w7665\nmembers: []\n'
              "extensions:\n  ameesh:\n    projects:\n      %s:\n"
              "        modules: [contract-witness]\n" % self.PROJECT)
        commit_all(root, "witness")
        canon = dict(AMEESH_CANON=root, AMEESH_CANON_UNTRUSTED="1", AMEESH_HOST=self.HOST)
        self.run_ok("canon", "check", "--host", self.HOST, **canon)
        self.run_ok("canon", "sync", "--host", self.HOST, "--bootstrap-ref", "main", **canon)
        return canon

    def test_activation_dispatch_and_named_store_operations(self):
        canon = self.activate()
        active = json.loads(self.run_ok("modules", "--active", "--json", **canon))
        self.assertIn((self.PROJECT, witness.NAME),
                      [(row["project"], row["module"]) for row in active["active"]])
        out = self.run_ok("work", "add", "--title", "witness 7665", "--app", self.PROJECT,
                          "--new-project", "--type", witness.TYPES[0], "--actor", "human:test",
                          **canon)
        item = int(re.search(r"#(\d+)", out).group(1))
        whoami = json.loads(self.run_ok("module", witness.NAME, "whoami",
                                        AGENT_MAIL_NAME="witness-actor", **canon))
        self.assertEqual((whoami["command"], whoami["connected"]), ("whoami", True))
        self.assertTrue(whoami["actor"])
        store = ModuleStore(self.db)
        self.assertEqual(witness.audit(store, self.PROJECT), [])
        self.assertEqual([f["rule"] for f in witness.audit(store, "elsewhere7665")],
                         ["witness-not-enabled", "witness-type-not-admitted"])
        recorded, latest, history = witness.record_and_read(store, item, actor="human:test")
        self.assertEqual((latest["id"], latest["payload"], latest["actor"]),
                         (recorded["id"], {"note": "seen"}, "human:test"))
        self.assertEqual([row["id"] for row in history], [recorded["id"]])
        self.assertIn("at_ts", history[0])

    def test_named_views_keep_their_admitted_columns(self):
        self.register("witness7665", "claude")
        fleet = client.read_view(self.db.query, "agent_mesh_overview",
                                 list(SURFACE["views"]["agent_mesh_overview"]))
        self.assertIn("witness7665", [row["name"] for row in fleet])
        for view, columns in sorted(SURFACE["views"].items()):
            with self.subTest(view):
                rows = client.read_view(self.db.query, view, list(columns))
                self.assertIsInstance(rows, list)
                described = {row["column_name"]: row["data_type"] for row in self.db.query(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s", (view,))}
                self.assertEqual({name: described.get(name) for name in columns}, columns)
        with self.assertRaises(ValueError):
            client.view_select("agent_mesh_overview", ["*"])


class CoverageTableTest(unittest.TestCase):
    """docs/api/module-contract-v1-coverage.md: every element, a live test."""

    @classmethod
    def elements(cls):
        names = {"compatibility"}
        names.update("cli:" + name for name in SURFACE["cli"])
        names.update("module:" + name for name in SURFACE["module"])
        names.update({"http:discovery", "http:operations", "http:authentication",
                      "http:idempotency", "http:events", "http:schemas"})
        names.update({"runner:agents-file", "runner:turn-context", "runner:turn-observation"})
        names.update("view:" + name for name in SURFACE["views"])
        names.update({"fixture", "stable-pin"})
        return names

    @classmethod
    def rows(cls):
        rows = {}
        for line in COVERAGE.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0].startswith("`"):
                rows[cells[0].strip("`")] = re.findall(r"`(tests\.test_[\w.]+)`", cells[1])
        return rows

    def test_every_contract_element_has_one_row(self):
        self.assertEqual(set(self.rows()), self.elements())

    def test_every_cited_test_exists(self):
        for element, names in sorted(self.rows().items()):
            with self.subTest(element):
                self.assertTrue(names or element in ("fixture", "stable-pin"), element)
                for name in names:
                    loader = unittest.TestLoader()
                    suite = loader.loadTestsFromName(name)
                    self.assertGreater(suite.countTestCases(), 0, name)
                    self.assertFalse(loader.errors, loader.errors)


if __name__ == "__main__":
    unittest.main()
