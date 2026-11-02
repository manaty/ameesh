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
  asdf) picks them, together, once per operation (kept 10 s, so a changed
  pin counts from the next command). The fill uses that same toolchain: its
  node first on `PATH`, the npm beside it (else the turn's npm), and
  `npm ci --prefix <temporary entry>` run FROM the package's directory, so a
  version manager's shim picks there what it picked for the key: a checkout
  that pins node 22 gets a tree built by node 22 and its npm, not by the
  machine default. The fill checks it again, from the same directory and
  environment, before and after `npm ci`: if node or npm no longer answers
  what the key says (a pin changed within those 10 s), the entry is
  discarded, nothing is linked or installed (`local-kept`, with the reason),
  and the next command resolves again. A source
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
  kept as it is (`local-kept`), even if it appears at the last moment or
  while a fill that then fails runs (no local `npm ci` over it).
* **Fallback**: if the entry cannot be filled (install error, lock still held
  after the wait), the package gets a local `npm ci`, as before.

### Commands

```
ameesh deps link [PATH…] [--no-fill] [--replace] [--force]  link; default .
ameesh deps local [PATH…]                          swap the link for a local npm ci
ameesh deps unlink [PATH…]                         same local install, before packaging
ameesh deps status [PATH…]                         per package: key, link, cache state
ameesh deps entries                                the cache entries, files, size, links
```

A PATH is a worktree (its packages are found down to depth 3, never through
`node_modules`, hidden directories or symlinks) or a package directory.
`link` leaves a real local `node_modules` alone unless `--replace` is given.

### Packaging projects (L248b, per package since #5417)

A packager walks the `node_modules` of the app it builds and needs a real
directory there. The guard is per package, not per repository: a desktop
Electron app must not force the backend, the website, the mobile app or the
shared libraries of the same monorepo to keep a local tree.

* A package is a **packaging root** when its own `package.json` declares
  `electron-builder`, `electron-packager`, `@electron/packager`, `pkg`,
  `@yao-pkg/pkg`, `nexe` or `@vercel/ncc` (including npm dependency aliases),
  invokes one of them in a script, or carries electron-builder's `build` key
  or an `electron-builder.{yml,yaml,json,json5,js,cjs,ts}` file. `deps link`
  refuses it and the turn-start hook removes a shared link there.
* A manifest elsewhere in the same git worktree (root, sibling, parent) guards
  only the app it names: electron-builder's `--projectDir DIR` (or its
  `--project` alias), resolved from the manifest's directory and from the
  repository root because an npm script can be run from either, and
  `directories.app` from its `build` key or an electron-builder YAML/JSON
  config. A command with no target guards the manifest's own directory and the
  packages below it; a packaging member of an npm workspace also guards the
  workspace root that hoists its tree. Thus `desktop/package.json` with
  electron-builder no longer guards `backend`, `website`, `mobile` or
  `shared/*`.
* `pkg`, `nexe` and `@vercel/ncc` guard the package that declares or invokes
  them and the packages below it (a root tooling script therefore keeps the
  whole tree local); their entry arguments are not resolved.
* A broken manifest in the package or on the way up to the repository root
  refuses the link (it could package this package); a broken sibling is
  ignored, since it cannot be shown to package it. Nested repositories,
  hidden and generated directories, and directory symlinks are excluded.
* The refusal happens before any install or replacement of a local tree.
  `--force` allows a development link explicitly; it does not make the link
  suitable for a release. Real local trees and links outside the shared cache
  are kept.
* `directories.app` is read from `package.json`'s `build` key and from
  JSON/JSON5 and YAML electron-builder files. A JS/TS config, or a YAML
  construct the small reader does not understand, is not inferred: the
  manifest keeps its conservative own-directory guard. Scripts hidden in
  external JS or shell files without declarations or recognized configuration
  cannot be inferred.

Turn-start linking applies the same guard and removes existing shared-cache
links only in the packages a packager reads, without installing dependencies.
The turn-start scan includes child packages even when the root has a lockfile.
Script commands are recognized at command positions (`npx` / `npm exec`, after
shell separators); `tsc --outDir pkg` or `echo electron-builder` do not trigger
the guard, and the `pkg` / `nexe` manifest configuration keys are detected too.
A `file:` dependency of a packaging app is not followed, nor a `cd DIR &&
packager` sequence, a target passed as a variable (`--projectDir "$APP"`) or an
explicit `--config FILE`: run `deps local` in the dependency or the app when
packaging that way.

Before packaging, run in the package directory:

```sh
ameesh deps unlink .
npm run dist
```

`unlink` is an alias of `local`: it removes the symlink and runs the real
`npm ci` locally, using `package-lock.json`, with development, optional and
peer dependencies and lifecycle scripts enabled. It reports the local
installation, leaves the shared cache and other worktrees' links intact, and
returns a failure if npm fails or a requested package has no lockfile, including
mixed requests where other packages succeed. Missing lockfiles are reported
before removing their links. `unlink` and `local` discover the root and its
child packages down to depth 3, even when the root has a manifest or lockfile.
A local tree already
present is kept; use `AMEESH_DEPS=off npm ci` to refresh it before a release.

In a packaging package, the npm wrapper passes install commands to the real
npm after removing any shared link. Thus a subsequent `npm ci` does not
recreate the link there, while `npm ci` in a sibling development package still
links the shared tree. `deps status` warns about an existing link in a
packaging package; its JSON includes `packagers` and `warning`. An invalid
manifest in the package or an ancestor is reported and cannot silently permit
a new link; a broken sibling does not block the other packages.

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
allows installs in a review worktree. With `policy.workspaces.housekeeping.deps: false`,
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

The fallback is always available: `ameesh deps unlink` (or `local`) in the package, then
the tool as before. Use it for anything that ships: an electron-builder
package, a release or OTA bundle, a portable Next standalone tree.

### Nexlink: what links, what stays local (#5417)

`nexlink` is a monorepo without a root manifest. Its package roots, the
development use and the distribution build behind each one:

| Package | Development | Distribution build | `node_modules` |
|---|---|---|---|
| `backend` | tsc, jest | Docker image installs its own tree | shared link |
| `website` | `next dev`, vitest | Next 16 `output: 'standalone'` (the link path appears in the traced tree); Docker images install their own tree | shared link; `deps local` before a portable standalone build |
| `mobile` | Expo / Metro, jest | EAS builds in the cloud; a local `expo export` bundle embeds the cache path in asset URLs | shared link; `deps local` before a local release or OTA bundle |
| `assist` | Expo / Metro, jest | EAS, same as mobile | shared link |
| `shared/*` | tsc, jest | bundled by the apps and Docker images | shared link |
| `desktop` | tsc, jest | electron-builder reads `desktop/node_modules`; a link silently produces a broken app (table above) | **local**, guard refuses a link; `deps unlink` before `npm run dist` |

The desktop manifest no longer guards the other packages (#5417). On the nine
base clones of `ameesh-agents-2` inventoried on 2026-11-02 (26.2 GB, 23.0 GB of
real `node_modules` and builds), the migration is:

```sh
ameesh deps status /home/ameesh/work/<clone>          # what is local/linked/absent
ameesh deps link --replace /home/ameesh/work/<clone>  # links the dev packages, keeps desktop local
```

An entry already in the host's cache is shared: each link adds one inode, and
the bytes freed are those of the local tree `--replace` deleted; a missing
entry is filled once by the first package that links it. `ameesh deps entries`
gives each entry's size and link count. Do not run two migrations in the same
tree at once; `deps link` serialises its own work through the
entry locks, but a concurrent `npm ci` is still a local install.

Measured on ameesh-agents-1 (2026-11-02, `du -s --block-size=1`): the cache
entries are 435.0 MiB for backend, 1073.6 MiB for website, 811.2 MiB for mobile
and 63-87 MiB for each shared package; one clone's twelve real shared trees use
893 MiB and `deps status` finds all twelve entries complete. On
ameesh-agents-2 the nine base clones held 23.0 GB of real `node_modules` and
builds on the same date, with `desktop` as the only packaging root: the trees
of the other packages are the linkable share, and the saving per clone is the
sum of the local trees `--replace` replaces, not the size of the cache.

### Garbage collection

The periodic housekeeping pass (`ameesh menage`) deletes an entry only when
all of these hold:

* no link points into it: neither a recorded one nor one found in the known
  worktrees (every worktree of the agents' repositories, checked at
  `<worktree>/<label>/node_modules`). Links are recorded BEFORE they are
  created, so a crash leaves a stale record, never a link the GC cannot see;
* `deps_idle` (default and floor 24 h) has passed since its
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
    deps_idle: 24h      # an unreferenced entry is deleted after this long
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

For a per-package migration (#5417), measure bytes, not only inodes:
`du -sb <clone>` and `du -sb <package>/node_modules` before, then
`ameesh deps link --replace <clone>`. `deps status --json` says which packages
are now linked and which stay local; `deps entries` gives the bytes the cache
shares. A clone stops storing the trees of the packages that linked; a
packaging package (desktop) stays local and is not part of the saving. The
same count applies to a base clone that never had a link: the bytes freed are
its real trees, already counted once in the cache.

## Out of scope

Moving nexlink to pnpm (one global store, hard links) would change the nexlink
repository, its CI and EAS builds. It is a proposal for the owner, not part of
this lot.
