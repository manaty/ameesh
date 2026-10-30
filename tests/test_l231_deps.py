# SPDX-License-Identifier: AGPL-3.0-only
"""node_modules shared per lockfile (lot #248, L231): cache key, fill once
under a lock, read-only entry, symlink, git exclude, local fallback, the npm
wrapper of the turns (review worktrees refused), garbage collection, and the
housekeeping wiring. Offline: fake installers and fake trees, no npm."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import deps, housekeeping as hk, menage

from . import support  # noqa: F401  (isolated environment, L142)

# the real ones, before _Base patches them
toolchain = deps.toolchain
#: a fake npm's first line: it installs into `--prefix` like the real one
PREFIX = 'd=.; p=; for a in "$@"; do [ "$p" = --prefix ] && d="$a"; p="$a"; done; cd "$d"'

LOCK = {"name": "app", "lockfileVersion": 3,
        "packages": {"": {"name": "app", "dependencies": {"left-pad": "^1.0.0"}},
                     "node_modules/left-pad": {"version": "1.3.0"}}}
MANIFEST = {"name": "app", "dependencies": {"left-pad": "^1.0.0"},
            "scripts": {"postinstall": "node scripts/patch.cjs", "test": "jest"}}


def _package(top: str, name: str = "app", lock: dict | None = None,
             manifest: dict | None = None) -> str:
    pkg = os.path.join(top, name)
    os.makedirs(os.path.join(pkg, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(pkg, "patches"), exist_ok=True)
    os.makedirs(os.path.join(pkg, "src"), exist_ok=True)
    with open(os.path.join(pkg, "package.json"), "w") as fh:
        json.dump(manifest or MANIFEST, fh)
    with open(os.path.join(pkg, deps.LOCKFILE), "w") as fh:
        json.dump(lock or LOCK, fh)
    for rel, text in (("scripts/patch.cjs", "// patch"), ("patches/x.patch", "--- a"),
                      ("src/index.js", "module.exports = 1")):
        with open(os.path.join(pkg, rel), "w") as fh:
            fh.write(text)
    return pkg


class FakeInstaller:
    """Writes a small node_modules (a package, a .bin symlink) where it runs,
    and remembers where; fails in the directories listed in `fail_in`."""

    def __init__(self, fail_in=()):
        self.calls: list[str] = []
        self.fail_in = tuple(fail_in)

    def __call__(self, directory: str, env=None, *, lock_fd=None) -> None:
        self.calls.append(directory)
        if any(directory.startswith(f) for f in self.fail_in):
            raise deps.FillError("npm ci failed (1): fake")
        # what the lifecycle script needs was copied beside the lockfile
        assert os.path.isfile(os.path.join(directory, "scripts", "patch.cjs"))
        assert os.path.isfile(os.path.join(directory, "patches", "x.patch"))
        modules = os.path.join(directory, deps.NODE_MODULES)
        os.makedirs(os.path.join(modules, "left-pad", "lib"))
        os.makedirs(os.path.join(modules, ".bin"))
        with open(os.path.join(modules, "left-pad", "lib", "index.js"), "w") as fh:
            fh.write("module.exports = s => s")
        os.symlink("../left-pad/lib/index.js", os.path.join(modules, ".bin", "left-pad"))


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          check=True, env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                               GIT_AUTHOR_EMAIL="t@t",
                                               GIT_COMMITTER_NAME="t",
                                               GIT_COMMITTER_EMAIL="t@t")).stdout


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.cache = os.path.join(self.base, "cache")
        os.makedirs(self.cache)
        self.patches = [mock.patch.object(
                            deps, "toolchain", side_effect=lambda pkg_dir=None, env=None: {
                                "node": "v26", "bindir": "", "npm": None,
                                "npm_version": "11"}),
                        mock.patch.object(deps, "_say")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        # entries are read-only: unseal before the temporary directory goes
        deps._unseal(self.base)
        self._tmp.cleanup()


class CacheKeyTests(_Base):
    def test_key_follows_what_decides_the_install(self):
        pkg = _package(os.path.join(self.base, "w1"))
        key = deps.cache_key(pkg)
        self.assertEqual(key, deps.cache_key(_package(os.path.join(self.base, "w2"))))
        self.assertEqual(deps.key_files(pkg), ["package-lock.json", "package.json",
                                               "patches/x.patch", "scripts/patch.cjs"])
        with open(os.path.join(pkg, "src", "index.js"), "w") as fh:
            fh.write("changed")                     # source: same key
        self.assertEqual(key, deps.cache_key(pkg))
        with open(os.path.join(pkg, "scripts", "patch.cjs"), "w") as fh:
            fh.write("// other patch")              # lifecycle script: new key
        self.assertNotEqual(key, deps.cache_key(pkg))
        other = _package(os.path.join(self.base, "w3"))
        with open(os.path.join(other, "patches", "x.patch"), "w") as fh:
            fh.write("--- b")
        self.assertNotEqual(key, deps.cache_key(other))
        self.assertNotEqual(key, deps.cache_key(_package(os.path.join(self.base, "w4")),
                                                node="v27"))


class LinkTests(_Base):
    def test_filled_once_then_linked_read_only_with_writable_tool_caches(self):
        installer = FakeInstaller()
        first = _package(os.path.join(self.base, "w1"))
        second = _package(os.path.join(self.base, "w2"))
        one = deps.link_package(first, self.cache, installer=installer)
        two = deps.link_package(second, self.cache, installer=installer)
        self.assertEqual((one["status"], one["filled"]), ("linked", True))
        self.assertEqual((two["status"], two["filled"]), ("linked", False))
        self.assertEqual(len(installer.calls), 1)          # npm ci once per lockfile
        target = deps.entry_modules(deps.nm_root(self.cache), one["key"], "app")
        # filled in a temporary directory of the key, renamed into place
        self.assertTrue(os.path.basename(os.path.dirname(installer.calls[0])).startswith(
            one["key"] + deps.TMP_SEP))
        for pkg in (first, second):
            link = os.path.join(pkg, "node_modules")
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), target)
            self.assertTrue(os.path.isfile(os.path.join(link, "left-pad", "lib",
                                                        "index.js")))
            self.assertTrue(deps.is_shared_link(link, self.cache))
        # only package.json and the lockfile stay beside the entry's node_modules
        self.assertEqual(sorted(os.listdir(os.path.dirname(target))),
                         ["node_modules", "package-lock.json", "package.json"])
        marker = deps.read_marker(deps.entry_of(deps.nm_root(self.cache), one["key"]))
        self.assertEqual((marker["schema"], marker["package"], marker["files"]),
                         (deps.SCHEMA, "app", 2))
        # read-only tree: no new file, no change, no deletion through the link
        with self.assertRaises(PermissionError):
            open(os.path.join(first, "node_modules", "left-pad", "new.js"), "w")
        with self.assertRaises(PermissionError):
            open(os.path.join(first, "node_modules", "left-pad", "lib", "index.js"), "w")
        with self.assertRaises(PermissionError):
            os.unlink(os.path.join(first, "node_modules", "left-pad", "lib", "index.js"))
        # babel/eslint (.cache) and vite/vitest (.vite) can still write
        for name in deps.WRITABLE:
            with open(os.path.join(first, "node_modules", name, "probe"), "w") as fh:
                fh.write("ok")
        self.assertEqual(sorted(os.path.basename(p) for p in deps.live_links(
            deps.entry_of(deps.nm_root(self.cache), one["key"]))), ["node_modules"] * 2)

    def test_local_directory_kept_unless_replace(self):
        pkg = _package(os.path.join(self.base, "w1"))
        os.makedirs(os.path.join(pkg, "node_modules", "old"))
        installer = FakeInstaller()
        self.assertEqual(deps.link_package(pkg, self.cache, installer=installer)["status"],
                         "local-kept")
        self.assertEqual(installer.calls, [])
        done = deps.link_package(pkg, self.cache, installer=installer, replace=True)
        self.assertEqual((done["status"], done["replaced"]), ("linked", True))
        self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))
        self.assertEqual([n for n in os.listdir(pkg) if "ameesh-old" in n], [])

    def test_fill_failure_falls_back_to_local_npm_ci(self):
        pkg = _package(os.path.join(self.base, "w1"))
        installer = FakeInstaller(fail_in=(deps.nm_root(self.cache),))
        done = deps.link_package(pkg, self.cache, installer=installer)
        self.assertEqual(done["status"], "local")
        self.assertIn("fake", done["reason"])
        self.assertEqual(installer.calls[-1], pkg)
        self.assertFalse(os.path.islink(os.path.join(pkg, "node_modules")))
        self.assertEqual(deps.entries(self.cache), [])     # no partial entry left

    def test_fill_held_by_another_agent_falls_back_after_the_wait(self):
        pkg = _package(os.path.join(self.base, "w1"))
        root = deps.nm_root(self.cache)
        os.makedirs(root)
        key = deps.cache_key(pkg)
        held = deps._lock(os.path.join(root, key + ".lock"), wait=0)
        try:
            done = deps.link_package(pkg, self.cache, installer=FakeInstaller(), wait=0)
        finally:
            held.close()
        self.assertEqual(done["status"], "local")
        self.assertIn("another fill", done["reason"])

    def test_no_fill_and_link_ready(self):
        installer = FakeInstaller()
        first = _package(os.path.join(self.base, "w1"))
        second = _package(os.path.join(self.base, "w2"))
        for top in ("w1", "w2"):
            _git(os.path.join(self.base, top), "init", "-q", "-b", "main")
        self.assertEqual(deps.link_package(first, self.cache, fill_missing=False)["status"],
                         "not-cached")
        self.assertEqual(deps.link_ready(os.path.join(self.base, "w1"), self.cache), [])
        deps.link_package(first, self.cache, installer=installer)
        ready = deps.link_ready(os.path.join(self.base, "w2"), self.cache)
        self.assertEqual([r["package"] for r in ready], [second])
        self.assertEqual(len(installer.calls), 1)
        review = _package(os.path.join(self.base, "nexlink-codex2-review-ime"))
        _git(os.path.dirname(review), "init", "-q", "-b", "main")
        self.assertEqual(deps.link_ready(os.path.dirname(review), self.cache), [])

    def test_find_packages_skips_node_modules_hidden_and_symlinks(self):
        top = os.path.join(self.base, "repo")
        for name in ("website", "mobile"):
            _package(top, name)
        _package(os.path.join(top, "website", "node_modules"), "dep")
        _package(os.path.join(top, ".worktrees"), "spike")
        os.symlink(os.path.join(top, "mobile"), os.path.join(top, "alias"))
        self.assertEqual(deps.find_packages(top),
                         [os.path.join(top, "mobile"), os.path.join(top, "website")])

    def test_symlink_ignored_by_git_where_the_pattern_is_a_directory(self):
        repo = os.path.join(self.base, "repo")
        os.makedirs(repo)
        _git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write("node_modules/\n")
        pkg = _package(repo, "mobile")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        done = deps.link_package(pkg, self.cache, installer=FakeInstaller())
        self.assertEqual(done["excluded"], "/mobile/node_modules")
        self.assertEqual(_git(repo, "status", "--porcelain"), "")
        # a second link of the same path adds nothing
        os.unlink(os.path.join(pkg, "node_modules"))
        self.assertIsNone(deps.link_package(pkg, self.cache)["excluded"])
        with open(os.path.join(repo, ".git", "info", "exclude")) as fh:
            self.assertEqual(fh.read().count("/mobile/node_modules"), 1)


class ReviewAndShimTests(_Base):
    def test_review_worktree_names(self):
        for name in ("nexlink-codex2-review-ime", "review-98-r2", "dh2b-review",
                     "codex1-rel-smoke-review-2119d7d", "ameesh-relecture-12"):
            self.assertTrue(deps.is_review_worktree(os.path.join(self.base, name)), name)
        for name in ("nexlink-codex1", "nexlink-codex1-billing-306", "reviewer-tools",
                     "ameesh-dev7"):
            self.assertFalse(deps.is_review_worktree(os.path.join(self.base, name)), name)

    def _npm(self, *args, cwd, env=None, installer=None):
        calls = []

        def execute(path, argv, env):
            # nothing marks the real npm: its scripts come back to the wrapper
            self.assertFalse([k for k in env if "PASSTHROUGH" in k])
            calls.append(argv[1:])
            return 0
        base_env = {"PATH": self.fake_bin, "AMEESH_CACHE_DIR": self.cache}
        base_env.update(env or {})
        code = deps.npm_shim(list(args), env=base_env, cwd=cwd, execute=execute,
                             installer=installer or FakeInstaller())
        return code, calls

    def setUp(self):
        super().setUp()
        self.fake_bin = os.path.join(self.base, "bin")
        os.makedirs(self.fake_bin)
        with open(os.path.join(self.fake_bin, "npm"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.fake_bin, "npm"), 0o755)

    def test_ci_links_instead_of_installing(self):
        pkg = _package(os.path.join(self.base, "nexlink-codex1"))
        os.makedirs(os.path.join(pkg, "src", "deep"))
        installer = FakeInstaller()
        code, calls = self._npm("ci", "--no-audit", cwd=os.path.join(pkg, "src", "deep"),
                                installer=installer)
        self.assertEqual((code, calls), (0, []))
        self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))
        code, calls = self._npm("install", cwd=pkg, installer=installer)
        self.assertEqual((code, calls), (0, []))
        self.assertEqual(len(installer.calls), 1)

    def test_install_refused_in_a_review_worktree(self):
        pkg = _package(os.path.join(self.base, "nexlink-codex2-review-ime"))
        for args in (("ci",), ("install",), ("i", "left-pad"), ("install-ci-test",)):
            code, calls = self._npm(*args, cwd=pkg)
            self.assertEqual((code, calls), (1, []), args)
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
        # reading is fine: npm test, npm run
        self.assertEqual(self._npm("test", cwd=pkg), (0, [["test"]]))
        # the explicit override
        code, _calls = self._npm("ci", cwd=pkg, env={"AMEESH_DEPS": "allow-review"})
        self.assertEqual(code, 0)
        self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))

    def test_install_of_a_package_unlinks_then_runs_npm(self):
        pkg = _package(os.path.join(self.base, "w1"))
        self._npm("ci", cwd=pkg)
        code, calls = self._npm("install", "is-odd", cwd=pkg)
        self.assertEqual((code, calls), (0, [["install", "is-odd"]]))
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))

    def test_passthrough_cases(self):
        pkg = _package(os.path.join(self.base, "w1"))
        # options that change the tree, lockfile out of sync, global, off
        for args, env in ((("ci", "--omit=dev"), None), (("ci",), {"AMEESH_DEPS": "off"}),
                          (("install", "-g", "x"), None), (("run", "build"), None)):
            code, calls = self._npm(*args, cwd=pkg, env=env)
            self.assertEqual((code, calls), (0, [list(args)]), args)
        manifest = dict(MANIFEST, dependencies={"left-pad": "^1.0.0", "is-odd": "^3"})
        stale = _package(os.path.join(self.base, "w2"), manifest=manifest)
        self.assertEqual(self._npm("ci", cwd=stale), (0, [["ci"]]))
        self.assertFalse(os.path.lexists(os.path.join(stale, "node_modules")))
        # a production turn gets its own local tree, never the dev one
        for env in ({"NODE_ENV": "production"}, {"npm_config_omit": "dev"},
                    {"NPM_CONFIG_PRODUCTION": "true"}):
            self.assertEqual(self._npm("ci", cwd=pkg, env=env), (0, [["ci"]]), env)

    def test_location_project_and_prefix_do_not_evade_the_review_refusal(self):
        review = _package(os.path.join(self.base, "nexlink-codex2-review-ime"))
        self.assertEqual(self._npm("install", "--location=project", cwd=review), (1, []))
        self.assertEqual(self._npm("--prefix", review, "ci", cwd=self.base), (1, []))
        pkg = _package(os.path.join(self.base, "nexlink-codex1"))
        code, calls = self._npm("--prefix", "app", "ci", cwd=os.path.dirname(pkg))
        self.assertEqual((code, calls), (0, []))
        self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))
        code, calls = self._npm("-C", pkg, "install", "is-odd", cwd=self.base)
        self.assertEqual((code, calls), (0, [["-C", pkg, "install", "is-odd"]]))
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))

    def test_unknown_value_options_and_tree_commands(self):
        review = _package(os.path.join(self.base, "nexlink-codex2-review-ime"))
        self.assertEqual(self._npm("--fetch-retries", "2", "ci", cwd=review), (1, []))
        self.assertEqual(self._npm("update", cwd=review), (1, []))
        pkg = _package(os.path.join(self.base, "w1"))
        self._npm("ci", cwd=pkg)
        self.assertTrue(os.path.islink(os.path.join(pkg, "node_modules")))
        self.assertEqual(self._npm("rebuild", cwd=pkg), (0, [["rebuild"]]))
        self.assertFalse(os.path.islink(os.path.join(pkg, "node_modules")))
        self.assertEqual(self._npm("--fetch-retries", "2", "test", cwd=review),
                         (0, [["--fetch-retries", "2", "test"]]))

    def test_r3_review_refuses_any_install_word_anywhere(self):
        review = _package(os.path.join(self.base, "nexlink-codex2-review-ime"))
        for args in (["run", "link"], ["--weird", "x", "install"], ["exec", "--", "ci"],
                     ["--unknown-opt", "dedupe"]):
            self.assertEqual(self._npm(*args, cwd=review), (1, []), args)
        self.assertEqual(self._npm("test", cwd=review), (0, [["test"]]))
        self.assertEqual(self._npm("run", "lint", cwd=review), (0, [["run", "lint"]]))

    def test_r3_tree_commands_get_a_full_local_tree_first(self):
        pkg = _package(os.path.join(self.base, "w1"))
        self._npm("ci", cwd=pkg)
        installer = FakeInstaller()
        code, calls = self._npm("prune", cwd=pkg, installer=installer)
        self.assertEqual((code, calls), (0, [["prune"]]))
        self.assertEqual(installer.calls, [pkg])        # npm ci from the lockfile, here
        self.assertTrue(os.path.isfile(os.path.join(pkg, "node_modules", "left-pad", "lib",
                                                    "index.js")))
        other = _package(os.path.join(self.base, "w2"))
        self._npm("ci", cwd=other)
        failing = FakeInstaller(fail_in=(other,))
        self.assertEqual(self._npm("update", cwd=other, installer=failing), (1, []))

    def test_install_shim_and_real_npm(self):
        shim_dir = os.path.join(self.base, ".menage", deps.SHIM_DIR)
        env = deps.install_shim(shim_dir, self.fake_bin, python="/usr/bin/python3")
        self.assertEqual(env["PATH"], shim_dir + os.pathsep + self.fake_bin)
        self.assertEqual(env["AMEESH_REAL_NPM"], os.path.join(self.fake_bin, "npm"))
        with open(os.path.join(shim_dir, "npm")) as fh:
            self.assertIn("exec /usr/bin/python3 -m ameesh.deps npm", fh.read())
        self.assertTrue(os.access(os.path.join(shim_dir, "npm"), os.X_OK))
        self.assertEqual(deps.real_npm({"PATH": env["PATH"]}),
                         os.path.join(self.fake_bin, "npm"))
        self.assertEqual(deps.install_shim(shim_dir, os.path.join(self.base, "none")), {})


class GcTests(_Base):
    def test_unreferenced_entry_deleted_once_idle(self):
        pkg = _package(os.path.join(self.base, "w1"))
        done = deps.link_package(pkg, self.cache, installer=FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done["key"])
        later = time.time() + 10 * 24 * 3600
        self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                              holders=lambda _p: []), [])   # linked
        os.unlink(os.path.join(pkg, "node_modules"))
        self.assertEqual(deps.gc_node_modules(self.cache,
                                              holders=lambda _p: []), [])   # not idle yet
        plan = deps.gc_node_modules(self.cache, now=later, dry_run=True,
                                    holders=lambda _p: [])
        self.assertEqual([p["action"] for p in plan], ["would-delete"])
        self.assertTrue(os.path.isdir(entry))
        held = deps._lock(os.path.join(deps.nm_root(self.cache), done["key"] + ".lock"),
                          wait=0)
        try:
            self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: []), [])  # filling
        finally:
            held.close()
        lines = deps.gc_node_modules(self.cache, now=later, holders=lambda _p: [])
        self.assertEqual([(line["kind"], line["action"]) for line in lines],
                         [("deps", "deleted")])
        self.assertFalse(os.path.lexists(entry))

    def test_unlinked_entry_deleted_after_a_day_by_default(self):
        # #3746: the default idle time is 24 h (was 3 days)
        pkg = _package(os.path.join(self.base, "w1"))
        done = deps.link_package(pkg, self.cache, installer=FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done["key"])
        os.unlink(os.path.join(pkg, "node_modules"))
        self.assertEqual(menage.Policy().deps_idle, deps.DEFAULT_DEPS_IDLE_S)
        self.assertEqual(deps.gc_node_modules(self.cache, now=time.time() + 23 * 3600,
                                              holders=lambda _p: []), [])
        self.assertTrue(os.path.isdir(entry))
        lines = deps.gc_node_modules(self.cache, now=time.time() + 25 * 3600,
                                     holders=lambda _p: [])
        self.assertEqual([(line["action"], line["path"]) for line in lines],
                         [("deleted", entry)])
        self.assertGreater(lines[0]["bytes"], 0)
        self.assertFalse(os.path.lexists(entry))

    def test_partial_entry_deleted_after_an_hour(self):
        root = deps.nm_root(self.cache)
        partial = os.path.join(root, "f" * 64, "app", "node_modules", "x")
        os.makedirs(partial)
        os.chmod(os.path.dirname(partial), 0o555)
        self.assertEqual(deps.gc_node_modules(self.cache, holders=lambda _p: []), [])
        lines = deps.gc_node_modules(self.cache, now=time.time() + 2 * 3600,
                                     holders=lambda _p: [])
        self.assertEqual([line["detail"] for line in lines], ["partial fill (no .complete.json)"])
        # the lock file stays: a waiting filler and a new one lock the same inode
        self.assertEqual(os.listdir(root), ["f" * 64 + ".lock"])

    def test_remove_tree_refuses_outside_and_never_follows_a_link(self):
        outside = _package(os.path.join(self.base, "w1"))
        root = deps.nm_root(self.cache)
        os.makedirs(root)
        with self.assertRaises(RuntimeError):
            deps.remove_tree(outside, root)
        link = os.path.join(root, "k")
        os.symlink(outside, link)
        deps.remove_tree(link, root)
        self.assertTrue(os.path.isfile(os.path.join(outside, "package.json")))


def _fake_npm(directory: str, log: str, *, sleeper: bool = False) -> str:
    """A real executable standing for npm: logs its arguments and NODE_ENV /
    npm_config_omit, writes a node_modules; with `sleeper`, leaves a
    background child (an install script) that inherits its descriptors."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "npm")
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n"
                 "echo \"$* NODE_ENV=${NODE_ENV:-} OMIT=${npm_config_omit:-}\" >> %s\n"
                 "d=.; p=; for a in \"$@\"; do [ \"$p\" = --prefix ] && d=\"$a\"; p=\"$a\"; done; cd \"$d\"\n"
                 "mkdir -p node_modules/left-pad && echo x > node_modules/left-pad/i.js\n"
                 % log)
        if sleeper:
            fh.write("(sleep 3) >/dev/null 2>&1 &\n")
    os.chmod(path, 0o755)
    return path


class ReviewFixesTests(_Base):
    """Review r1 of #234 (codex, 2026-10-12): one test per fix."""

    def _repo(self, name: str) -> str:
        repo = os.path.join(self.base, name)
        os.makedirs(repo)
        _git(repo, "init", "-q", "-b", "main")
        return repo

    def test_1_key_has_npm_label_and_mode_and_link_target_ignores_the_worktree_name(self):
        one = _package(self._repo("repo-agent1"), "mobile")
        two = _package(self._repo("repo-agent2"), "mobile")
        self.assertEqual((deps.package_label(one), deps.package_label(two)),
                         ("mobile", "mobile"))
        self.assertEqual(deps.cache_key(one, npm="11"), deps.cache_key(two, npm="11"))
        self.assertNotEqual(deps.cache_key(one, npm="11"), deps.cache_key(one, npm="12"))
        self.assertNotEqual(deps.cache_key(one, npm="11"),
                            deps.cache_key(one, npm="11", label="assist"))
        top = _package(os.path.dirname(self._repo("repo-agent3")), "repo-agent3")
        self.assertEqual(deps.package_label(top), "_root")
        with mock.patch.object(deps, "npm_version", return_value="11"):
            installer = FakeInstaller()
            deps.link_package(one, self.cache, installer=installer)
            done = deps.link_package(two, self.cache, installer=installer)
        self.assertEqual((done["status"], done["filled"]), ("linked", False))
        # the second worktree's link resolves: the entry is named by the key
        self.assertTrue(os.path.isfile(os.path.join(two, "node_modules", "left-pad", "lib",
                                                    "index.js")))
        self.assertTrue(done["target"].endswith(os.path.join("mobile", "node_modules")))

    def test_1_fill_is_always_an_explicit_dev_install(self):
        log = os.path.join(self.base, "npm.log")
        npm = _fake_npm(os.path.join(self.base, "fakebin"), log)
        pkg = _package(os.path.join(self.base, "w1"))
        env = {"PATH": os.path.dirname(npm) + os.pathsep + "/usr/bin:/bin",
               "NODE_ENV": "production", "npm_config_omit": "dev",
               "NPM_CONFIG_PRODUCTION": "true", "HOME": self.base}
        deps.npm_ci(pkg, env)
        with open(log) as fh:
            line = fh.read().strip()
        self.assertTrue(line.startswith("ci --include=dev --include=optional --include=peer "
                                        "--ignore-scripts=false"), line)
        self.assertTrue(line.endswith("NODE_ENV= OMIT="), line)
        self.assertNotIn("NPM_CONFIG_PRODUCTION", deps.fill_env(env))

    def test_2_fill_lock_outlives_the_filler_through_npm_children(self):
        log = os.path.join(self.base, "npm.log")
        npm = _fake_npm(os.path.join(self.base, "fakebin"), log, sleeper=True)
        pkg = _package(os.path.join(self.base, "w1"))
        root = deps.nm_root(self.cache)
        key = deps.cache_key(pkg)
        env = {"PATH": os.path.dirname(npm) + os.pathsep + "/usr/bin:/bin", "HOME": self.base}
        with mock.patch.object(deps, "key_files", return_value=["package-lock.json",
                                                                "package.json"]):
            deps.fill(pkg, root, key, env=env)       # our lock is closed on return
        held = deps._lock(os.path.join(root, key + ".lock"), wait=0)
        self.assertIsNone(held)                       # the install script still holds it
        again = deps._lock(os.path.join(root, key + ".lock"), wait=10)
        self.assertIsNotNone(again)                   # released when it exits
        again.close()

    def _complete_entry(self, pkg: str) -> tuple[str, dict]:
        done = deps.link_package(pkg, self.cache, installer=FakeInstaller())
        return deps.entry_of(deps.nm_root(self.cache), done["key"]), done

    def test_3_gc_reads_again_under_the_lock(self):
        pkg = _package(os.path.join(self.base, "w1"))
        entry, done = self._complete_entry(pkg)
        os.unlink(os.path.join(pkg, "node_modules"))
        later = time.time() + 10 * 24 * 3600
        real_lock = deps._lock

        def lock_then_link(path, **kw):
            fh = real_lock(path, **kw)
            # between the listing and the lock, a worktree links the entry
            os.symlink(done["target"], os.path.join(pkg, "node_modules"))
            deps._record_link(entry, os.path.join(pkg, "node_modules"))
            return fh
        with mock.patch.object(deps, "_lock", side_effect=lock_then_link):
            self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: []), [])
        self.assertTrue(os.path.isdir(entry))

    def test_4_gc_keeps_the_lock_file(self):
        pkg = _package(os.path.join(self.base, "w1"))
        entry, done = self._complete_entry(pkg)
        lock_file = os.path.join(deps.nm_root(self.cache), done["key"] + ".lock")
        inode = os.stat(lock_file).st_ino
        os.unlink(os.path.join(pkg, "node_modules"))
        lines = deps.gc_node_modules(self.cache, now=time.time() + 10 * 24 * 3600,
                                     holders=lambda _p: [])
        self.assertEqual([line["action"] for line in lines], ["deleted"])
        self.assertEqual(os.stat(lock_file).st_ino, inode)

    def test_5_record_written_before_the_link_and_gc_scans_worktrees(self):
        pkg = _package(os.path.join(self.base, "w1"))
        entry, done = self._complete_entry(pkg)
        other = _package(os.path.join(self.base, "w2"))
        with mock.patch.object(deps, "_symlink", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                deps.link_package(other, self.cache, installer=FakeInstaller())
        records = os.listdir(os.path.join(entry, deps.LINKS))
        self.assertEqual(len(records), 2)            # recorded although not linked
        # a link nobody recorded (a crash before #234 r1, a copy): found by the scan
        os.unlink(os.path.join(pkg, "node_modules"))
        for name in os.listdir(os.path.join(entry, deps.LINKS)):
            os.unlink(os.path.join(entry, deps.LINKS, name))
        os.symlink(done["target"], os.path.join(other, "node_modules"))
        later = time.time() + 10 * 24 * 3600
        tops = [os.path.join(self.base, "w1"), os.path.join(self.base, "w2")]
        self.assertEqual(deps.gc_node_modules(self.cache, now=later, worktrees=lambda: tops,
                                              holders=lambda _p: []), [])
        self.assertEqual(deps.live_links(entry), [os.path.join(other, "node_modules")])

    def test_6_gc_spares_an_entry_in_use_and_keeps_a_grace_period(self):
        pkg = _package(os.path.join(self.base, "w1"))
        entry, _done = self._complete_entry(pkg)
        later = time.time() + 10 * 24 * 3600
        # a link seen alive now: the grace runs from now, even with idle 0
        self.assertEqual(deps.gc_node_modules(self.cache, now=time.time(), idle=0,
                                              holders=lambda _p: []), [])
        os.unlink(os.path.join(pkg, "node_modules"))
        self.assertEqual(deps.gc_node_modules(self.cache, now=time.time() + 3600, idle=0,
                                              holders=lambda _p: []), [])
        busy = [{"pid": 1, "why": "fichier projeté", "cmd": "node vite"}]
        self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                              holders=lambda _p: busy), [])

        def unknown(_p):
            raise deps.platform.NotAvailable("holders", "test")
        kept = deps.gc_node_modules(self.cache, now=later, holders=unknown)
        self.assertEqual(kept[0]["action"], "kept")
        self.assertIn("file holders unreadable", kept[0]["detail"])
        self.assertTrue(os.path.isdir(entry))
        self.assertEqual(len(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: [])), 1)

    def test_7_gc_deletes_nothing_when_the_worktree_inventory_expired(self):
        """#3802 B/P1 (cx1) : si le budget expire PENDANT l'inventaire des
        worktrees, la liste est incomplète ; l'entrée peut avoir un lien
        vivant dans un worktree omis, donc rien n'est supprimé."""
        pkg = _package(os.path.join(self.base, "w1"))
        entry, done = self._complete_entry(pkg)
        other = _package(os.path.join(self.base, "w2"))
        os.unlink(os.path.join(pkg, "node_modules"))
        for name in os.listdir(os.path.join(entry, deps.LINKS)):
            os.unlink(os.path.join(entry, deps.LINKS, name))
        os.symlink(done["target"], os.path.join(other, "node_modules"))
        later = time.time() + 10 * 24 * 3600
        clock = [0.0]
        budget = hk.PassBudget(10.0, clock=lambda: clock[0])

        def worktrees():
            clock[0] = 20.0        # the inventory itself consumed the budget
            return []              # the worktree with the live link is omitted

        lines = deps.gc_node_modules(self.cache, now=later, worktrees=worktrees,
                                     holders=lambda _p: [], budget=budget)
        self.assertTrue(os.path.isdir(entry), "an incomplete inventory must keep it")
        self.assertIn("inventory incomplete",
                      " ".join(line.get("detail") or "" for line in lines))

    @unittest.skipUnless(deps.platform.is_linux(),
                         "les fichiers projetés se lisent dans /proc/<pid>/maps (Linux)")
    def test_6_holders_see_mapped_files(self):
        import sys
        exe_dir = os.path.dirname(os.path.realpath(sys.executable))
        try:
            found = deps.platform.holders(exe_dir, mapped=True)
        except deps.platform.NotAvailable:
            self.skipTest("no way to read processes here")
        self.assertIn(os.getpid(), [h["pid"] for h in found])

    def test_8_npm_arguments(self):
        words = deps._npm_words(["--prefix", "mobile", "ci", "--location=project"])
        self.assertEqual(words[:2], ("ci", []))
        self.assertEqual(words[3]["--prefix"], "mobile")
        self.assertFalse(deps._global(words[2], words[3]))
        for args in (["-g"], ["--global"], ["--location=global"], ["--location", "global"]):
            w = deps._npm_words(args + ["install", "x"])
            self.assertTrue(deps._global(w[2], w[3]), args)
        w = deps._npm_words(["--global=false", "install"])
        self.assertFalse(deps._global(w[2], w[3]))


class ReviewR2Tests(_Base):
    """Review r2 of #234 (codex, 2026-10-12): one test per fix."""

    def test_1_fill_sees_a_whitelisted_environment_only(self):
        env = {"PATH": "/bin", "HOME": "/h", "LANG": "C.UTF-8", "LC_ALL": "C",
               "TMPDIR": "/t", "npm_config_cache": "/c", "PATCH_MODE": "x",
               "npm_config_script_shell": "/bin/zsh", "NODE_ENV": "production",
               "npm_config_omit": "dev", "AMEESH_CACHE_DIR": "/a"}
        self.assertEqual(deps.fill_env(env), {"PATH": "/bin", "HOME": "/h",
                                              "LANG": "C.UTF-8", "LC_ALL": "C",
                                              "TMPDIR": "/t", "npm_config_cache": "/c",
                                              "SHARP_IGNORE_GLOBAL_LIBVIPS": "1"})

    def test_2_fill_works_in_a_temporary_directory_renamed_into_place(self):
        pkg = _package(os.path.join(self.base, "w1"))
        root = deps.nm_root(self.cache)
        key = deps.cache_key(pkg)
        seen = []

        def installer(directory, env=None, *, lock_fd=None):
            seen.append(directory)
            FakeInstaller()(directory, env)
        # a dead fill left its temporary directory, a stray script writes there
        stray = os.path.join(root, key + deps.TMP_SEP + "1-dead")
        os.makedirs(os.path.join(stray, "app", "node_modules"))
        deps.fill(pkg, root, key, installer=installer)
        self.assertIn(deps.TMP_SEP, seen[0])          # never the entry's own path
        self.assertFalse(seen[0].startswith(deps.entry_of(root, key) + "/"))
        with open(os.path.join(stray, "app", "node_modules", "late.js"), "w") as fh:
            fh.write("stray")
        self.assertFalse(os.path.exists(os.path.join(deps.entry_of(root, key), "app",
                                                     "node_modules", "late.js")))
        self.assertEqual(sorted(n for n in os.listdir(root) if deps.TMP_SEP in n),
                         [os.path.basename(stray)])   # ours renamed, the stray one left
        # another publisher first: our temporary directory is discarded
        other = _package(os.path.join(self.base, "w2"))

        def racing(directory, env=None, *, lock_fd=None):
            FakeInstaller()(directory, env)
        os.rename(deps.entry_of(root, key), os.path.join(self.base, "moved"))
        with mock.patch.object(deps, "read_marker", side_effect=lambda e: (
                {"key": key} if os.path.isdir(e) and e == deps.entry_of(root, key)
                and os.path.exists(os.path.join(e, "x")) else None)):
            os.makedirs(os.path.join(deps.entry_of(root, key), "x"))
            self.assertEqual(deps.fill(other, root, key, installer=racing), {"key": key})
        self.assertEqual(sorted(n for n in os.listdir(root) if deps.TMP_SEP in n),
                         [os.path.basename(stray)])

    def test_2_gc_deletes_abandoned_temporary_fills_after_the_grace(self):
        root = deps.nm_root(self.cache)
        stray = os.path.join(root, "a" * 64 + deps.TMP_SEP + "1-dead")
        os.makedirs(os.path.join(stray, "app", "node_modules"))
        os.chmod(os.path.join(stray, "app"), 0o555)
        soon = time.time() + 3600
        later = time.time() + deps.GRACE_S + 60
        self.assertEqual(deps.gc_node_modules(self.cache, now=soon, holders=lambda _p: []),
                         [])
        busy = [{"pid": 1, "why": "dossier courant", "cmd": "node postinstall"}]
        self.assertEqual(deps.gc_node_modules(self.cache, now=later, holders=lambda _p: busy),
                         [])
        held = deps._lock(os.path.join(root, "a" * 64 + ".lock"), wait=0)
        try:
            self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: []), [])
        finally:
            held.close()
        lines = deps.gc_node_modules(self.cache, now=later, holders=lambda _p: [])
        self.assertEqual([line["detail"] for line in lines], ["abandoned temporary fill"])
        self.assertFalse(os.path.lexists(stray))

    def test_r3_temp_gc_looks_at_the_whole_tree_within_a_deadline(self):
        root = deps.nm_root(self.cache)
        stray = os.path.join(root, "b" * 64 + deps.TMP_SEP + "1-dead")
        deep = os.path.join(stray, "app", "node_modules", "pkg", "lib")
        os.makedirs(deep)
        later = time.time() + deps.GRACE_S + 60
        old = time.time() - 2 * deps.GRACE_S
        for current, _dirs, _files in os.walk(stray):
            os.utime(current, (old, old))
        with open(os.path.join(deep, "late.js"), "w") as fh:
            fh.write("still written")                  # a stray script, deep down
        future = later - 60
        os.utime(os.path.join(deep, "late.js"), (future, future))
        for current in (stray, os.path.join(stray, "app")):
            os.utime(current, (old, old))
        self.assertEqual(deps.gc_node_modules(self.cache, now=later, holders=lambda _p: []),
                         [])
        os.utime(os.path.join(deep, "late.js"), (old, old))
        with mock.patch.object(deps, "TEMP_WALK_S", -1.0):  # walk cut short: kept
            self.assertEqual(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: []), [])
        self.assertEqual(len(deps.gc_node_modules(self.cache, now=later,
                                                  holders=lambda _p: [])), 1)

    def test_3_cheap_filter_refreshes_last_alive_of_linked_entries(self):
        pkg = _package(os.path.join(self.base, "w1"))
        done = deps.link_package(pkg, self.cache, installer=FakeInstaller())
        entry = deps.entry_of(deps.nm_root(self.cache), done["key"])
        old = time.time() - 30 * 24 * 3600            # created and linked a month ago
        for name in os.listdir(os.path.join(entry, deps.LINKS)):
            os.utime(os.path.join(entry, deps.LINKS, name), (old, old))
        marker_path = os.path.join(entry, deps.COMPLETE)
        os.chmod(entry, 0o755)
        with open(marker_path) as fh:
            marker = json.load(fh)
        marker["created"] = old
        with open(marker_path, "w") as fh:
            json.dump(marker, fh)
        now = time.time()
        self.assertEqual(deps.gc_node_modules(self.cache, now=now, idle=0,
                                              holders=lambda _p: []), [])
        self.assertTrue(os.path.isfile(os.path.join(entry, deps.ALIVE)))
        # the link is replaced; a running server may still lazy-load from it
        os.unlink(os.path.join(pkg, "node_modules"))
        os.utime(os.path.join(entry, deps.ALIVE), (now, now))
        self.assertEqual(deps.gc_node_modules(self.cache, now=now + 3600, idle=0,
                                              holders=lambda _p: []), [])
        self.assertTrue(os.path.isdir(entry))

    def test_4_command_is_the_first_npm_command_name(self):
        for args, command in ((["--fetch-retries", "2", "ci"], "ci"),
                              (["--prefix", "test", "ci"], "ci"),
                              (["--loglevel", "warn", "install", "x"], "install"),
                              (["-C", "app", "--foo", "bar", "update"], "update"),
                              (["--fetch-retries", "2"], None)):
            self.assertEqual(deps._npm_words(args)[0], command, args)


class NexlinkVerificationTests(_Base):
    """#249, the real check on nexlink: one test per fix."""

    def test_1_fill_forces_sharp_to_its_prebuilt_libvips(self):
        env = {"PATH": "/bin", "SHARP_IGNORE_GLOBAL_LIBVIPS": ""}
        self.assertEqual(deps.fill_env(env)["SHARP_IGNORE_GLOBAL_LIBVIPS"], "1")
        self.assertEqual(deps.fill_env({})["SHARP_IGNORE_GLOBAL_LIBVIPS"], "1")

    def _toolchains(self):
        """A version manager stand-in, answering by the directory it runs in
        (`.pin`: node 22 and its npm 10.9, else node 26 and its npm 11.19):
        `node`, and `shim-npm`, an npm shim with no npm beside the node; each
        npm logs its own path and the directory it ran from when installing."""
        fakebin = os.path.join(self.base, "fakebin")
        os.makedirs(fakebin)
        log = os.path.join(self.base, "npm-ran.log")
        install = ('echo "$0 $PWD" >> %s\n%s\nmkdir -p node_modules/x\n'
                   % (log, PREFIX))
        for major, npm in (("22", "10.9"), ("26", "11.19")):
            bindir = os.path.join(self.base, "node%s" % major, "bin")
            os.makedirs(bindir)
            with open(os.path.join(bindir, "npm"), "w") as fh:
                fh.write('#!/bin/sh\nif [ "$1" = --version ]; then echo %s; exit 0; fi\n%s'
                         % (npm, install))
            os.chmod(os.path.join(bindir, "npm"), 0o755)
        with open(os.path.join(fakebin, "node"), "w") as fh:
            fh.write('#!/bin/sh\nif [ -f .pin ]; then v=22; else v=26; fi\n'
                     'case "$1" in --version) echo v$v;; -p) if [ "$2" = process.version ]; then echo v$v; '
                     'else printf "v$v\\n%s/node$v/bin/node\\n"; fi;; '
                     'esac\n' % self.base)
        with open(os.path.join(fakebin, "shim-npm"), "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = --version ]; then '
                     '[ -f .pin ] && echo 10.9 || echo 11.19; exit 0; fi\n%s' % install)
        for name in ("node", "shim-npm"):
            os.chmod(os.path.join(fakebin, name), 0o755)
        deps._probes.clear()
        self.addCleanup(deps._probes.clear)
        return fakebin, log

    def test_2_key_and_fill_use_the_node_and_npm_the_package_pins(self):
        fakebin, log = self._toolchains()
        pinned = _package(os.path.join(self.base, "main"))
        open(os.path.join(pinned, ".pin"), "w").close()
        loose = _package(os.path.join(self.base, "wt"))
        root = deps.nm_root(self.cache)
        with mock.patch.dict(os.environ, {"PATH": fakebin + os.pathsep + "/usr/bin:/bin"}), \
                mock.patch.object(deps, "toolchain", toolchain):
            os.environ.pop("AMEESH_REAL_NPM", None)
            self.assertEqual((deps.node_version(pinned), deps.npm_version(pinned)),
                             ("v22", "10.9"))
            self.assertEqual((deps.node_version(loose), deps.npm_version(loose)),
                             ("v26", "11.19"))
            self.assertNotEqual(deps.cache_key(pinned), deps.cache_key(loose))
            done = deps.link_package(pinned, self.cache)
            # the npm binary that ran is the one the key asked, from the package
            with open(log) as fh:
                self.assertEqual(fh.read().split(),
                                 [os.path.join(self.base, "node22/bin/npm"), pinned])
            self.assertEqual(deps.read_marker(deps.entry_of(root, done["key"]))["npm"], "10.9")
            self.assertTrue(os.path.islink(os.path.join(pinned, "node_modules")))
            # the turn's AMEESH_REAL_NPM (a shim answering by its cwd) does not
            # override the npm beside the package's node...
            shim = os.path.join(fakebin, "shim-npm")
            os.environ["AMEESH_REAL_NPM"] = shim
            self.assertEqual(deps.toolchain(pinned)["npm"],
                             os.path.join(self.base, "node22/bin/npm"))
            # ...and without one, the shim runs from the package's directory for
            # the fill as for the key: npm 10.9 both times, never the cache's 11
            os.unlink(os.path.join(self.base, "node22/bin/npm"))
            os.unlink(os.path.join(pinned, "node_modules"))
            deps._probes.clear()
            os.unlink(log)
            tool = deps.toolchain(pinned)
            self.assertEqual((tool["npm"], tool["npm_version"]), (shim, "10.9"))
            marker = deps.fill(pinned, root, deps.cache_key(pinned, label="other", tool=tool),
                               tool=tool)
            with open(log) as fh:
                self.assertEqual(fh.read().split(), [shim, pinned])
            self.assertEqual(marker["npm"], "10.9")

    def test_r3_a_toolchain_changed_since_the_key_discards_the_fill(self):
        # an npm shim answering by directory, no npm beside the node: the pin
        # changes within the TTL, the key (cached) says npm 10.9, the fill
        # would run npm 11.19
        fakebin, log = self._toolchains()
        os.unlink(os.path.join(self.base, "node22/bin/npm"))
        os.unlink(os.path.join(self.base, "node26/bin/npm"))
        pkg = _package(os.path.join(self.base, "main"))
        pin = os.path.join(pkg, ".pin")
        open(pin, "w").close()
        shim = os.path.join(fakebin, "shim-npm")
        root = deps.nm_root(self.cache)
        with mock.patch.dict(os.environ, {"PATH": fakebin + os.pathsep + "/usr/bin:/bin",
                                          "AMEESH_REAL_NPM": shim}), \
                mock.patch.object(deps, "toolchain", toolchain):
            tool = deps.toolchain(pkg)
            self.assertEqual((tool["node"], tool["npm"], tool["npm_version"]),
                             ("v22", shim, "10.9"))
            os.unlink(pin)                              # within the TTL
            done = deps.link_package(pkg, self.cache)
            self.assertEqual(done["status"], "local-kept")
            self.assertIn("toolchain changed before the fill", done["reason"])
            self.assertIn("npm_version 11.19, key 10.9", done["reason"])
            self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
            self.assertFalse(os.path.exists(log))                  # npm ci never ran
            self.assertIsNone(deps.read_marker(deps.entry_of(root, done["key"])))
            self.assertEqual([n for n in os.listdir(root) if deps.TMP_SEP in n], [])
            # the next attempt resolves again: the new pin, its own key, linked
            again = deps.link_package(pkg, self.cache)
            self.assertEqual(again["status"], "linked")
            self.assertNotEqual(again["key"], done["key"])
            with open(log) as fh:
                self.assertEqual(fh.read().split(), [shim, pkg])

    def test_r3_a_toolchain_changing_during_the_install_discards_it(self):
        fakebin, log = self._toolchains()
        pkg = _package(os.path.join(self.base, "main"))
        pin = os.path.join(pkg, ".pin")
        open(pin, "w").close()
        root = deps.nm_root(self.cache)
        real_npm_ci = deps.npm_ci

        def installer(directory, env=None, *, lock_fd=None):
            real_npm_ci(directory, env, lock_fd=lock_fd)
            os.unlink(pin)                              # the pin changes meanwhile

        with mock.patch.dict(os.environ, {"PATH": fakebin + os.pathsep + "/usr/bin:/bin"}), \
                mock.patch.object(deps, "toolchain", toolchain):
            os.environ.pop("AMEESH_REAL_NPM", None)
            done = deps.link_package(pkg, self.cache, installer=installer)
        self.assertEqual(done["status"], "local-kept")
        self.assertIn("toolchain changed after the fill (node v26, key v22)", done["reason"])
        self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
        self.assertIsNone(deps.read_marker(deps.entry_of(root, done["key"])))
        self.assertEqual([n for n in os.listdir(root) if deps.TMP_SEP in n], [])

    def test_r3_a_toolchain_changing_during_a_failed_install_has_no_fallback(self):
        fakebin, log = self._toolchains()
        pkg = _package(os.path.join(self.base, "main"))
        pin = os.path.join(pkg, ".pin")
        open(pin, "w").close()
        calls = []

        def installer(directory, env=None, *, lock_fd=None):
            calls.append(directory)
            os.unlink(pin)                              # the shim switches...
            raise deps.FillError("npm ci failed (1): fake")   # ...and npm ci fails

        with mock.patch.dict(os.environ, {"PATH": fakebin + os.pathsep + "/usr/bin:/bin"}), \
                mock.patch.object(deps, "toolchain", toolchain):
            os.environ.pop("AMEESH_REAL_NPM", None)
            done = deps.link_package(pkg, self.cache, installer=installer)
            self.assertEqual(done["status"], "local-kept")
            self.assertIn("toolchain changed during the fill (node v26, key v22", done["reason"])
            self.assertEqual(len(calls), 1)             # no local npm ci in the package
            self.assertFalse(os.path.lexists(os.path.join(pkg, "node_modules")))
            self.assertEqual([m for m in deps._probes if m[0] == pkg], [])   # memo dropped
            # a failure with the same toolchain still falls back to a local npm ci
            open(pin, "w").close()
            calls.clear()

            def failing(directory, env=None, *, lock_fd=None):
                calls.append(directory)
                raise deps.FillError("npm ci failed (1): fake")

            done = deps.link_package(pkg, self.cache, installer=failing)
        self.assertEqual(done["status"], "local-failed")
        self.assertEqual(len(calls), 2)

    def test_r1_4_toolchain_is_one_entry_not_kept_past_its_ttl(self):
        fakebin, _log = self._toolchains()
        pkg = _package(os.path.join(self.base, "main"))
        pin = os.path.join(pkg, ".pin")
        open(pin, "w").close()
        with mock.patch.dict(os.environ, {"PATH": fakebin + os.pathsep + "/usr/bin:/bin"}), \
                mock.patch.object(deps, "toolchain", toolchain):
            before = deps.cache_key(pkg)
            os.unlink(pin)                  # the pin changes at the same path
            self.assertEqual(deps.cache_key(pkg), before)         # within the TTL
            self.assertEqual(deps.toolchain(pkg)["bindir"], os.path.join(self.base, "node22/bin"))
            with mock.patch.object(deps.time, "monotonic",
                                   return_value=time.monotonic() + deps.PROBE_TTL_S + 1):
                self.assertNotEqual(deps.cache_key(pkg), before)
                tool = deps.toolchain(pkg)
                # version and binary expire together: never v26 with node22/bin
                self.assertEqual((tool["node"], tool["bindir"], tool["npm_version"]),
                                 ("v26", os.path.join(self.base, "node26/bin"), "11.19"))

    def test_r2_1_a_node_modules_made_during_a_failed_fill_is_kept(self):
        pkg = _package(os.path.join(self.base, "w1"))
        fake = FakeInstaller(fail_in=[self.cache])

        def installer(directory, env=None, *, lock_fd=None):
            if directory.startswith(self.cache):
                os.makedirs(os.path.join(pkg, "node_modules", "mine"))  # a local npm ci
            fake(directory, env, lock_fd=lock_fd)

        done = deps.link_package(pkg, self.cache, installer=installer)
        self.assertEqual(done["status"], "local-kept")
        self.assertIn("fake", done["reason"])
        self.assertEqual(len(fake.calls), 1)            # no fallback npm ci in the package
        self.assertTrue(os.path.isdir(os.path.join(pkg, "node_modules", "mine")))

    def test_r1_1_a_node_modules_made_during_the_fill_is_kept(self):
        pkg = _package(os.path.join(self.base, "w1"))
        fake = FakeInstaller()

        def installer(directory, env=None, *, lock_fd=None):
            fake(directory, env, lock_fd=lock_fd)
            os.makedirs(os.path.join(pkg, "node_modules", "mine"))   # a local npm ci

        done = deps.link_package(pkg, self.cache, installer=installer)
        self.assertEqual(done["status"], "local-kept")
        self.assertTrue(os.path.isdir(os.path.join(pkg, "node_modules", "mine")))
        self.assertFalse(os.path.islink(os.path.join(pkg, "node_modules")))
        # it appears between the re-check and the symlink: refused, kept
        other = _package(os.path.join(self.base, "w2"))
        real_record = deps._record_link

        def record(entry, link):
            real_record(entry, link)
            os.makedirs(os.path.join(link, "mine"))

        with mock.patch.object(deps, "_record_link", record):
            done = deps.link_package(other, self.cache, installer=FakeInstaller())
        self.assertEqual(done["status"], "local-kept")
        self.assertTrue(os.path.isdir(os.path.join(other, "node_modules", "mine")))
        self.assertEqual([n for n in os.listdir(other) if ".ameesh-" in n], [])

    def test_r1_2_link_ready_skips_a_worktree_nested_in_the_turns(self):
        top = os.path.join(self.base, "root")
        first = _package(top, "backend")
        _git(top, "init", "-q", "-b", "main")
        nested = _package(os.path.join(top, "agent-b"), "backend")
        _git(os.path.dirname(nested), "init", "-q", "-b", "main")
        deps.link_package(nested, self.cache, installer=FakeInstaller())
        os.unlink(os.path.join(nested, "node_modules"))
        ready = deps.link_ready(top, self.cache)
        self.assertEqual([r["package"] for r in ready], [first])
        self.assertFalse(os.path.lexists(os.path.join(nested, "node_modules")))

    def test_3_link_ready_stays_in_the_git_worktree_of_the_turn(self):
        # a turn started in the parent of every worktree (an orchestrator's
        # ~/development/manaty) linked 262 packages, review worktrees included
        first = _package(os.path.join(self.base, "nexlink-a"), "backend")
        deps.link_package(first, self.cache, installer=FakeInstaller())
        sibling = _package(os.path.join(self.base, "nexlink-b"), "backend")
        review = _package(os.path.join(self.base, "nexlink-c-review-1"), "backend")
        for pkg in (first, sibling, review):
            _git(os.path.dirname(pkg), "init", "-q", "-b", "main")
        self.assertEqual(deps.link_ready(self.base, self.cache), [])
        self.assertEqual(deps.link_ready(os.path.dirname(review), self.cache), [])
        self.assertEqual(deps.link_ready(os.path.join(sibling, "src"), self.cache)[0]["package"],
                         sibling)
        self.assertFalse(os.path.lexists(os.path.join(review, "node_modules")))


class _Cfg:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = "pc"
        self.container_runtime = "none"


class MenageWiringTests(_Base):
    def test_policy_keys(self):
        parsed, problems, unknown = menage.parse_policy({"deps": False, "deps_idle": "12h"})
        self.assertEqual((parsed, problems, unknown),
                         ({"deps": False, "deps_idle": 12 * 3600.0}, [], []))
        self.assertEqual(menage.parse_policy({"deps": "yes"})[1], ["`deps`: boolean expected"])
        self.assertTrue(menage.Policy().deps)

    def test_turn_env_puts_the_npm_wrapper_first(self):
        fake_bin = os.path.join(self.base, "bin")
        os.makedirs(fake_bin)
        with open(os.path.join(fake_bin, "npm"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(fake_bin, "npm"), 0o755)
        cfg = _Cfg(os.path.join(self.base, "state"))
        env = menage.turn_env(cfg, menage.Policy(), "dev7", {"PATH": fake_bin})
        shim_dir = os.path.join(cfg.state_dir, menage.MANAGED_DIR, deps.SHIM_DIR)
        self.assertEqual(env["PATH"].split(os.pathsep), [shim_dir, fake_bin])
        self.assertEqual(env["AMEESH_REAL_NPM"], os.path.join(fake_bin, "npm"))
        off = menage.turn_env(cfg, menage.Policy(deps=False), "dev7", {"PATH": fake_bin})
        self.assertNotIn("PATH", off)

    def test_turn_begin_links_ready_packages_of_the_cwd(self):
        cfg = _Cfg(os.path.join(self.base, "state"))
        policy = menage.Policy(worktrees=False)
        croot = menage.cache_root(cfg, policy)
        first = _package(os.path.join(self.base, "w1"))
        second = _package(os.path.join(self.base, "w2"))
        deps.link_package(first, croot, installer=FakeInstaller())
        _git(os.path.dirname(second), "init", "-q", "-b", "main")
        turn = menage.Turn.begin(cfg, policy, "dev7", os.path.dirname(second),
                                 new_session=False, base_env={"PATH": ""})
        try:
            self.assertTrue(os.path.islink(os.path.join(second, "node_modules")))
            self.assertIn(("deps", "linked", second),
                          [(e["kind"], e["action"], e["path"]) for e in turn.entries])
        finally:
            turn.lock.release()


if __name__ == "__main__":
    unittest.main()
