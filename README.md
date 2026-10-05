# agent-mesh

A multi-harness agent mesh (Claude Code, Codex, DeepSeek Harness): a durable
mailbox on Postgres, one runner per machine, agent leases, readable threads,
actions behind a gate, and human approvals proved by passkey receipts.

Spec: [`MESH-SPEC.md`](MESH-SPEC.md). v0 (local, files, still in production):
`agent-mail.v0.py`, `nexlink-agent.v0.sh` — **not touched by v1**.

## ameesh v1

What v1 does (design: [`docs/design/specification.md`](docs/design/specification.md)):

* **canon** — an OKF git repository declares humans, hosts, agents and
  placements; ameesh reads it only at the merged canonical commit and syncs it
  into the registry of each host. An agent without a resolved human
  `responsible`, or placed against its host's policy, is never claimed;
* **runner** — one per host, leases, three harnesses (Claude Code, Codex,
  DeepSeek Harness) resumed on the same session, events and `ameesh attach`;
* **readable threads** — every message that goes through ameesh is appended in
  clear to a Markdown thread per project or lot;
* **actions behind a gate** — an `irreversible` or `costly` action (e.g. merging
  a PR with `git-merge`) runs only with a valid receipt bound to its digest,
  single use, with expiry; an unknown outcome is reconciled, never retried
  blindly;
* **human approval** — `ameesh-approve`, a separate service under its own Unix
  user, shows the action recomputed from the database and has the human sign
  it with a passkey on their phone; passkeys are enrolled by a canon PR. The
  former Ed25519 owner-key ceremony is abandoned
  ([0012](docs/design/decisions/0012-autorite-par-ameesh-approve.md)).

```
ameesh canon check | show | sync                  # canon → registry of this host
agent-runner [--once]  |  ameesh attach <agent>   # the host's runner; interactive takeover
ameesh mail send <agent> "<text>"                 # agent-mail, unchanged for the hooks
ameesh fil list | show <project> [<lot>]          # readable threads
ameesh action propose | execute | reconcile …     # the gate
ameesh action request | fetch-receipt <id>        # ask ameesh-approve, attach the receipt
ameesh decisions [--for human:ID]                 # what waits for a human
ameesh receipt verify | authenticator list        # receipts, trust registry
ameesh review-class <files…|--diff REF>           # review class of a change (canon policy)
ameesh-approve serve | enroll-link | gen-token    # the approval service (own user)
ameesh approve-check [--json]                     # service and verifiers share RP ID/origins
ameesh progress [--json|--html F] [--since 24h]  # lots, agents, milestones, budget (docs/PROGRESS.md)
ameesh alerts [--follow] --json                   # long turn, idle with mail, dead runner, big session, stale lot
ameesh restart <agent> --brief FILE|-             # stop the turn, forget the session, brief first
ameesh interrupt <agent> <message…>               # direct interruption (authorised senders only)
ameesh set <agent> tier=fast session_policy=par-lot|taille|jamais
ameesh cost turns | gauges | balance --json       # usage per turn, plan gauges history, paid-per-token balance
```

Operating agents (L26: session per lot, enriched `list --json`, alerts,
restart, balances — schemas): [`docs/EXPLOITATION.md`](docs/EXPLOITATION.md).

Demo — the whole scenario of the end-to-end test (`tests/test_bout_en_bout.py`),
commented, on a temporary database that is dropped afterwards (fictitious
organisation, fake harnesses, fake `gh`, software passkey):

```bash
scripts/demo-v1.sh
```

Switching the real site from v0: [`docs/BASCULE.md`](docs/BASCULE.md).
Hosting profile « cluster » (one persistent agent per Kubernetes pod, image,
example manifests, read-only supervisor role):
[`docs/profils/cluster.md`](docs/profils/cluster.md).
Hosting one team's ameesh-approve (own host, RP ID = that host, strict
profile, optional local TLS behind a TLS-passthrough gateway, database role):
[`docs/profils/heberger-ameesh-approve.md`](docs/profils/heberger-ameesh-approve.md).

## State — v1 points 1 to 5 are built (bench)

| v1 (spec §5) | State |
|---|---|
| 1. agent-mail on Postgres (`agent_registry`, `agent_mailbox`, migrations, LISTEN/NOTIFY, CLI compatible v0) | **done** — [`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md) |
| 2. one runner per machine, 3 harness adapters with session resume, leases, wake on NOTIFY | **done** — same document |
| 3. owner authority by Ed25519 signature, content- and expiry-bound; two key roles (`owner` = authority, `agent` = provenance only); approvals with single use | **done** on the bench — [`docs/V1-AUTORITE-MESH.md`](docs/V1-AUTORITE-MESH.md); **superseded** for human authority by ameesh-approve receipts (decision 0012), Ed25519 keys remain for agent provenance and tests |
| 4. `work_items` state machine (intake → build → qa → merged → promoted, + blocked/waiting_human), loop bound, event ledger | **done**; the board stays in git |
| 5. `mesh list` observability | **done** — `agent-mesh list` / `show`, on `agent_mesh_overview` |

Nothing is wired to the real agents yet: v1 runs on a **test bench** (fake
harnesses, a local Postgres container). `~/.local/bin/agent-mail` and the v0
scripts are untouched. The switch is described step by step, with rollback, in
[`docs/BASCULE.md`](docs/BASCULE.md) — to be executed with the owner.

## Quickstart (bench)

```bash
scripts/pg-up.sh                    # container ameesh-pg, postgres:17, 127.0.0.1:55432
export PYTHONPATH=src
# Aucun secret n'est codé dans le dépôt : le mot de passe du banc vient de
# PGPASSWORD ou de ~/.pgpass (mode 600) ; sinon donnez le DSN complet.
export AMEESH_DSN=postgresql://agent_mesh@127.0.0.1:55432/agent_mesh

python3 -m ameesh migrate       # versioned migrations, idempotent
python3 -m ameesh doctor --notify-test

# register an agent pinned to this host, then run the machine's runner
python3 -m ameesh.runner register deepseek7 deepseek --cwd ~/src/acme
python3 -m ameesh.runner --max-turns 1

# talk to it
python3 -m ameesh mail send deepseek7 "ton lot a bougé"
python3 -m ameesh list            # mesh list: host, lease, unread, budget, key

# human approval: canon, gate, ameesh-approve and a software passkey, end to end
scripts/demo-v1.sh

# work items
python3 -m ameesh.mesh_cli work add --title "un lot" --app nexlink --assignee deepseek7
python3 -m ameesh.mesh_cli work list      # + column DÉLAI (phase and age, R19)
python3 -m ameesh.mesh_cli work show 1    # milestones and durations: demande → gel → revue → fusion
```

Repo-local wrappers: `bin/ameesh`, `bin/agent-mail`, `bin/agent-runner`, `bin/ameesh-approve`
(they only set `PYTHONPATH=src`; nothing is installed into `~/.local/bin`).

## Commands

`agent-mail` keeps the v0 interface — and the v0 hook JSON, byte for byte:

```
agent-mail send <dest> <texte…> [--from NOM] [--lot ID] [--kind request|reply|notify|event] [--urgent]
                                               # dest = nom ou "all" ; --urgent : événement (C9)
agent-mail list                                # nom, outil, âge, non lus, dossier, hôte, bail
agent-mail inbox [NOM]                         # non lus, sans les marquer lus
agent-mail whoami                              # identité liée (nom + bail), jamais le dossier
agent-mail alias <NOM> <DOSSIER> [CHANTIER]
agent-mail status "<travail en cours>"
agent-mail hook <claude|codex|deepseek>        # JSON identique à la v0
agent-mail statusline                          # barre d'état Claude Code
agent-mail migrate                             # migrations versionnées
agent-mail doctor [--notify-test]              # pilote, schéma, migrations, LISTEN/NOTIFY
```

Les **événements** (`--kind event`) réveillent l'agent comme un message, mais un
lot d'événements n'en réveille qu'un par `AMEESH_EVENT_COALESCE` secondes
(défaut 120) ; `--urgent` perce le regroupement. Un `--urgent` venu d'un
expéditeur habilité (`AMEESH_INTERRUPT_SENDERS`, demain une capacité du canon)
**interrompt le tour en cours** : le harnais est arrêté, la consigne repart en
attente et le message passe en tête. La **rotation de session** (résumé de
reprise puis session neuve, seuils `AMEESH_SESSION_MAX_TOKENS` /
`AMEESH_SESSION_MAX_TURN_SECONDS`) et l'**adoption d'un dossier de travail
déplacé** (`AMEESH_WORKTREE_ROOTS`) sont décrites dans
[`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md). Depuis L26, la
session tourne aussi **au changement de lot** (politique `par-lot`, défaut
`AMEESH_SESSION_POLICY` ; `taille` pour un orchestrateur, `jamais`), voir
[`docs/EXPLOITATION.md`](docs/EXPLOITATION.md). Quand un canon est
configuré, l'exécuteur lance `canon sync` au démarrage puis toutes les
`AMEESH_CANON_SYNC_INTERVAL` secondes (défaut 300) ; un échec ne l'arrête
jamais.

If Postgres is unreachable, the CLI falls back to the v0 file mailbox
(`~/.local/state/agent-mail`) with a readable warning on stderr; hooks fall back
silently and never fail the agent.

Every message (Postgres or file fallback) is also appended to a **readable
thread** (R12): one Markdown file per thread under `AMEESH_THREADS` (default
`~/.local/state/ameesh/fils/<project>/<lot or _projet>.md`). The project is the
sender's `chantier`, else the recipient's, else `AMEESH_PROJECT`, else
`default`; the lot is `--lot`. Each body is copied verbatim into a fenced code
block whose fence is longer than any backtick run it contains, so no line of a
message can become a heading, HTML or anything else in the rendered thread.
A thread write failure is reported on stderr and
never loses the message. Unreadable bodies (empty, JSON only, control/binary
characters, base64/hex blobs over 200 characters, even folded into lines or
spaced out) are refused;
`--allow-structured` is for tests and tools only.

```
ameesh fil list                                 # threads (index + local files)
ameesh fil show <projet> [<lot>] [--last N] [--meta]
ameesh fil tail <projet> [<lot>] [--last N]     # follow; Ctrl-C to exit
```

`agent-mesh` (observability, authority, work items):

```
agent-mesh list [--json] | show <agent>
agent-mesh key generate --out DIR --i-am-the-owner     # bench / agent provenance keys
agent-mesh key register <agent> --public-key FICHIER [--role owner|agent] | key show|list|revoke <agent>
agent-mesh approve --key FICHIER --action A --hash H [--kind K] [--expires 48h]
agent-mesh approvals [--action A] [--hash H] | verify --action A --hash H [--consume]
agent-mesh work add|list|show|move|note …
agent-mesh import-v0 | export-v0                       # v0 → Postgres and back
agent-mesh migrate | doctor [--notify-test]
```

The Ed25519 keys and `approve`/`verify` are the bench's authority model. In
v1 a human's authority is a receipt signed on their phone through
ameesh-approve and checked by the gate (`ameesh action`, `ameesh receipt`);
there is no owner-key ceremony any more, and an Ed25519 key kept on an agents'
host never counts as human authority — it only proves an agent's provenance.

### Canon (v1, lot L2)

The OKF canon declares members (`Member`, humans), hosts (`Host` + policy),
agents (`Agent`) and placements (`Placement`) — spec §4. ameesh reads only the
frontmatter, and only from the **approved source**: the git objects of the
merged canonical branch (`AMEESH_CANON_REF`, else the root member's `ref` in
`federation.yaml` as `origin/<ref>`, else `origin/main`). Uncommitted edits and
unpushed local commits are reported, never used. A directory that is not a git
repository is read only with `AMEESH_CANON_UNTRUSTED=1` (tests, prototypes), and
every finding and sync line is then tagged `[NON APPROUVÉ]`.

```
ameesh canon check [--host H] [--json] [--canon DIR] [--ref REV] [--fetch]   # exit 1 on any error
ameesh canon show  [--json] …                                     # cards read, and from which commit
ameesh canon sync  [--host H] [--json] …                          # canon → registry of host H
ameesh agent spawn <name> --by <creator> --ttl 2h [--cwd DIR] [--prompt TEXT]
```

| Setting | Meaning |
|---|---|
| `AMEESH_CANON` (config `canon`) | root of the OKF bundle; members of `federation.yaml` present locally (`workspace_path`) are followed |
| `AMEESH_CANON_REF` (config `canon_ref`) | canonical revision to read (e.g. `origin/main`) |
| `AMEESH_CANON_UNTRUSTED=1` | allow a non-git canon (never by default) |
| `AMEESH_REQUIRE_RESPONSIBLE` (config `require_responsible`) | R14 in `claimable`: an agent without a resolved human `responsible` is not claimable. **Default: on when a canon is configured, off otherwise** (bench compatibility). `0`/`1` force it. Expired ephemeral agents are never claimable, whatever this setting. |

`canon sync` writes only **declarative** columns (`harness`, `host`, `cwd`,
`model`, `budget_usd`, `responsible`, `team`, `provider`, `credential_mode`,
`capabilities`, `canon_ref` = `<member>:<path>@<commit>`), never state columns
(lease, session, status, spend, prompt). `responsible` is written only when it
resolves to a unique human `Member` and the agent has no blocking error;
otherwise it is cleared (not claimable). An error tied to an agent blocks that
agent, one tied to a host blocks the agents placed there, any other error
(federation, unreadable profile card) blocks every agent of the host. An agent
removed from the canon is set `stopped`; if it is in a turn, sync only marks
`status_text` (nothing is killed) and the next sync stops it once the turn is
over. Agents registered by hand (no `canon_ref`) and ephemeral agents are left
alone. `agent spawn` creates an ephemeral agent whose `responsible` is copied
from its creator at creation (R14), with capabilities ⊆ `{read, propose}` (and
⊆ the creator's), a mandatory expiry (max 7 days, never beyond an ephemeral
creator's). A fictitious example canon lives in [`examples/canon/`](examples/canon/).

**Canon state, fail closed (§4.1).** Every `canon sync` records the canon's
status for the host in `canon_state`: `ok`, `invalid` (read, but an error
blocks the whole host: federation, unreadable profile card, `Host` card) or
`unreadable` (root missing, broken repository, revision not found, non-git
directory refused), with a diagnostic and the last valid commit. An unreadable
canon writes **only** that row. Agents governed by the canon (`canon_ref` set,
and ephemeral agents whose creator lineage leads to one) are claimable only
while their host's state is `ok`; no state means not claimable. Leases,
sessions and turns already running are never touched; agents registered by
hand are unaffected. The SQL condition is `registry.canon_claim_predicate_sql()`
(used by `claimable()` and `claim()`, to be reused by `attach`). `canon check`
prints the status for the host and the recorded state; `mesh list --json`
exposes `canon_governed`, `canon_status`, `canon_diagnostic`,
`canon_checked_ts` and `canon_claim_ok`.

**Governed placement (C4, lot L3).** The project's responsible human decides
where an agent runs (`Placement`), within the policy of the host's responsible
(`Host`). For every canon agent of the host, `canon sync` writes whether its
placement on *that* host is admitted (declarative columns `placement_ok`,
`placement_diagnostic`, `placement_ref`, migration 0022): refused when the host
policy rejects its harness, provider, model or credential mode, when it has no
placement on this host, when the placement is ambiguous or the `Host` card is
missing. The claim condition requires `placement_ok` for agents governed by
the canon; a refused, missing or not-yet-evaluated verdict closes new claims,
never a running lease. A verdict only holds for the profile it judged: every
verdict is written with `placement_profile`, the SQL function
`ameesh_placement_profile(host, harness, provider, model, credential_mode)` of
the evaluated values, and the claim condition also requires it to equal the
row's current profile. Any change made outside `canon sync` (`ameesh run
register` with another harness or on another machine, `import-v0`, manual SQL)
closes new claims until the next sync, without any trigger. Ephemeral agents
inherit their canon creator's verdict only when their own profile is the one
evaluated for that creator. Agents registered by hand are unaffected. ameesh
never moves an agent by itself:

```
ameesh placement check [--agent A] [--json] …   # read-only: current placements (admitted or
                                                # refused, and why), admissible hosts and modes;
                                                # exit 1 if a placement is refused
```

`canon check` lists the host's placements (admitted or refused) and the
registry verdicts whose profile diverged since evaluation, `canon sync`
reports each agent's verdict, and `mesh list --json` exposes `placement_ok`,
`placement_diagnostic`, `placement_ref`, `placement_profile` (evaluated),
`placement_profile_current` and `placement_profile_ok`.

`agent-runner`:

```
agent-runner [--host H] [--runner-id ID] [--agents a,b] [--once [--wait S]]
             [--dry-run] [--max-turns N] [--lease-ttl S] [--poll S] [--migrate]
agent-runner register <nom> <claude|codex|deepseek> [--cwd DIR] [--prompt TEXTE]
             [--session ID] [--chantier C] [--model M] [--budget USD]
agent-runner stop <nom>
ameesh attach <agent> [--wait] [--ttl S]      # session interactive sur le bail (C9)
```

## Language choice

**Python 3 (stdlib) + `psycopg` when importable, otherwise the `psql` binary.**
Justified in [`docs/V1-MAILBOX-RUNNER.md`](docs/V1-MAILBOX-RUNNER.md):
v0 is Python (same alias/identity/hook semantics, one runtime, no second
toolchain), the stdlib already covers JSON/subprocess/sockets/threads, and Node
has no stdlib Postgres client — it would have forced a dependency, whereas the
Python path runs with **zero** dependencies via `psql`.

## Tests

```bash
scripts/test.sh                      # starts the container, runs the suite twice
scripts/test.sh tests.test_runner    # one module
scripts/test.sh tests.test_bout_en_bout   # the v1 end-to-end test (spec §13)
```

158 tests against the local container, run twice (`psql`, then `psycopg` with
`cryptography`): migrations and immutability, mailbox and LISTEN/NOTIFY round
trips, lease races on 2 and 4 concurrent connections, **two runner processes
competing for the same lease**, lease expiry takeover, fencing of a stale
runner, notification waking a runner in service mode, the three adapters (argv +
session resume, via fake harness binaries that never call the real CLIs), v0
hook-JSON equality, the file fallback, RFC 8032 vectors, **OpenSSL cross-check**
of the Ed25519 implementation, signed/unsigned/tampered/expired messages,
approval single use, `mesh list`, the work_items state machine and its loop
bound, v0 import/export, and the codex3 verdict probes: two key roles
(owner/agent), signed expiry and creation time bound to the row, nonce-based
single-use approvals, live-lease checks in renew/begin_turn/take_pending_prompt,
heartbeat stopping the harness on DB failure, durable `current_prompt`, and the
hook marking mail delivered only after a successful write, heartbeat waking at
the lease deadline, SIGTERM→SIGKILL escalation, prompt restored after a failed
or killed turn, the consumed-nonce ledger backfilled on upgrade (0007), and a
statement timeout (server and client) and a renewal call abandoned at the lease
deadline, process-group termination with SIGKILL on lease loss (even after the
parent dies), the merge of two prompts when a turn is restored, and a
lease-bound agent-mail identity that is never derived from the working
directory, and a renewal budget with no floor once the lease deadline has
passed.

## Layout

```
src/ameesh/
  cli.py          agent-mail (v0-compatible)
  runner.py       agent-runner: leases, turns, NOTIFY wake, CLI
  adapters.py     claude / codex / deepseek command lines and JSONL readers
  db.py           psycopg or psql; LISTEN/NOTIFY; JSON row marshalling
  registry.py     agent_registry: presence, leases, prompts, budget
  mail.py         agent_mailbox: send, unread, deliver, history
  fil.py          readable threads: Transport, file transport, index, ameesh fil
  migrations.py   NNNN_name.sql, checksummed, advisory-locked
  migrations/     0001_init.sql … 0009_threads.sql, 0011_receipts.sql, 0013_work_item_milestones.sql,
                  0014_cost.sql, 0022_placement.sql (numéros réservés : 0010, 0012, 0015–0021)
  review.py       politique de revue par classe de risque (canon) ; ameesh review-class
  mesh_cli.py     agent-mesh: mesh list, keys, approvals, work items, review-class, import/export v0
  authority.py    Ed25519 proofs bound to content and expiry; approvals
  signing.py      Ed25519 (pure RFC 8032 + optional cryptography/nacl), key files
  jcs.py          RFC 8785 canonical JSON (receipt challenges, action digests)
  p256.py         ECDSA P-256 / ES256, verification only (pure Python)
  cose.py         minimal CBOR, COSE_Key public keys (EC2 P-256, OKP Ed25519)
  receipts.py     ameesh-receipt/1 approval receipts, authenticators, standing grants
  receipts_cli.py ameesh receipt verify, ameesh authenticator list
  approve_client.py ameesh-approve service client (POST /requests, GET /receipts/<id>)
  work.py         work_items state machine, loop bound, event ledger
  canon.py        OKF canon: git-object reading, minimal YAML, profile, validation,
                  placement_violations (host policy)
  canon_sync.py   canon → registry (declarative columns), ephemeral agents
  placement.py    governed placement (C4): verdict per agent and host, admissible hosts
  canon_cli.py    ameesh canon check|show|sync, ameesh agent spawn
  backend.py      Postgres backend + v0 file fallback
  identity.py     v0 alias/name resolution (shared aliases.tsv)
tests/            unittest suite, fakebin/ fake harnesses and gh, webauthn_soft.py and
                  banc_v1.py (software passkey and canon PR: tests and demo only)
scripts/          pg-up.sh, test.sh, demo-v1.sh
```

## License

ameesh is released under **AGPL-3.0-only**. The full, unmodified GNU Affero
General Public License v3 text is in [`LICENSE`](LICENSE); it covers every file
of this repository, including the SQL migrations (they carry no per-file header,
because their sha256 is recorded in the database schema) and the test fixtures.
External contributions require a contribution agreement (CLA).
