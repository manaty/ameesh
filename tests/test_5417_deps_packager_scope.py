# SPDX-License-Identifier: AGPL-3.0-only
"""#5417 : the packaging guard is per package, not per repository.

A desktop Electron app in a monorepo must keep a local `node_modules`; the
backend, website, mobile app and shared libraries around it link to the
shared cache as before.
"""
import json
import os
from pathlib import Path

from ameesh import deps, deps_packagers

from . import test_l231_deps as l231


class ScopeTests(l231._Base):
    def repo(self, name="repo"):
        root = Path(self.base, name)
        root.mkdir()
        l231._git(str(root), "init", "-q", "-b", "main")
        return root

    def package(self, top, name="app", **manifest):
        scripts = dict(l231.MANIFEST.get("scripts", {}))
        scripts.update(manifest.pop("scripts", {}))
        return l231._package(str(top), name=name,
                             manifest=dict(l231.MANIFEST, **manifest, scripts=scripts))

    def tooling(self, top, scripts, name="package.json"):
        """A manifest without a lockfile: root/sibling tooling."""
        top = Path(top)
        top.mkdir(parents=True, exist_ok=True)
        Path(top, name).write_text(json.dumps({"scripts": scripts}))
        return str(top)

    def npm(self, args, cwd):
        calls = []

        def execute(path, argv, env):
            calls.append(argv[1:])
            return 0
        code = deps.npm_shim(list(args),
                             env={"PATH": self.fake_bin, "AMEESH_CACHE_DIR": self.cache},
                             cwd=cwd, execute=execute,
                             installer=l231.FakeInstaller())
        return code, calls

    def setUp(self):
        super().setUp()
        self.fake_bin = os.path.join(self.base, "bin")
        os.makedirs(self.fake_bin)
        npm = Path(self.fake_bin, "npm")
        npm.write_text("#!/bin/sh\n")
        npm.chmod(0o755)

    def test_a_desktop_app_does_not_guard_the_rest_of_the_monorepo(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "npm run build && electron-builder"})
        others = [self.package(root, "backend"),
                  self.package(root, "website"),
                  self.package(root, "mobile"),
                  self.package(root / "shared", "lib")]
        self.assertEqual(deps._packagers(desktop), ["electron-builder"])
        for pkg in others:
            with self.subTest(package=os.path.relpath(pkg, root)):
                self.assertEqual(deps._packagers(pkg), [])
        installer = l231.FakeInstaller()
        done = {item["package"]: item for item in
                deps.link_tree(str(root), self.cache, installer=installer)}
        self.assertEqual(done[desktop]["status"], "packager-refused")
        self.assertIn("deps unlink", done[desktop]["reason"])
        for pkg in others:
            with self.subTest(package=os.path.relpath(pkg, root)):
                self.assertEqual(done[pkg]["status"], "linked")
                self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))
        self.assertEqual(len(installer.calls), len(others))

    def test_turn_start_removes_the_link_only_from_the_packaging_package(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "electron-builder"})
        backend = self.package(root, "backend")
        shared = self.package(root / "shared", "lib")
        installer = l231.FakeInstaller()
        for pkg in (desktop, backend, shared):
            deps.link_package(pkg, self.cache, installer=installer, force=True)
        done = deps.link_ready(str(root), self.cache)
        self.assertEqual([(item["package"], item["status"]) for item in done],
                         [(desktop, "unlinked")])
        self.assertFalse(os.path.lexists(os.path.join(desktop, "node_modules")))
        for pkg in (backend, shared):
            self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))

    def test_turn_start_links_only_the_dev_packages_with_a_ready_entry(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "electron-builder"})
        backend = self.package(root, "backend")
        shared = self.package(root / "shared", "lib")
        installer = l231.FakeInstaller()
        for pkg in (desktop, backend, shared):
            deps.link_package(pkg, self.cache, installer=installer, force=True)
            os.unlink(os.path.join(pkg, "node_modules"))
        done = deps.link_ready(str(root), self.cache)
        self.assertEqual({item["package"]: item["status"] for item in done},
                         {backend: "linked", shared: "linked"})
        self.assertFalse(os.path.lexists(os.path.join(desktop, "node_modules")))
        self.assertTrue(os.path.islink(os.path.join(backend, "node_modules")))
        self.assertTrue(os.path.islink(os.path.join(shared, "node_modules")))

    def test_status_warns_only_about_the_packaging_package(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "electron-builder"})
        backend = self.package(root, "backend")
        installer = l231.FakeInstaller()
        for pkg in (desktop, backend):
            deps.link_package(pkg, self.cache, installer=installer, force=True)
        items = {item["package"]: item for item in deps._status(str(root), self.cache)}
        self.assertEqual(items[desktop]["packagers"], ["electron-builder"])
        self.assertIn("deps unlink", items[desktop]["warning"])
        self.assertNotIn("packagers", items[backend])
        self.assertNotIn("warning", items[backend])

    def test_npm_ci_links_the_siblings_and_runs_locally_in_the_desktop(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "electron-builder"})
        backend = self.package(root, "backend")
        installer = l231.FakeInstaller()
        code, calls = self.npm(("ci",), cwd=backend)
        self.assertEqual((code, calls), (0, []))
        self.assertTrue(os.path.islink(os.path.join(backend, "node_modules")))
        code, calls = self.npm(("ci",), cwd=desktop)
        self.assertEqual((code, calls), (0, [["ci"]]))
        self.assertFalse(os.path.lexists(os.path.join(desktop, "node_modules")))
        self.assertEqual(installer.calls, [])

    def test_project_dir_targets_only_the_named_app(self):
        for index, script in enumerate(("electron-builder --projectDir app",
                                        "electron-builder --project-dir app",
                                        "electron-builder --project app",
                                        "npx --yes electron-builder --projectDir=app",
                                        "npx electron-builder --project=app",
                                        "electron-builder -c.directories.app=app",
                                        "electron-builder --config.directories.app app")):
            with self.subTest(script=script):
                root = self.repo("repo-project-dir-%d" % index)
                app = self.package(root, "app")
                backend = self.package(root, "backend")
                self.tooling(root / "tools", {"dist": script})
                self.assertEqual(deps._packagers(app), ["electron-builder"])
                self.assertEqual(deps._packagers(backend), [])
                self.assertEqual(deps.link_package(app, self.cache,
                                                   installer=l231.FakeInstaller())["status"],
                                 "packager-refused")
                self.assertEqual(deps.link_package(backend, self.cache,
                                                   installer=l231.FakeInstaller())["status"],
                                 "linked")

    def test_project_dir_relative_to_the_tooling_package(self):
        root = self.repo()
        desktop = self.package(root, "desktop")
        backend = self.package(root, "backend")
        self.tooling(root / "tools", {"dist": "electron-builder --projectDir ../desktop"})
        self.assertEqual(deps._packagers(desktop), ["electron-builder"])
        self.assertEqual(deps._packagers(backend), [])

    def test_directories_app_in_the_build_key_and_in_the_yaml_config(self):
        for config in ("manifest", "yaml", "json5"):
            with self.subTest(config=config):
                root = self.repo("repo-" + config)
                app = self.package(root, "app")
                backend = self.package(root, "backend")
                Path(root, "package.json").write_text(json.dumps({}))
                if config == "manifest":
                    Path(root, "package.json").write_text(json.dumps(
                        {"build": {"directories": {"app": "app"}}}))
                elif config == "yaml":
                    Path(root, "electron-builder.yml").write_text(
                        "appId: test\ndirectories:\n  output: release\n  app: app\n")
                else:
                    Path(root, "electron-builder.json5").write_text(
                        '{\n  // comment\n  "directories": {"output": "release", "app": "app"}\n}\n')
                self.assertEqual(deps._packagers(app), ["electron-builder"])
                self.assertEqual(deps._packagers(backend), [])

    def test_a_packaging_workspace_member_guards_the_root_that_hoists_its_tree(self):
        root = l231._package(str(self.base), name="workspace",
                             manifest=dict(l231.MANIFEST,
                                           workspaces=["desktop", "backend"]))
        l231._git(root, "init", "-q", "-b", "main")
        desktop = Path(root, "desktop")
        desktop.mkdir()
        Path(desktop, "package.json").write_text(json.dumps({
            "name": "desktop",
            "devDependencies": {"electron-builder": "26"},
            "scripts": {"dist": "electron-builder"}}))
        backend = Path(root, "backend")
        backend.mkdir()
        Path(backend, "package.json").write_text(json.dumps({"name": "backend"}))
        self.assertEqual(deps.find_packages(root), [root])
        self.assertEqual(deps._packagers(root), ["electron-builder"])
        installer = l231.FakeInstaller()
        self.assertEqual(deps.link_package(root, self.cache, installer=installer)["status"],
                         "packager-refused")
        self.assertEqual(installer.calls, [])
        self.assertFalse(os.path.lexists(os.path.join(root, "node_modules")))

    def test_a_root_packaging_command_still_guards_the_tree_below_it(self):
        root = self.repo()
        app = self.package(root, "app")
        backend = self.package(root, "backend")
        self.tooling(root, {"dist": "electron-builder"})
        for pkg in (app, backend):
            with self.subTest(package=os.path.relpath(pkg, root)):
                self.assertEqual(deps._packagers(pkg), ["electron-builder"])
                self.assertEqual(deps.link_package(
                    pkg, self.cache, installer=l231.FakeInstaller())["status"],
                    "packager-refused")
        Path(root, "package.json").unlink()
        for pkg in (app, backend):
            self.assertEqual(deps.link_package(
                pkg, self.cache, installer=l231.FakeInstaller())["status"], "linked")

    def test_an_invalid_sibling_does_not_block_but_an_invalid_ancestor_does(self):
        root = self.repo()
        backend = self.package(root, "backend")
        self.tooling(root / "tools", {"dist": "electron-builder"})
        Path(root / "tools", "package.json").write_text("{broken")
        self.assertEqual(deps._packagers(backend), [])
        self.assertEqual(deps.link_package(backend, self.cache,
                                           installer=l231.FakeInstaller())["status"],
                         "linked")
        Path(root, "package.json").write_text("{broken")
        installer = l231.FakeInstaller()
        self.assertEqual(deps.link_package(backend, self.cache, installer=installer)["status"],
                         "package-invalid")
        self.assertEqual(installer.calls, [])

    def test_targets_exposes_the_resolved_scope_without_running_anything(self):
        root = self.repo()
        desktop = self.package(root, "desktop",
                               devDependencies={"electron-builder": "26"},
                               scripts={"dist": "electron-builder"})
        self.assertEqual(deps_packagers.targets(desktop), {"electron-builder": [None]})
        tools = self.tooling(root / "tools", {
            "dist": "electron-builder --projectDir app",
            "bundle": "ncc build src/cli.ts -o dist"})
        self.assertEqual(deps_packagers.targets(tools),
                         {"electron-builder": ["app"], "@vercel/ncc": [None]})
        app = self.tooling(self.base, {}, name="package.json")
        Path(app, "package.json").write_text(json.dumps(
            {"build": {"directories": {"app": "web"}}}))
        self.assertEqual(deps_packagers.targets(app),
                         {"electron-builder": ["web"]})
