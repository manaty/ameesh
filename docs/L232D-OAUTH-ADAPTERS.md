# Private OAuth login challenges (L232d)

`ameesh accounts login` reconnects an **installed** harness CLI on a host
through a private challenge approved by a human. It never copies another
host's credential cache, never inherits an ambient key, and never puts the
authorization URL or the one-time code in a public thread, log or `repr`: they
travel from the CLI's output to `ameesh-approve` in memory, and come back to
the approver only after a WebAuthn assertion on the private link.

The first slice (L232d socle) covers the pinned adapters, the private
challenge kind in `ameesh-approve`, the receipt binding and the post-login
health write. A real provider login is a **pilot** step, gated on the
preconditions at the end of this document.

## Command

```
ameesh accounts login <harness> [<account>] [--approver human:<id>] \
    [--scope "TEXT"] [--wait SECONDS] [--json]
```

- `<harness>`: `claude`, `codex` or `gh`.
- `<account>`: a declared account of the host (`accounts` key); omitted, the
  harness's primary account is used. `gh` is a **single** connection: there is
  no declared account, the profile is synthetic (`GH_CONFIG_DIR`, else
  `~/.config/gh`) and its account name is the empty string, like the other
  `gh` readers (`host_preflight`, `sentinel`).
- `--approver human:<id>`: the member who signs the challenge. Optional when
  `notify.default_human` is configured on the host; an explicit `--approver`
  overrides that default. Without either, the command refuses to start.
- `--wait`: bounded wait for the receipt, default 900 s; the challenge's TTL is
  `min(15 min, provider expiry)`.
- Exit code 0 only when the decision is `done` **and** the health probe says
  `healthy`; 1 on any refusal; 130 on Ctrl-C (the challenge is cancelled and
  the login process group is killed).

The command prints the private link and waits. The link alone shows nothing:
the URL and the code appear only after the approver asserts WebAuthn. Opening
the view again is allowed (viewing does not consume the challenge nonce);
signing `done` or `cancel` does, and the stored URL/code are erased as soon as
a signed receipt exists.

## Who may open a challenge: closed by default (B1)

`POST /login-requests` and `GET /login-receipts/<id>` form the **login API**.
It is **closed by default**: the service answers `403 login_api_closed`
whatever the token. Opening it is a *written* choice of the service JSON,
never an environment variable:

```json
{"login_api": "open", "login_token_file": "/etc/ameesh-approve/login-token"}
```

- the login API accepts **only** the dedicated token in `login_token_file`
  (0600, same file checks as the service token; created with
  `ameesh-approve gen-token --login`). The team service token — readable by
  every agent of the team — is never accepted there: otherwise any holder
  could file a device code generated elsewhere, with `host` set to the
  expected host, and the passkey page would present it as a fact (the
  device-auth grant goes to the process that generated the code, which the
  service cannot bind).
- `login_token_file` must be a **different file** from `token_file`; the
  service refuses the configuration otherwise. It also refuses equal token
  values, even when they come from two different files.
- the login token is held by the login **supervisor/executor** of the account,
  out of the agents' reach (no agent process reads it, no agent-readable
  file). Its custody is a pilot decision (see preconditions); a token readable
  by the agents would degrade the confinement back to a declared-only claim.
- the ameesh client reads `AMEESH_LOGIN_TOKEN_FILE` (`login_token_file`);
  without it, `ameesh accounts login` refuses before publishing.
- each challenge also gets a **close capability** (`close_token`, 256 bits)
  returned once to its creator. `POST /login-close/<id>` requires that
  capability *and* the dedicated token: a guardian cannot close another
  guardian's challenge, and the capability is never logged, stored in the
  clear or shown by the public view (N3, #2178).

The page states what the service actually knows: the host and the requester
are shown as **"declared by the caller, not verified"**, and the private view
warns that the code authorises the device that **generated** it — "only
continue if you started this connection yourself". That residual risk
(device-auth cannot be bound to a host by the service) must be accepted
explicitly by the pilot for the pilot host; the confinement above limits the
exposure to the custody of the login token.

## Pinned adapters

| harness | command | pinned | authorization URL | refused options |
|---|---|---|---|---|
| `claude` | `claude auth login --claudeai` | `2.1.x` | `claude.ai`, `claude.com`, `anthropic.com` | `--console`, `--api-key`, `setup-token` |
| `codex` | `codex login --device-auth` | `0.159.x` | `chatgpt.com`, `openai.com` | `--api-key` |
| `gh` | `gh auth login --hostname github.com --git-protocol https --web` | `2.102.x` | `github.com` | `--clipboard`, `--insecure-storage`, `--with-token` |

An installed version outside the pinned couple, a pre-release, or an
unrecognized flow is refused before any publication (`unsupported_version`,
`flow_unrecognized`). There is no fallback and no cache copy.

The CLI runs in its own process group, with an environment stripped of every
known credential variable (a declared account never succeeds "by
inheritance"), in the account's private home (existing, owned, not writable by
others — checked before the CLI starts). One live challenge per
`(host, harness, account)`; a second request is refused `already_pending`.

Known boundary: the Claude flow is a **paste-back** (the CLI prints the URL,
the code comes back from the browser to the terminal). This slice has no
private return channel for it, so it is refused before publishing
(`flow_unrecognized`). Enabling Claude in the pilot requires a private return
channel or an approved alternative.

## Receipt and health

The approver's signature covers a summary without URL or code; the receipt is
bound to `login_id`, `host`, `provider`, `account`, `scope` and `exp`, all
**recomputed from our own request** — never read from the receipt. `ameesh`
verifies the signature, consumes the nonce once (a second acceptance is
refused `REPLAY`).

On `cancel`, the process group is killed (SIGTERM, SIGKILL after the grace
delay). On `done`, the supervisor first waits, bounded by `settle`
(`PROVIDER_END_S`), for the **provider end**: the CLI must exit by itself with
code 0. A CLI that had to be killed, was interrupted, or exited non-zero stays
`provider_refused` — health is never probed on it, so an old `auth.json` cannot
be mistaken for proof of this attempt. Only after a clean provider end are the
account probes run.

A `done` receipt does not prove the grant: after the decision, the same probes
as `sentinel` / `ameesh accounts check` are run on the account's home and their
verdict is written to `credential_health` for the next placement or handoff
(`host_preflight` reads the same table, interface unchanged). The recorded
`source` is `sentinel`: the `credential_health_source_check` constraint admits
only `sentinel | turn | manual | relay`, and the on-demand pass already writes
`sentinel` with these probes. A distinct `login` source would need a migration.

## Hardening after the pilot (lot #2178)

The five non-blocking findings of the #366 security review are addressed here,
with the N1 fallback limitation below; none changes the activation preconditions
of #366.

- **Safe process-group cleanup (N1).** The supervisor observes the provider
  exit with `waitid(WNOWAIT)` and does **not** reap the leader before killing
  the group: the zombie keeps the PID (hence the session pgid) reserved, so
  `killpg` cannot target a recycled PID. A leader already reaped is never
  signalled; the cleanup is local only. A clean exit 0 stays a zombie until
  `done`/`cancel`/`close`, which then kills leftover children and reaps it.
  Without `waitid(WNOWAIT)` (including macOS with Python before 3.13), the
  fallback uses `proc.poll()` and immediately kills the remaining group.
  This retains a theoretical PID reuse window between reaping and `killpg`;
  N1 is not fully resolved on these platforms.
- **No redundant remote closure (N4).** `Supervisor.close()` skips requests
  already terminal: no `close_login` call and no `technical closure` log line
  for a challenge already `healthy`/closed; only local references are dropped.
- **Fresh credential proof (N2).** The credential fingerprint (a truncated
  SHA-256, never a secret) is read **before** the CLI starts. A healthy probe
  only becomes `healthy` when the post-login fingerprint is present and
  differs (or no credential existed before); the same fingerprint never
  proves a new login, and no row is overwritten in that case. For `gh` the
  fingerprint is that of the **effective github.com token**
  (`gh auth token --hostname github.com`, so `hosts.yml` or the keyring), not
  of the `hosts.yml` file: a comment in the file no longer fakes a new
  credential. When no token can be read (locked keyring, no token), the
  verdict carries no fingerprint and the fresh-credential proof is refused.
  The effective token is read again after the health probe; a change makes
  the verdict `unknown`, so one token's health cannot validate another.
- **`gh` probe pinned to github.com (N5).** `probe_health("gh")` and the
  sentinel's `gh` pass run `gh auth status --hostname github.com`, and
  `login_env` drops `GH_HOST`; another host can no longer make the
  `credential_health` line look healthy.
- **Cleaned version probe (N6).** `pinned_version` runs `<cli> --version` with
  the same cleaned environment as the login (`login_env`): inherited
  `GH_TOKEN`, `ANTHROPIC_API_KEY`, … are not visible to the probe.
- **Per-challenge close capability (N3).** See the login-API section above:
  the dedicated token no longer suffices to close another guardian's
  challenge.

Observable categories (never a value, never a digest): `already_pending`,
`unsupported_version`, `flow_unrecognized`, `expired`, `cancelled`,
`provider_refused`, `healthy`, `failed`.

## Verification

Targeted tests of the slice, no full suite:

- `tests/test_l232d_oauth.py` — pinned commands, flow parsing, cleaned
  environment, one live request, expiry/cancel/refusal categories, process
  group termination without reaping the leader, close of an already terminal
  request without a remote call, receipt binding and publisher.
- `tests/test_l232d_challenge_view.py` — the private challenge kind in the real
  service (mute link, assertion, private view, signed receipt, nonce replay),
  the honest page wording, and the login-API confinement: closed by default
  (`403 login_api_closed`), the team service token refused (`401`), the
  dedicated login token the only one accepted, and the per-challenge close
  capability required (N3).
- `tests/test_l232d_accounts_login.py` — CLI helpers (identity, profile,
  private home, version pinning with the cleaned environment, refusals).
- `tests/test_l232d_credential_health.py` — post-login health writing, the
  fresh-credential fingerprint proof (N2) including the `gh` effective-token
  fingerprint (keyring included, no fallback to `hosts.yml` on a comment or a
  missing token), and the `gh` probe bound to `--hostname github.com` (N5).
- `tests/test_l232d_login_e2e.py` — the whole chain against the **real**
  service on loopback with a migrated database: fake codex CLI → real
  `POST /login-requests` → WebAuthn assertion → private view → `done` → real
  receipt fetch → verification and nonce consumption → process killed →
  health row read back. This witness found the `source` constraint defect
  above, which no fake-store test could see.

## Pilot preconditions (not covered by the socle)

- the pilot host is chosen and admitted under the existing preflight rules; no
  root, no new SSH channel, no `ameesh-secrets` sharing;
- confirmed `--device-auth` support for the installed Codex on that host;
- a private return channel (or an approved alternative) before Claude can be
  activated;
- one lease/cap/guard per operation, a rollback path and a canary run;
- the security review of this slice is acquired **before** any real login is
  attempted;
- the pilot records its explicit acceptance of the residual risk (declared
  host/requester; the device code authorises the device that generated it),
  and names the custody of the login token: which account/process holds it,
  outside the agents' reach. The service JSON and its directory follow the
  same custody: they are not writable by the agents, otherwise an agent could
  open `login_api` or redirect `login_token_file` to a token it controls —
  the token's custody alone does not close that path.

See also `docs/COMPTES.md` for account declaration and the sentinel's health
rules.
