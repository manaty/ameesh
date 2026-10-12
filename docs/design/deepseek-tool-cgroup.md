# DeepSeek tool placement (#490)

DeepSeek Harness 0.2.0-rc.2 uses `dsh-subprocess-local` 0.1.5-rc.2 to
launch ordinary commands and PTYs through `systemd-run --user --scope`.
These transient `dsh-subprocess-*.scope` units live under `app.slice`, outside
the runner service. The runner's `MemoryMax` and any future weights therefore
do not cover its tool work. See [the read-only study #477](https://github.com/manaty/ameesh/blob/2d93d4ffc68b80b6a63be52a763e930051483092/docs/design/etudes/l477-cgroup-weight-effectiveness.md)
(PR [#461](https://github.com/manaty/ameesh/pull/461)).

## Adapter behavior

On Linux, the DeepSeek adapter adds the packaged `deepseek_cgroup.cjs` preload
to `NODE_OPTIONS`, preserving existing options. This covers headless and attach
launches; the resident ACP driver also uses the adapter environment.

Before dsh starts, the preload resolves its actual installed provider and
checks its version and the SHA-256 of `lib/index.js`. The reviewed module is:

```
@deepseek-ai/dsh-subprocess-local 0.1.5-rc.2
a3f85e92ce5eddc824348f83cf685d3e417a26c5f1ab602a81fe1bd13315fe2c
```

The preload refuses only dsh's native scope probe, before any user command
has run. The provider selects its existing process-group/PTY fallback;
ordinary commands, their children, and `setsid` descendants inherit the
caller's cgroup. Named ESM `child_process` bindings are updated too. An
unexpected ordinary native scope launch is refused before execution. Other
subprocesses, `systemd-run` invocations, the user bus and installed npm files
are unaffected. The preload removes its own option before tool children start.

A missing, changed or unreviewed provider logs a warning and exits 127 with
no stdout/session. The headless runner classifies this as `host_not_ready`,
preserves the instruction and emits its existing operational alert. Review a
provider upgrade and its regression results before changing the pin. There is
no silent fallback to an unreviewed native scope path. A resident driver reports
startup failure; its eventual runner integration must retain host-error handling.

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

It proves the native-scope baseline, inherited cgroups for ordinary children
and `setsid`, PTY placement, and normal termination. Standard CI runs the
policy, incompatible-provider and read-only-check tests without this optional
dependency. VM behavior remains to be measured after authorized deployment.

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
provider module and fails loudly on upgrades. This is temporary and loses
exhaustive escaped-descendant cleanup at the tool-range level.

**Requested resolution:** Document and expose an option to disable native
scope creation under an external supervisor, with ordinary/PTY and `setsid`
regressions. Once available, ameesh can remove this preload.

The [upstream repository](https://github.com/deepseek-ai/deepseek-harness)
had `has_issues=false` on 2026-10-12, so no upstream issue could be opened.
Mesh-design requested that this report remain attached to the PR for the owner
to choose the upstream contact channel.
