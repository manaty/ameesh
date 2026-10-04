# The agent mesh — how this chantier works, where it stands, where it goes

**Status:** documentation of the working setup (2026-10-03) and a v1 proposal.
The proposal (§5) waits for the owner's decisions (§6). Companion of
`README.md` (distributed agent-mail AM1–AM9, Slack replacement AM10–AM15) and
of the mesh design of an existing ticket-pipeline orchestrator (see §3).

## 1. State of the art (October 2026)

- **Parallel coding agents isolated in git worktrees** became mainstream in
  February 2026: Claude Code Agent Teams, Codex CLI parallel agents, Windsurf
  parallel Cascade agents, all shipped within two weeks.
- **Several vendors side by side** ("agentmaxxing"): Codex CLI, Claude Code,
  Gemini CLI, each in its worktree, **with the human as the coordinator**.
- **Orchestrator–workers** and multi-angle reviews are common patterns.
- **Agent-to-agent protocols** exist (Google A2A, MCP for tools), but no
  common runtime spans harnesses from different vendors.

Sources: morphllm.com/ai-agent-orchestration;
codex.danielvaughan.com/2026/04/11/agentmaxxing-parallel-multi-cli-orchestration;
morphllm.com/claude-orchestrator; docs.kanaries.net (parallel code agents);
tembo.io/blog/claude-code-multi-agent-orchestration.

**What this chantier adds** to that state of the art:

1. **The coordinator is an agent** (the orchestrator), not the human. The owner
   sets goals and takes the decisions that are his: product, money,
   production, publishing on his behalf.
2. **Cross-vendor reviews with roles by model.** Codex reviews DeepSeek's code;
   Claude reviews security and infrastructure; nobody reviews their own package.
   Different models make different mistakes, so reviews catch more.
3. **Cost-aware routing.** Judgement work (orchestration, reviews) goes to the
   strongest models, which run on monthly plans whose limits must not be
   reached. Implementation goes to DeepSeek, which is billed per token and
   cheap (board 2026-10-07-0800).
4. **A durable blackboard plus a mailbox.** The board is in git: decisions,
   freezes, verdicts, the migration ledger. agent-mail is per-agent inboxes
   delivered by harness hooks. Together they span three different harnesses.
5. **Self-relaunching headless agents** on persistent sessions, independent of
   any vendor's UI.
6. **A software team's process**, enforced by agents:
   - packages, freezes, reviews and merges;
   - a migration ledger;
   - security lessons turned into CI (the 092 rule, now `sql-invariant.yml`);
   - end-to-end verification on staging.

## 2. The setup as it runs (local, one PC)

| Piece | What | Where |
|---|---|---|
| Agents | claude1–2 (Claude Code), codex1–2 (Codex CLI), deepseek1–5 (DeepSeek Harness) | one git worktree each: `~/development/manaty/nexlink-<name>` |
| Runner | `nexlink-agent start <name> <tool> "<brief>" [session]`. One **turn** is a headless call: `claude -p --resume`, `codex exec resume`, `dsh --profile agent --session-id`. The loop resumes the **same session** when mail arrives, and nudges once after 20 min idle. | `~/.local/bin/nexlink-agent`; state in `~/.local/state/nexlink-agents/<name>/` |
| Restart after a reboot | `nexlink-agents`: every agent on its saved session, a resume message, the orchestrator | `~/.local/bin/nexlink-agents` |
| Mailbox | `agent-mail send/inbox/list/status`. Hooks (SessionStart, UserPromptSubmit, PostToolUse, Stop) inject unread mail into the agent's context, or keep a finishing agent running when mail is waiting. Messages from agents never carry the owner's authority. | `~/.local/bin/agent-mail`; inboxes in `~/development/.agent-mail-state` |
| Blackboard | `docs/board/messages/*.md` (one message per decision, freeze or verdict), `docs/board/workstreams/*.md` (one file per package) | the repository, pushed to GitHub |
| Watch | `nexlink-watch`: merges, owner-addressed board messages, blockers, unread mail over 15 min, dead loops | `~/.local/bin/nexlink-watch` (run by the orchestrator) |
| Rules | review ring and roles by model; no self-review; freeze → review → merge; board posts never carry code; never chain a push to a rebase; apply only your own migration; `REVOKE … FROM PUBLIC, anon, authenticated`; production is the owner's act | board 2026-10-03-0115, 2026-10-06-0630, 2026-10-07-0800 |

**What is local and hand-made about it:**
- inboxes are files on one disk;
- the runner is a shell loop per agent;
- sessions are files of each harness (`~/.claude`, `~/.codex`, `~/.dsh`);
- the owner's authority is a convention, not a proof;
- there is one machine: when it lost power on 2026-10-03, everything stopped.

## 3. What an existing ticket-pipeline orchestrator already has

Called *the ticket orchestrator* below; it is a separate, earlier system.

- **Postgres state**: `work_items`, a cross-pipeline state machine
  (`intake → build → qa → merged → promoted`, plus `blocked` and
  `waiting_human`), and a `run_states` ledger.
- **Kubernetes Jobs** for heavy work, and drain on shutdown.
- **A harness adapter**, Codex only: one-shot
  `codex exec`, no session resume.
- **A governance gate**: a push that touches DDL, routes
  or contracts waits for an approval bound to the diff hash, by Slack thread,
  reaction or GitHub comment, and the approval survives a restart.
- **A human loop with SLA**: `waiting_human` is asked by Slack or the issue,
  replies are polled, and the pipeline resumes.
- **Model routing** (OpenAI or DeepSeek), secret
  sanitising, and Ed25519-signed human receipts (Studio).
- **A mesh design not yet built**: resident
  agents on Codex app-server threads, `agent_registry` and `agent_mailbox`
  tables, a Supervisor and a Judgement thread.
- **Limits**: one replica, runs local to the pod, coordination by a 5-minute
  polling sweep, no agent-to-agent messaging.

The two setups complement each other almost exactly:
- **the ticket orchestrator has the distributed infrastructure**: Postgres, Kubernetes, the
  governance gate and the human loop;
- **this chantier has the multi-harness runtime**: three vendors, session
  resume and a real-time mailbox;
- the ticket orchestrator's planned tables are exactly what agent-mail needs to leave the PC.

## 4. Requirements for the distributed version

- **R1.** Agents of one chantier run on several machines (PCs, servers,
  Kubernetes pods) and still exchange mail in real time.
- **R2.** Any harness: Claude Code, Codex, DeepSeek Harness, and future ones
  through an adapter.
- **R3.** One agent runs in exactly one place at a time (a lease).
- **R4.** The owner's authority is **proven** (a signature), never inferred from
  a message's text.
- **R5.** Nothing is lost on a crash or reboot: the mailbox is durable, a turn
  can be resumed, and a dead runner's leases expire.
- **R6.** Transport-agnostic: Postgres first, then Nexlink (agents as members of
  a page), with Slack as an adapter. This is the AM spec's trajectory and the
  standing requirement that Nexlink serves organisations.
- **R7.** Cost and plan awareness: per-agent model and budget, and a view of the
  vendor plans in use.

## 5. Proposal — mesh v1

1. **agent-mail on Postgres.**
   - Tables `agent_registry` (name, chantier, harness, host, lease, status,
     model, budget) and `agent_mailbox` (from, to, body, created, delivered,
     signature), as in the ticket orchestrator's design.
   - `LISTEN/NOTIFY` wakes the recipient's runner at once.
   - The CLI and the hooks keep their interface, so the three harnesses change
     nothing.
2. **One runner per machine**, generalising `nexlink-agent` and the ticket
   orchestrator's Codex runner.
   - Adapters for the three harnesses, with session resume.
   - The runner claims the agents assigned to its host, renews their leases
     and runs turns.
   - It runs as a user service on a PC or as a pod; heavy work can still use
     Kubernetes Jobs.
   - In v1 an agent stays pinned to its host, because harness sessions are
     local files. Moving sessions comes later.
3. **The owner's authority via a governance gate** (as in the ticket orchestrator).
   - Decisions are asked in a thread (Slack now, Nexlink later).
   - The owner's answer is recorded with an Ed25519 signature, bound to what
     it approves (a diff hash, a migration, a production release).
4. **The board stays in git**; a `work_items` state machine tracks the
   packages.
5. **Observability**: a single `mesh list` shows every agent, its host, its
   lease, its unread mail and its budget, replacing `agent-mail list` and
   `nexlink-agent list`.

## 6. Decisions (owner)

| # | Question | Proposal |
|---|---|---|
| M1 | Where the mesh's code lives | a standalone package (`agent-mesh`), usable by other systems and by this chantier |
| M2 | Go to build v1 (§5 points 1–2) | — |
| M3 | Which Postgres | a dedicated Supabase project for agents, rather than another system's schema or Nexlink's databases |
| M4 | First second machine | a small server, so the agents survive the PC being off |
