# Cutover to ameesh v1 — step by step, reversible

This plan moves the project from the v0 scripts (`agent-mail.v0.py`,
`nexlink-agent.v0.sh`, file mailboxes) to ameesh v1, **without ever breaking a
running agent**. It replaces the old bench plan and starts from the draft
[`design/bascule-v1.md`](design/bascule-v1.md). Substantive changes:

* **no more Ed25519 key ceremony on the PC**: a human's authority is
  proven by a signed receipt out of the agents' reach, on their phone, through
  ameesh-approve ([0012](design/decisions/0012-autorite-par-ameesh-approve.md)).
  The agent Ed25519 key remains for provenance (R13) and tests, never
  as human authority;
* a **bootstrap canon** describes members, host, agents and placements
  (spec §4); `ameesh canon sync` is run **on each host before any
  claim**: without an "ok" canon status, no canon agent is
  claimable;
* **orchestrators become turn-based agents**, with `ameesh attach`
  to talk to them directly
  ([0014](design/decisions/0014-orchestrateurs-a-tours-et-placement.md)).

Cutover rules:

* **one change at a time**, and **each step has its rollback**,
  symmetrical, written on this page;
* **v0 stays intact** until step 8: `~/.local/bin/agent-mail`,
  `nexlink-agent`, `~/.local/state/agent-mail` and
  `~/.local/state/nexlink-agents` are neither modified nor deleted;
* **never two loops on the same session**: the v0 loop does not know the
  v1 lease. A v0 loop is stopped, and **its whole process group is
  dead**, before a v1 runner starts for that agent (and conversely
  on rollback). Running both in parallel is reserved for dummy
  agents;
* **authority is proven**: no decision is recognized from its text;
* when in doubt: `ameesh export-v0 --agents <nom>` puts the mail back into the
  file mailbox and v0 takes over again.

Acts reserved to the owner, marked **[P]**: creating repositories and
system users, enrolling their passkey, any spending, any
network exposure.

---

## Prerequisites — v1 finished and end-to-end test green

1. All v1 lots merged with a review OK (spec §13), including L3
   (placement in the claim predicate).
2. On the bench, the end-to-end test and the demo pass, on both
   pilots:

   ```bash
   scripts/test.sh tests.test_bout_en_bout   # canon → runner → thread → gate → receipt → reconciliation
   scripts/demo-v1.sh                        # the same scenario, commented, temporary database erased
   ```

3. The integration gaps found by the test (lot L9) are closed (lot
   L9b) and the test passes **without workarounds**: `canon sync` fills the
   authenticator registry, `ameesh action request` / `fetch-receipt`
   talk to ameesh-approve, action transitions are in the thread, the
   thread of a canon agent follows its team. `tests/banc_v1.py` now only
   plays the phone (SOFTWARE test passkey — **never** in production)
   and the canon PR.

**Rollback:** nothing has changed.

---

## Step 0 — backups and freeze (15 min, no change)

```bash
mkdir -p ~/backups
tar czf ~/backups/v0-$(date +%F-%H%M).tgz -C ~ \
    .local/state/agent-mail .config/agent-mail .local/state/nexlink-agents
sha256sum ~/.local/bin/agent-mail ~/.local/bin/nexlink-agent | tee ~/backups/v0-shas.txt
nexlink-agent list > ~/backups/v0-agents.txt     # agent, tool, state, session
```

Note for each agent its tool and its session
(`~/.local/state/nexlink-agents/<nom>/{tool,session}`): this is the cutover
map. No agent should be in the middle of a long turn.

**Rollback:** nothing has changed; the backup serves all steps.

---

## Step 1 — database and package

**Durable Postgres** ("local" profile,
[0016](design/decisions/0016-stockage.md)): container with automatic
restart and daily `pg_dump` copied off the PC, or system package. The
bench container is not a production database.

```bash
python3 -m venv ~/.local/share/ameesh/venv
~/.local/share/ameesh/venv/bin/pip install -e ~/development/manaty/ameesh
for c in ameesh agent-runner ameesh-approve; do
  ln -s ~/.local/share/ameesh/venv/bin/$c ~/.local/bin/$c
done
# ~/.local/bin/agent-mail STAYS v0 until step 8

install -m 700 -d ~/.config/ameesh
cat > ~/.config/ameesh/config.json <<'JSON'
{ "dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh", "host": "pc-smichea",
  "project": "ameesh", "humans": ["smichea"] }
JSON
chmod 600 ~/.config/ameesh/config.json      # the password: ~/.pgpass (0600)
ameesh migrate
ameesh doctor --notify-test
ameesh import-v0 --dry-run                  # what would be imported, without touching anything
```

Mandatory restore drill: `pg_dump`, restore into a throwaway
database, `ameesh doctor` on it, deletion.

**Rollback:** remove the links from `~/.local/bin` (except
`agent-mail`, still v0); stop the database. Nothing depends on it yet.

---

## Step 2 — bootstrap canon **[P] for creating the repository**

Project canon repository (`manaty/home` per
[0005](design/decisions/0005-canon-okf.md), or a folder of the project
repository), modeled on [`examples/canon/`](../examples/canon/):

* `federation.yaml` (roles: project lead = `human:smichea`);
* one `Member` per human (`membres/smichea.md`, `authenticators: []` for
  now);
* one `Host` `pc-smichea` (lead `human:smichea`; policy: harnesses
  claude, codex, deepseek; `subscription` modes for claude/codex, `api-key`
  for deepseek; **working directories**: `work_roots` per team, with the
  `{agent}` template for one worktree per agent, and `work_dirs` for
  exceptions — see [EXPLOITATION](EXPLOITATION.md#dossier-de-travail-des-agents-l31-l35));
* one `Agent` (with `responsible`, without the `approve` capability) and one `Placement`
  (admission: `hosts: [pc-smichea]`, **without** `cwd`) per agent: claude1–2, codex1–3,
  deepseek1–7 and the two orchestrators. The list and the tools come from
  the step 0 map. A generator `ameesh canon init --from-v0` does not exist
  yet: the cards are written by hand (or by an agent, as a
  draft) and the owner reviews the PR.

Then, on the host:

```bash
git clone <dépôt du canon> ~/canon && echo "AMEESH_CANON=~/canon"   # or "canon" in config.json
ameesh canon check --fetch        # 0 errors; read at the merged commit of origin/main
ameesh canon sync --fetch --bootstrap-ref main   # first time only (see below)
ameesh list                       # canon agents: host, lead, no lease

# all agents still run on v0: keep them out of reach of any
# v1 runner (idle restart, v1 mail) until their own cutover
for a in claude1 claude2 codex1 codex2 codex3 deepseek1 deepseek2 deepseek3 \
         deepseek4 deepseek5 deepseek6 deepseek7 <orchestrateurs>; do
  agent-runner stop "$a"
done
```

No runner is running yet: nothing starts. `canon sync` only writes
the declarative columns (harness, host, folder, lead…), never lease,
session or status, and **never lifts** a stop set by `agent-runner stop`.

Authenticator registry (spec §8.2): the authoritative canonical branch
**never** comes from the commit being read (its own `federation.yaml` could
authorize itself). It comes from the host configuration (`AMEESH_CANON_REF` /
`canon_ref`, set by its lead), otherwise from the manifest of the last commit
already applied (`authenticator_syncs` log in the database), otherwise — first
bootstrap of the registry — from `--bootstrap-ref <branche>` (logged, ignored
afterwards); without any of these, authenticator synchronization is
refused. `--ref` chooses what is read, never what is canonical: the
commit read must be reachable from the trusted remote-tracking
branch, and descend from the last applied commit (never going
backwards; check and writes are serialized by a registry lock).
A change of `ref` in the manifest only takes effect once merged
into the current canonical branch. Known limitation (spec §11): an agent that
shares ameesh's Unix user can rewrite the clone's `refs/remotes/…`
(or the host configuration); only the monotonicity in the database then prevents it
from going backwards. After a **legitimate** rewrite of the canonical branch's
history, everything is refused ("ne descend pas du dernier commit
appliqué" (does not descend from the last applied commit)) until the owner empties the log
(`DELETE FROM authenticator_syncs`) and re-bootstraps with `--bootstrap-ref`.

**Rollback:** remove `AMEESH_CANON` (or `canon` from the configuration).
The registry keeps its columns and its stops, with no effect as long as no
runner is running.

**Several canons** (manaty + Acme, L42): `canons` in
`config.json` or `AMEESH_CANONS` — see
[EXPLOITATION](EXPLOITATION.md#plusieurs-canons-l42-l43--décision-0031). The first
remains the default canon; a host with a single canon changes nothing. Since
L44 (migration 0035), each canon synchronizes its humans' passkeys
into its own registry: give a `ref` to each entry of `canons`, or
bootstrap each subsequent canon once (`ameesh canon sync --canon <id>
--bootstrap-ref main`, logged); then rerun `deploy/sql/role-approve.sql`
(ameesh-approve reads `actions.canon` and `authenticators.canon`).

---

## Step 3 — ameesh-approve **[P]**

1. **Dedicated system user** `ameesh-approve`, distinct from the agents'
   user; its state directory in `0700`, its service token in `0600`:

   ```bash
   sudo useradd --system --create-home ameesh-approve
   sudo -u ameesh-approve ameesh-approve gen-token
   ```

   ameesh, on the agents' side, reads a copy of this token: it allows **requesting**
   an approval, never signing one. Copy in `0600` owned by the agents' user
   (refused otherwise), then in `~/.config/ameesh/env`:
   `AMEESH_APPROVE_URL=http://127.0.0.1:8765` (https, or http on the
   loopback only) and `AMEESH_APPROVE_TOKEN_FILE=<copie>`. ameesh never
   displays or writes it (neither thread nor log), goes through no
   proxy and follows no redirect.
2. **Exposure to the phone — a Nexlink page**
   ([0017](design/decisions/0017-approbation-via-page-nexlink.md)). The service
   only listens on the loopback (`--bind 127.0.0.1 --port 8765`); the Nexlink
   page relays it over HTTPS. The RP ID is the page's domain, the origin
   `https://<domaine>` (`AMEESH_APPROVE_RP_ID`, `AMEESH_APPROVE_ORIGINS`, the
   same on the ameesh side to verify receipts). **Going live is an act of the
   owner**, after the L7 security review OK; a change of
   domain requires re-enrolling the passkeys.
3. **Enrolling the owner's passkey, through the canon**:

   ```bash
   sudo -u ameesh-approve ameesh-approve enroll-link --approver human:smichea
   #   → single-use link, to open ON THE PHONE; the created passkey becomes
   #     a PROPOSAL (file under <état>/proposals/), nothing is active
   ```

   The owner checks the fingerprint out of band, copies the proposed entry
   into `authenticators` of `membres/smichea.md` **by PR**, merges it, then
   `ameesh canon sync --fetch` on each host: it copies
   `Member.authenticators` into the trust registry (spec §8.2), from the
   merged commit of the canonical branch only; `ameesh authenticator
   list` shows the active passkey, with the canon commit. A host whose
   clone lags behind the registry (removal already applied by another
   host) refuses to write to it (`authentificateurs : REFUSÉ … --fetch`): it never
   reactivates a removed passkey.
4. **Real test**: a `shell-noop` action of class `irreversible` stays
   blocked without a receipt (`ameesh action execute` → `[receipt_required]`);
   `ameesh action request <id> --approver human:smichea` files the request and
   writes the link in the thread; once signed on the phone,
   `ameesh action fetch-receipt <id>` retrieves the receipt, verifies it and
   attaches it; execution goes through, and replaying the receipt fails
   (`ameesh receipt verify` → `[replay]`).

**Rollback:** stop the service (`systemctl stop ameesh-approve`) and
remove the Nexlink page. Irreversible actions stay blocked — the
safe behavior. Removing a passkey: a PR that deletes it from the canon, then
`canon sync`.

---

## Step 4 — a pilot agent (`deepseek7`)

In this order, with no shortcut:

1. wait for its current turn to end (`nexlink-agent list`, the agent's
   window);
2. stop the v0 loop and **wait for its whole group to die**:

   ```bash
   PID=$(cat ~/.local/state/nexlink-agents/deepseek7/pid)
   PGID=$(ps -o pgid= -p "$PID" | tr -d ' ')
   SESSION=$(cat ~/.local/state/nexlink-agents/deepseek7/session)
   nexlink-agent stop deepseek7
   while pgrep -g "$PGID" >/dev/null || pgrep -f -- "$SESSION" >/dev/null; do sleep 2; done
   ```

   If a harness survives beyond a turn, `kill -TERM -- -"$PGID"` then
   `kill -KILL -- -"$PGID"`: no process must still hold the session;
3. take over mail and session on this stable state, then lift the stop set
   in step 2 (the prompt puts the agent back in the queue, on the v0 session):

   ```bash
   ameesh import-v0 --agents deepseek7
   agent-runner register deepseek7 deepseek --session "$SESSION" \
       --prompt "Reprise sous ameesh v1 : continue ton lot en cours."
   ameesh canon sync              # restores the canon columns; "ok" status required
   ameesh show deepseek7          # session = $SESSION, status queued, no lease
   ```

4. start the v1 runner for this agent only, then a round trip:

   ```bash
   agent-runner --agents deepseek7 --poll 5
   #   → "bail acquis : deepseek7" (lease acquired), "LISTEN agent_mail, agent_lease"
   ameesh mail send deepseek7 "aller-retour de bascule" --from smichea
   ameesh fil show ameesh         # the message is in the thread, in plain text
   ameesh list                    # lease, unread, budget
   ```

   **Hooks**: the hooks' command line does not change
   (`agent-mail hook <harnais>`). An agent launched by the runner receives
   `AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID` and `AMEESH_LEASE_EPOCH`: the hook only
   delivers if the lease is alive and held by this runner. The folder never
   gives an identity. An external session only has one if it is
   bound (`ameesh mail bind`, L41): see
   [Switching the hooks to v1](#switching-the-hooks-to-v1-step-8-brought-forward-for-the-hooks-decision-0030).

   **Mail during coexistence (until step 8)**: the installed
   `agent-mail` command (`~/.local/bin/agent-mail`, the one the hooks call
   by its absolute path) is still v0: `agent-mail inbox` reads the
   **file** mailbox there, never the database. The v1 runner therefore no longer sends
   the agent to `agent-mail inbox`: a turn triggered by mail,
   events (grouped) or a priority message carries the **content** of the
   v1 messages in its prompt (sender, timestamp, body readable as
   in the thread, reminder that an agent message never has the owner's
   authority). Cap on the **whole** prompt, in UTF-8 bytes
   (`AMEESH_PROMPT_MAIL_MAX`, 20,000 by default, resume summary included);
   a full resume brief plus its frame exceeds that nominal cap, and the mail
   part keeps a 2,000-byte floor — worst case ≈ 22,045 bytes, see
   `docs/EXPLOITATION.md`: oldest first, the remaining count is indicated, and
   those messages stay undelivered for the next turn.

   A single delivery rule, for the runner as for the v1 hook:
   **reserve** under a token (the agent's row is locked in the
   registry and the `owner`/`epoch` lease checked in the same statement; a
   message already reserved is never reserved twice), **show**, then
   **settle** that reservation only, lease still alive. The runner
   settles right after launching the harness and cancels if the turn does not start
   (harness missing, folder missing, launch failure); the hook therefore never
   sees what is in the prompt, and delivers what arrives during the
   turn. Lease lost between reservation and delivery: nothing is settled, the delivery
   is flagged "incertaine" (uncertain) (log and thread); crash: the reservation expires
   or changes epoch, and the message is delivered again, marked "re-livré" (re-delivered)
   — a flagged duplicate, never a loss.

   The agent **replies with `agent-mail send` (v0)** until step 8: the
   reply goes to the v0 file mailbox and the operator sees it as before.
   To check that a v1 message was indeed seen: `ameesh fil show <projet>`
   and the runner's log (the "messages" turn quotes the prompt), rather
   than `agent-mail inbox`.

**Rollback, symmetrical:**

```bash
# 1. no more v1 turns for this agent, then stop its runner (Ctrl-C or
#    SIGTERM: lease returned, harness killed with its group); check nothing remains
agent-runner stop deepseek7
pkill -TERM -f "ameesh.runner.*--agents deepseek7|agent-runner --agents deepseek7"
ameesh show deepseek7                       # lease: —
while pgrep -f -- "$SESSION" >/dev/null; do sleep 2; done
# 2. the mail goes back into the file mailbox
ameesh export-v0 --agents deepseek7
# 3. the v0 loop resumes the SAME session
nexlink-agent start deepseek7 deepseek "<consigne>" "$SESSION"
```

Messages that arrived in the database in the meantime are rewritten into the v0 mailbox; none
is lost.

---

## Step 5 — the other agents, in waves of 1 or 2

Same procedure as step 4, agent by agent: end of turn, v0 stop and death
of the group, `import-v0`, `agent-runner register … --session … --prompt …` (which
lifts the step 2 stop), `ameesh canon sync`. As soon as two or more agents
are cut over, a single runner per host serves them (`agent-runner` without
`--agents`): agents still on v0 remain stopped in the registry, it does not
claim them.

After each wave:

```bash
ameesh list                 # no cut-over agent without a lease; no v0 agent with a lease
ameesh canon check          # valid canon, "ok" status recorded for the host
ameesh fil list             # the threads progress
ameesh doctor --notify-test
```

**Rollback (per agent), symmetrical:** `agent-runner stop <nom>` (no more
new turns; `canon sync` never lifts this stop), wait until its lease
is returned and its group is dead (`ameesh show <nom>`, `pgrep -f` on its
session), `ameesh export-v0 --agents <nom>`, then `nexlink-agent start` on the
same session. To cut it over again later: the step 4 procedure.

---

## Step 6 — the orchestrators, turn-based agents

For each orchestrator (declared in the canon like the other agents):

1. the **monitoring service** that fed the session's `Monitor`
   now drops its findings into the orchestrator's mailbox, as
   events: `ameesh mail send <orchestrateur> "<constat lisible>" --kind
   event [--urgent] --from surveillance`. Events are grouped (at most
   one wake-up every `AMEESH_EVENT_COALESCE` seconds, 120 by default) except
   `--urgent`;
2. end of the interactive session (end of turn, harness exit, **death of the
   group** as in step 4), `import-v0`, `agent-runner register …
   --session … --prompt …`, `ameesh canon sync`: the runner claims it and
   wakes it up with turns (messages, grouped events), on the same
   session;
3. to talk to it directly: `ameesh attach <orchestrateur>` (refused during
   a turn, unless `--wait`). Attach takes the lease as `attach:<utilisateur>@<hôte>`,
   suspends automatic claiming, launches the harness interactively on the
   same session; on exit, the lease goes back to the runner.

**Rollback, symmetrical:** `agent-runner stop <orchestrateur>`, lease
returned and group dead; stop the monitoring's event drops;
`ameesh export-v0 --agents <orchestrateur>`; restart the interactive session
on the same session, with monitoring as `Monitor` in the session.

---

## Step 7 — supervision (systemd)

**The runner**, a user service; the canon is synchronized before any
claim (an unreadable or invalid canon does not prevent startup: it
closes claiming of canon agents, fail closed):

```ini
# ~/.config/systemd/user/agent-runner.service
[Unit]
Description=ameesh — exécuteur de l'hôte (un par machine)
After=network-online.target

[Service]
# L106: explicit PATH — the unit may start before the session imports
# its own (mise shims and installs, node for dsh)
Environment=PATH=%h/.local/share/mise/shims:%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=%h/.config/ameesh/env        # AMEESH_HOST, AMEESH_CANON, AMEESH_APPROVE_* (PATH=… possible)
ExecStartPre=-%h/.local/share/ameesh/venv/bin/ameesh canon sync --fetch
ExecStart=%h/.local/share/ameesh/venv/bin/agent-runner --poll 5
Restart=always
RestartSec=5
# stop: SIGTERM to the runner only, which drains (running turns and background
# work finish, at most AMEESH_DRAIN_SECONDS = 30 min) — see
# EXPLOITATION.md, "Arrêt et redémarrage de l'exécuteur" (Stopping and restarting the runner)
KillMode=mixed
TimeoutStopSec=35min

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now agent-runner
journalctl --user -u agent-runner -f
ameesh doctor --harness     # L106: resolved binaries (path, origin), missing units
```

**One runner per agent** (the form adopted on the workstation): the template
`deploy/systemd/ameesh-runner-agent@.service` (same explicit PATH), one
instance per driven agent — **including an agent held in `ameesh attach`**:
if the interactive session dies, its runner resumes the same
session (L106). `ameesh doctor` flags a driven agent without an enabled unit.

```bash
install -m 0644 'deploy/systemd/ameesh-runner-agent@.service' ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-runner-agent@<agent>
```

After each PR merged into the canon: `ameesh canon sync --fetch` on each
host (by hand or with a timer).

**The monitoring service** of the orchestrators, a user service alongside
the runner.

**Push alerts** (L38): `ameesh notify`, a user service alongside
the runner — example unit `deploy/systemd/ameesh-notify.service`,
configuration and secrets in [EXPLOITATION.md](EXPLOITATION.md#alertes-poussées--ameesh-notify).

**macOS** (L65): the same two user services are launchd **LaunchAgents**,
written and loaded by `ameesh service install` (the host runner and
`ameesh notify`, or one runner per agent with `--agent`); `ameesh service
status` reads launchd, `ameesh service uninstall` removes them. The
installation guide is
[site/docs/guides/install.md](../site/docs/guides/install.md#macos-remote-database-launchd-services-and-keychain).

**ameesh-approve [P]**, a system service under its own user, never the
agents' user:

```ini
# /etc/systemd/system/ameesh-approve.service
[Unit]
Description=ameesh-approve — approbations humaines (passkeys)
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

**Remote database (VPN, WireGuard)**: since L72, the runner rides out a
transient database outage without stopping (see
[EXPLOITATION.md](EXPLOITATION.md#panne-passagère-de-la-base-l72)). Two
DSN settings complement this: TCP *keepalives* so that a dead connection
(LISTEN, psycopg driver) is detected within a minute rather than at the kernel's TCP
timeout (a quarter of an hour or more), and a connection timeout suited to the link:

```bash
# ~/.config/ameesh/env (or AMEESH_DSN of the unit)
AMEESH_DSN='postgresql://ameesh@10.77.0.1:5432/ameesh?keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3&tcp_user_timeout=60000'
AMEESH_CONNECT_TIMEOUT=10   # default since L72 (3 s before)
```

On the server side, `tcp_keepalives_idle = 60` in `postgresql.conf` likewise frees
the sessions of a vanished client.

To watch: cut-over agents without a live lease, unread messages older than 15 min,
expired leases, `ameesh decisions` (pending approvals, unknown outcomes to
settle), canon status per host (`ameesh canon check`).

**Rollback:** `systemctl --user disable --now agent-runner` (leases returned,
harnesses killed with their group), then, agent by agent, the step 4
rollback; `systemctl disable --now ameesh-approve`.

---

## Step 8 — end of life of v0 (after one to two weeks)

* `~/.local/bin/agent-mail` finally points to v1 (link to the venv);
* keep `nexlink-agent`, `nexlink-agents` and the file state **read-only**
  for one more month;
* `nexlink-agents` (restart after reboot) is replaced by the systemd
  services;
* archive the step 0 backup with the cutover date; only delete
  the v0 scripts after a tested restore.

**Rollback:** reinstall the scripts from the backup, point
`~/.local/bin/agent-mail` back to v0, `ameesh export-v0` for the mail, then
the v0 loops (step 4, rollback).

---

## Switching the hooks to v1 (step 8 brought forward for the hooks, decision 0030)

**Owner's action [P].** This section replaces a command in
`~/.local/bin` and modifies the harness configurations
(`~/.claude*/settings.json`, `~/.codex*/hooks.json`). No agent does it
in their place: an agent can prepare the commands, but it is the owner
who runs them and who checks. It brings step 8 forward **for the hooks
only** (decision 0030 "No work without a possible wake-up", point 6,
lot L41): `nexlink-agent` and the v0 file state stay in place.

### Findings

* The workstation's hooks call `~/.local/bin/agent-mail`, which is still
  **v0**, directly or through the local bridge `~/.local/bin/ameesh-session-mail-hook`
  (secondary and tertiary Claude configurations), which sets `AGENT_MAIL_NAME`
  then calls v0.
* v0 **derives the identity from the folder** (`AGENT_MAIL_NAME`, otherwise an alias from
  `~/.config/agent-mail/aliases.tsv` by prefix, otherwise the folder name): a
  session under `~/Work` took on the `orchestrateur` identity of another team.
* v0 reads the **file** mailbox: an external session never sees its
  v1 mail (Postgres). Even bound through the bridge, it only receives the file
  mailbox.
* v1 (`agent-mail hook <harnais>`, L41) never derives the identity from the
  folder: `AGENT_MAIL_NAME` (and the lease if the runner set it), otherwise the
  **session binding** (`ameesh mail bind`, source `session`) found through
  the session identifier that the harness passes to the hook, with a check of the ancestor
  PID if it is bound. With neither: **nothing is delivered, nothing is
  written**, exit 0.

### Before starting

1. ameesh installed with L41 in the venv, schema migrated: `ameesh migrate`
   (applies `0034_liaisons_de_session`), then `ameesh mail bindings` answers
   "aucune liaison de session" (no session binding).
2. **No agent left under a v0 loop** (`nexlink-agent list`: no active
   agent): after the replacement, `agent-mail send` and `agent-mail inbox`
   talk to the database, no longer to the file mailbox. An agent still driven by v0
   would be so without mail: hand it back to the runner first (steps 4 to 6),
   or postpone this section.
3. Pending v0 mail for external sessions: `ameesh import-v0
   --dry-run`, then `ameesh import-v0 --agents <noms>` to move it into the database.
4. Note, for each interactive session to keep reachable, its harness, its
   session identifier and the harness PID (`ps -o pid,args -C claude`,
   `-C codex`). The identifier is the one the harness passes to the hook:
   Claude shows it in `/status` and in the transcript name
   (`~/.claude*/projects/…/<id>.jsonl`), Codex in the session file
   name (`~/.codex*/sessions/…/rollout-…-<id>.jsonl`).

### Backup

```bash
mkdir -p ~/backups/hooks-v1-$(date +%F-%H%M) && cd "$_"
cp -a ~/.local/bin/agent-mail agent-mail.v0
cp -a ~/.local/bin/ameesh-session-mail-hook .
cp -a ~/.config/ameesh/external-session-bindings.json .
for f in ~/.claude*/settings.json ~/.codex*/hooks.json; do
  cp -a "$f" "$(echo "${f#$HOME/}" | tr / _)"
done
sha256sum * > SHA256SUMS
```

### Cutover

Order matters (L46). The local bridge sets `AGENT_MAIL_NAME` (source
`explicit`): as long as it calls `agent-mail`, a v1 behind it
would deliver to the external session the mail of an agent named in the bridge —
including an agent the runner already drives (`coordinateur`), and
would write on its row. The bridge is therefore **removed first**, while
`agent-mail` is still v0 (which only reads the file mailbox and does not touch
the database); v1 only comes afterwards. Since L46, v1 refuses
`explicit` delivery anyway to an agent holding a live lease, and
a hook without a lease now only changes `last_seen` on the row of an
`execute` agent; the order below remains the rule (it also protects an agent
between two turns, without a live lease at that moment).

1. **Replace, in the harness configurations, the hooks that call
   the bridge** with a direct call to `agent-mail` (same events, same
   harness):

   ```text
   /home/smichea/.local/bin/ameesh-session-mail-hook claude   →   /home/smichea/.local/bin/agent-mail hook claude
   ```

   (`~/.claude-secondary/settings.json`, `~/.claude-tertiary/settings.json`.)
   Until point 3, these hooks still call v0, like those of
   `~/.claude`, `~/.codex` and `~/.codex-secondary` today: nothing is read
   from the database. The bridge `ameesh-session-mail-hook` and its file stay in
   place, unused, until the end of life of v0 (step 8).

   ```bash
   grep -c ameesh-session-mail-hook ~/.claude*/settings.json ~/.codex*/hooks.json   # 0 everywhere
   ```

2. **Import the bridge's bindings** (inert for v0; the bridge's `cwd`
   folder is not carried over: it never gives an identity):

   ```bash
   ameesh mail bind --import ~/.config/ameesh/external-session-bindings.json
   ameesh mail bindings
   ```

   The report states, entry by entry, what is imported and what is ignored
   with the reason: `execute` agent (driven by the runner, which delivers its
   mail: this is the expected case for an agent like `coordinateur` after being taken over by
   the runner — L46: the import never forces), agent holding a live
   lease, session already bound to another agent, invalid harness or name. An
   agent not yet known to the registry is registered there as **external**. A
   session resumed since the bridge was written has a **new PID**: re-bind
   it with the current PID, otherwise the hook will refuse it ("pid … absent de
   l'ascendance" (pid … not among the ancestors)):

   ```bash
   ameesh mail bind <agent> --session <id> --harness claude|codex|deepseek --pid <PID du harnais>
   ```

   `--pid` is recommended: without it, anyone who knows the session
   identifier on this host receives the agent's mail. An `execute` agent is
   refused ("agent mené par l'exécuteur ; une session externe lui volerait son
   courrier" (agent driven by the runner; an external session would steal its mail)): reclassify it first (`ameesh set <agent> mode=externe`) if it
   is actually a human session; `--force` is only a last resort.

3. **Replace `agent-mail` with v1** (the link to the venv, like the
   other commands of step 1) — last, once the bridge is removed from the
   configurations:

   ```bash
   ln -sfn ~/.local/share/ameesh/venv/bin/agent-mail ~/.local/bin/agent-mail
   agent-mail --help | head -3          # "agent-mail (mesh v1)"
   ```

   The hooks that call `agent-mail hook <harnais>` and the status bar
   (`agent-mail statusline`) switch to v1 through this link.

### Verification

```bash
grep -c ameesh-session-mail-hook ~/.claude*/settings.json ~/.codex*/hooks.json   # 0 everywhere
ameesh mail bindings
```

* **In a bound session** (ask the agent to run the command, or
  from a shell launched by the session): `agent-mail whoami` displays
  `<agent> [session] liaison <harnais> <id> sur <hôte>, pid <N>, par <qui>`.
  Then `ameesh mail send <agent> "essai de remise v1" --from smichea`
  from a terminal: the message appears at the session's next prompt,
  and `ameesh fil show <projet>` shows it as delivered.
* **In an unbound session**: `agent-mail whoami` answers "identité non
  liée" (identity not bound) (code 1); a message sent to an agent whose name the folder
  would carry **does not appear** in the session, and stays unread in the database
  (`ameesh mail inbox <agent>`).
* A runner agent: its next turn goes as before
  (`ameesh list`, runner log), its hook delivers under its lease.

### Rollback, symmetrical

1. Restore v0: `cp -a ~/backups/hooks-v1-<date>/agent-mail.v0
   ~/.local/bin/agent-mail` (the file, not a link), then `agent-mail
   --help | head -3` ("boîte aux lettres locale" (local mailbox)).
2. Restore the harness configurations from the backup (the hooks
   of the secondary and tertiary Claude configurations call
   `ameesh-session-mail-hook` again), and check the checksums
   (`sha256sum -c SHA256SUMS` in the backup folder, for the
   restored files).
3. v1 mail that arrived in the meantime for external sessions:
   `ameesh export-v0 --agents <noms>` rewrites it into the file mailbox, which
   v0 reads again.
4. The bindings in the database are inert for v0; revoke them if the
   cutover is abandoned: `ameesh mail unbind --session <id> --harness <h>` for
   each (`ameesh mail bindings`).

---

## Updating ameesh to L35 — working directories

Since L31 (v1.3.0), the working directory comes from the `Host` card, no longer from the
`cwd` of the `Placement` cards. A host that had declared nothing saw its agents
receive an empty `cwd` and stay blocked ("dossier de travail absent :
None" (working directory missing: None)). L35 adds `policy.work_dirs`, the `{agent}` template and a transitional
fallback to the old `cwd`.

**What v1.3.0 does with the L35 settings** (local acceptance test below):
`canon check` 1.3.0 says **nothing**; `work_dirs` is ignored; `{agent}` in
`work_roots` is copied **literally** (`/wt/acme-{agent}` for all
agents of the team). A 1.3.0 `canon check` therefore never validates an L35
canon, and a 1.3.0 runner still active (sync at startup and every
`AMEESH_CANON_SYNC_INTERVAL`, 300 s) would write these wrong paths to the registry.
Hence the order: **stop the old one, install L35, validate with L35, then
only publish**.

1. **Prepare without publishing.** Write the settings on a canon branch
   (PR **not merged**): `policy.work_roots` per team, with `{agent}`
   for one worktree per agent (e.g. `nexlink:
   "~/development/manaty/nexlink-{agent}"`), and `policy.work_dirs: {<agent>:
   <chemin>}` for exceptions. Local acceptance test with the L35 binary, outside
   the host's installation and without a database:

   ```bash
   python3 -m venv /tmp/ameesh-l35 && /tmp/ameesh-l35/bin/pip install -q <dépôt ameesh au gel L35>
   git clone -q <url du dépôt du canon> /tmp/canon-l35     # throwaway copy, ~/canon intact
   printf '{}\n' > /tmp/ameesh-config-vide.json            # chemin explicite = fichier existant (#1127)
   AMEESH_CONFIG=/tmp/ameesh-config-vide.json /tmp/ameesh-l35/bin/ameesh canon check \
       --canon /tmp/canon-l35 --ref origin/<branche-PR> --host pc-smichea
   #   expected: 0 errors, no host-policy-invalid, admission-cwd-inherited
   #   or host-work-dir-missing
   ```

2. **Stop the old runner, and with it its periodic sync**:
   `systemctl --user stop agent-runner` (leases returned, running turns killed with
   their group). Do not run **any** 1.3.0 `ameesh canon sync` from here on.
3. **Install L35** (repository installed as editable: `git -C
   ~/development/manaty/ameesh checkout <gel L35>`), then `ameesh --version`
   and `ameesh canon check --fetch` with THIS binary on the current canon
   (still without the new settings): the `admission-cwd-inherited` fallback
   appears there wherever the old `cwd` values are still in use.
4. **Publish**: merge the canon PR, then `ameesh canon check --fetch`
   (L35 binary): 0 errors, no `admission-cwd-inherited` or
   `host-work-dir-missing`.
5. `ameesh canon sync --fetch`: no "cwd hérité" (inherited cwd) or "aucun
   dossier de travail" (no working directory) line; `ameesh list --json` shows a distinct and
   existing `cwd` for each agent.
6. `systemctl --user start agent-runner`. An agent left `blocked`
   ("dossier absent" (folder missing)) restarts on its own as soon as the registry's `cwd` is right.
7. Later, remove the `cwd` from the `Placement` cards (ignored:
   `admission-cwd-ignored`).

**Rollback to v1.3.0 — the package alone is not enough.** An L35 canon
read by 1.3.0 gives literal paths (`…-{agent}`) and ignores `work_dirs`.
In order:

1. `systemctl --user stop agent-runner` (L35 binary).
2. **Make the canon 1.3.0-compatible before any sync**: a PR that removes
   `work_dirs` and every `{agent}`, and keeps only `work_roots: {<équipe>:
   <chemin>}` / `work_root`; `canon check` of the **L35** binary on this
   branch (it validates a subset that 1.3.0 reads identically), then
   merge.
3. **Limitation of 1.3.0**: a single folder per team and per host
   (`work_roots[équipe]` or `work_root/<équipe>`); the `cwd` of the
   `Placement` cards is ignored. Several agents of the same team with
   distinct worktrees (the nexlink case) **cannot** each run in their
   own in 1.3.0. For each of these agents, before restarting: either
   stop it (`agent-runner stop <agent>`), or accept the team's shared folder
   if only one agent is active at a time, or hand it back to v0
   (`ameesh export-v0 --agents <agent>`, step 4).
4. Reinstall 1.3.0 (`git -C ~/development/manaty/ameesh checkout v1.3.0`),
   `ameesh canon check --fetch`, `ameesh canon sync --fetch`, check
   `ameesh list --json` (no `cwd` containing `{`, no active agent without a
   folder), then `systemctl --user start agent-runner`.

**Local compatibility acceptance test** (done for L35 on the example canon,
without database or runner, reading the same canon with both versions):

| Test canon (`atelier`) | L35: orchestre / relecteur | 1.3.0: orchestre / relecteur | `canon check` 1.3.0 |
|---|---|---|---|
| `work_roots: {acme-web: '/wt/acme-{agent}'}`, `work_dirs: {relecteur: /wt/relecture}` | `/wt/acme-orchestre` / `/wt/relecture` | `/wt/acme-{agent}` / `/wt/acme-{agent}` | no findings |
| `work_roots: {acme-web: /wt/acme-web}` | `/wt/acme-web` / `/wt/acme-web` | `/wt/acme-web` / `/wt/acme-web` | no findings |

---

## Updating to L36–L46 — decisions 0030 and 0031

Lots L36 to L46 bring migrations **0030 to 0036**: agent mode and
stop reason (0030), delegation with a deadline (0031), several canons (0032),
session account (0033), session bindings (0034), authenticators per
canon (0035), then the L46 review fixes (0036: `actions.canon`
immutable except for a rebase of the default canon, start time of the bound PID). The
actions marked [P] are the owner's.

**What breaks the old code — everything is updated TOGETHER.**

* **0032**: the key of `canon_state` becomes `(host, canon)`. The old code
  writes the canon status via `ON CONFLICT (host)`: after the migration, each
  of its synchronizations fails (no more unique constraint on `host` alone).
* **0035**: the unique index of authenticators becomes `(canon, facade,
  credential_id)`. The old code inserts via `ON CONFLICT (facade,
  credential_id)`: its passkey synchronization fails.
* **0036**: L42–L45 code that changed the order of the canons would see its
  rebase refused (`actions.canon` immutable) — closed, but blocked.

So **all hosts and all services sharing the database** —
runners (`agent-runner`) of each host, `ameesh-approve`, `ameesh notify`,
`canon sync` timers, v1 `agent-mail` hooks — are stopped, then updated
**together**, before being restarted. No host "lagging behind".

**No code rollback without restoring the database.** The
migrations cannot be undone: putting the old code back on a migrated database
breaks it (see above). The backup taken **before** `ameesh migrate` is the
only rollback.

### Procedure

1. **Stop everywhere** (each host, then [P] the system service):

   ```bash
   systemctl --user stop agent-runner ameesh-notify      # on EACH host
   sudo systemctl stop ameesh-approve                    # [P]
   ```

   Check that no lease remains alive: `ameesh list` (no `bail:`), or
   wait for them to expire.

2. **Database backup, before any migration**:

   ```bash
   pg_dump --format=custom --file ~/backups/ameesh-avant-L46-$(date +%F-%H%M).dump "$AMEESH_DSN"
   ```

   (The step 1 restore drill applies here too.)

3. **Code and migrations**: install ameesh L46 in each host's venv
   (and `/opt/ameesh/venv` for ameesh-approve [P]), then, **only once**:

   ```bash
   ameesh migrate            # applies 0030 … 0036
   ```

4. **[P] SQL roles rerun** (idempotent, they fail without changing anything if
   the contract is not met): ameesh-approve now reads `actions.canon` and
   `authenticators.canon` (otherwise it answers 503), the supervisor reads the
   new columns (`mode`, `stop_reason`, `canon`, `canon_id`,
   `session_bindings.pid_start`…):

   ```bash
   PGOPTIONS='-c search_path=<schéma>' psql "<DSN admin>" -v ON_ERROR_STOP=1 -f deploy/sql/role-approve.sql
   PGOPTIONS='-c search_path=<schéma>' psql "<DSN admin>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
   ```

5. **Reclassify existing external agents by hand.** 0030 gives
   `mode = execute` to every existing row; only an agent BORN from a hook without
   a lease is born `externe`. Every human session already in the registry (interactive
   orchestrator, a human's Codex or Claude session) must be reclassified,
   otherwise the runner could claim it and take over its session:

   ```bash
   ameesh set <agent> mode=externe       # an external agent requires a human lead
   ```

   (Since L46, the runner never claims an `externe` agent, and a hook
   without a lease now only touches `last_seen` on the row of an `execute` agent.)

6. **Restart everywhere** (`systemctl --user start agent-runner ameesh-notify`;
   [P] `sudo systemctl start ameesh-approve`), then the **verification**
   below.

### Visible changes

* **`send all`** now only goes to the sender's **team** (or project)
  (L36); a sender with neither team nor project keeps the
  global broadcast.
* **`work add --assignee` / `work assign`** refuse a name **unknown** to the
  registry and an `externe` agent without `--externe` (L37, rule 2 of 0030);
  L46: a bare name of a known human is normalized to `human:<id>`, a
  temporarily invalid canon no longer blocks, and the refusal says what to do
  (`agent-runner register …`, `human:<id>`, `--externe`).
* **Physical `max_agents`** (the strictest of the `Host` cards of the host's
  canons, L43) **caps the runner**: beyond it, agents stay
  waiting (runner log), nothing is stopped.
* `ameesh mail bind` refuses an `execute` agent unless `--force`; `ameesh adopt`
  revokes the session bindings of the adopted agent (L46).
* v1 `agent-mail` hooks and local bridge: see the section "Switching the hooks to
  v1" above — the bridge is removed **before** `agent-mail` switches
  to v1.

### Verification after migration

```sql
select mode, status, stop_reason, count(*) from agent_registry group by 1, 2, 3 order by 1, 2, 3;
```

* no human session in `execute` (otherwise: step 5);
* `stop_reason`: NULL for every running agent; for `stopped` /
  `dead` ones already in the database, the reason their free text gave (`manuel`,
  `bail_expire`, `retire_du_canon`), NULL otherwise — an old stop without a reason
  raises `stopped_with_mail` if it has mail: restart it or stop it
  again (`agent-runner stop`, reason `manuel`);
* `ameesh canon check` then `ameesh list`: canon agents `idle`, canon `ok`
  on each host; `ameesh alerts`: no unexpected `dead_runner` or
  `orphan_lot`;
* ameesh-approve answers (no 503); `ameesh notify --once --dry-run`.

**Rollback**: stop all services again, **restore the
step 2 backup** (`pg_restore --clean --if-exists -d "$AMEESH_DSN"
<fichier>`), put the old code back on each host, restart. Never the old
code on the migrated database.

---

## Hosting a second canon (Acme) — L45, decision 0031

To run on `pc-smichea` agents declared in the Acme
canon (`~/development/acme/home`, federation
`acme-ingenierie`), alongside those of the manaty canon. The steps
marked [P] are owner actions.

**Prerequisites.** ameesh with lots L42 to L44 installed; `ameesh migrate`
applied (migrations 0030 to 0035); **SQL roles rerun**:
`deploy/sql/role-approve.sql` and `deploy/sql/role-superviseur.sql` (otherwise
ameesh-approve answers 503: it can no longer read `actions.canon`).

1. **[P] ameesh cards in the Acme canon**, through a PR on
   acme/home, reviewed and merged:
   - `federation.yaml`: `extensions: {ameesh: {scope: ameesh}}` (the only free
     key of the OKF Federation schema) — ameesh only reads
     `ameesh/`; the `type: Agent` entries of `org/agent-harness/` (Claude
     Code sub-agents) are ignored instead of making the canon invalid;
   - `ameesh/membres/smichea.md` (Member), `ameesh/hotes/pc-smichea.md`
     (Host: harnesses, providers, modes, `work_dirs` of Acme's agents;
     **same `max_agents` as the manaty card** — the smaller of the two
     caps the whole runner);
   - `ameesh/agents/<agent>.md` and `ameesh/placements/<agent>-pc-smichea.md`
     for each Acme agent (e.g. `acme-docs`, `acme-securite-docs`).
   Check before merging, on the PR branch:
   `ameesh canon check --canon <copie de la branche> --ref HEAD` → 0 errors.
2. **[P] Host configuration** (`~/.config/ameesh/config.json`): switch
   from `canon`/`canon_ref` to a list, with the manaty canon remaining **first**
   (default canon: do not reorder in the same action):
   ```json
   "canons": [
     {"path": "/home/smichea/canon", "ref": "origin/main"},
     {"path": "/home/smichea/development/acme/home", "ref": "origin/main"}
   ]
   ```
   The `ref` of the second canon is its trusted branch for
   authenticators (L44); without it, bootstrap it once:
   `ameesh canon sync --canon acme-ingenierie --bootstrap-ref main`.
3. **Check**, without launching anything:
   - `ameesh canon check`: one block per canon, both valid;
   - `ameesh canon sync --fetch`: each canon only touches its own rows;
     no manaty agent stopped;
   - `ameesh placement check --canon acme-ingenierie`: Acme's agents
     admitted on `pc-smichea`;
   - `ameesh hosts`: origin of each limit (`[manaty]`,
     `[acme-ingenierie]`).
4. **Move an agent to another canon** (e.g. an Acme agent declared
   provisionally in the manaty canon, or `coordinateur`): [P] remove its
   card from the manaty canon (PR merged, the agent is stopped "retiré du
   canon" (removed from the canon), its name is freed), then [P] declare it in the Acme
   canon: at the next synchronization, the row changes canon and
   restarts, **with its mailbox and its history**. In the other order, the second
   canon gets `canon-name-conflict` (local to the card) and writes nothing.
5. **Agents**: `ameesh resume <agent>` (or `ameesh adopt` for an existing
   interactive session) to start them under the runner.

**Rollback.** Remove the second canon from `canons` (or go back to
`canon`/`canon_ref`): the runner sets its status to `invalid`
(`close_unconfigured`), its agents are no longer claimable, **nothing is
stopped or erased**; the manaty canon is not touched. The rows of the second
canon stay in the registry with their `canon` column, ready for a
new attempt.

## Updating to L60–L74

Lots L60, L61, L62, L70, L71, L72 and L74 bring two migrations,
**0041** (L60: `agent_registry.context_max_tokens`, `turn_costs.spend_key`
and its unique index) and **0042** (L70: `budget_limits`, `budget_events`,
`NOTIFY ameesh_budget` trigger). The package now depends on psycopg
(L61). Two scripts, run by the owner from the workstation, perform each
action in separate steps, stop at the first error and display
no secret: [`deploy/mise-a-jour/poste.sh`](../deploy/mise-a-jour/poste.sh)
and [`deploy/mise-a-jour/vm.sh`](../deploy/mise-a-jour/vm.sh). They read
their parameters (server address, server personas, runners of the
old database) from `~/.config/ameesh/bascule.env`, the one from the L57
cutover, outside the repository.

**Unlike L36–L46, nothing breaks the old code.** 0041 and 0042
only add nullable columns, a unique index on a column that
the old code does not write (NULL everywhere), and new tables. A runner
still on the old version therefore runs without error on a migrated database:
we migrate once, then restart each runner **at its first moment
outside a turn**, without a general stop. Rolling back the code does not require
restoring the database.

**What operations will see after the restart.**

* **L60**: a DeepSeek session that re-read more than 15 M tokens in its last
  turn is rotated with a resume summary **before the next turn**. The
  long DeepSeek sessions will therefore rotate at the first turn following the
  update: this is expected. Per-agent setting:
  `ameesh set <agent> context_max_tokens=…` (0 = disabled).
* **L74** (decision 0034): the choice of the plan account changes rule
  (capacity that expires earliest, session continuity). An account
  switch at the first turn is possible, logged with its reason.
* **L70**: `ameesh budget` shows the caps in force. Empty database: the
  host configuration and the defaults apply as before.
* **L72**: a runner survives a transient database outage (status
  "base injoignable" (database unreachable), resumes on its own).

### Workstation — `deploy/mise-a-jour/poste.sh`

The venv `~/.local/share/ameesh/venv` is installed **non-editable**, from
a snapshot `~/.local/share/ameesh/src-<sha>` (link `src-current`).
The `installer` step follows the same practice.

1. `poste.sh verifier`: target databases (without password), runners, SSH
   access to the server. Nothing is modified.
2. `poste.sh sauvegarder`: the server backup (`ameesh-sauvegarde`) dumps the
   mesh database on the VM into its guarded local copy, then `poste.sh`
   retrieves that dump compressed over SSH into `~/backups/ameesh/`, checking
   its size and SHA-256 before publishing it. `pg_dump` of the old database
   follows. The retrieval is bounded and reports a timeout with the bytes
   already written: a slow WireGuard link no longer fails silently.
3. `poste.sh installer [REF]`: snapshot of commit REF, installed with its
   dependencies (psycopg). The previous version is recorded for rollback.
   Running runners keep the old code in memory: continue without
   waiting.
4. `poste.sh migrer`: `ameesh migrate` **only once** for the mesh
   database, shared by the workstation and the server. Then the old database, with its
   own configuration (`config-ancienne-base.json`): **yes, it too**.
   Runner startup only checks that the schema is present, but the
   new code writes `turn_costs.spend_key` at each turn and reads
   `context_max_tokens`: without 0041, its turns would fail.
5. `poste.sh role`: `deploy/sql/role-superviseur.sql` (0041 columns and
   0042 tables in the contract) reapplied on each database **where the role already
   exists**. On the server, as local superuser, over SSH. A missing
   role is not created: that is a separate decision. The same step sets
   `jit = off` on the mesh role `ameesh` and on the mesh database (#3503): the
   client already forces it per session, this covers psql, deploy scripts and
   any new role.
6. `poste.sh redemarrer`: each active `ameesh-runner-agent@<p>` is
   restarted **one by one**: the unit is stopped first (SIGTERM closes
   admission; the turn in progress drains, bounded by `TimeoutStopSec`), the
   safe point is proven (`ActiveState=inactive` and `MainPID=0`), then it is
   started. No idle window is awaited: a persona in a long turn no longer
   blocks or starves the update. Then `ameesh-notify`, then the
   runners of the old database (`UNITES_RESTE`), read with its configuration.
   A unit that does not stay active stops the script. So does a runner that
   held its persona's lease before the restart and does not take it again
   (#502, `bail.sh`): the lease must be held by the unit's own runner (its
   `--runner-id`, else host:pid), at an epoch past the one read before, and
   still valid at the database clock, within 24 tries 5 s apart. A persona
   without a lease before the restart is not checked: a WARNING names it, and
   the summary counts it. This is a check at restart, not monitoring: a lease
   lost after the first good reading is not seen, and a slow runner costs up
   to about 130 s.
7. `poste.sh controler`: `ameesh doctor`, `ameesh budget`,
   `ameesh accounts list`, `ameesh projects`, `ameesh alerts`, failed
   units, `doctor` of the old database. It also proves that the live
   `MainPID` of `ameesh-notify` and of **every active runner** really executes
   the current venv, from its concrete `argv[0]` in `/proc`: the stable
   `ExecStart` launcher (#1969) proves nothing for a process already started
   before the switch (#3311).

### Server — `deploy/mise-a-jour/vm.sh` (from the workstation)

On the server (L55 profile), ameesh is a detached clone in
`/opt/ameesh/src`, installed non-editable in `/opt/ameesh/venv`. The
personas run under `ameesh-runner@<p>` (system service, user
`ameesh`), outside the repository; their memory cap and their slice come from the
`deploy/systemd/systeme/` add-on (L159c, docs/EXPLOITATION.md,
"Plafonds mémoire des agents" (Agent memory caps)). **No migration here**: the server's database is the one that
`poste.sh migrer` has just migrated.

1. `vm.sh verifier`: installed commit and version, units, applied
   migrations.
2. `vm.sh installer [REF]`: the server fetches REF from origin; if it does not
   find it (unpushed branch), it receives it as a git bundle over SSH.
   Detached checkout, installation with psycopg. The previous commit is
   recorded in `/opt/ameesh/precedent`.
3. `vm.sh redemarrer`: refuses as long as 0041 and 0042 are not applied,
   then restarts each server persona outside a turn, one by one, with the
   same lease check as `poste.sh redemarrer` (#502), the database clock and
   the unit's `--runner-id` being read on the server.
4. `vm.sh controler`: active units, fifteen log lines per persona,
   the live `MainPID`'s concrete `argv[0]` against the current venv (#3311),
   `ameesh show`, `ameesh doctor` on the server.

`/opt/ameesh/src` only keeps the files of the installed commit. Since
L135, `deploy/serveur/` (L55, L57) is in main: a main commit keeps
`installer-serveur.sh` replayable from this clone. An older commit
(before L135) makes these files disappear from the clone; the installed copies
(systemd template, backup script) then stay in place.

### Rollback

* **Code, workstation**: `poste.sh retour` reinstalls the previous snapshot
  (the earlier `src-<sha>`), then restarts as in step 6.
* **Code, server**: `vm.sh retour` puts back the commit from
  `/opt/ameesh/precedent`, then restarts.
* **Database**: nothing to undo. The old code ignores the columns and tables
  of 0041 and 0042. The caps set by `ameesh budget` are no longer
  applied by the old code: it falls back to those of the configuration.
  Restoring a step 2 backup (`pg_restore --clean`, runners
  stopped) is only useful in case of damaged data. This rollback loses everything
  written since.

## Updating to 1.6.0

1.6.0 brings together L73 (automatic housekeeping), L94 (under-utilization and
`balance_low`), L95 (accurate budget, `ameesh cost correct`), L96 (road
map, `ameesh plan`, `ameesh work plan`), L105 (mid-turn caps)
and L106 (host shutdown). It brings five migrations, which follow 0042:

| Migration | Lot | Content |
|---|---|---|
| **0043** `grand_livre_corrige` | L95 | `turn_costs.void_reason`, `turn_cost_corrections` table (append-only log) |
| **0044** `feuille_de_route` | L96 | planned dates of `work_items` and `work_packages`, `commitments` table, thread index by sender |
| **0045** `menage` | L73 | `/tmp` columns in `host_resources`, `managed_worktrees` and `housekeeping_log` tables |
| **0046** `plafonds_du_tour` | L105 | `agent_registry.turn_max_seconds`, `turn_mail_max` |
| **0047** `alimentation_hote` | L106 | `host_resources.on_ac`, `battery_percent` (developed under number 0106) |

**As for L60–L74, nothing breaks the old code**: nullable columns
or columns with a default, and new tables. A 1.5.x runner runs
without error on a migrated database. **The reverse is false**: a 1.6.0 runner
writes the 0045 and 0047 columns at each host reading, and reads those
of 0046 at each turn. So we migrate **before** restarting, never after.

The scripts are those of L60–L74, in the same order; only the migration
check of `vm.sh redemarrer` changes (0041 to 0047).

1. **Workstation**: `poste.sh verifier`, `sauvegarder`, `installer [REF]`.
2. **Migrations**: `poste.sh migrer` applies 0043 to 0047 on the mesh
   database, only once, then on the old database. Check:
   `ameesh doctor` no longer reports a missing migration.
3. **Supervisor role**: `poste.sh role` reapplies
   `deploy/sql/role-superviseur.sql` on each database where the role exists. The
   contract now reads `turn_costs.void_reason` and `turn_cost_corrections`
   (0043), the planned dates and `commitments` without its note (0044),
   `managed_worktrees`, `housekeeping_log` and the `/tmp` columns (0045),
   the turn caps (0046) and the host power supply (0047).
   `commitments.note` is content: it is only read with
   `role-superviseur-contenus.sql`, applied separately, by decision. Without
   this step, a supervisor fails on the new columns, without breaking
   anything else.
4. **Stop the ghost agent `orchestrator`**, before the restart. This name
   is registered without a runner driving it. After the update,
   `ameesh doctor` would flag it as a driven agent without a unit (L106), and the
   liveness and under-utilization alerts would count it:

   ```bash
   ameesh show orchestrator            # check: no turn, no open lot
   agent-runner stop orchestrator      # stop reason `manuel`, never restarted on its own
   ```

   A lot still assigned to `orchestrator` is reassigned first
   (`ameesh work assign <lot> <agent>`).
5. **PATH drop-in for the old database's runner.** The repository's
   `ameesh-runner-agent@` and `ameesh-notify` units carry an explicit PATH
   since L106: at host startup, they start before
   the session imports its own (harness not found, `node` missing for
   `dsh`). The old database's unit (the one in `UNITES_RESTE`) was written
   by hand, without this PATH. We add a drop-in to it, without rewriting the unit:

   ```bash
   u=<unité de l'ancienne base>        # e.g. ameesh-runner-<base>
   mkdir -p ~/.config/systemd/user/$u.service.d
   cat > ~/.config/systemd/user/$u.service.d/path.conf <<'INI'
   [Service]
   Environment=PATH=%h/.local/share/mise/shims:%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin
   INI
   systemctl --user daemon-reload
   systemctl --user show $u -p Environment    # the PATH appears there
   ```

   The restart in the next step takes it into account.
6. **Restarting the runners**: `poste.sh redemarrer` (one by one, outside a
   turn, then `ameesh-notify`, then the old database), then, from the workstation,
   `vm.sh installer [REF]` and `vm.sh redemarrer`, which refuses as long as 0041 to
   0047 are not all applied.
7. **Check**: `poste.sh controler`, `vm.sh controler`, then on each
   host:

   ```bash
   ameesh doctor --harness     # harness binaries resolved, path and origin
   ameesh doctor               # no driven agent without an enabled runner unit
   ameesh hosts                # /tmp and power supply in the reading
   ameesh alerts
   ```

   `doctor --harness` must find each harness **through the unit's PATH**
   or its known locations, not through that of an open terminal.

**What operations will see after the restart.**

* **L105**: a driven agent's turn is closed at the safe point after 30 min
  (`AMEESH_TURN_MAX_SECONDS`), and the mail hook delivers at most
  5 messages per turn (`AMEESH_TURN_MAIL_MAX`). The usual long turns
  will be cut and resume at the next turn, in the same session. Per
  agent: `ameesh set <agent> turn_max_seconds=… turn_mail_max=…` (0 =
  unbounded).
* **L73**: turns receive a `TMPDIR` per agent and per session, under
  `<état>/.menage/tmp`. Worktrees created during a turn are removed at
  the end of their lot if they are clean and pushed. `ameesh menage` is a
  dry run by default; nothing outside the marked folders is deleted.
* **L94**: new alerts (`plan_underused`, `idle_capacity`,
  `orchestrator_held`, `host_underused`, `balance_low`), pushed by
  `ameesh notify` by default. A first wave is expected.
* **L95**: the progress page separates spending paid per token from the
  value consumed on plans. `ameesh cost correct` is a read-only
  dry run; `--apply` corrects the ledger in one transaction,
  after reviewing the dry run.
* **L96**: the Gantt chart at the top of `ameesh progress`, dated commitments
  (`ameesh plan`) and the `engagement_overdue` alert.
* **L106**: a host that is not ready (harness not found, database
  unreachable) puts its agents in "hôte non prêt" (host not ready) without stopping them
  (`host_not_ready`). On battery, no new turn below 25 %, clean
  stop below 10 % (`host_power_low`).

**Rollback**: as for L60–L74 (`poste.sh retour`, `vm.sh
retour`). The database stays migrated: the old code ignores the columns and
tables of 0043 to 0047. The corrections from `ameesh cost correct --apply`
remain written; the old code does not read `void_reason` and therefore recounts the
discarded rows.

## Updating to 1.6.2

1.6.2 brings L117 (amendment of 0034): no migration, a simple
restart of the runners. **What operations will see**: the next
new session of a plan agent will go to the account furthest behind
its pace, first to a never-used account. A switch
(`account_switches`, type `bascule`) is therefore expected to the secondary,
then the tertiary, logged with its reason ("sans relevé : 0 % utilisé,
le plus en retard sur son rythme ; avant : …" (no reading: 0 % used, furthest behind its pace; before: …)). Running sessions
stay on their account as long as it is below its threshold. Before the
update, check that the credentials of never-used accounts are present
(`ameesh accounts list`, status `ok`): this is the first time they will be used.
`ameesh --version` (and the first line of `ameesh doctor`) states the installed
version: check for "ameesh 1.6.2" on each host after the update.
Rollback: the old code, with no other action.

## Migrate before installing (L151)

**Finding of 10/10.** An `agent-mail send` failed on a missing column,
between `poste.sh installer` and `poste.sh migrer`. Runners keep
the old code in memory until they restart, but the venv also serves
every command the agents run (`agent-mail send`, `ameesh work`…):
as soon as it is installed, these commands pick up the new code, which reads
columns the database does not have yet.

**Choice: reverse the order, rather than make the code tolerant.** Tolerant
code would have to check for the presence of each new column on every
query, in every future lot: a single omission is enough to bring the outage back,
and these guards would never be removed. The repository's migrations, for
their part, are almost always additive (nullable columns or with a default, new
tables, replaced constraints). Compatibility must also hold on long-lived
connections: adding a column changes the result type of `SELECT *`. Psycopg's
automatically prepared statements can then fail with SQLSTATE `0A000`
(`cached plan must not change result type`), even though the same old query
works on a fresh connection. Automatic preparation is disabled in `db.py`
(#2339); parameters remain bound normally. See the
[Psycopg prepared statement documentation](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html).
We migrate **first**, then install, then restart.

**Order of steps, workstation side** (the previous sections remain valid
for the content of each version):

1. `poste.sh verifier`, `poste.sh sauvegarder`.
2. `poste.sh migrer [REF]` (REF defaults to HEAD, as for `installer`, #1245;
   the L232b release controller always migrates the snapshot of its pinned
   commit): extracts the `src-<sha>` snapshot of REF and runs
   **its** `ameesh migrate --additive-only`, without installing it
   (`PYTHONPATH=<instantané>/src`, with the venv's Python). Mesh database, only
   once, then the old database. `migrate --dry-run` first displays each
   missing migration, "additive" or "NON additive" (NOT additive) with the reason.
   Both preview and execution retain `--additive-only` even if REF is installed.
   After each database migration and the target code's `doctor --probe`,
   `auto-take-probe.py` runs with the **installed** interpreter in isolated mode
   (`python -I`), using the installed auto-take code in dry-run mode and a
   database connection forced read-only. It also reads review proposals even
   when other work is available. This fresh-connection probe cannot inspect
   prepared plans held by existing runners; the connection policy above and the
   warm-connection migration regression test cover that case.
   A failure means the old code cannot serve the migrated schema. It must not
   leave that code in place: the target commit has already passed
   `doctor --probe`, so `poste.sh migrer` finishes the other database's
   migration, then **installs the target commit and restarts this host's
   runners immediately** (#2357), instead of stopping the update. The restart
   is thrown into a transient `systemd-run --user` unit when the user manager
   accepts it, as any restart started from a runner (see
   [AUDITEUR](AUDITEUR.md)); a plain shell without it falls back to a direct
   restart. If that immediate installation or restart fails, stop and prepare a
   compatible fix at once: the database stays migrated, so do not undo it and
   do not let the old code keep serving it. The mesh database is migrated once
   for the whole mesh: install and restart the other hosts without delay
   (`vm.sh installer`, `vm.sh redemarrer`, or the staging sequence), rather than
   waiting for an ordinary rollout. The **staging sequence** (`staging.sh`) sets
   `MIGRER_SANS_REDEMARRAGE=1` for this step: it is not stopped by the failed
   probe, and installs/restarts the hosts itself in the canonical order (agents
   VMs first, poste last, canon §14).
3. `poste.sh installer [REF]`: refuses as long as `doctor --probe` of the new
   code reports a missing migration, on the mesh database or the old one.
   After the automatic fallback above it is already done (and idempotent);
   `poste.sh role` in the next step still applies.
4. `poste.sh role`, `poste.sh redemarrer`, then `vm.sh installer [REF]` and
   `vm.sh redemarrer`. **`vm.sh installer` also refuses** as long as the mesh
   database does not have all of REF's migrations (read from the server's clone,
   `src/ameesh/migrations/`, compared with `schema_migrations`);
   `vm.sh redemarrer` does the same check for the installed code (it
   replaces the hard-coded "0041 to 0047" check).
5. `poste.sh controler`, `vm.sh controler`.

**What is additive.** `ameesh migrate --additive-only` applies nothing
(code 3) if a missing migration contains an action that breaks the previous
code: `drop table`, `drop column`, `drop schema|type`, a view or a
function deleted without being recreated in the same migration, a rename,
a type change, `set not null`, `add column … not null` without a default,
`truncate`, `delete from`. What examining the text cannot see (a
tightened constraint, a rewritten function that changes behavior), the
migration declares with a `-- ameesh: non additive` comment. All
migrations delivered up to 0051 are additive (verified by
`tests/test_migrations_additives.py`).

**Non-additive migration: separate procedure.** `poste.sh migrer` stops
before writing, even when REF is installed or `MIGRER_APRES=1` is set.
A separate owner-approved migration plan must name its exact commit, backup,
drain and recovery procedure. An installed symlink cannot authorise a
non-additive migration; this command never disables its additive guard.
Rollback of such a separately approved operation requires the database backup.

**New dependency.** The new code is run with the packages of the venv
in place. If REF adds a dependency that `ameesh migrate` needs,
`migrer` fails at import: first install that dependency in the venv
(`pip install <paquet>`), then resume at `migrer`.

## Atomic venv switch (#1969)

`poste.sh installer` and `vm.sh installer` (server and agents) no longer run
`pip` in the **live** venv: they prepare a **versioned** venv next to it
(`venv-<sha7>`, with its own pip, dependencies and controls), then switch in a
single rename. A concurrent command (`agent-mail send`, `ameesh work`, a
runner's import) therefore sees either the whole old code or the whole new
code — never the incomplete state of the 16/10 incident
(`ModuleNotFoundError ameesh.platform.process`, missing `ameesh`/code 127).
Controls run **before** the switch: imports (`psycopg`, `ameesh`),
`ameesh --version`, and the console script's shebang (direct, or pip's
`/bin/sh` preamble when the venv path exceeds 127 bytes) pointing at the
versioned venv's Python.
The shebang is checked before executing the script, with a limit of 8192
bytes per preamble line. A refusal identifies the offending line and cause,
including an unexpected interpreter in the `exec` line. Apostrophes work in
direct shebangs and quoted shell executables. If pip emits a shell preamble
with an unquoted apostrophe (a long path without spaces), preparation refuses
it with a quoting diagnostic; use a venv path without apostrophes in that case.

The live `venv` is **not** a venv that a switch rewrites under running
processes: it holds stable **launchers** (`bin/python`, `bin/ameesh`, and a
refusing `bin/pip`). Each launcher reads the `venv-courant` symlink at start
and `exec`s the versioned venv's binary, so the interpreter sees a concrete
path (`venv-<sha7>/lib/pythonX/site-packages`) — a process already running
keeps importing from **its** version, whatever a later switch does (review
B1 of 16/10: a process launched through a symlinked venv kept the symlink
path and could mix old and new submodules).

The per-turn `ameesh` / `agent-mail` shims (#2763) point at that same stable
launcher when the `bascule.py` marker is present: they resolve `venv-courant`
at each call instead of naming the runner's versioned venv. Two runners
started on different versions therefore write the **same** shim instead of
overwriting the shared directory with their own path; the tool version of a
turn no longer depends on the last runner that started one (#3311). Without
the marker (tools venv, worktree, CI) the shim keeps the running venv.

`activer` only replaces the `venv-courant` symlink (temp + `rename()`), so
there is no window without a live version. The **first** conversion of a real
venv does not move or rewrite it: its `lib/` is reused by link as the
previous version `venv-<sha7>` (adopted without pip), only the Python interpreters in `bin/` and
`bin/ameesh` become launchers — a running process imports from `lib/`, not
from `bin/`. `retour` points the symlink back to the recorded previous
version, **without pip**, including that first conversion (review B2: the
agents' rollback used to re-run `preparer`/pip); it is idempotent, and a
rollback before any switch is a no-op. A preparation failure removes its
partial directory and leaves the active version untouched. `src-current` and
`previous-sha` keep their meaning. The helper is
`deploy/mise-a-jour/bascule.py` (`preparer`, `activer --precedent`, `retour`,
`menage`, `etat`), copied to the VM (server and agents) so that install and
rollback use the same version whatever commit is deployed.

Each update creates a complete venv, and `menage` (#3792) stops them from
accumulating: `activer` applies it at the end of a successful switch
(best-effort, it never fails the switch), and
`bascule.py menage --venv <venv>` shows what it would do (`--apply` to remove).
It keeps the **target** of `venv-courant` (read from the symlink, never from a
name pattern), the name recorded in `venv.precedent`, and every venv a live
process names by an absolute path (`/proc/<pid>/cmdline`, or `ps` without
`/proc`). The `venv-courant` symlink and other links are never candidates —
only real directories are enumerated — and nothing is removed when the link,
the share or the processes cannot be read. A venv prepared but never activated
cannot be told apart from an old one: the cleanup may remove it, and
`preparer` rebuilds it. `retour` does not clean up: the version it switches
away from stays on disk for inspection and a retry.

A systemd unit must therefore never name a versioned venv (#4754): a watch or
wake unit armed from a process whose `PATH` held `venv-<sha7>/bin` failed with
203/EXEC once that venv was removed. `ameesh watch` writes the stable path
(`venv-courant/bin/ameesh` next to the versioned venv, else
`~/.local/bin/ameesh`), and the periodic host pass (runner and
`ameesh menage`) rewrites the `ExecStart` of the `ameesh-watch-*` and
`ameesh-wake-*` units written before, journaled as `unit` / `repinned`.

When writing a launcher fails, its temporary file is removed on a best effort
basis and the previous destination stays intact; a cleanup error preserves the
original write error.

Launcher bookkeeping follows `bin/` itself (#2230). An activation that writes
the new launchers and then fails before the switch (full disk, read-only
`bin/`) leaves the marker naming the new version; the next successful
activation still removes every launcher of ours that the live version no longer
provides, so an orphan does not survive an interrupted switch. A script that
exists only in the new version may still be absent for the moment between the
launcher writes and the symlink rename (accepted window: it exits 127 with a
clear message). `retour` renames the symlink **first** and rewrites the
launchers best-effort, so a full disk or a read-only `bin/` cannot block a
manual rollback. A script adopted from the previous version becomes a launcher
when returning, and disappears at the next activation of a version that does
not provide it — by design.

## Update to 1.10.0 (path B, mediated executor)

1.10.0 brings path B, the mediated executor (L107 to L115,
[EXECUTEUR-MEDIEE.md](EXECUTEUR-MEDIEE.md)): a lent device, without database
access, runs agents by talking to the mesh server through `/api/exec/v1`
(contract 1.1). Three migrations follow main's 0057 (0058 to 0061 belong to
other lots):

| Migration | Lot | Content |
|---|---|---|
| **0062** `mediated_executor` | L108 | tables `exec_idempotency`, `exec_host_availability`, `exec_audit` |
| **0063** `executor_identity` | L110 | tables `executor_invitations`, `executors`, `executor_tokens`, `executor_assertion_jti`, `executor_events` |
| **0064** `model_relay` | L111 | `turn_costs.source` (`harness` by default), `executor`, `lease_owner`, `lease_epoch` |

**Nothing changes for a classic host.** The tables are new, the `source`
column has a default, and a 1.9.x executor runs without error on a migrated
database: the three migrations are additive, so the L151 order below
applies unchanged (migrate, install, restart). The spending cap ignores rows
with `source = 'device'`, which no classic host writes. Without a host gate
configured (`AMEESH_HOST_GATE`), the executor behaves as in 1.9.0.

**Nothing is switched on.** The `ameesh serve --exec-only` server, a
Postgres role of its own for the server (`ameesh_exec`, to be created), the
image release key (IMAGE-EXECUTEUR.md §9), the signed image and the
enrolment of a first device (`ameesh host enroll`) are separate deployment
steps, each submitted to the owner.

Rollback: the previous code. The database stays migrated; the previous code
ignores the tables of 0062 and 0063 and the `source` column of 0064 (it then
also counts `device` rows, which only exist once a device has been
enrolled).

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| two loops on the same session | v0 stop **and death of the group** before any v1 start (and the reverse); exclusive lease on the v1 side; `ameesh list` after each step |
| the database goes down during the cutover | the CLI falls back to the v0 files with a warning; the hooks stay silent |
| a message imported twice | `import-v0` moves the files into `imported/` and does not rewrite a message already in the thread |
| invalid or unreadable canon | no new claim of canon agents (fail closed); leases and running turns intact; `ameesh canon check` says why |
| a misplaced agent or one without a lead | not claimable (R14, C4); reason in `status_text` |
| update of a host without declared working directories (L31) | transitional fallback to the old `cwd` (L35); stop the old runner, install L35 and validate with it **before** publishing `work_dirs` / `{agent}`; `canon check` warns `admission-cwd-inherited` / `host-work-dir-missing` |
| rollback to 1.3.0 with an L35 canon | literal `{agent}` paths, `work_dirs` ignored: make the canon compatible (one folder per team) and stop the agents that need their own worktree **before** any 1.3.0 sync |
| an external session takes another agent's identity through its folder (v0 hooks) | hooks switched to v1 (decision 0030, L41): identity through `AGENT_MAIL_NAME` or explicit session binding, ancestor PID checked; without a binding, nothing is delivered |
| an irreversible action without a human | the gate requires a receipt bound to the fingerprint, single-use, with an expiry; unknown outcome → `reconcile`, never an automatic retry |
| ameesh-approve exposed too early | nothing goes live before the L7 review; exposure = owner's act; service on the loopback only |
| fake passkey enrollment | enrollment is only a proposal; out-of-band confirmation and reviewed canon PR before any activation |
| unmerged PR declaring itself canonical (`federation.yaml`) | the trusted branch comes from the host, the last applied commit or the bootstrap, never from the commit read: refusal |
| two concurrent `canon sync` runs (two hosts, an old canon) | registry lock taken before the check, monotonicity: an old commit never reactivates a removed passkey |
| replayed receipt | nonce consumed at launch, in the database (`mesh_consumed_nonces`) |
| the PC's disk dies | daily `pg_dump` off the PC, step 1 restore drill |
