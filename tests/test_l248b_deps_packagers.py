# SPDX-License-Identifier: AGPL-3.0-only
"""L248b : packagers get a real local tree, without network or real npm."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest import mock

from ameesh import deps, deps_packagers, menage
from . import test_l231_deps as l231


class PackagerTests(l231._Base):
    def package(self, **changes):
        return l231._package(os.path.join(self.base, "worktree"),
                             manifest=dict(l231.MANIFEST, **changes))

    def cli(self, *args):
        output = io.StringIO()
        with mock.patch.object(deps, "resolve_cache_root", return_value=self.cache), \
                contextlib.redirect_stdout(output):
            code = deps.main(list(args))
        return code, output.getvalue()

    def test_declared_packagers_refuse_link_without_touching_a_local_tree(self):
        for field in deps_packagers.DEPENDENCIES:
            for name in deps_packagers.PACKAGERS:
                with self.subTest(field=field, name=name):
                    pkg = self.package(**{field: {name: "^1"}})
                    local = Path(pkg) / "node_modules"
                    local.mkdir(exist_ok=True)
                    sentinel = local / "keep.txt"
                    sentinel.write_text("keep")
                    installer = l231.FakeInstaller()
                    result = deps.link_package(pkg, self.cache, replace=True, installer=installer)
                    self.assertEqual(result["status"], "packager-refused")
                    self.assertIn(name, result["packagers"])
                    self.assertIn("deps unlink", result["reason"])
                    self.assertEqual(installer.calls, [])
                    self.assertEqual(sentinel.read_text(), "keep")
                    self.assertFalse(local.is_symlink())
        self.assertFalse(os.path.exists(deps.nm_root(self.cache)))

    def test_script_commands_and_dependency_aliases_are_detected(self):
        for command, expected in (("npx electron-builder --linux", "electron-builder"),
                                  ("node x.js && pkg app.js", "pkg"),
                                  ("./node_modules/.bin/nexe app.js", "nexe"),
                                  ("npx @electron/packager@19 .", "@electron/packager"),
                                  ("ncc build app.js", "@vercel/ncc")):
            with self.subTest(command=command):
                pkg = self.package(scripts={"dist": command})
                self.assertEqual(deps_packagers.packagers(pkg), [expected])
        pkg = self.package(devDependencies={"builder": "npm:electron-builder@26"})
        self.assertEqual(deps_packagers.packagers(pkg), ["electron-builder"])
        pkg = self.package(devDependencies={"bundle": "npm:@vercel/ncc@0.38"})
        self.assertEqual(deps_packagers.packagers(pkg), ["@vercel/ncc"])
        pkg = self.package(scripts={"metadata": "npm pkg set version=1"})
        self.assertEqual(deps_packagers.packagers(pkg), [])

    def test_force_is_explicit_and_status_warns_on_an_existing_link(self):
        pkg = self.package(devDependencies={"electron-builder": "26"})
        installer = l231.FakeInstaller()
        result = deps.link_tree(pkg, self.cache, installer=installer, force=True)[0]
        self.assertEqual(result["status"], "linked")
        target = os.readlink(os.path.join(pkg, "node_modules"))
        before = list(installer.calls)
        refused = deps.link_package(pkg, self.cache, installer=installer)
        self.assertEqual(refused["status"], "packager-refused")
        self.assertEqual(os.readlink(os.path.join(pkg, "node_modules")), target)
        self.assertEqual(installer.calls, before)
        code, text = self.cli("status", pkg)
        self.assertEqual(code, 0)
        self.assertIn("WARNING", text)
        self.assertIn("electron-builder", text)
        code, text = self.cli("status", pkg, "--json")
        item = json.loads(text)["items"][0]
        self.assertIn("deps unlink", item["warning"])
        self.assertEqual(item["packagers"], ["electron-builder"])
        code, text = self.cli("link", pkg, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["items"][0]["status"], "packager-refused")
        code, text = self.cli("link", pkg, "--force", "--no-fill", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["items"][0]["status"], "linked")

    def test_root_and_sibling_tooling_guard_the_app_without_their_own_lockfile(self):
        pkg = self.package()
        root = Path(pkg).parent
        l231._git(str(root), "init", "-q", "-b", "main")
        for directory in (root, root / "tools"):
            with self.subTest(directory=directory):
                directory.mkdir(exist_ok=True)
                manifest = directory / "package.json"
                manifest.write_text(json.dumps({"scripts": {
                    "dist": "electron-builder --projectDir app"}}))
                installer = l231.FakeInstaller()
                refused = deps.link_package(pkg, self.cache, installer=installer)
                self.assertEqual(refused["status"], "packager-refused")
                self.assertEqual(installer.calls, [])
                deps.link_package(pkg, self.cache, installer=installer, force=True)
                self.assertIn("electron-builder", deps._status(pkg, self.cache)[0]["warning"])
                done = deps.link_ready(pkg, self.cache)
                self.assertEqual(done[0]["status"], "unlinked")
                self.assertFalse(Path(pkg, "node_modules").is_symlink())
                manifest.unlink()

    def test_config_only_apps_refuse_links_and_warn_on_forced_links(self):
        pkg = self.package()
        manifest_path = Path(pkg, "package.json")
        for config in ("build", *deps_packagers.BUILDER_CONFIGS):
            with self.subTest(config=config):
                if config == "build":
                    manifest_path.write_text(json.dumps(dict(l231.MANIFEST, build={})))
                else:
                    Path(pkg, config).write_text("{}")
                self.assertEqual(deps.link_package(pkg, self.cache)["status"],
                                 "packager-refused")
                deps.link_package(pkg, self.cache, installer=l231.FakeInstaller(), force=True)
                self.assertIn("deps unlink", deps._status(pkg, self.cache)[0]["warning"])
                Path(pkg, "node_modules").unlink()
                if config == "build":
                    manifest_path.write_text(json.dumps(l231.MANIFEST))
                else:
                    Path(pkg, config).unlink()

    def test_repository_detection_stays_out_of_nested_repositories_and_symlinks(self):
        pkg = self.package()
        root = Path(pkg).parent
        l231._git(str(root), "init", "-q", "-b", "main")
        nested = root / "nested"
        nested.mkdir()
        l231._git(str(nested), "init", "-q", "-b", "main")
        Path(nested, "package.json").write_text('{"build": {}}')
        outside = Path(self.base, "outside")
        outside.mkdir()
        Path(outside, "package.json").write_text('{"build": {}}')
        (root / "alias").symlink_to(outside, target_is_directory=True)
        self.assertEqual(deps._packagers(pkg), [])

    def test_turn_start_never_links_a_packager_even_with_a_ready_entry(self):
        pkg = self.package(devDependencies={"pkg": "5"})
        l231._git(os.path.dirname(pkg), "init", "-q", "-b", "main")
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller(), force=True)
        os.unlink(os.path.join(pkg, "node_modules"))
        self.assertEqual(deps.link_ready(pkg, self.cache), [])
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))

    def test_turn_start_removes_legacy_shared_link_and_keeps_local_and_external_trees(self):
        pkg = self.package()
        l231._git(os.path.dirname(pkg), "init", "-q", "-b", "main")
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller())
        target = Path(os.path.realpath(os.path.join(pkg, "node_modules")))
        # A link created before the packager declaration/corrective guard.
        Path(pkg, "package.json").write_text(json.dumps(dict(l231.MANIFEST, build={})))
        done = deps.link_ready(pkg, self.cache)
        self.assertEqual(done[0]["status"], "unlinked")
        self.assertIn("deps unlink", done[0]["reason"])
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
        self.assertTrue((target / "left-pad/lib/index.js").is_file())
        self.assertEqual(deps.link_ready(pkg, self.cache), [])
        local = Path(pkg, "node_modules")
        local.mkdir()
        (local / "keep").write_text("local")
        self.assertEqual(deps.link_ready(pkg, self.cache), [])
        self.assertEqual((local / "keep").read_text(), "local")
        (local / "keep").unlink()
        local.rmdir()
        outside = Path(self.base, "external-modules")
        outside.mkdir()
        local.symlink_to(outside, target_is_directory=True)
        self.assertEqual(deps.link_ready(pkg, self.cache), [])
        self.assertTrue(local.is_symlink())

    def test_turn_begin_records_legacy_link_removal(self):
        pkg = self.package(build={})
        l231._git(os.path.dirname(pkg), "init", "-q", "-b", "main")
        cfg = l231._Cfg(os.path.join(self.base, "state"))
        policy = menage.Policy(worktrees=False)
        cache = menage.cache_root(cfg, policy)
        deps.link_package(pkg, cache, installer=l231.FakeInstaller(), force=True)
        turn = menage.Turn.begin(cfg, policy, "dev7", pkg,
                                 new_session=False, base_env={"PATH": ""})
        try:
            self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
            records = [item for item in turn.entries if item["kind"] == "deps"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["action"], "unlinked")
            self.assertIn("deps unlink", records[0]["detail"])
        finally:
            turn.lock.release()

    def test_unlink_uses_lockfile_locally_and_leaves_other_cache_users_intact(self):
        pkg = self.package(devDependencies={"electron-builder": "26"})
        other = l231._package(os.path.join(self.base, "other"),
                             manifest=dict(l231.MANIFEST, devDependencies={"electron-builder": "26"}))
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller(), force=True)
        deps.link_package(other, self.cache, force=True)
        lock_before = Path(pkg, deps.LOCKFILE).read_bytes()
        manifest_before = Path(pkg, "package.json").read_bytes()
        target = os.path.realpath(os.path.join(pkg, "node_modules"))
        installer = l231.FakeInstaller()
        original_local = deps.make_local
        with mock.patch.object(deps, "make_local",
                               side_effect=lambda p: original_local(p, installer=installer)):
            code, text = self.cli("unlink", pkg)
        self.assertEqual(code, 0)
        self.assertIn("local node_modules installed with npm ci", text)
        self.assertEqual(installer.calls, [pkg])
        self.assertFalse(Path(pkg, "node_modules").is_symlink())
        self.assertTrue(Path(pkg, "node_modules/left-pad/lib/index.js").is_file())
        self.assertEqual(Path(pkg, deps.LOCKFILE).read_bytes(), lock_before)
        self.assertEqual(Path(pkg, "package.json").read_bytes(), manifest_before)
        self.assertTrue(os.path.islink(os.path.join(other, "node_modules")))
        self.assertTrue(Path(target, "left-pad/lib/index.js").is_file())
        self.assertNotIn("warning", deps._status(pkg, self.cache)[0])

    def test_unlink_failure_is_not_reported_as_success(self):
        pkg = self.package(devDependencies={"pkg": "5"})
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller(), force=True)
        original_local = deps.make_local
        with mock.patch.object(deps, "make_local", side_effect=lambda p: original_local(
                p, installer=l231.FakeInstaller(fail_in=(pkg,)))):
            code, text = self.cli("unlink", pkg, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["items"][0]["status"], "local-failed")
        self.assertFalse(Path(pkg, "node_modules").exists())

    def test_unlink_cli_runs_real_npm_ci_without_the_wrapper(self):
        pkg = self.package(devDependencies={"electron-builder": "26"})
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller(), force=True)
        fake = Path(self.base, "npm-real")
        fake.write_text("#!" + sys.executable + "\n" + '''import json
from pathlib import Path
import sys
assert not Path('node_modules').is_symlink()
Path('npm-args.json').write_text(json.dumps(sys.argv[1:]))
Path('npm-lock.json').write_bytes(Path('package-lock.json').read_bytes())
Path('node_modules/transitive').mkdir(parents=True)
Path('node_modules/transitive/index.js').write_text('module.exports = 1')
''')
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"AMEESH_REAL_NPM": str(fake)}):
            code, text = self.cli("unlink", pkg, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["items"][0]["status"], "local")
        self.assertEqual(json.loads(Path(pkg, "npm-args.json").read_text()),
                         list(deps.INSTALL_ARGS + deps.FILL_OPTIONS))
        self.assertEqual(Path(pkg, "npm-lock.json").read_bytes(),
                         Path(pkg, deps.LOCKFILE).read_bytes())
        self.assertTrue(Path(pkg, "node_modules/transitive/index.js").is_file())

    def test_unlink_requires_a_lockfile_before_removing_the_link(self):
        pkg = self.package()
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller())
        Path(pkg, deps.LOCKFILE).unlink()
        result = deps.make_local(pkg, installer=l231.FakeInstaller())
        self.assertEqual(result["status"], "no-lockfile")
        self.assertTrue(Path(pkg, "node_modules").is_symlink())
        self.assertEqual(self.cli("unlink", pkg)[0], 1)

    def test_unlink_mixed_paths_reports_missing_lock_and_installs_valid_package(self):
        pkg = self.package()
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller())
        Path(pkg, deps.LOCKFILE).unlink()
        good = l231._package(os.path.join(self.base, "good"))
        installer = l231.FakeInstaller()
        original = deps.make_local
        for command in ("unlink", "local"):
            with self.subTest(command=command), mock.patch.object(
                    deps, "make_local", side_effect=lambda p: original(p, installer=installer)):
                code, output = self.cli(command, pkg, good, "--json")
                self.assertEqual(code, 1)
                items = json.loads(output)["items"]
                self.assertEqual(items[0]["status"], "no-lockfile")
                self.assertEqual(items[1]["status"],
                                 "local" if command == "unlink" else "local-kept")
                self.assertTrue(Path(pkg, "node_modules").is_symlink())
        self.assertEqual(installer.calls, [good])
        code, output = self.cli("unlink", str(Path(pkg).parent), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["items"][0]["status"], "no-lockfile")

    def test_invalid_manifest_refuses_link_and_status_explains_why(self):
        pkg = self.package()
        Path(pkg, "package.json").write_text("{broken")
        installer = l231.FakeInstaller()
        self.assertEqual(deps.link_package(pkg, self.cache, installer=installer)["status"],
                         "package-invalid")
        self.assertEqual(installer.calls, [])
        self.assertIn("invalid package.json", deps._status(pkg, self.cache)[0]["warning"])
        for manifest in ({"scripts": []}, {"devDependencies": []}, {"scripts": {"x": 42}}):
            with self.subTest(manifest=manifest):
                Path(pkg, "package.json").write_text(json.dumps(manifest))
                self.assertEqual(deps.link_package(pkg, self.cache, installer=installer)["status"],
                                 "package-invalid")


class ShimTests(l231._Base):
    _npm = l231.ReviewAndShimTests._npm

    def setUp(self):
        super().setUp()
        self.fake_bin = os.path.join(self.base, "bin")
        os.makedirs(self.fake_bin)
        npm = Path(self.fake_bin, "npm")
        npm.write_text("#!/bin/sh\n")
        npm.chmod(0o755)

    def test_packager_install_passes_to_real_npm_after_unlink(self):
        lock = json.loads(json.dumps(l231.LOCK))
        lock["packages"][""]["devDependencies"] = {"electron-builder": "26"}
        lock["packages"]["node_modules/electron-builder"] = {"version": "26"}
        pkg = l231._package(os.path.join(self.base, "worktree"),
                            lock=lock,
                            manifest=dict(l231.MANIFEST, devDependencies={"electron-builder": "26"}))
        self.assertTrue(deps.lock_in_sync(pkg))
        installer = l231.FakeInstaller()
        deps.link_package(pkg, self.cache, installer=installer, force=True)
        target = os.path.realpath(os.path.join(pkg, "node_modules"))
        for args in (("ci",), ("install", "--no-audit"), ("ci", "--omit=dev")):
            with self.subTest(args=args):
                if not os.path.lexists(os.path.join(pkg, "node_modules")):
                    os.symlink(target, os.path.join(pkg, "node_modules"))
                code, calls = self._npm(*args, cwd=pkg)
                self.assertEqual((code, calls), (0, [list(args)]))
                self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
                self.assertTrue(Path(target, "left-pad/lib/index.js").is_file())
        review = l231._package(os.path.join(self.base, "acme-review-release"),
                               manifest=dict(l231.MANIFEST, devDependencies={"pkg": "5"}))
        self.assertEqual(self._npm("ci", cwd=review), (1, []))

    def test_root_config_prevents_wrapper_from_relinking_the_app(self):
        pkg = l231._package(os.path.join(self.base, "worktree"))
        root = Path(pkg).parent
        l231._git(str(root), "init", "-q", "-b", "main")
        deps.link_package(pkg, self.cache, installer=l231.FakeInstaller())
        Path(root, "package.json").write_text('{"build": {}}')
        self.assertEqual(self._npm("ci", cwd=pkg), (0, [["ci"]]))
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
