---
type: Study
title: "Study — automatic bug detection and repair loop"
description: "How an existing ticket orchestrator automatically detects and repairs bugs surfaced by interactions, what works, its limits, and the generic transposition to ameesh."
status: draft
tags: [auto-reparation, signaux, triage, verification, cloture]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-04T03:05:00+02:00" }
stale_after: 2027-04-04
---

# Source

Read-only study of an existing ticket orchestrator, in production,
that receives error and conversation signals, opens lots, has agents
implement the fixes, verifies, has them reviewed and delivers. The
precise references (files, lines) are kept outside this repository (rule
[0015](../decisions/0015-interfaces-des-facades.md)).

# The observed pipeline

1. **Signals**: webhooks from an error tracking tool (with stack trace and
   occurrences), audit alerts (bursts of server errors, deterministic
   fingerprint, recurrence = comment), conversations with an agent (which sorts
   bug / enhancement / question and looks for duplicates), data checker
   (audit of production rows + adversarial second look, verdict
   "explained / data fix / code fix", active or
   historical class), external ticket tool (routing to the repository with a
   confidence threshold). Correlation with deployments by time window.
2. **Three-stage deduplication**: structured fingerprint (type, area,
   observed, expected, discriminators), cheap prefilter, conservative final
   decision (prefer false negatives). Only one active lot per ticket.
3. **Triage**: bug → automatic repair; enhancement → never
   automatic, waits for a human if the requester is not on the team;
   data fix → proposal reviewed by a human, never executed
   alone. Execution failures → one retry, then a deduplicated self-healing
   ticket; infrastructure blockers sent to ops, not to the requester.
4. **Repair**: dedicated clone and branch, reproduce first, smallest
   fix, mandatory tests, report; heavy execution isolated; secrets
   removed from the agent's environment, push token injected only at
   push time. **Guards on the diff**: dependency substitutes, allowed scope,
   contract changes (DDL, routes, response fields, tool schemas)
   → wait for a human with **approval bound to the diff's digest** and a
   replayable patch.
5. **Verification**: local verification commands, then CI, then automatic
   QA (coverage according to file type, green CI), red-CI
   fixer, loop bounded to two; conflicts reported, never resolved alone;
   tracking of abandoned PRs.
6. **Closure**: merge into the integration branch after green QA; the
   promotion to pre-production stays human; idempotent notification of the
   requester at each step.

# Observed limits (not to reproduce)

- decisions read from free text or messaging reactions (an
  unrelated reaction started an execution);
- **implicit approval by silence** after an escalation delay;
- a single replica, ephemeral workspaces, in-memory state → repeatedly
  orphaned lots; non-durable switches;
- transitions inferred by correlation, without an event log;
- no post-deployment monitoring nor reopening on recurrence;
- false alarms from the governance gate and approvals left
  unanswered; a support agent that created more work than it
  saved.

# Transposition to ameesh (generic capabilities)

| # | Capability | ameesh already has | Missing |
|---|---|---|---|
| A1 | signal intake (authenticated webhooks, email, thread) | thread, inbox | `signals` table, adapters |
| A2 | classifier (nature, severity, confidence; infra vs decision) | — | everything |
| A3 | fingerprint and two-level deduplication (occurrence, class) | — | everything |
| A4 | correlation with a deployed version | — | everything |
| A5 | **self-repair policy in the canon** (sources, natures, severities, budgets, sensitive paths, durable switch) | canon, review policies | the schema |
| A6 | repair execution (reproduce first, report, diff guards) | runner, sessions | diff guards |
| A7 | change gate on the diff's digest, replayable patch | receipts, action gate | wiring |
| A8 | verification evidence (command, output, digest) | `qa`/`build` states | evidence |
| A9 | request to a human (recipient resolved by the canon, deadline by severity, substitutes, escalation) — **never implicit approval for an irreversible action** | `waiting_human`, decision queue | deadlines and escalation |
| A10 | post-delivery monitoring, reopening on recurrence, requester notification | — | everything |
| A11 | self-healing of executions (retry, then lot) | — | everything |
| A12 | sweep with watchdog, blocked lots, summary in the thread | partial | the rest |
