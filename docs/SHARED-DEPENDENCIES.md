# Shared dependencies: node_modules per lockfile, a cargo target per agent

Lot #248 (L231), after the 2026-10-11 19:20 outage: the btrfs METADATA of
pc-smichea was full. 144 `node_modules` and 6 Rust `target/` directories in
nexlink worktrees held millions of inodes. Reflinks share data blocks, not
inodes, so they don't help here. The fix is to keep one copy per host and
point worktrees to it.

## node_modules: one read-only copy per lockfile

```
<cache_root>/nm/<key>/                        one entry per key
    .complete.json                            written last: the entry is complete
    .links/<sha1 of a link path>              one small file per worktree link
    .last-alive                               last time the GC saw a live link
    <label>/package.json, package-lock.json
    <label>/node_modules/                     read-only, except .cache/ and .vite/
<cache_root>/nm/<key>.lock                    the entry's lock (never deleted)
<worktree>/<label>/node_modules -> <cache_root>/nm/<key>/<label>/node_modules
```

* `<cache_root>` is the housekeeping cache root (`$AMEESH_CACHE_DIR` in a
  turn; default `<state>/.menage/cache`, on disk).
* `<label>` is the package's path in its repository (`mobile`,
  `shared/remote-help`, `_root` for a package at the top), the same in every
  worktree of the repository whatever the worktree's directory is called.
* `<key>` is a sha256 of what decides the installed tree: `package-lock.json`,
  `package.json`'s lifecycle scripts (`preinstall`, `install`,
  `postinstall`, `prepare`), the package files these scripts name
  (`node scripts/patch-ios-webrtc.cjs`), `patches/**`, `.npmrc`, the label,
  the install mode, the node and npm versions, the OS and the CPU. Node and
  npm are asked from the package's directory, where a version manager (mise,
  asdf) picks them (probes kept 10 s, so a changed pin counts from the next
  command). The fill runs with that node first on `PATH` and with the npm
  beside it, the binary the key asked (a shim run from the cache would pick
  the machine default): a checkout that pins node 22 gets a tree built by
  node 22 and its npm, not by the machine default. A source
  change doesn't change the key. A lockfile, patch, node or npm change gives
  a new entry. The link target depends only on the key.
* **One install mode**: a fill is always a full development install,
  `npm ci --include=dev --include=optional --include=peer
  --ignore-scripts=false`. Its environment is a whitelist: `PATH`, `HOME`,
  `LANG`, `LANGUAGE`, `LC_*`, `TMPDIR`/`TMP`/`TEMP` and the npm download
  cache (`npm_config_cache`); no `NODE_ENV`, no other `npm_config_*`, nothing
  an install script could read, so the key needs no environment. Every fill
  also gets `SHARP_IGNORE_GLOBAL_LIBVIPS=1`: with a libvips installed on the
  host, sharp otherwise builds from source and the fill fails. The
  explicit flags win over an npmrc. A turn that asks
  for another tree (`NODE_ENV=production`, `npm ci --omit=dev`) gets a local
  install from the real npm, never the shared tree.
* **Filling**: `npm ci` runs once, in a unique temporary directory
  `<cache_root>/nm/<key>.tmp-<pid>-<random>`, under
  `<cache_root>/nm/<key>.lock`, so a second agent waits for the first one
  (up to 30 min) instead of installing again. The lock's descriptor is passed
  to npm. Only the key files are copied beside the lockfile for the install,
  and only `package.json` and the lockfile are kept. The tree is then made
  read-only (directories 0555, files without write bits) and the marker is
  written. Finally the directory is renamed to `<key>`, atomically, only if
  `<key>` doesn't exist yet; otherwise it is discarded. A stray install
  script left by a dead fill can only write into ITS temporary directory,
  never into a published entry. If the fill fails, its temporary directory
  is deleted. An install script that writes absolute paths sees the
  temporary path.
* **Read-only on purpose**: nothing can change the shared tree through a link.
  That includes `npm install <pkg>`, a postinstall run again, and
  `rm -rf node_modules/` with a trailing slash, which follows the link.
  `node_modules/.cache` (babel-loader, eslint, find-cache-dir) and
  `node_modules/.vite` (vite, vitest) stay writable. They are shared by the
  worktrees of one lockfile; these tools key their content by hash.
* **Never over a real directory**: without `--replace`, a real
  `node_modules` that appears while the entry fills (a local `npm ci`) is
  kept as it is (`local-kept`), even if it appears at the last moment.
* **Fallback**: if the entry cannot be filled (install error, lock still held
  after the wait), the package gets a local `npm ci`, as before.

### Commands

```
ameesh deps link [PATH…] [--no-fill] [--replace]   link (fill once if needed); default .
ameesh deps local [PATH…]                          swap the link for a local npm ci
ameesh deps status [PATH…]                         per package: key, link, cache state
ameesh deps entries                                the cache entries, files, size, links
```

A PATH is a worktree (its packages are found down to depth 3, never through
`node_modules`, hidden directories or symlinks) or a package directory.
`link` leaves a real local `node_modules` alone unless `--replace` is given.

### `npm` in a turn

`menage.turn_env` puts an `npm` wrapper first on the turn's PATH
(`<state>/.menage/bin-deps/npm`, real binary in `$AMEESH_REAL_NPM`). Agents
keep typing npm:

| Command | In a review worktree | Elsewhere |
|---|---|---|
| `npm ci`, `npm install` (no package), neutral options only (`--prefer-offline`, `--no-audit`, `--silent`…), lockfile in sync with `package.json` | refused | `ameesh deps link --replace`: links the shared tree (fills it once) |
| `npm install <pkg>`, `npm ci --omit=dev`, lockfile out of sync | refused | the shared link is removed, then the real npm installs a local tree |
| `npm update`, `uninstall`, `rebuild`, `link`, `dedupe`, `prune` (and aliases) | refused | a local tree is installed first (`npm ci` from the lockfile, never an empty `node_modules`), then the real npm runs the command on it |
| `npm install -g …`, `npm test`, `npm run …`, anything else | passed through | passed through |

`AMEESH_DEPS=off` passes everything through. `AMEESH_DEPS=allow-review`
allows installs in a review worktree. With `policy.housekeeping.deps: false`,
no wrapper is installed.

In a review worktree there is no parsing to get wrong: ANY argument that
names an install-type command (`ci`, `install`, `i`, `add`, `update`,
`uninstall`, `rebuild`, `link`, `dedupe`, `prune` and their aliases) refuses
the call, wherever it sits; `npm run link` is a false positive, accepted
there. Elsewhere, a form the wrapper does not recognise passes through to
the real npm: the shared tree is read-only, so a stray install fails with
EACCES and changes nothing.

The command is the first word that is an npm command name, wherever it sits
(`npm --fetch-retries 2 ci` is `ci`); the word after `--prefix`, `-C`,
`--location`, `--workspace`… written without `=` is never taken for it.
`--prefix DIR` (`-C DIR`) is followed: the review check and the link apply to
DIR. `--location=project` or `user` is not global. Nothing marks the real
npm as "already wrapped" (the wrapper never finds itself as the real npm),
so an `npm ci` run by a script of `npm run …` comes back through the
wrapper.

At the start of each turn, the runner also links the packages of the agent's
main worktree that have no `node_modules` yet, when their entry is already in
the cache. This hook never installs anything. It covers the git worktree
that holds the turn's directory, nothing outside git and no worktree nested
in it.

### Review worktrees get no dependencies

A review reads the diff. Tests run in the agent's main worktree: check the
branch out there. A worktree is a review worktree when a directory name from
the package up to the worktree's top has the word `review`, `relecture` or
`revue` (`nexlink-codex2-review-ime`, `review-98-r2`, `dh2b-review`). There,
the wrapper refuses every install and the turn-start hook links nothing. This
saves several GB and hundreds of thousands of inodes per review.

### Git

`.gitignore` patterns such as `node_modules/` only match directories, not a
symlink: a linked `node_modules` would show as untracked. On the first link
of a path, `ameesh deps link` adds the anchored path (`/mobile/node_modules`)
to the repository's `info/exclude`, which all its worktrees share, unless git
already ignores the link.

### Tools that follow the link

Node resolves a module through its real path, so the packages of the entry
resolve their own dependencies inside the entry. The real path keeps a
`/node_modules/` segment, so patterns such as Jest's `transformIgnorePatterns`
still match.

Verified on nexlink (pc-smichea, 2026-10-12, #249), one worktree linked to
the cache, node 26.8.1:

| Tool | Result through the link |
|---|---|
| jest, ts-jest (backend, mobile, desktop) | works: backend 3381/3383 (the 2 failures are an order-dependent flake, they pass alone), mobile 1781/1781, desktop 639/639 plus `node --test` 39/39 |
| vitest (website) | works: 902/902 |
| next build (website, Next 16, Turbopack) | **refused** as is: "Symlink [project]/node_modules is invalid, it points out of the filesystem root". Works once `turbopack.root` and `outputFileTracingRoot` are the nearest directory holding both the project and the link's target. The `output: 'standalone'` tree then nests the project under that root and links back to the cache: fine locally, not portable; Docker images install their own tree and are unaffected |
| expo export / Metro (mobile, Expo 54) | works without a config change (1827 modules). The asset URLs carry the cache path (`/assets/____.local/state/ameesh/.menage/cache/nm/<key>/…`): fine for development, not for a release or OTA bundle |
| electron-builder 26 (desktop) | **packages a broken app without an error**: its module walk checks `lstat(node_modules).isDirectory()`, false for a link, so only the direct dependencies reach `app.asar` (10 modules, 3.7 MB, against 54 modules, 12 MB from a real directory); only a "cannot find path for dependency" warning shows. Run `ameesh deps local` before `npm run dist` |
| postinstall scripts that patch `node_modules` (mobile, assist) | run once, during the fill, before the tree is sealed; covered by the key (`patches/**`, script files) |

The fallback is always available: `ameesh deps local` in the package, then
the tool as before. Use it for anything that ships: an electron-builder
package, a release or OTA bundle, a portable Next standalone tree.

### Garbage collection

The periodic housekeeping pass (`ameesh menage`) deletes an entry only when
all of these hold:

* no link points into it: neither a recorded one nor one found in the known
  worktrees (every worktree of the agents' repositories, checked at
  `<worktree>/<label>/node_modules`). Links are recorded BEFORE they are
  created, so a crash leaves a stale record, never a link the GC cannot see;
* `deps_idle` (default 3 days, never less than 24 h) has passed since its
  creation, its last link, and the last time the GC saw a link to it alive;
* no process uses a file in it: current directory, open file, executable or
  mapped file such as a native `.node` module or the electron binary
  (`platform.holders(…, mapped=True)`). If the processes can't be read, the
  entry is kept;
* its lock is free (no fill, no link being made).

Each pass refreshes `.last-alive` of every entry it sees linked. It also
deletes an abandoned temporary fill (`<key>.tmp-…`) in which nothing changed
for 24 h (newest mtime over the whole tree; a walk that doesn't finish
within 30 s keeps it), with no process in it and its key's lock free, and an unpublished `<key>`
directory (no marker, lock free, no process) older than an hour. Everything is read again under the entry's lock right before
deleting. Lock files are never deleted, so two fillers always lock the same
inode. Without a readable registry, nothing is deleted. Each deletion is
logged (`kind` `deps`).

The housekeeping of idle worktrees (#247) never follows a symlink. It skips a
symlinked `node_modules` and never enters `<cache_root>/nm`.

```yaml
policy:
  housekeeping:
    deps: true          # npm wrapper, turn-start links, GC (default true)
    deps_idle: 3d       # an unreferenced entry is deleted after this long
```

## Rust: one CARGO_TARGET_DIR per agent

`deploy/systemd/ameesh-runner-agent@.service.d/cargo-target.conf` sets
`CARGO_TARGET_DIR=%h/.cache/cargo-target/%i`: one build directory per agent
and host, instead of one `target/` per worktree (~14 GB each for nexlink
`desktop/host`). An agent runs one turn at a time, and cargo's own lock
serialises its sub-agents. Install it next to the unit (see the file), then
`systemctl --user daemon-reload`; it applies at each agent's next restart.

Local scripts that hard-code `host/target/debug/...` must read
`$CARGO_TARGET_DIR` when it is set (nexlink: `desktop/src/remote-help/host-supervisor.ts`,
`desktop/README.md`, `desktop/host/tools/*.mjs`, `desktop/host/scripts/*`;
reported to the nexlink orchestrator). CI is unaffected.

## Measuring

`df -i` means nothing on btrfs (inodes are allocated on demand). Two
measurements, before and after:

* metadata used: `/sys/fs/btrfs/<uuid>/allocation/metadata/bytes_used` and
  `total_bytes`;
* inodes: count the files of a sample, e.g.
  `find <worktree>/website/node_modules -xdev | wc -l`, times the number of
  `node_modules` directories found by
  `find ~/development/manaty -maxdepth 4 -name node_modules -not -type l`.
  For the cache itself, use `ameesh deps entries`, which reports the
  `files` of each entry.

## Out of scope

Moving nexlink to pnpm (one global store, hard links) would change the nexlink
repository, its CI and EAS builds. It is a proposal for the owner, not part of
this lot.
