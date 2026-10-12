---
type: Runbook
title: "Cutover to ameesh v1 (draft)"
description: "Step-by-step, reversible plan to move the current project from the v0 scripts to ameesh v1: bootstrap canon, passkey approval, turn-based orchestrators."
status: draft
tags: [bascule, runbook, v1]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:00:00+02:00" }
sources:
  - { resource: "../BASCULE.md", title: "Bench cutover plan (replaced by this one when L9 is merged)" }
  - { resource: "specification.md", title: "v1 specification" }
---

# Principles

Those of `BASCULE.md` still hold: one tool at a time, every step
reversible, v0 untouched until the end, `ameesh export-v0` to roll
back. Changes compared with the bench: **no Ed25519 key ceremony**
([0012](decisions/0012-autorite-par-ameesh-approve.md)); a **bootstrap
canon** describes members, host, agents and placements; the orchestrators
become turn-based agents ([0014](decisions/0014-orchestrateurs-a-tours-et-placement.md)).

Acts reserved to the owner (marked **[P]**): creating repositories and
system users, enrolling their passkey, any spending, any
network exposure.

# Step 0 — backups (unchanged)

See `BASCULE.md` step 0.

# Step 1 — database and package

Local Postgres ("local" profile, [0016](decisions/0016-stockage.md)):
container with automatic restart and a daily `pg_dump` off the PC, or
system package. Venv, `pip install -e`, `ameesh` and `agent-runner` links;
`ameesh migrate`, `ameesh doctor --notify-test`. `~/.local/bin/agent-mail`
stays on v0.

# Step 2 — bootstrap canon **[P] for creating the repository**

Project canon repository (`manaty/home` per the agreement in principle in
[0005](decisions/0005-canon-okf.md), or a folder of the project repository) with:

- `federation.yaml` (roles: project lead = `human:smichea`);
- one `Member` per human;
- one `Host` `pc-smichea` (lead `human:smichea`, policy: harnesses
  claude, codex, deepseek; `subscription` modes for claude/codex,
  `api-key` for deepseek);
- one `Agent` and one `Placement` per current agent. The list comes from the v0 state
  (`~/.local/state/nexlink-agents/<nom>/tool`): claude1–2, codex1–3,
  deepseek1–7, plus the two orchestrators. An `ameesh canon init
  --from-v0` tool (to be added to L2 or L9) generates these cards as drafts; the
  owner reviews the PR.

Check: `ameesh canon check` without errors, then `ameesh canon sync`.

**Rollback**: remove `AMEESH_CANON`; the registry keeps its columns.

# Step 3 — ameesh-approve **[P]**

1. Dedicated system user (`ameesh-approve`), distinct from the agents' user.
2. **Exposure to the phone**: WebAuthn requires a secure context (HTTPS or
   `localhost`) and a stable domain (RP ID). The phone cannot reach the PC's
   `localhost`: an HTTPS address reachable by the phone is needed —
   for example a private network with a certificate (Tailscale-like), a Nexlink
   page, or a small domain. **Decision: a Nexlink page**
   ([0017](decisions/0017-approbation-via-page-nexlink.md)); putting it online remains an owner act.
3. Enrolling the owner's passkey: the service produces a PR to the
   canon (`Member.authenticators`); the owner merges it.
4. Test: a `shell-noop` action of class `irreversible` stays blocked without
   a receipt, goes through with a receipt signed on the phone, and replaying the receipt fails.

**Rollback**: stop the service; irreversible actions stay
blocked (safe behavior).

# Step 4 — a pilot agent

**Never two loops on the same session**: the v0 loop does not know about the v1
lease, and two harnesses would resume the same session. The parallel run
of the old plan is reserved for independent dummy agents (tests).

For the real pilot (`deepseek7`), in this order:

1. wait for its current turn to end; `nexlink-agent stop deepseek7`;
2. **wait for the whole process group** of the v0 loop to die (no
   harness left on its session);
3. `ameesh import-v0 --agents deepseek7` (mail, session) on this stable state;
4. `agent-runner --agents deepseek7`; round trip; check that the message
   appears in the **thread** (`ameesh fil show`).

**Rollback, symmetrical**: stop the v1 runner for this agent
(SIGTERM, lease released) and wait for its group to die; `ameesh export-v0
--agents deepseek7`; restart the v0 loop on the same session.

# Step 5 — the other agents, in waves

Same procedure as step 4, agent by agent (v0 stop and group death before any v1 start). After each wave: `ameesh list`,
`ameesh canon check`, `ameesh fil list`.

# Step 6 — the orchestrators

1. Monitoring service that drops its events into the
   orchestrator's mailbox (`kind = event`), in place of the in-session `Monitor`.
2. End of the interactive session; the runner wakes the orchestrator through
   turns (batched events).
3. `ameesh attach orchestrateur` to talk directly; on exit, the lease
   returns to the runner.

**Rollback**: `ameesh attach` then stay interactive; restart
monitoring in the session.

# Step 7 — supervision (user systemd)

Like `BASCULE.md` step 7, plus the monitoring service and, under its own
user, `ameesh-approve`.

# Step 8 — end of life of v0

Like `BASCULE.md` step 8.
