# DeepSeek tool placement (#490)

DeepSeek Harness 0.2.0-rc.2 uses `dsh-subprocess-local` 0.1.5-rc.2 to
launch ordinary commands and PTYs through `systemd-run --user --scope`.
These transient `dsh-subprocess-*.scope` units live under `app.slice`, outside
the runner service. The runner's `MemoryMax` and any future weights therefore
do not cover its tool work. See [the read-only study #477](https://github.com/manaty/ameesh/blob/2d93d4ffc68b80b6a63be52a763e930051483092/docs/design/etudes/l477-cgroup-weight-effectiveness.md)
(PR [#461](https://github.com/manaty/ameesh/pull/461)).

## Adapter behavior

On Linux, the DeepSeek adapter adds the packaged `deepseek_cgroup.cjs` preload
first in `NODE_OPTIONS`, preserving other options. This covers headless and attach
launches; the resident ACP driver also uses the adapter environment.

The preload uses Node's synchronous module hooks (**Node >=22.15**). An early
availability check resolves the CLI provider through Node's `exports` rules.
Independently, every actual provider module load is checked before evaluation,
including **profile-local `node_modules` overrides**, absolute URLs used by
the dsh loader, and ESM `import` conditions differing from `require`.
The checked bytes are the source returned by Node's load chain, not `pkg.main`.
The reviewed provider entry is:

```
@deepseek-ai/dsh-subprocess-local 0.1.5-rc.2
a3f85e92ce5eddc824348f83cf685d3e417a26c5f1ab602a81fe1bd13315fe2c
```

The entry's imported helper modules are pinned too:

```
runner-launch-COYGu0Dl.js 674f0acce6cf7969a5cc9a5c188c7a8d76ff766434634b908d9a4b8a659b1c1a
runner.js                16719698906a0508aa7163e8a200dd5eb023ba59fe64f451a2b570936e44d47b
```

For the verified entry only, the loader replaces its `child_process` import
with module-local bindings refusing dsh's native scope probe before any user
command runs. It changes the module source **in memory**, leaving installed
files untouched and Node's global/CommonJS/ESM bindings intact. The provider
selects its existing process-group/PTY fallback;
ordinary commands, their children, and `setsid` descendants inherit the
caller's cgroup. An unexpected ordinary native scope launch is refused before
execution. Other
subprocesses, `systemd-run` invocations, the user bus and installed npm files
are unaffected, even with an executable named `systemd-run` and identical
arguments. The preload removes its own option before tool children start,
preserving other Node options in ordinary and PTY Node tool processes.

Before installation, the guard refuses any provider file already present in
`require.cache`, and any `NODE_OPTIONS`/`execArgv` `--require`, `-r` or `--import`
resolving to the provider. It does not try to patch an evaluated module. The
guard must precede other `--require` preloads, so those cannot retain an
unguarded `Worker` constructor before interception. The adapter puts it first,
including when repositioning an existing guard option.
The guard itself must use `--require`, not the later ESM `--import` phase,
which could already have populated Node's separate ESM cache.

At installation the guard refuses `--loader`, `--experimental-loader` and
`--experimental-vm-modules` in both `NODE_OPTIONS` and `execArgv` (including
`--flag=value` forms). It also replaces `node:module.register()` with a
throwing function and synchronizes its named ESM binding. Asynchronous loaders
otherwise evaluate modules in an internal loader thread outside the public
`Worker` wrapper and the thread-local synchronous hooks. Worker options are
checked against the same restrictions before construction.

Deliberate evaluation of provider source through `vm.SourceTextModule` inside
the harness process bypasses the module load hook. This remains outside the
threat model: agent tool commands execute in separate processes, whereas this
path requires code deliberately evaluating the provider inside the harness.
Refusing `--experimental-vm-modules` is a low-cost mitigation on the supported
Node runtime, not a general sandbox against arbitrary in-process evaluation
or retained privileged Node bindings.

Node's synchronous module hooks belong to one thread. The guard therefore
wraps `worker_threads.Worker`, updates its ESM binding, and injects its preload
explicitly into worker `execArgv`, even for **`execArgv: []`**, file workers,
**`eval: true`**, and nested workers. A worker installs a thread-local hook
that **refuses every provider load before evaluation**, with a warning and
worker exit 127. It also refuses provider caches and declared provider preloads.
Workers without a provider remain usable.

For a private worker environment, the guard is first in `NODE_OPTIONS` too,
because Node processes those requires before `execArgv`. This keeps a foreign
preload from retaining an unguarded constructor for nested workers.
**`env: SHARE_ENV` is supported without ambient `--require` preloads**; that
combination is refused explicitly because installing the guard first there
would change the shared parent's environment. Existing memory options remain
shared. No temporary shared-environment mutation or after-the-fact repair is
used. Ordinary child Node **processes** still lose the guard — including a
**`dsh` launched as a tool command, or any nested dsh**, which can create
native `dsh-subprocess-*.scope` units under `app.slice` again (the read-only
check below reports them): that accepted residual is separate from
same-process worker threads.

Unavailable protection no longer stops the turn (#1460). A missing dsh package
or provider, a changed or unreviewed provider (such as `dsh-subprocess-local`
0.2.0-rc.2 under `/opt/node/lib/node_modules` on the VMs), unavailable source,
an unexpected module format or export, or an unsupported Node module-hook API
logs `[ameesh] WARNING: DeepSeek cgroup protection unavailable, running dsh
without it: …` on stderr, and dsh runs as installed. At startup no hook is
installed and the preload removes its own option from `NODE_OPTIONS`; a
profile-local provider failing verification later is loaded unmodified. Its
tools may then run in native `dsh-subprocess-*` scopes outside the runner
cgroup: the warning is the only signal, the turn is not `host_not_ready`.
Misuse of the guard still exits 127 before provider execution (provider
preloaded or cached, guard not first or loaded with `--import`, asynchronous
loaders and VM-module flags, a provider in a worker, `SHARE_ENV` with ambient
preloads), classified `host_not_ready` by the headless runner. Review a
provider upgrade and its regression results before changing the pin. A
resident driver reports startup failure; its eventual runner integration must
retain host-error handling.

**Before enabling DeepSeek on a VM.** Check the `node` dsh resolves on its own
PATH is **>= 22.15** (synchronous module hooks), and pin the dsh/provider
version rather than letting it auto-update: an unreviewed upgrade is loaded
unmodified (warning above), so tools may leave the runner cgroup. The same
unguarded path applies when `AMEESH_DSH_BIN` points at a Node wrapper outside
the `@deepseek-ai/dsh` package: the guard cannot resolve the package to verify
it, warns `cannot locate the dsh package/provider …`, and dsh runs as
installed — no longer an exit 127 since #1460, while the misuse refusals above
still are.

## Cleanup limit and follow-up

**We lose dsh's separate scope ownership of each tool range. Descendants that
escape their process group/session may survive dsh disposal or turn shutdown;
they are then caught only by stopping the runner unit** (under its normal
systemd cgroup cleanup policy). Placement still keeps their memory charged to
that unit. Stopping a harness process group alone does not provide this guarantee.

This fixes runner-level placement and cap coverage. It does not promise
exhaustive attribution or cleanup **per turn**. PR
[#449](https://github.com/manaty/ameesh/pull/449) (lot #476, cx1) makes the same
distinction: subreaper adoption can retain uncertainty, not reconstruct every
descendant's originating turn.

Separate lot **#498**, initial estimate **8 h**: a delegated cgroup per turn,
populated before execution, with stable ownership, bounded teardown and
persisted attribution. Cover user/system runner units, guest/mediated execution,
double fork, `setsid`, and handoff between live turns; coordinate with #476 and
#472 and the L159 guard. Mesh-design scheduled its initial study for 14/10,
after #490 and #476 merge, with owner agreement required for host changes.
No delegation, unit, weight or VM change is part of #490. On-VM activation
and measurements require mesh-design's agreement for this lot.

## Read-only placement check

Run on the host as the runner's user, supplying the **runner PID**, not a tool
PID (use `systemctl [--user] show UNIT -p MainPID --value`):

```bash
python -m ameesh.deepseek_cgroup --runner-pid 12345
```

Exit 0 means the observed descendants and attributable dsh scopes are in the
runner cgroup or below it; exit 1 lists outside PIDs/cgroups; exit 2 means
the check could not establish the placement. `/proc` is read only. The PID
embedded in a dsh scope name associates its reparented members with the
observed harness. Unrelated agents' scopes are excluded. An exited harness,
an already reparented fallback child, or a process created after the snapshot
may be unattributable: this is a placement check, not an exhaustive census.

The real-provider regression is offline, with short-lived local commands and
no model call. It requires the installed provider explicitly:

```bash
DSH_TEST_SUBPROCESS_LOCAL=/path/to/node_modules/@deepseek-ai/dsh-subprocess-local \
  PYTHONPATH=src:tests python -m unittest tests.test_l490_deepseek_cgroup -v
```

It proves the native-scope baseline, inherited cgroups for ordinary children,
`setsid` and Node grandchildren, PTY placement, and normal termination. It also
uses **dsh's actual profile loader**, accepts a reviewed profile-local provider,
and runs version/source/export drift unguarded with the warning (no 127), with
the CLI provider unchanged. Standard CI runs the module-local binding,
actual-source/exports verification, incompatible-provider (warning, dsh still
runs) and read-only-check tests without this optional dependency.
Real-provider regressions also reproduce refusal of a previously cached reviewed
provider and declared require/import preloads, and thread-local refusal with
default/empty `execArgv`, `SHARE_ENV`, eval, nested and retained-constructor
workers. Native probes in those worker witnesses are stubbed, never executed.
The PTY fixture waits for `setsid --wait` and the output stream's end before
evaluating placement. VM behavior remains to be measured after authorized deployment.

## Reusable upstream report

**Title:** Allow supervisor-owned cgroup inheritance instead of user-systemd
tool scopes

**Provider:** `@deepseek-ai/dsh-subprocess-local` 0.1.5-rc.2, dsh 0.2.0-rc.2,
Linux cgroup v2, reproducible on systemd 255 and 261.

**Reproduction:** Start dsh in a systemd service with `MemoryMax=6G`. Through
its subprocess service, run `cat /proc/self/cgroup`, then
`setsid sh -c 'cat /proc/self/cgroup'`. Both tools run under
`user@UID.service/app.slice/dsh-subprocess-PID-*.scope`, rather than the caller's
runner service. The scopes have unlimited memory. A PTY uses the same selection.

**Expected:** A supervisor can select inherited containment before any command
runs. An explicit provider setting disabling native Linux scope creation would
allow ameesh to use its existing cgroup; both ordinary and terminal paths should
respect it, retain documented cleanup behavior, and never replay commands after
a potentially successful native launch. A future delegated-cgroup option could
preserve native ownership without leaving the supervisor's resource boundary.
Choosing a sibling slice alone cannot enforce the caller service's `MemoryMax`.

**Current workaround:** A narrowly targeted Node preload refuses the native
capability probe, selecting the provider's existing PGID fallback. It pins the
provider module; an unreviewed upgrade runs without it, after a warning. This
is temporary and loses exhaustive escaped-descendant cleanup at the tool-range
level.

**Requested resolution:** Document and expose an option to disable native
scope creation under an external supervisor, with ordinary/PTY and `setsid`
regressions. Once available, ameesh can remove this preload.

The [upstream repository](https://github.com/deepseek-ai/deepseek-harness)
had `has_issues=false` on 2026-10-12, so no upstream issue could be opened.
Mesh-design requested that this report remain attached to the PR for the owner
to choose the upstream contact channel.
