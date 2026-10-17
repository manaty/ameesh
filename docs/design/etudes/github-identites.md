# GitHub identities per role, and a journal of agent gestures (#1645)

Lot #1645, under the #1606 umbrella (process in code). Author: ameesh-dev4, 2026-10-15.

## Finding

On 2026-10-15 at 11:46Z, the CI of the 11:00 package (PR #1162, head 63b08e0d0) was cancelled. Every action on that PR and its runs is recorded under `smichea`, the owner: the cancellation, the `ci-full` labels, and auto-merge being armed then disarmed. No one could say which agent did it.

The cause is structural. No ameesh code chooses a GitHub credential:

* An agent turn starts from `os.environ.copy()` of the executor (runner.py, turn environment; `_attach_env` for `ameesh attach`). The executor's units load `~/.config/ameesh/env`, so the owner's `GH_TOKEN` reaches every turn. Without it, `gh` reads the owner's `~/.config/gh/hosts.yml`, and git uses the global helper written by `gh auth setup-git`.
* `accounts.launch_env` strips only model-account variables (`ANTHROPIC_API_KEY`, `CLAUDE_CONFIG_DIR`, …), never `GH_TOKEN`/`GITHUB_TOKEN`.
* ameesh's own gestures (`land.pr_merge`, the git-merge connector, issue projection in `plan_github`, read-only `gh` calls in radar, ci, versions, lot_measures) also inherit the executor's environment.
* The gestures that caused the incident (cancelling a run, labelling, arming auto-merge) are **not in ameesh code at all**: agents run `gh run cancel`, `gh pr edit --add-label` and `gh pr merge --auto` themselves, from their turn. ameesh records none of them.

The only per-identity precedent is the guest runners (`deploy/guest/`), which load one `GH_TOKEN` per agent from `/etc/ameesh/guest-<agent>.env`.

### Where ameesh itself writes to GitHub

| Operation | Code | Trigger |
|---|---|---|
| PR merge (`gh pr merge --match-head-commit`) | `land.pr_merge` (land.py) | radar land pass, `ameesh work land` |
| PR merge | `connectors/git_merge.py` | `ameesh action execute` |
| Direct merge, pushed over SSH | `merge_pr.direct_merge` | `ameesh work merge-pr` |
| Push of a lease ref, a land, a resolution | `land._land`, `land.take_remote_lease`, `resolutions.push` | land pass, `work resolve --done` |
| Issues, labels, comments, sub-issues | `plan_github._Repo` | `ameesh work project-github`, `ameesh notify` (projection) |
| Check runs | `scripts/ci-flow.py` | CI, with `github.token` |
| Re-run of failed jobs | `scripts/ci-relance-hosted.py` | CI, with `github.token` |
| Release | `deploy/mise-a-jour/poste.sh` | operator |

No ameesh code cancels a run, dispatches a workflow, labels a PR or arms auto-merge: those gestures come only from agents.

## What this lot ships (reversible)

### 1. A journal of gh gestures, on by default

The executor writes a generated `gh` into `<state>/github/bin/` and puts it first on the turn's `PATH` (turns and `ameesh attach`). Before running the real `gh` with `execv`, it appends one JSON line to `<state>/github/gestes-YYYY-MM.jsonl` (mode 0600). The line carries the time, agent, turn id, lot, role, host, pid, cwd and arguments. Exit code, signals and stdin/stdout are those of the real `gh`.

* Only a safe projection of the arguments is written (semantic allowlist): a known command and subcommand (`pr merge`, `run cancel`…; anything else becomes `[commande]`), the names of known options, the value of an identifier option (`--repo`, `--match-head-commit`, `--label`…) when it has the shape of an identifier, and positional arguments only for commands whose positions are identifiers (`pr`, `issue`, `run`, `workflow`, `release`, `label`, `repo`, `cache`, `api` — an API path loses its query string), and only before the first option: after it, an arity mistake (an option boolean in one subcommand and value-taking in another, such as `--comment` for `pr review` and `pr close`) could make a value look like a positional, so every later positional becomes `[valeur]`. Only flags that are boolean in every gh subcommand are treated as booleans; any other known option consumes the next argument, which masks one argument too many when gh reads the option as a boolean. Positionals of other commands (`search code <query>`, `secret set <name>`) become `[valeur]`. If the argument after a value-taking option starts with a dash, the filter can no longer tell how gh aligns options and values: for `pr review 1 -a --body --head=…`, gh reads `-a` as a boolean and `--head=…` as the body. So nothing more is written from that argument on. The same holds after an unknown option, a short option with a glued value (`-ab` may be a group of booleans whose last one takes the next argument), or `--`: every remaining argument becomes `[valeur]`. Every free value — body, title, header, `-f key=value`, a secret's `--body` — becomes `[valeur]`; a recognisable token (`ghp_…`, `github_pat_…`, even glued to other characters) becomes `[jeton]`. Nothing is truncated, so nothing is half-masked. The journal answers "who did what to which object", never "with which content"; a gesture through an unknown option loses its detail rather than risk a leak.
* Only the values of a fixed list of options (`--repo`, `--head`, `--match-head-commit`, `--label`, `-X`…) are kept, and only when they look like identifiers; a free value (`--json`, `--body`…) is masked.
* A write failure prints a warning and never blocks the command.
* `ameesh github gestes --match 'run cancel' --since 24h` answers "who cancelled this run". `--agent` and `--json` filter and format the output.
* It is turned off with `"journal": false` in `github-identities.json`.

This is **attribution, not enforcement**. An agent that calls the real `gh` by its absolute path, or the API through `curl`, is not journaled. Enforcement comes from the permissions of separate identities (§3). That holds only if an agent cannot read another role's token: on a host where all agents share one Unix user, mode 0600 does not separate them, and every agent can read every `token_file`. Real separation needs tokens that agents never see (a GitHub App minter that hands out short-lived installation tokens per turn), or separate Unix users or hosts. Until then, per-role identities limit what a role's token can do, not which agent uses it.

### 2. An identity per role, inert until configured

`<config>/github-identities.json` maps agents to a role and each role to a token file. It holds no secret:

```json
{"journal": true,
 "default_role": "agent",
 "roles": {
   "agent":  {"token_file": "~/.config/ameesh/github/agent.token"},
   "pilote": {"token_file": "~/.config/ameesh/github/pilote.token",
              "agents": ["mesh-design", "orchestrateur"]}}}
```

When an agent's role has a readable token file in mode 0600 (a wider mode is refused), the turn gets:

* `GH_TOKEN` set to that token, with the inherited `GITHUB_TOKEN`/`GH_ENTERPRISE_TOKEN` removed;
* an empty `GH_CONFIG_DIR` per role, so the owner's `hosts.yml` is no longer read;
* a git credential helper for `https://github.com` that answers with the role's token. An empty helper value first clears the inherited helpers for that URL. The keys are added through `GIT_CONFIG_COUNT`, after any existing ones.

Without the file, without a role, or with an unreadable token, nothing changes: the turn keeps the inherited identity and the executor logs why. `ameesh github identity <agent>` shows the resolution and never prints the token.

### What this lot does not change

* ameesh's own gestures (land, projection, connectors) keep the executor's identity. Moving them to the `train` and `projection` identities is the next lot (§5), once the identities exist.
* SSH remotes (`git@github.com:…`) still push with the host's SSH key. `merge_pr` relies on that path on purpose (EXPLOITATION, `work merge-pr`). An HTTPS remote is needed for a push to be attributed to the role.
* Guest runners keep their per-agent `GH_TOKEN`.

## 3. Roles and minimal permissions (proposal)

GitHub's fine-grained permissions cannot separate "dispatch a workflow" from "cancel a run": both are `Actions: write`. The only way to keep agents from cancelling a package's CI is therefore to give them no `Actions: write` at all, and to route dispatching through an identity that only ameesh tooling holds.

| Role | Holders | Contents | Pull requests | Issues | Actions | Checks / statuses | Workflows |
|---|---|---|---|---|---|---|---|
| `agent` (default) | dev, review and qa agents | write (push their own branches) | write (open, comment, review) | write | **read** | read | — |
| `pilote` | mesh-design, orchestrateur | write | write | write | **read** | read | — |
| `ci` | the executor's dispatch tool only (lot CI on a frozen head) | read | read | — | write | write (`lot/<suite>` statuses) | — |
| `train` | the 11:00/23:00 cut, the merge queue, `work land` | write | write | — | read | read | write (PRs touching `.github/workflows/**`) |
| `projection` | `ameesh notify` on the `github.host` host | read | — | write | — | — | — |
| owner | releases, prod, secrets | (unchanged, the owner's own account) | | | | | |

Branch rules complete the permissions. On `main`, `develop` and `release/**`, only `train` (plus the owner) may push or merge. Agents keep pushing `lot/**`, `agent/**` and `review/**`. On ameesh (public), this is a ruleset with `train` in the bypass list. On Nexlink (private, free plan, no rulesets or branch protection), only the permissions protect anything. That is one more reason to keep `Contents: write` away from any identity that does not need it, and it argues for the paid plan or for moving the gate into the ameesh merge queue, which is already the case.

## 4. What the owner must create before activation

None of this can be done by an agent. Two forms are possible; the choice is the owner's:

1. **Machine accounts** (`manaty-agent`, `manaty-pilote`, `manaty-train`, `manaty-ci`, `manaty-projection`) added to the organisation, each with a *fine-grained* PAT restricted to the manaty repositories and to the permissions above. Expiry is at most one year and the tokens must be renewed. Each gesture then appears under the role's account. This is the simplest form: `token_file` is all this lot needs.
2. **One GitHub App per role** (`ameesh-train[bot]`, …), installed on the organisation. Its tokens last one hour: they need a minter (JWT signed with the App's private key, exchanged for an installation token) that refreshes `token_file` before expiry. This is more secure (no long-lived token, permissions declared on the App), and needs a small follow-up lot for the minter. It is the recommended target.

In both cases:

* Each token is a host secret, in a 0600 file under `~/.config/ameesh/github/`, never in a repository or a message.
* `github-identities.json` is written by the operator.
* On a host where all agents run as the same Unix user, every agent can read every `token_file` despite mode 0600. Activation then gives attribution (the account shown on GitHub) and the permissions of the role used, not a separation between agents of that user: an agent can still pick up another role's token. Separation needs separate Unix users or hosts, or tokens minted per turn that the agent never reads (§1).
* The owner's token is then removed from `~/.config/ameesh/env` on the agent hosts.

Activation is gradual and reversible: one role at a time, starting with `agent` (no `Actions: write`). Deleting the file restores the inherited identity on the next turn.

## 5. Proposed follow-ups

* **#1645-b** (after the identities exist): ameesh's own gestures choose their identity explicitly. `land` and `merge_queue` use `train`, `plan_github` projection uses `projection`, and lot CI dispatch uses `ci` through an ameesh command (`ameesh ci dispatch <lot> --head <sha>`), the only path that keeps `Actions: write`. Each of these gestures is also journaled, with the ameesh actor (`--actor`, which `land` parses today without using).
* **#1645-c**: the App token minter, if option 2 is chosen.
* **#1645-d**: retention of the journal (monthly files, the `ménage` policy) and a reading of it in `ameesh progress` (gestures per lot).
