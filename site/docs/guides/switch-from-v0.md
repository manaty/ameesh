# Switch from a v0 setup

ameesh v0 was a set of local scripts: `agent-mail` with a file mailbox under
`~/.local/state/agent-mail`, and a loop script that starts and resumes each
agent's harness session. This guide moves a running team from v0 to ameesh v1
**without ever breaking an agent in progress**. It is adapted from the
step-by-step plan in the repository (`docs/BASCULE.md`, in French).

## Rules of the switch

- **One change at a time**, and **every step has its rollback**, symmetric,
  written below.
- **v0 stays intact** until the last step: the v0 `agent-mail`, the v0 loop and
  their state directories are neither modified nor deleted.
- **Never two loops on the same session.** The v0 loop does not know v1 leases.
  A v0 loop is stopped, **and its whole process group is dead**, before a v1
  runner starts for that agent (and the other way round on rollback).
- **Authority is proven**: no decision is recognised from its text.
- When in doubt: `ameesh export-v0 --agents <name>` puts the mail back in the
  file mailbox and v0 takes over again.

Acts reserved to the owner are marked **[O]**: creating repositories and
system users, enrolling their passkey, any spending, any network exposure.

## Prerequisites

On the bench, the end-to-end test and the demo pass, on both drivers:

```bash
scripts/test.sh tests.test_bout_en_bout
scripts/demo-v1.sh
```

**Rollback:** nothing has changed.

## Step 0: backups and freeze (no change)

Archive the v0 state (mailbox, v0 configuration, the loop's per-agent state),
record the checksums of the v0 scripts, and list every agent with its harness
and **session id**: this is the map of the switch. No agent should be in the
middle of a long turn.

**Rollback:** nothing has changed; the backup serves every later step.

## Step 1: database and package

A **durable Postgres** (a container with automatic restart and a daily
`pg_dump` copied off the machine, or a system package). The bench container is
not a production database.

```bash
python3 -m venv ~/.local/share/ameesh/venv
~/.local/share/ameesh/venv/bin/pip install -e <path to your ameesh clone>
# link ameesh, agent-runner and ameesh-approve into your PATH;
# the v0 agent-mail stays in place until step 8

install -m 700 -d ~/.config/ameesh
cat > ~/.config/ameesh/config.json <<'JSON'
{ "dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh", "host": "<this host>",
  "project": "<project>", "humans": ["<your id>"] }
JSON
chmod 600 ~/.config/ameesh/config.json      # the password goes in ~/.pgpass (0600)
ameesh migrate
ameesh doctor --notify-test
ameesh import-v0 --dry-run                  # what would be imported, without touching anything
```

Do a restore drill: `pg_dump`, restore into a throwaway database, run
`ameesh doctor` on it, drop it.

**Rollback:** remove the links from your `PATH` (not the v0 `agent-mail`);
stop the database. Nothing depends on it yet.

## Step 2: bootstrap canon [O] for the repository

Create the project's canon on the model of
[Write a canon](write-a-canon.md): a `Member` per human (`authenticators: []`
for now), a `Host` for this machine with its policy, and an `Agent` (with
`responsible`, without `approve`) and a `Placement` (`cwd` = the agent's
current working directory) for **every** agent of the step 0 map, including
orchestrators. There is no generator yet: the cards are written by hand (or
drafted by an agent) and the owner reviews the pull request.

```bash
git clone <canon repository> ~/canon       # then AMEESH_CANON=~/canon, or "canon" in config.json
ameesh canon check --fetch                 # 0 errors; read at the merged commit of origin/main
ameesh canon sync --fetch --bootstrap-ref main   # first time only
ameesh list                                # canon agents: host, responsible, no lease

# every agent still runs on v0: keep them out of reach of any v1 runner until their own switch
for a in <every agent of the map>; do agent-runner stop "$a"; done
```

No runner runs yet, so nothing starts. `canon sync` writes only declarative
columns and **never lifts** a stop set by `agent-runner stop`.

**Rollback:** remove `AMEESH_CANON` (or `canon` from the configuration). The
registry keeps its columns and stops, with no effect while no runner runs.

## Step 3: ameesh-approve [O]

1. **A dedicated system user** for ameesh-approve, distinct from the agents'
   user; its state directory `0700`, its service token `0600`:

    ```bash
    sudo useradd --system --create-home ameesh-approve
    sudo -u ameesh-approve ameesh-approve gen-token
    ```

    The agents' side reads a `0600` copy of this token: it lets ameesh
    **request** an approval, never sign one. Set
    `AMEESH_APPROVE_URL=http://127.0.0.1:8765` (HTTPS, or HTTP on loopback only)
    and `AMEESH_APPROVE_TOKEN_FILE=<copy>` in the agents' environment.

2. **Expose it to the phone** over HTTPS through a reverse proxy; the service
   itself listens on loopback only (`--bind 127.0.0.1 --port 8765`). The RP ID
   is the public host name, the origin `https://<host>`
   (`AMEESH_APPROVE_RP_ID`, `AMEESH_APPROVE_ORIGINS`, identical on the ameesh
   side, which verifies receipts; check with `ameesh approve-check`). The
   service starts in the strict profile, and can also terminate TLS itself
   behind a passthrough gateway: see
   [Host ameesh-approve for a team](host-ameesh-approve.md). Going online is an
   owner's act. Changing the host name later requires enrolling passkeys
   again.

3. **Enrol the owner's passkey, through the canon:**

    ```bash
    sudo -u ameesh-approve ameesh-approve enroll-link --approver human:<your id>
    # → single-use link, to open ON THE PHONE; the passkey becomes a PROPOSAL, nothing is active
    ```

    Check the fingerprint out of band, add the proposed entry to
    `authenticators` of your `Member` card **by pull request**, merge it, then
    `ameesh canon sync --fetch` on each host. `ameesh authenticator list` shows
    the active passkey with the canon commit it comes from.

4. **Real test:** a `shell-noop` action of class `irreversible` stays blocked
   without a receipt (`[receipt_required]`); `ameesh action request <id>
   --approver human:<your id>` writes the link into the thread; once signed,
   `ameesh action fetch-receipt <id>` fetches, verifies and attaches the
   receipt; execution passes; replaying the receipt fails (`[replay]`).

**Rollback:** stop the service and remove the exposure. Irreversible actions
stay blocked, which is the safe behaviour. To remove a passkey: a pull request
that removes it from the canon, then `canon sync`.

## Step 4: one pilot agent

In this order, without shortcuts:

1. wait for the end of its current turn;
2. stop its v0 loop and **wait until its whole process group is dead** (no
   process may still hold the session; escalate `TERM` then `KILL` on the
   process group if a harness survives);
3. take over mail and session on that stable state, and lift the stop set in
   step 2:

    ```bash
    ameesh import-v0 --agents <agent>
    agent-runner register <agent> <harness> --session "<session id>" \
        --prompt "Resuming under ameesh v1: continue your current lot."
    ameesh canon sync              # restores the canon columns; canon state "ok" required
    ameesh show <agent>            # session = <session id>, status queued, no lease
    ```

4. start the v1 runner for this agent only, then a round trip:

    ```bash
    agent-runner --agents <agent> --poll 5
    ameesh mail send <agent> "switch round trip" --from <your id>
    ameesh fil show <project>      # the message is in the thread, in clear
    ameesh list                    # lease, unread, budget
    ```

**Hooks:** the hooks' command line does not change (`agent-mail hook
<harness>`, with byte-identical JSON). A harness started by the runner receives
`AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID` and `AMEESH_LEASE_EPOCH`; the hook
delivers only while that lease is alive.

**Rollback, symmetric:** `agent-runner stop <agent>`, stop its runner
(`SIGTERM` releases the lease and kills the harness group), check that the
lease is released (`ameesh show <agent>`) and nothing holds the session,
`ameesh export-v0 --agents <agent>`, then restart the v0 loop on the **same**
session. Messages that arrived in the database meanwhile are written back to
the v0 mailbox; none is lost.

## Step 5: the other agents, in waves of one or two

Same procedure as step 4, agent by agent. Once two or more agents are switched,
a single runner per host serves them (`agent-runner` without `--agents`); agents
still on v0 stay stopped in the registry and are not claimed. After each wave:

```bash
ameesh list                 # no switched agent without a lease; no v0 agent with a lease
ameesh canon check          # canon valid, state "ok" recorded for the host
ameesh fil list             # threads are moving
ameesh doctor --notify-test
```

**Rollback (per agent):** as in step 4.

## Step 6: orchestrators become turn-based agents

1. The monitoring that used to feed the interactive session now drops its
   findings into the orchestrator's mailbox as **events**:
   `ameesh mail send <orchestrator> "<readable finding>" --kind event [--urgent]
   --from monitoring`. Events are coalesced (at most one wake-up per
   `AMEESH_EVENT_COALESCE` seconds, 120 by default) unless `--urgent`.
2. End the interactive session (end of turn, harness exit, **group dead**),
   `import-v0`, `agent-runner register … --session … --prompt …`,
   `ameesh canon sync`: the runner claims it and wakes it by turns.
3. Set its session policy to `taille` (rotation on size and turn duration, never on a change of lot), since it receives messages
   from every lot: `ameesh set <orchestrator> session_policy=taille`.
4. To talk to it directly: `ameesh attach <orchestrator>` (refused during a
   turn, unless `--wait`). On exit, the lease goes back to the runner.

**Rollback, symmetric:** stop it, stop the event feed, `export-v0`, restart the
interactive session on the same session id.

## Step 7: supervision (systemd)

The **runner** as a user service; the canon is synced before any claim (an
unreadable or invalid canon does not prevent start-up: it closes claims of
canon agents, fail closed):

```ini
# ~/.config/systemd/user/agent-runner.service
[Unit]
Description=ameesh runner (one per host)
After=network-online.target

[Service]
EnvironmentFile=%h/.config/ameesh/env        # AMEESH_HOST, AMEESH_CANON, AMEESH_APPROVE_*
ExecStartPre=-%h/.local/share/ameesh/venv/bin/ameesh canon sync --fetch
ExecStart=%h/.local/share/ameesh/venv/bin/agent-runner --poll 5
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**ameesh-approve [O]** as a system service under its own user, never the
agents':

```ini
# /etc/systemd/system/ameesh-approve.service
[Unit]
Description=ameesh-approve (human approvals with passkeys)
After=network-online.target

[Service]
User=ameesh-approve
EnvironmentFile=/etc/ameesh-approve/env       # AMEESH_DSN, AMEESH_APPROVE_RP_ID, _ORIGINS, _PUBLIC_URL
ExecStart=/opt/ameesh/venv/bin/ameesh-approve serve --bind 127.0.0.1 --port 8765
Restart=on-failure
NoNewPrivileges=yes
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

After each merged canon pull request: `ameesh canon sync --fetch` on each host
(by hand or with a timer). Watch: `ameesh alerts --follow --json` (long turn,
idle with mail, dead runner, session too big, stale lot), `ameesh decisions`
(pending approvals, unknown outcomes), the canon state per host. See
[Operate agents](operate-agents.md).

**Rollback:** disable the runner service (leases released, harness groups
killed), then step 4's rollback agent by agent; disable ameesh-approve.

## Step 8: end of life of v0 (after one or two weeks)

Point `agent-mail` to v1, keep the v0 scripts and file state **read-only** for
another month, replace v0's restart mechanism by the systemd services, archive
the step 0 backup with the switch date, and delete the v0 scripts only after a
tested restore.

**Rollback:** reinstall the scripts from the backup, point `agent-mail` back to
v0, `ameesh export-v0` for the mail, then the v0 loops (step 4 rollback).

## Risks and countermeasures

| Risk | Countermeasure |
|---|---|
| two loops on one session | v0 stopped **and group dead** before any v1 start (and the reverse); exclusive v1 lease; `ameesh list` after each step |
| the database goes down during the switch | the CLI falls back to the v0 files with a warning; hooks stay silent |
| a message imported twice | `import-v0` moves files to `imported/` and does not rewrite a message already in the thread |
| invalid or unreadable canon | no new claim of canon agents (fail closed); running leases and turns intact; `ameesh canon check` says why |
| an agent misplaced or without a responsible human | not claimable; reason in `status_text` |
| an irreversible action without a human | the gate requires a receipt bound to the digest, single use, with expiry; unknown outcome → `reconcile`, never an automatic retry |
| ameesh-approve exposed too early | service on loopback only; exposure is an owner's act |
| forged passkey enrolment | enrolment is only a proposal; out-of-band check and reviewed canon pull request before activation |
| an unmerged pull request that declares itself canonical | the trusted branch comes from the host, the last applied commit or the bootstrap, never from the commit read |
| two concurrent `canon sync` (two hosts, an old canon) | registry lock taken before the check, monotonicity: an old commit never re-activates a removed passkey |
| a replayed receipt | nonce consumed at launch, in the database |
| the machine's disk dies | daily `pg_dump` off the machine, restore drill of step 1 |
