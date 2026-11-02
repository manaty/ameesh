# SPDX-License-Identifier: AGPL-3.0-only
"""Release advisories are read-only and scoped to shared-cache links."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from ameesh import deps
from . import test_l231_deps as l231


class ReleaseBundleWarningTests(l231._Base):
    def status(self, pkg, *, as_json=True):
        output = io.StringIO()
        args = ["status", pkg] + (["--json"] if as_json else [])
        with mock.patch.object(deps, "resolve_cache_root", return_value=self.cache), \
                contextlib.redirect_stdout(output):
            self.assertEqual(deps.main(args), 0)
        return json.loads(output.getvalue())["items"][0] if as_json else output.getvalue()

    def package(self, manifest):
        return l231._package(self.base, manifest=dict(l231.MANIFEST, **manifest))

    def shared_link(self, pkg):
        link = Path(pkg, "node_modules")
        # A stale cache entry must still warn; no cache fill or npm is needed.
        link.symlink_to(Path(self.cache, "nm", "old-key", "app", "node_modules"))
        return link

    def test_warns_for_expo_and_next_without_modifying_links(self):
        for manifest, command in (({"dependencies": {"expo": "54"}}, "expo export"),
                                  ({"devDependencies": {"next": "16"}}, "next build"),
                                  ({"dependencies": {"web": "npm:next@16"}}, "next build"),
                                  ({"scripts": {"bundle": "npx --yes expo@54 export"}}, "expo export"),
                                  ({"scripts": {"build": "npm exec -- next build"}}, "next build")):
            with self.subTest(manifest=manifest):
                pkg = self.package(manifest)
                link = self.shared_link(pkg)
                target = os.readlink(link)
                item = self.status(pkg)
                self.assertIn(command, item["warning"])
                self.assertIn("ameesh deps unlink", item["warning"])
                self.assertNotIn("packagers", item)
                text = self.status(pkg, as_json=False)
                self.assertIn("WARNING", text)
                self.assertIn(command, text)
                self.assertEqual(os.readlink(link), target)
                link.unlink()

    def test_local_absent_and_external_links_do_not_warn(self):
        pkg = self.package({"dependencies": {"expo": "54", "next": "16"}})
        link = Path(pkg, "node_modules")
        self.assertNotIn("warning", self.status(pkg))
        link.mkdir()
        self.assertNotIn("warning", self.status(pkg))
        link.rmdir()
        link.symlink_to(Path(self.base, "external-node_modules"))
        self.assertNotIn("warning", self.status(pkg))

    def test_other_packages_and_command_arguments_do_not_warn(self):
        pkg = self.package({"scripts": {"x": "echo expo export; echo next build"}})
        self.shared_link(pkg)
        self.assertNotIn("warning", self.status(pkg))

    def test_packager_warning_is_preserved_alongside_bundle_advisory(self):
        pkg = self.package({"devDependencies": {"electron-builder": "26", "next": "16"}})
        self.shared_link(pkg)
        warning = self.status(pkg)["warning"]
        self.assertIn("electron-builder", warning)
        self.assertIn("next build", warning)
