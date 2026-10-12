# Explicit blocking prerequisites (L385)

Use a structured link when an execution lot waits for another execution lot:

```sh
ameesh work dependencies 212 --add 213
ameesh work dependencies 212 --json
ameesh work dependencies 212 --remove 213
```

The first command means that lot #212 waits for lot #213. Without an edit
option, the command lists the declared prerequisites. `--add` and `--remove`
are mutually exclusive. The actor follows the normal `work` session identity
rules; `--actor` has the same meaning as in other work commands.

Both lots must exist and be open for a new link. A lot cannot block itself.
Cycles in the direct prerequisite graph, including transitive cycles, are refused. Graph edits serialize in
one transaction, so concurrent opposite links cannot both commit. Each
successful edit records its actor, updates activity on the waiting lot and
adds an audit event. Repeating an existing addition or absent removal leaves
activity and the audit journal unchanged. A removed link is absent from the
current graph; its addition/removal remains in the audit journal.

A prerequisite blocks while its lot is open. `merged`, `promoted` and `closed`
prerequisites remain visible with `blocking: false` but stop deferring
stagnation. A reopened prerequisite becomes blocking again. Closing a blocker
does not reset activity on the waiting lot: if that lot has already been idle
for six hours, its stale alert can return immediately. Removing a link is an
explicit edit and records ordinary activity.

`work list/show --json` and `progress --json` expose a `dependencies` array
with each blocker ID, current state, `blocking`, creation actor and timestamp.
They also expose the common `planned_wait` result. Human-readable work output
shows the planned wait, and `work show` lists satisfied prerequisites too.
The progress text/page display the wait; the page's lot detail lists all
prerequisites, including satisfied ones.
Dependencies are read in groups without the view's project or rendering
limit; a blocker outside the displayed window still suppresses `stale_lot`
and its reminders. Links combine with existing open commitment dependencies.
A future planned start takes precedence in the displayed `planned_wait`.

A dependency controls inactivity alerts, not permission to run, change state,
merge or deploy. It does not change the lot's state to `blocked` or enforce a
merge gate. Notes and mail are never parsed into dependency edges.

For a commit prerequisite, link to a lot whose delivery includes verifying
that commit in the required target. A hash in a note or a local branch does
not prove the commit is in `main`. Existing merge/content verification can
provide the delivery evidence; the dependency graph does not verify Git
ancestry. If a prerequisite is abandoned, closing it also releases the wait,
so the waiting lot must be reassessed rather than treating abandonment as
proof of delivery.

Migration 0373 adds `work_item_dependencies`. The supervisor may read this
graph but cannot edit it. Current graph writes and audit events commit or
roll back together on both PostgreSQL drivers.
