---
type: Study
title: "Study — the persona across harnesses: identity, prompts and memory from one session to the next"
description: "What Claude Code, Codex, DeepSeek Harness and an ACP agent offer to designate a persona and keep its memory across sessions, through relay and in parallel; specification of the Persona card, of sessions, of designation to the harness and of a per-persona memory in its own git repository, neutral, replaceable and possibly lasting; visibility rule checked at placement."
status: draft
tags: [persona, session, memoire, harnais, visibilite, double-numerique, mcp, acp]
generated: { by: "etude-persona/claude-opus-5-5", at: "2026-10-05T21:00:00+02:00" }
stale_after: 2027-01-05
sources:
  - { resource: "../decisions/0029-persona-et-session.md", title: "Decision 0029 (persona and session)" }
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Decision 0025 (one session per lot)" }
  - { resource: "../decisions/0027-bascule-automatique-entre-comptes.md", title: "Decision 0027 (accounts, relay)" }
  - { resource: "../decisions/0028-ressources-des-hotes-et-repartition.md", title: "Decision 0028 (dynamic placement)" }
  - { resource: "../decisions/0007-standards-des-harnais.md", title: "Decision 0007 (MCP, skills, AGENTS.md)" }
  - { resource: "../decisions/0021-catalogue-des-harnais.md", title: "Decision 0021 (harness descriptors)" }
  - { resource: "comptes-multiples.md", title: "Study of multiple accounts" }
  - { resource: "standards-des-harnais.md", title: "Study of harness standards" }
  - { resource: "https://code.claude.com/docs/en/memory", title: "Claude Code: CLAUDE.md and auto memory" }
  - { resource: "https://code.claude.com/docs/en/cli-reference", title: "Claude Code: options, system prompts on resume" }
  - { resource: "https://code.claude.com/docs/en/hooks", title: "Claude Code: hooks (SessionStart, compaction)" }
  - { resource: "https://learn.chatgpt.com/docs/agent-configuration/agents-md", title: "Codex: AGENTS.md" }
  - { resource: "https://learn.chatgpt.com/docs/customization/memories.md", title: "Codex: memories" }
  - { resource: "https://developers.openai.com/codex/config-reference", title: "Codex: configuration reference" }
  - { resource: "https://developers.openai.com/codex/hooks", title: "Codex: hooks" }
  - { resource: "https://agentclientprotocol.com/protocol/session-setup", title: "ACP: session/new, session/load" }
  - { resource: "https://agentclientprotocol.com/protocol/extensibility", title: "ACP: _meta and extensions" }
---

# Question

Decision [0029](../decisions/0029-persona-et-session.md) separates the
**persona** (durable identity, without position) from the **session**
(ephemeral execution in a harness). It entrusts memory to the future in-house
harness and leaves ameesh only the designation of the persona. Until that
harness exists — and perhaps instead of it — we need to know:

1. how a persona keeps **its** memory and its identity from one session to
   the next with the current harnesses (Claude Code, Codex, DeepSeek Harness
   `dsh`, and any ACP agent through the bridge of lot L16);
2. what survives the **relay** (change of account or harness,
   [0027](../decisions/0027-bascule-automatique-entre-comptes.md)) and the
   host **move** ([0028](../decisions/0028-ressources-des-hotes-et-repartition.md));
3. what happens when **several sessions of the same persona** run in
   parallel (one per lot, [0025](../decisions/0025-une-session-par-lot.md));
4. criterion added by the owner: can an interim solution that is **neutral
   with respect to the harness** be flexible enough to **last**, to the point
   of doing without an in-house harness?

# Method

As for the [study of multiple accounts](comptes-multiples.md), without any
paid call, without logging into an account, without reading or displaying any
secret:

* `--help` of the harnesses, `codex features list`;
* strings of the binaries (Claude Code, Codex) for the **names** of settings
  and variables;
* installed documentation of DeepSeek Harness (READMEs of the
  `@deepseek-ai/dsh-*` packages) and online documentation (see the sources);
* inventory of the **structure** of a workstation's configuration directories
  (`~/.claude`, `~/.codex`, `~/.dsh`): file and directory names only, never
  the content of credentials, conversations or memories;
* reading of ameesh's code (`canon.py`, `adapters.py`, `runner.py`) and of the
  branch of lot L16 (descriptors, ACP bridge), under review.

Versions observed: Claude Code 2.1.286, Codex CLI 0.159.3, DeepSeek Harness
0.1.5-rc.2.

**Levels of certainty** used in the tables:

* **verified**: observed on the workstation (help, binary string, directory
  structure, installed README);
* **documented**: written in the official documentation, not tested here;
* **assumed**: inferred, to be checked in a lot (conformance test bench L17).

Column legend: **account** = the location changes when the account changes
(`CLAUDE_CONFIG_DIR`, `CODEX_HOME`); **directory** = it depends on the
working directory; **R / W / I / G** = ameesh can **r**ead it, **w**rite it,
**i**nject it at launch, **g**et it back after the session.

# Claude Code

## Mechanisms

| Mechanism | Location | Account? | Directory? | R / W / I / G | Certainty |
|---|---|---|---|---|---|
| User CLAUDE.md, user `rules/` | `<config>/CLAUDE.md`, `<config>/rules/` (`<config>` = `~/.claude` or `CLAUDE_CONFIG_DIR`) | **yes** | no | yes / yes / yes / — | documented; tie to the configuration directory assumed (everything is stored there, see the accounts study) |
| Project CLAUDE.md, `.claude/rules/` | `./CLAUDE.md`, `./.claude/CLAUDE.md`, ancestors; subdirectories on demand | no | **yes** | yes / yes (but versioned in the repository) / — / — | documented |
| CLAUDE.local.md | project root, not versioned | no | **yes** | yes / yes / — / — | documented; **its presence prevents AGENTS.md from being read** by default |
| AGENTS.md | working directory and ancestors, if there is no CLAUDE.md | no | yes | yes / yes / — / — | documented (≥ 2.1.277) |
| `@path` imports | in a CLAUDE.md, four levels | — | — | — | documented; an import outside the directory requires interactive approval the first time (**unusable without a human**) |
| Additional directory | `--add-dir D` + `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` loads `D/CLAUDE.md`, `D/.claude/rules/` | no | no | yes / yes / **yes** / — | documented; variable verified in the binary |
| Appended system prompt | `--append-system-prompt[-file]` | no | no | — / — / **yes** / — | verified (`--help`); **frozen at the first turn** and reused on resume until compaction (`--system-prompt-snapshot`, documented) |
| Replaced system prompt | `--system-prompt[-file]` | no | no | — / — / yes / — | documented; loses the default tool and safety prompts: not recommended |
| Auto memory | `<config>/projects/<project>/memory/MEMORY.md` + one file per memory | **yes** | **yes** (key = git repository, shared between worktrees) | yes / yes / — / yes | documented; structure verified (`memory/MEMORY.md` present in several projects) |
| Auto memory location | `autoMemoryDirectory` setting, accepted **from `--settings`** | no | no | — / — / **yes** / — | documented; name verified in the binary |
| Disabling | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `autoMemoryEnabled: false`, `--bare` | — | — | — / — / yes / — | documented; variable verified |
| Project directory name | `CLAUDE_CODE_PROJECT_DIR_NAME` (with `CLAUDE_CONFIG_DIR`) | yes | no | — / — / yes / — | documented (≥ 2.1.234); verified in the binary |
| Subagent memory | `memory: user/project/local` field → `agent-memory/<name>/` | user: yes; project/local: yes (directory) | depending on scope | yes / yes / — / yes | documented; strings verified |
| `SessionStart` hook | settings (`<config>/settings.json`, project, or `--settings`) | depending on the file | depending on the file | — / — / **yes** (`additionalContext`, 10,000 characters, beyond that: file + preview) / — | documented; triggers `startup`, `resume`, `clear`, `compact`, `fork` |
| `PreCompact` / `PostCompact`, `Stop`, `SessionEnd` hooks | same | — | — | — / — / yes (`Stop` can restart with context) / — | documented; names verified |
| Skills | `<config>/skills/`, `.claude/skills/`, `--plugin-dir` | depends | depends | yes / yes / yes / — | documented; `--plugin-dir` verified |
| MCP servers | `--mcp-config` (+ `--strict-mcp-config`) | no | no | — / — / **yes** / — | verified (`--help`); the `instructions` of an MCP server are shown to the model (observed in a session on this workstation) |
| Session transcript | `<config>/projects/<project>/<session>.jsonl` | yes | yes | yes / — / — / yes | verified (accounts study) |

## Conclusions for Claude Code

* **Native memory is stored per account and per repository, not per persona.**
  Two personas working in the same repository share a memory; a persona that
  changes repository finds another one; a change of account (another
  `CLAUDE_CONFIG_DIR`) loses it, unless `projects/` is shared through a link,
  as L30 already does to resume sessions — that link then also shares the
  memory, for everyone.
* **ameesh can redirect it** without touching the account's files:
  `--settings '{"autoMemoryDirectory": "<persona directory>"}'`. It can also
  turn it off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`).
* **Designation without writing to the repository**:
  `--append-system-prompt-file` (identity, role, rules), `--add-dir` on a
  persona directory containing a generated `CLAUDE.md` (with the variable
  above), `SessionStart` hook passed through `--settings` to inject the memory
  index, including **after a compaction** (`matcher: compact`).
* The system prompt is **frozen when the session is created**: this is
  compatible with one session per lot; a persona modified in the canon only
  affects subsequent sessions.
* `--bare` turns off CLAUDE.md, hooks, skills and memory all at once: useful
  for a fully driven harness, but it also turns off the agent-mail hooks.

# Codex

| Mechanism | Location | Account? | Directory? | R / W / I / G | Certainty |
|---|---|---|---|---|---|
| Global AGENTS.md | `$CODEX_HOME/AGENTS.override.md`, then `$CODEX_HOME/AGENTS.md` | **yes** | no | yes / yes / yes / — | documented; `AGENTS.override.md` verified in the binary |
| Project AGENTS.md | from the repository root to the current directory (`AGENTS.override.md`, `AGENTS.md`, fallback names) | no | **yes** | yes / yes (versioned) / — / — | documented; `project_doc_max_bytes` limit (32 KiB) |
| Developer instructions | `developer_instructions` key (injected before AGENTS.md), can be passed with `-c` | no | no | — / — / **yes** / — | documented; key verified in the binary; `-c` verified (`--help`); **effect on resume (`exec resume`) assumed** |
| Replaced base instructions | `model_instructions_file` | no | no | — / — / yes / — | documented; not recommended (loses the harness's prompts) |
| Profiles | `-p <name>` → `$CODEX_HOME/<name>.config.toml` | **yes** | no | yes / yes / yes / — | verified (`codex exec --help`) |
| Memories | `$CODEX_HOME/memories/` (`memory_summary.md`, `MEMORY.md`, `raw_memories.md`, `rollout_summaries/`); `[memories]` settings: `generate_memories`, `use_memories`, `disable_on_external_context`, `min_rate_limit_remaining_percent` | **yes** | no (global to the `CODEX_HOME`) | yes / risky / — / yes | `memories` feature **stable but disabled** on this workstation (verified); file names verified in the binary; a `memories_1.sqlite` database exists (verified); generation **in the background, after inactivity**, by the harness (documented); unavailable in some regions at launch (documented) |
| Hooks | `$CODEX_HOME/hooks.json`, `config.toml` `[hooks]`, `<repository>/.codex/hooks.json` | yes (global) / yes (project) | — | — / — / **yes** (`SessionStart` → `additionalContext`, developer message) / — | documented; `SessionStart`, `PreCompact`, `PostCompact` verified in the binary; ameesh already launches with `--dangerously-bypass-hook-trust` |
| `notify` | command called at the end of a turn | yes | — | — / — / — / yes | name verified; details assumed |
| MCP servers | `config.toml` `[mcp_servers]`, can be passed with `-c` | yes (file) / no (`-c`) | — | — / — / yes / — | documented; `codex mcp` verified |
| Skills | `$CODEX_HOME/skills/` | yes | — | yes / yes / yes / — | structure verified |
| Sessions | `$CODEX_HOME/sessions/…`, SQLite state database, tied to the creating account | yes | — | yes / — / — / yes | verified (accounts study) |

## Conclusions for Codex

* **Native memory is global to the `CODEX_HOME`, hence to the account** — and
  L30 makes the `CODEX_HOME` the account. A persona that moves to the
  secondary account loses its native memory; two personas on the same account
  share it. It is produced in the background by the harness, on its own
  criteria: neither its timing nor its content is under control. **Leave it
  disabled** for sessions launched by ameesh.
* **Designation**: `-c developer_instructions="…"` at each turn (text
  generated from the card, bounded), `SessionStart` hook for the memory index,
  `-c mcp_servers.…` for an MCP server. Write nothing into `CODEX_HOME` (an
  account's directory, shared between personas).
* To check: that `developer_instructions` passed with `-c` does apply after
  `exec resume`, and the size allowed on the command line (otherwise a
  session-specific profile file).

# DeepSeek Harness (`dsh`)

| Mechanism | Location | Account? | Directory? | R / W / I / G | Certainty |
|---|---|---|---|---|---|
| Workspace instructions | `$DSH_HOME/AGENTS.md` then the project chain (`AGENTS.md`, `CLAUDE.md`, and `*.local.md`), `maxBytes` budget (65,536 by default) | no (the account is a key) | yes (project) | yes / yes / yes / — | verified (`dsh-agent-instructions` README) |
| Deployment persona | `dsh-system-prompt`: `personaPrefix`, `personaSuffix` (templates) | no | no | — / — / **yes** through `--patch` | verified (README); ameesh already writes a `--patch` for the model |
| Agent persona | `dsh-persona` row (`prefix`, `suffix`, `complete`) in a preset | no | no | — / — / yes | verified (README); to be mounted in a preset, not globally |
| Profiles, patches | `$DSH_HOME/profiles/<name>/` (`cordis.patch.yml`), `$DSH_HOME/cordis.patch.yml`, `--patch` | no | no | yes / yes / yes / — | verified (README, structure) |
| Hooks | `$DSH_HOME/hooks.json` read by the `dsh-hooks-claude-code` / `dsh-hooks-codex` bridges; `SessionStart` adds context; commands only | no | — | — / — / yes / — | verified (README, structure) |
| Native memory | **no** memory package; the documentation points to third-party memory MCP servers | — | — | — | verified (package list) |
| MCP servers | `dsh-mcp-client`: **tools only** (neither MCP resources nor prompts) | no | — | — / — / yes / — | verified (README) |
| Sessions | `$DSH_HOME/sessions`, independent of the key | no | — | yes / — / — / yes | verified (accounts study) |

## Conclusions for dsh

* No native memory: nothing to lose on relay, everything to provide.
* It is the most **open** harness: the system prompt is composed by patch
  (persona prefix and suffix, execution context, tool order), compaction is a
  configurable plugin. ameesh can designate the persona there cleanly
  (`personaSuffix` through a session patch) — **assumed**, to be tested.
* A memory MCP server is seen there only through its **tools**: do not count
  on MCP resources or prompts.

# ACP (ameesh bridge, lot L16)

| Element | What the protocol carries | What the ameesh bridge can provide | Certainty |
|---|---|---|---|
| `session/new` | `cwd`, `mcpServers` | session directory; **MCP servers** (ameesh-fil, memory) — the bridge currently sends `mcpServers: []` | documented; L16 code read |
| `session/load` / `session/resume` | `sessionId`, `cwd`, `mcpServers`, `additionalDirectories` (if advertised); `load` **replays** the history | same MCP servers **redeclared** at each resume | documented |
| System prompt, persona | **no standard field** | only the **first message** (`session/prompt`) and the instruction files of the `cwd` (AGENTS.md, if the agent reads them) | documented |
| `_meta` | free extension (`traceparent` etc. reserved) | `_meta` carrying the persona and session identity — **informative**: an agent may ignore it (dsh states it reads none) | documented; dsh README verified |
| Client-side `fs/*` | reading/writing files in the `cwd` | bounded to the session directory by the bridge | L16 code read |

## Conclusions for ACP

The only **portable** channel for the persona is content: the **brief** of the
first message (identity, prompts, memory index) and the **MCP servers**
declared at `session/new` and at each resume. Everything else (system prompt,
hooks, compaction) belongs to the agent. The persona of an ACP agent is
therefore "best effort": stated, not enforced.

# MCP, the common path

All four harnesses speak MCP; a **"persona memory"** server provided by
ameesh is therefore reachable everywhere:

| Harness | Per-session declaration without touching the account | Server instructions seen by the model | Tools | Certainty |
|---|---|---|---|---|
| Claude Code | `--mcp-config` | yes | yes | verified |
| Codex | `-c mcp_servers.<name>…` | assumed | yes | documented / assumed |
| dsh | `--patch` (`dsh-mcp-client`) or ACP `mcpServers` | no (tools only) | yes | verified (README) |
| ACP | `mcpServers` of `session/new`/`resume`/`load` | depends on the agent | yes | documented |

Limits: MCP **injects nothing at startup** — the model must call a tool to
read; the initial read therefore still goes through the prompt or a hook.
Each tool costs tokens on every request. And the stateless MCP 2026-07-28
revision forces serving two versions for a while
([study of standards](standards-des-harnais.md)).

# Summary by harness

| | Claude Code | Codex | dsh | ACP |
|---|---|---|---|---|
| Native memory | per account × repository, redirectable | per account, background, not under control | none | the agent's, opaque |
| Lost on account change | yes (unless `projects/` shared) | yes | — | depends on the agent |
| Lost on harness change | yes | yes | — | yes |
| Persona system prompt | appended by option, frozen at the 1st turn | `developer_instructions` through `-c` | patch (prefix/suffix) | none: first message |
| Injection at startup | `SessionStart` hook (also after compaction) | `SessionStart` hook | `SessionStart` hook (bridge) | first message |
| MCP per session | yes | yes | yes (tools) | yes |
| Parallel sessions | yes (one transcript per session) | yes (write locks) | yes | depends on the agent |

**Main finding**: no native memory is stored **per persona**; all are tied to
the account, the repository or the agent, and none passes from one harness to
another. A persona that must survive the relay therefore cannot entrust its
memory to the harness of the moment.

# Proposed specification

## 1. `Persona` card in the canon (replaces `Agent`)

```yaml
type: Persona
title: redacteur                 # name (registry grammar), stable identity
responsible: human:alice         # REQUIRED (R14)
team: acme-web
description: "Writes and maintains the product documentation."
capabilities: [read, report-drift, propose]   # `approve` forbidden (R8)
tools: [git, mcp:ameesh-fil]
reviewers: [relecteur]
budget_usd_per_day: 20
session_policy: par-lot          # par-lot | taille | jamais (0025)
harnesses:                       # ordered preferences, not a position
  - { harness: claude, model: opus, effort: high }
  - { harness: codex, model: "gpt-…", effort: high }
  - { harness: deepseek }
credential_modes: [subscription, api-key]
memory:
  mode: neutral                  # neutral | native | none (§4)
  repository: "git@forge.example:equipe/persona-redacteur.git"   # ONLY piece of information: the location
  path: ""                       # subdirectory if the repository is shared by a group of personas
instructions: []                 # other canon cards to attach (optional)
---
Markdown body: the persona's own voice and rules.
```

* The **body** of the card becomes the source of the persona's prompts
  (ameesh so far only read the frontmatter); it is reviewed like the rest of
  the canon.
* No position: no host, no directory, no account, no session identifier.
  Allowed hosts fall under **admission** (formerly `Placement`, without
  `cwd`), already planned in lot L31.
* The singular `harness`/`model`/`provider` become the ordered list
  `harnesses`: it is the list the relay walks through.
* `memory.repository` only gives the **location** of the persona's repository
  (§4); no permissions, no credentials, no content in the canon. That same
  repository serves the **visibility rule** (§1 bis).
* No field specific to the digital twin: a twin is a persona whose
  repository is private to its human (§5).

**Migration.** For a while, the loader reads both forms:

* `type: Agent` is read as `Persona` (`kind: agent`), with a
  `persona-legacy-agent` finding (warning); `harness`/`model`/`provider`
  yield a one-element `harnesses` list;
* `type: Placement` is read as admission; a `cwd` present is **ignored**
  with an `admission-cwd-ignored` finding, then refused at the end of the
  period;
* the registry (`agent_registry`, key `name`) remains indexed by the persona's
  name; session state moves out of it (§2).

## 1 bis. Visibility: the single rule, checked at placement

Rule of [0029](../decisions/0029-persona-et-session.md): **a persona runs on a
human's machine only if that human has access to the persona's repository**;
for a server or a cluster host, the rule applies to its responsible person
(`responsible` of the `Host` card) and to its administrators.

Proposed implementation, in the placement predicate (L31):

* **Who must have access**: the set `H(host)` = the host's responsible person
  + administrators declared in the `Host` card (new `admins` key, list of
  `human:<id>`).
* **How to check it**, as chosen in the host's configuration:
  1. **through the git hosting service**: the forge's API tells, for each
     human of `H(host)` linked to their forge account (link `human:<id>` →
     forge account, to be carried by the `Member` card), whether they have at
     least read access to the repository; the call is made with an ameesh
     token limited to reading permissions;
  2. **through a trial clone** made with the credentials of the human
     responsible for the host (`git ls-remote` is enough): if it fails,
     access is not established. This path proves nothing for administrators;
     it suits a personal workstation (a single human).
* **Result**: host admitted, or **excluded** with a readable finding
  (`persona-hidden-from-host`); never admitted by default when the check
  fails (fail closed). The result is cached for a short duration and
  rechecked at each session opening and at each move (0028).
* **Revocation**: access removed from the repository excludes the host for
  subsequent sessions; the running session finishes its turn and is then
  stopped, and its local copy is pushed then erased (§4).

## 2. Recording a session

A sessions table (migration to be numbered when the time comes), one row per
execution:

| Field | Content |
|---|---|
| `id` | ameesh identifier (UUID), distinct from the harness's |
| `persona` | persona name |
| `work_item` | the lot (0025); `NULL` for a long-running orchestrator session |
| `harness`, `harness_version` | L16 descriptor and pinned version |
| `model`, `effort`, `tier` | effective settings |
| `account` | **name** of the account profile (L30), never a secret |
| `host`, `runner_id`, `lease_epoch` | where and under which lease |
| `cwd` | working directory (execution state, 0029) |
| `harness_session_id` | native identifier (`--resume`, `exec resume`, `--session-id`, ACP) |
| `parent_session` | previous session in the chain (L11 rotation, relay, move) |
| `relay_reason` | `rotation`, `compte`, `harnais`, `hôte`, `restart` |
| `started_at`, `ended_at`, `status`, `end_reason` | life cycle (lot merge, abandonment…) |
| `memory_journal` | path of the session's memory journal, in the ephemeral copy (§4) |
| `memory_commit` | commit of the persona's repository at opening, then commit pushed at closing |

* **Parallel sessions**: at most one **active** session per
  `(persona, work_item)`; several lots, several sessions. The lease moves
  from the persona to the session. The identity set in the harness's
  environment remains `AGENT_MAIL_NAME=<persona>` (existing tools keep
  working), complemented by `AMEESH_SESSION=<id>`.
* **Mail**: a message addressed to the persona and carrying a lot goes to
  that lot's session; without a lot, to the long-running session if it
  exists, otherwise it waits (routing decision to be specified in the lot).
* The relay and the move **close** a session and open another one linked by
  `parent_session`; native resume is only a case where `harness_session_id`
  is kept.

## 3. Designating the persona to the harness

ameesh **generates** from the card a persona text (identity, role,
responsible person, rules, prohibitions, tools, lot) and a **memory index**
(§4), then passes them through the strongest channel of each harness,
**without ever writing to the working repository or to an account's
directory**:

| Harness | Identity and prompts | Memory index at startup | After compaction | Tools |
|---|---|---|---|---|
| Claude Code | `--append-system-prompt-file <session>/persona.md` | `SessionStart` hook (`startup`, `resume`) passed through `--settings` | same hook, `matcher: compact` | `--mcp-config` |
| Codex | `-c developer_instructions=…` (or session-specific profile) | `SessionStart` hook | `PostCompact` hook or reminder at the next turn (to be tested) | `-c mcp_servers…` |
| dsh | session `--patch`: `personaSuffix` of `dsh-system-prompt` | `SessionStart` hook (hooks bridge) | assumed: compaction plugin | `--patch` (`dsh-mcp-client`) |
| ACP | at the head of the **first message**; informative `_meta` | in the first message | nothing guaranteed: reminder by ameesh at the next turn | `mcpServers` at each `new`/`resume`/`load` |

The generated text is the same for all harnesses; only the channels differ
(field of the L16 descriptor, for example `ameesh.persona`). The session
files live in ameesh's state directory
(`~/.local/state/ameesh/sessions/<id>/`), not in the worktree.

## 4. Memory while waiting for the in-house harness

### Three paths compared

| | (a) Native memory of each harness | (b) Per-persona memory directory, neutral | (c) Memory MCP server |
|---|---|---|---|
| Scope | account × repository (Claude), account (Codex), nothing (dsh), opaque (ACP) | **the persona**, wherever it runs | the persona |
| Account or harness relay | **lost** (except Claude with shared `projects/`) | kept | kept |
| Parallel sessions | uncontrolled concurrent writes | explicit rule (one journal per session) | explicit rule (server side) |
| Read at startup | automatic | injected by hook or prompt | **no**: a tool call is needed, or an extra injection |
| Writing | decided by the model and the harness | convention + memory turn at the end of the lot | tool called by the model |
| Readable and reviewable by a human | yes (Markdown), but scattered | yes (Markdown, git) | depends on the storage |
| Operating cost | none | one directory (git) per memory repository | a service to run on each host or over the network |
| Replaceable by the in-house harness | no | **yes**: the format is the contract | yes, if the storage is (b) |

### Proposed neutral format (b)

```
<persona repository>/[<path>/]
├── MEMORY.md            # index: one line per memory, ≤ 200 lines
├── souvenirs/<sujet>.md # one memory or one topic; frontmatter: type, modified, source
└── journal/<session>.md # additions from ONE session, append-only
```

* Same conventions as Claude Code's auto memory (short index, one file per
  topic, `type` in the frontmatter: `user`, `feedback`, `project`,
  `reference`): the most mature of the memories observed, and readable by the
  other harnesses as plain Markdown. The frontmatter follows OKF, which lets
  the canon and the existing tooling read it.
* **One git repository per persona** (or per group of personas that can see
  each other, each in its `path` subdirectory). The card only gives its
  location (`memory.repository`).
* **Limited access**: a session receives only an **access credential limited
  to that repository** (deploy key or token scoped to a single repository,
  with write access), supplied by the host at launch and never in the canon;
  it sees no other memory.
* **Ephemeral local copy**: at opening, clone into the session's state
  directory (`~/.local/state/ameesh/sessions/<id>/memoire/`, permissions
  0700, private during the session); at closing (end of lot, relay, move,
  stop), commit, **push**, then **erasure** of the copy. A refused push keeps
  the copy and raises an alert instead of erasing. Thus the memory follows
  the persona from one host to another (0028) without staying on any.

### Concurrent writing rule

1. **A session writes only its journal** `journal/<session>.md`,
   append-only: two parallel sessions never write the same file.
2. **Consolidation by a single writer**: at the end of the lot (or on
   relay), ameesh runs a **memory turn** in the session — like the summary
   turn of the L11 rotation — which proposes the changes to `souvenirs/` and
   to `MEMORY.md`; writing is done by commit on the copy then push: the
   remote repository serves as a lock (a refused push forces starting over
   from the latest commit). A conflict on `souvenirs/` is a signal, never
   resolved silently; the journals, one file per session, do not collide.
3. Consolidated journals are archived (not erased): a consolidation can be
   redone.
4. No secrets: consolidation goes through a secrets filter and refuses the
   commit when in doubt.

For Claude Code, a variant to be tested: `autoMemoryDirectory` pointed at a
**session-specific** directory, pre-filled with a copy of the index; auto
memory then writes there, and the consolidation compares. It keeps the
model's native writing behaviour without concurrency on the index.

### What the relay must transfer

| Element | From Claude Code to Codex (example) |
|---|---|
| Identity and prompts | regenerated from the card, through the new harness's channel |
| Persona memory | pushed by the old session, recloned by the new one (same repository), index reinjected |
| Journal of the current session | file already neutral; picked up in the new session |
| Lot resume summary (L11) | produced under the old harness/account, placed in the brief |
| Native transcript | **not transferable** between harnesses; kept for audit |
| Native memory of the harness left | ignored (disabled in `neutral` mode) |

### Recommendation for the interim

* **(b) as storage and contract**, native memory **disabled** for sessions
  launched by ameesh in `neutral` mode (Claude:
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` or redirection; Codex: `memories` left
  disabled), so as to have only one memory per persona and not let a second
  one diverge in an account.
* **(c) later, as a facade** over the same files (tools `memoire.lire`,
  `memoire.chercher`, `memoire.noter`) when a harness has no hook; it works
  on the session's ephemeral copy, with the same limited credential.
* **ameesh does not become the owner of the memory**: no table, no format of
  its own in the database. Management of the directory (injection, journal,
  consolidation) lives in a **separate component** ("persona memory"),
  called by the runner at the three fixed points (opening, relay, end of
  lot); the core of ameesh only **designates** the persona and the location
  of its repository, as [0029](../decisions/0029-persona-et-session.md)
  requires. The in-house harness, if it comes, reads the same format and
  replaces this component.

## 4 bis. Can the interim solution last?

### What it covers durably

* The **identity** of a persona in any harness (text generated from the
  canon, through each one's strongest channel).
* A **per-persona memory**, independent of the account, the repository, the
  harness and the host, reviewable and versioned.
* The **relay** and the **move**: the persona survives, only the native
  transcript is lost, compensated by the resume summary.
* **Parallel sessions** through the rule "one journal per session, a single
  consolidator".
* **Subscriptions** usable only through their provider's tools: nothing
  forces leaving Claude Code or Codex.

### What would be missing compared with an in-house harness

| Need | Claude Code | Codex | dsh | ACP |
|---|---|---|---|---|
| Control of the system prompt | append only (replacing costs the tool prompts); frozen at the 1st turn | append (`developer_instructions`) or full replacement | **full** (patch) | none |
| Compaction, context | decided by the harness; adjustable window (`--autocompact`); reinjection by `compact` hook | decided by the harness; compaction hooks (to be tested) | configurable plugin | opaque |
| Guarantee that memory is written | none during the session; guaranteed at ameesh's fixed points (memory turn) | same | same | same |
| Retrieval during the session | hooks, file reading, MCP: on the model's initiative | same | same | MCP only |
| Observability | `stream-json` stream, hooks (`InstructionsLoaded`) | JSONL, session logs | JSON, OTel telemetry | `session/update` |
| Parallel sessions | yes | yes | yes | depends on the agent |
| Relay | by summary (native resume only between Claude accounts with shared `projects/`) | by summary | same session (same `DSH_HOME`) | depends on the agent |

### Conditions for this gap to remain acceptable

1. **Short sessions** (one per lot, 0025): compaction becomes rare, and with
   it the main blind spot.
2. **Memory writes at ameesh's fixed points** (opening, relay, end of lot),
   not only on the model's initiative.
3. **Conformance test bench** (L17) that checks, per harness and per
   version, that the persona is indeed designated, that the index is indeed
   injected (including after compaction) and that the journal is indeed
   written; harness versions pinned.
4. **Injection budgets** respected: index ≤ 200 lines, hook context
   ≤ 10,000 characters for Claude, instructions ≤ 32 KiB for Codex, 64 KiB
   for dsh.
5. **No dependency on undocumented internals** of the harnesses
   (experimentation variables, SQLite databases, transcript formats).
6. Accept that an ACP agent receives the persona only **stated**.

**Conclusion.** For build agents, review agents and digital twins (personas
like any other), whose needs are "an identity, durable notes, one
session per lot", the interim solution **can last** without an in-house
harness. An in-house harness would only be justified for fine-grained control
of context during the session (reminder policy, compaction driven by the
persona, transactional writing of memory at every turn), or if the
third-party harnesses closed the channels used here. Recommendation: **build
the interim as a permanent but replaceable solution** — the memory format and
the persona text are the contract; the in-house harness becomes one more
harness that reads this contract, not a prerequisite.

## 5. Digital twin

No more special rule: **a digital twin is a persona whose repository is
private to its human**. The visibility rule (§1 bis) therefore makes it run
only on that human's machines (or on a host of which they are the sole
responsible person and administrator); its memory, in that repository, is
seen only by them. It never approves, like any persona
([0012](../decisions/0012-autorite-par-ameesh-approve.md)): `approve` is
refused at load time and ameesh-gate accepts only the human's passkey.
Its powers remain to be decided (0029). An import of the human's existing
native memory (for example their Claude Code auto memory) into the twin's
repository is done only at their request, as a copy.

## 6. Proposed lots

Lot and migration numbers to be assigned by the orchestrator; none is
reserved here.

| Lot | Content | Depends on | Risks |
|---|---|---|---|
| P1 — Persona card and visibility | `Persona` type (+ body), ordered `harnesses`, `memory.repository`, transitional reading of `Agent` and `Placement`, findings; `admins` key of `Host` cards; human → forge account link; **visibility check in the placement predicate** (forge or trial clone, host excluded otherwise, short cache, revocation) | L2; to be merged into L31 (0029 puts it there) | prolonged dual reading; L31 already loaded; dependency on the forge API (unavailable = host excluded) |
| P2 — multiple sessions | sessions table, lease per session, `AMEESH_SESSION`, mail routing by lot, `ameesh sessions`; visibility rechecked at each session opening | L26, L30, L31 | overhaul of the runner (one worker per name today); the riskiest |
| P3 — designation to the harness | "persona.md" generator from the canon; descriptor field `ameesh.persona` (channels per harness); `SessionStart` hooks through `--settings`/session config; per-session MCP servers in the ACP bridge | L16, P1 | effect of `-c` on resume (Codex); dsh patch not tested |
| P4 — persona memory (separate component) | neutral format, repository per persona, access credential limited to the repository supplied by the host, ephemeral copy (clone, push, erasure), journal per session, injection of the index, memory turn at the end of the lot and on relay, consolidation under lock, secrets filter, disabling of native memories in `neutral` mode | P2, P3, L11 | push conflicts between parallel sessions; copy not erased after a refused push; quality of consolidations |
| P5 — memory MCP facade (optional) | MCP server over the same files, two versions of MCP | P4 | token cost; tools only in dsh |
| P6 — memory and persona conformance | L17 test bench cases: designation, injection after compaction, journal written, Claude → Codex relay | L17, P4 | paid calls behind the gate |
| P7 — powers of the digital twin | only if the decision on its powers adds rules; otherwise nothing to do (a twin = a persona with a private repository) | P1, P4; owner's decision | personal data |

Order: P1 with L31, then P3 (useful as soon as there is a single session per
persona), P2, P4; P5 and P6 afterwards; P7 only if a decision requires it.

# Points for the owner to decide

Decided on 2026-10-05: one git repository per persona (or per group of
personas that can see each other), only its location in the canon, access
limited to that repository, ephemeral local copy; single visibility rule
(0029), digital twin without a special rule.

Remaining:

1. **Amend 0029?** The recommended interim makes the memory format and the
   persona text a **neutral contract**, managed by a separate component, and
   makes the in-house harness optional. To be confirmed, or kept strictly as
   a temporary bridge.
2. **Native memories** disabled by default for sessions launched by ameesh
   (proposed), or left active alongside?
3. **Visibility check**: through the forge API (proposed for servers and the
   cluster, covers administrators) or by trial clone with the responsible
   person's credentials (sufficient for a personal workstation); and where to
   declare a host's administrators (`admins` key of the `Host` card,
   proposed).
4. **Credential limited to the repository**: who creates and renews it (the
   human responsible for the persona, an ameesh forge application), and
   where the host keeps it.
5. **Who consolidates**: the session itself (memory turn, proposed), a
   reviewer, or the responsible human for certain types of memories
   (`feedback` for example)?
6. **P1 in L31** as 0029 says, or a separate lot so as not to weigh down L31?
7. **Powers of the digital twin** (already opened by 0029).
