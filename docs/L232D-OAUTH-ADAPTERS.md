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
ameesh accounts login <harness> [<account>] --approver human:<id> \
    [--scope "TEXT"] [--wait SECONDS] [--json]
```

- `<harness>`: `claude`, `codex` or `gh`.
- `<account>`: a declared account of the host (`accounts` key); omitted, the
  harness's primary account is used. `gh` is a **single** connection: there is
  no declared account, the profile is synthetic (`GH_CONFIG_DIR`, else
  `~/.config/gh`) and its account name is the empty string, like the other
  `gh` readers (`host_preflight`, `sentinel`).
- `--approver human:<id>`: required, the member who signs the challenge.
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
refused `REPLAY`), then kills the process group (SIGTERM, SIGKILL after the
grace delay) and probes the account.

A `done` receipt does not prove the grant: after the decision, the same probes
as `sentinel` / `ameesh accounts check` are run on the account's home and their
verdict is written to `credential_health` for the next placement or handoff
(`host_preflight` reads the same table, interface unchanged). The recorded
`source` is `sentinel`: the `credential_health_source_check` constraint admits
only `sentinel | turn | manual | relay`, and the on-demand pass already writes
`sentinel` with these probes. A distinct `login` source would need a migration.

Observable categories (never a value, never a digest): `already_pending`,
`unsupported_version`, `flow_unrecognized`, `expired`, `cancelled`,
`provider_refused`, `healthy`, `failed`.

## Verification

Targeted tests of the slice, no full suite:

- `tests/test_l232d_oauth.py` — pinned commands, flow parsing, cleaned
  environment, one live request, expiry/cancel/refusal categories, process
  group termination, receipt binding and publisher.
- `tests/test_l232d_challenge_view.py` — the private challenge kind in the real
  service (mute link, assertion, private view, signed receipt, nonce replay),
  the honest page wording, and the login-API confinement: closed by default
  (`403 login_api_closed`), the team service token refused (`401`), the
  dedicated login token the only one accepted.
- `tests/test_l232d_accounts_login.py` — CLI helpers (identity, profile,
  private home, version pinning, refusals).
- `tests/test_l232d_credential_health.py` — post-login health writing.
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
  outside the agents' reach.

See also `docs/COMPTES.md` for account declaration and the sentinel's health
rules.
