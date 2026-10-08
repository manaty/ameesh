# Sessions

A **session** is the harness's own conversation: a Claude Code session, a Codex
thread, a DeepSeek Harness session. ameesh does not replace it; it records the
session id and resumes the **same** session at every turn, so the agent keeps
its context between messages.

## Resume

The session id is stored in the registry and in the agent's state directory on
the host. After a crash, the runner reads it back from the registry, else from
the file. `ameesh attach` opens the same session interactively.

## Rule: one fresh session per lot

Long sessions degrade. The project's rule (design decision 0025) is:

1. **Build and review agents open a fresh session for each lot**, started with
   a self-contained brief (state, scope, rules, known defects). The session is
   **kept until the lot is merged**, reviews and fixes included, then dropped.
2. **Reviewers** open a fresh session per reviewed lot, kept for re-freezes of
   the same lot.
3. **Orchestrators** keep a long session, rotated with a resume summary when it
   grows, plus written state (memory, log) that survives the loss of the
   session.
4. The size of the context re-read per turn is watched; a session that misses
   instructions is rotated without waiting.

## How ameesh applies it

Each agent has a **session policy**, set with `ameesh set <agent>
session_policy=…` (default `AMEESH_SESSION_POLICY`, `par-lot`):

| Policy | Effect |
|---|---|
| `par-lot` | a fresh session when the lot of the turn differs from the session's lot, opened on a resume summary; rotation on size and turn duration stays as a safeguard |
| `taille` | rotation on size and turn duration only: for orchestrators, which receive messages from every lot |
| `jamais` | no rotation |

The `session_too_big` alert (`ameesh alerts`) watches the tokens re-read per
turn, and `ameesh restart <agent> --brief FILE` starts a fresh session on a
brief at any time. See [Operate agents](../guides/operate-agents.md).

## Rotation with a resume summary

Whatever the policy (except `jamais`), when a session exceeds `AMEESH_SESSION_MAX_TOKENS` (default 150 000) or a turn
lasted more than `AMEESH_SESSION_MAX_TURN_SECONDS` (default 900 s), after at
least `AMEESH_SESSION_MIN_TURNS` turns (default 3) and **never during a turn**:

1. a summary turn is played in the current session;
2. the summary is written to the readable thread;
3. the old session id is kept in the agent's session history;
4. the next turn opens a **new** session, prefixed with the summary.

## Adopting and resuming a session

Since v1.4.0, putting a session back under ameesh is an operation of ameesh,
never a prompt such as "resume session X":

- `ameesh adopt <agent> --session ID --harness H` puts an **existing
  interactive session**, once closed, under the runner. It finds the session
  file under the harness's declared accounts and records the **account of
  origin** of the session.
- `ameesh resume <agent> [--fresh] [--brief FILE]` restarts a stopped, dead or
  idle agent. It keeps the recorded session when the account of the next turn
  can resume it; if the old account is still usable but the session cannot
  move, the runner rotates at the first turn with a summary written under the
  account of origin; otherwise (or with `--fresh`) the fresh session opens on
  a **deterministic resume brief** that ameesh builds without calling a model
  (identity and role, open lots and what they wait for, recent thread
  exchanges, unread count, path of the old transcript).

See [Operate agents](../guides/operate-agents.md#adopt-an-interactive-session-ameesh-adopt).

## Moved working directories

When an agent is registered, the git identity of its working directory
(common repository and branch) is recorded. If the directory disappears (for
example a renamed worktree), a **unique** candidate is searched under
`AMEESH_WORKTREE_ROOTS`, with bounded depth. It is adopted (registry updated,
thread notified) only if it is not already another agent's working directory.
The working directory itself comes from the host's policy (see
[Runner and leases](runner-and-leases.md#working-directory)); a directory that
is missing blocks the agent until it comes back.

## Model and effort per agent

```bash
ameesh set <agent> model=<model> effort=<effort> tier=<tier>   # an empty value goes back to the default
```

The change takes effect at the **next turn**. The canon carries defaults and
caps; `ameesh set` writes execution state.
