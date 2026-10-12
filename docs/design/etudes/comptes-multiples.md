---
type: Study
title: "Study — isolating an account and its sessions, per harness (multiple accounts)"
description: "How Claude Code, Codex and DeepSeek Harness store credentials and sessions, and whether a session can be resumed under another account; basis of lot L30."
status: stable
tags: [comptes, forfaits, sessions, budgets]
generated: { by: "codex3-l30/claude-opus-5-5", at: "2026-10-05T10:30:00+02:00" }
stale_after: 2027-01-05
sources:
  - { resource: "decisions/0027-bascule-automatique-entre-comptes.md", title: "Decision 0027" }
  - { resource: "decisions/0019-budgets-et-routage.md", title: "Decision 0019 (pace guard)" }
  - { resource: "decisions/0025-une-session-par-lot.md", title: "Decision 0025 (sessions, resume summary)" }
---

# Question

To switch a harness from one account to another **between two turns**
([0027](../decisions/0027-bascule-automatique-entre-comptes.md)), we need
to know, for each harness:

1. how an account is selected (environment variable, folder);
2. where the credentials live and where the sessions live;
3. whether a session opened under account A can be resumed under account B.

# Method

Without any paid call or login to an account:

* `--help` of the three harnesses and installed documentation (packages of
  DeepSeek Harness);
* reading the binary's strings (Claude Code, Codex) for file and
  variable names;
* **inventory** of the existing configuration folders of a work
  station: file names and permissions only, never their content
  (no credentials file opened);
* an **offline** experiment with Claude Code: `HOME` and
  `CLAUDE_CONFIG_DIR` pointing to empty sandbox folders, no
  key variable, a dummy session (two invented JSON lines) — the
  harness stops at "Not logged in" before any billable network call.

Versions observed: Claude Code 2.1.x, Codex CLI 0.159.x,
DeepSeek Harness (`@deepseek-ai/dsh`) 0.1.5.

# Claude Code

**Account selection: `CLAUDE_CONFIG_DIR`.** Without it, the configuration
folder is `~/.claude` and the global file `~/.claude.json` is at the
root of the home folder; with it, **everything** is under the designated
folder, including `.claude.json`. The path must be absolute.

| Item | Location |
|---|---|
| credentials (Linux) | `<dossier>/.credentials.json`, 0600 (macOS: keychain, key derived from the folder) |
| global configuration | `<dossier>/.claude.json` (or `~/.claude.json` by default) |
| settings, hooks | `<dossier>/settings.json` |
| sessions | `<dossier>/projects/<chemin du dossier de travail encodé>/<session>.jsonl` |
| miscellaneous state | `sessions/`, `file-history/`, `todos/`, `shell-snapshots/`, `history.jsonl` |

**Resume experiment** (offline, see Method):

| Case | Result |
|---|---|
| `--resume S` under an empty folder B | "No conversation found with session ID" |
| session S in `A/projects`, launched under B | "No conversation found": sessions are per folder |
| same, `B/projects` → symbolic link to `A/projects` | session **found** (`system/init` emitted with id S), then "Not logged in" (B has no credentials) |
| under A | session found |

**Conclusion.** The account and the sessions are separable: the credentials
are a separate file, the session a file in `projects/`. A session is
**resumable under another account** if the two configuration folders
**share `projects/`** (symbolic link). The provider's prompt cache
is lost on the account change (the first turn rereads the whole context),
but the conversation continues.

Worth knowing for a secondary folder: the hooks (`settings.json`) and the
configuration (`.claude.json`) are specific to the folder; a new folder does not have
the agent-mail hooks. Linking them (symbolic link to the primary's `settings.json`)
or copying them is part of the preparation.

**Gauges.** The plan is read from the turn's `stream-json` stream
(`rate_limit_event`), not from a file in the folder: ameesh attributes them to the
account through a **marker** written into the agent's stream before each turn.

# Codex

**Account selection: `CODEX_HOME`** (default `~/.codex`). The SQLite state
database can be moved separately (`CODEX_SQLITE_HOME`, or `sqlite_home` in
the configuration).

| Item | Location |
|---|---|
| credentials | `<CODEX_HOME>/auth.json`, 0600 (or the keychain if `cli_auth_credentials_store` requests it) |
| configuration | `<CODEX_HOME>/config.toml`, `hooks.json` |
| sessions | `<CODEX_HOME>/sessions/AAAA/MM/JJ/rollout-…-<id>.jsonl` + index (`session_index.jsonl`) + SQLite databases (`state_*.sqlite`, `thread_history_*.sqlite`) |
| gauges | `rate_limits` (primary, secondary) of the `token_count` events in the session logs |

The header of each session (`session_meta`) carries **the identifier of the
creating account** (`creator_account_id`, `creator_user_id`), copied into the threads
table of the state database. A resume via `codex exec resume <id>` looks for
the thread in the index, the database and the logs of the current `CODEX_HOME`.

**Conclusion.** Sharing sessions between two `CODEX_HOME` would also require
sharing the SQLite state database between two processes of different
accounts (risk of corruption, undocumented behaviour), for a
session explicitly tied to its creating account; and the gauges, which
live in those same logs, would no longer be attributable to one account.
Not verifiable without a paid call. **Chosen: no continuity; rotation
with resume summary** (the summary is produced under the old account, which
alone can resume the session; mechanism L11).

# DeepSeek Harness

**Account = API key**, read in this order: launch environment
(`DEEPSEEK_API_KEY`), the harness's credentials file
(`$DSH_HOME/.credentials.yaml`, 0600), the project's `.env`, the `.env` of the
harness folder. The environment variable wins.

| Item | Location |
|---|---|
| harness folder | `DSH_HOME` (default `~/.dsh`) |
| sessions | `$DSH_HOME/sessions` |
| profiles | `$DSH_HOME/profiles/<nom>` |

**Conclusion.** Changing the key via `DEEPSEEK_API_KEY` touches neither the folder
nor the sessions: **the same session is resumed** under the other key. (Files
uploaded to the provider are indexed per key: they would be
re-sent, with no impact on the conversation.) No plan gauge: the
threshold of a pay-per-token account is a **cap** (spend for the hour,
attributed to the account) or a **balance** (read per account).

# Summary

| Harness | Selection | Credentials | Sessions | Resume under another account |
|---|---|---|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR` | the folder's `.credentials.json` | the folder's `projects/` | **yes**, if `projects/` is shared (link); otherwise rotation with summary |
| Codex | `CODEX_HOME` | the folder's `auth.json` | the folder's `sessions/` + SQLite database, tied to the account | **no**: rotation with summary |
| DeepSeek Harness | `DEEPSEEK_API_KEY` | API key | `$DSH_HOME/sessions`, independent of the key | **yes** (same `DSH_HOME`) |

# Consequences for ameesh (L30)

* Account profiles **per host** (host configuration, key `accounts`):
  `config_dir` (folder, 0700, credentials present — presence only) or
  `api_key_env` (variable or key file 0600).
* The continuity decision is mechanical: same session storage (resolved
  path) → resume; otherwise rotation with summary, and the thread says so.
* Login steps remain human: `CLAUDE_CONFIG_DIR=<dossier> claude`
  then `/login`; `CODEX_HOME=<dossier> codex login`. See
  [the operations page](../../COMPTES.md).
* Compliance with each provider's terms of use regarding the use
  of several accounts remains the team's responsibility (0027).

# Amendment (0034, L74)

Switching at the threshold and returning to the primary (0027 §2–3) are replaced by
a choice by expiry: the accounts form a pool, a session keeps
its account as long as it is under its threshold, and a new session (or a
rotation) goes to the account whose unused capacity expires the earliest —
amended on 2026-10-10 (L117): to the account furthest behind its pace,
the expiry now serving only as a tie-breaker. The
continuity table above remains the rule for resume or summary when
a session changes account. See [the operations page](../../COMPTES.md).
