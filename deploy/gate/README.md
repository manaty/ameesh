# Credential gate (`ameesh-gate`)

Imposed path for KMS/IAM and GitHub production acts, lot #7767. The study,
the migration in five stages and the owner decision are in
`docs/design/pre-act-credentials-7767.md`; this directory only carries the
installable pieces.

## Invariant

The gate account `ameesh-gate` owns the cloud and GitHub credential stores.
No agent account has a usable KMS, IAM or production-GitHub credential. An
act leaves the agent only through `ameesh gate act`, which sends the command
to the broker over a Unix socket. The broker:

1. authenticates the caller with `SO_PEERCRED` (uid or client group);
2. matches the command against a root-owned policy: exact executable, exact
   argv prefix, fixed/allowed/required flags, exact positional targets, and
   the credential-dumping flags refused everywhere;
3. runs the pre-act check itself (`pre_act.inspect`) on the lot; a STOP, a
   stale check or a duplicate receipt means nothing is executed;
4. runs the command with the gate account's environment, bounded in time and
   output;
5. writes `pre-act/2` start and end evidence on the lot.

A production `gh workflow run` is checked against the applied canon: only the
project's production branch and declared production workflows, only the
declared repository, and `pregeste=<id>` is injected by the broker. A caller
cannot supply `pregeste` itself.

## Files

| file | role |
| --- | --- |
| `ameesh-gate.service` | hardened systemd unit, runs as `ameesh-gate` |
| `broker.json.example` | policy example: two read-only KMS rules and one production dispatch |
| `config.json.example` | broker configuration: DSN without password, host, canon |
| `install.sh` | account, venv, files, unit, `--check`, `--uninstall` |

The example policy is deliberately minimal. It is a template, not the
production policy: the real one is root-owned, dated and reviewed like code.
Rules with a positional argument (for example `gcloud kms keyrings describe
KEYRING`) must set `targets`; without it the broker leaves positionals free,
which is only acceptable for commands that take none. Every flag must be
declared in `fixed`, `allow`, `required` or `free`: any other long flag, any
short flag and any abbreviation is refused, and `--flags-file` is refused
everywhere. A rule that creates, imports or sets a key version is an owner
act, not a routine gate act; add it only after the owner decision names the
key.

## Before installing

The owner decision (design section 7) must name: the host, the KMS service
account, the keyring, the GitHub repository, the machine identity, the
window, the installer and the escrow destination. The installer creates no
cloud or GitHub identity and grants no role. In particular:

* create the KMS service account with KMS rights on `nexlink-messages` only;
* create the GitHub machine identity with `Actions: write` and `Contents:
  read` on `manaty/nexlink` only, and put it in the gate account's `gh`
  store;
* escrow the current `gcloud` and `gh` stores offline, mode 0600, with a date;
* keep the bureau account out of `/var/lib/ameesh-gate` and
  `/etc/ameesh-gate`.

## Install

From a checkout of ameesh, as root, on the act host:

```sh
sudo deploy/gate/install.sh --client bureau --pgpass /root/gate.pgpass --wheel /root/ameesh.whl
# review /etc/ameesh-gate/broker.json and /etc/ameesh-gate/config.json
sudo deploy/gate/install.sh --check --live
sudo systemctl enable --now ameesh-gate.service
```

Without `--wheel`, the installer builds from this checkout, whose build
backend then runs as root: the source tree must be root-owned and free of
group/world-writable entries, otherwise the installer refuses (review N2).
A root-owned checkout is acceptable; a wheel is the safer path.

`--client` adds an existing account to `ameesh-gate-client`; add only the
bureau account, never an agent. `--pgpass` installs the gate's `.pgpass`
(mode 0600, owned by the gate account). `--wheel FILE` installs from a
prebuilt wheel instead of this checkout. Existing policy and configuration
files are never overwritten; the installer writes the examples as
`*.example` and copies them to the real names only when those are absent.

The account has no login shell, no sudo and no root access. The unit uses
`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, a private
temporary directory, a restricted address-family list and `UMask=0077`.

## Operate

The caller must have an open pre-act check on the lot, or use
`--wait-until` so the broker waits for the window:

```sh
ameesh gate act --lot 1234 --kind kms --target nexlink-messages -- \
  gcloud kms keyrings describe nexlink-messages --project=nexlink-prod \
  --location=asia-southeast1

ameesh gate act --lot 1234 --kind prod --wait-until 2026-11-19T08:00:00+00:00 -- \
  gh workflow run deploy-api-prod.yml --repo manaty/nexlink --ref main
```

The result is a `ameesh-gate-result/1` object on stdout (`--json` keeps it
machine-readable). The command runs only after the check; the broker records
the start before executing and the end with the exit code and bounded output
digests. The full argv is never stored.

Audit the traces:

```sh
ameesh gate audit --since 2026-11-18T00:00:00+00:00
```

It reports starts without an end after the stale delay, ends without a start,
and duplicate starts. Exit code 2 means an anomaly. The audit is detective:
the preventive barrier is the IAM scope and the GitHub token, which are not
in the agent's hands.

## Check and diagnose

`install.sh --check` verifies the account, groups, modes and ownership, the
private credential directories, the JSON files (a password in the DSN is
refused), and loads the policy as the gate account. `--live` additionally
runs `ameesh gate broker check`, which requires the canon and a transactional
database connection and refuses a remote database.

```sh
sudo deploy/gate/install.sh --check
sudo -u ameesh-gate env HOME=/var/lib/ameesh-gate \
  AMEESH_CONFIG=/etc/ameesh-gate/config.json \
  AMEESH_CREDENTIAL_GATE=/etc/ameesh-gate/broker.json \
  /var/lib/ameesh-gate/venv/bin/ameesh gate broker check
journalctl -u ameesh-gate.service
```

The broker fails closed: a missing policy, canon, database or executable
means no act. A broker down means KMS/production acts are blocked.

## Rollback

```sh
sudo deploy/gate/install.sh --uninstall          # stop, remove unit; keep stores
sudo deploy/gate/install.sh --uninstall --purge --force
```

`--uninstall` keeps `/var/lib/ameesh-gate` (credential stores, venv) and
`/etc/ameesh-gate` (policy, configuration). `--purge --force` removes both and
the account; run it only after the offline escrow is verified. Restoring the
bureau's `gcloud`/`gh` stores and the IAM roles is an owner act, not an
installer action.

## Limits

* The gate account is the new single point: a compromise gives every admitted
  act. It is not root, has no login, and its stores are 0700.
* The policy is the security boundary; a broad rule reopens the bypass.
* No per-lot rate limit, no high availability, no signed receipts in this
  first version. Cloud audit logs and IAM review remain the detective
  backstop.
* A GitHub owner token outside the gate remains a bypass until migration
  stage 4.5 removes it.
