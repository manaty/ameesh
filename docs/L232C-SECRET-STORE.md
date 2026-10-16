# Secret store, reference delivery and rotation (L232c)

`deploy/mise-a-jour/coffre-release.py` turns the mesh's existing protected
directory (`/etc/ameesh`, `0750 root:ameesh`, environment files `0640`) into a
reference-addressed store. It is source only: it is not installed, it opens no
SSH connection, and its tests need neither root nor a real store.

The inventory of the existing store (2026-10-13) is in
`docs/design/etudes/l232-autonomous-host-provisioning.md`: no vault, no
delivery API, and a `~/.config/ameesh/config.json` observed at `0664` with a
live database password. Nothing here widens that: the bridge validates the
modes it depends on instead of inheriting them.

## References and policy

A reference is `<file>#<KEY>` (`harnais.env#DEEPSEEK_API_KEY`): a plain file
name in the store directory and an environment key. Never a path, a glob, a
command or a value.

The mesh side reads one exact-key private document
(`/etc/ameesh-secrets/policy.json`, root-owned, `0600`/`0640`):

```json
{"version": 1, "host": "ameesh-agents-2",
 "store": {"root": "/etc/ameesh", "max_bytes": 65536},
 "references": {"harnais.env#DEEPSEEK_API_KEY": {"destinations": [
   {"host": "ameesh-agents-2", "account": "deepseek1", "kind": "account_file",
    "path": "/home/deepseek1/.config/ameesh/deepseek.key", "owner": "deepseek1",
    "group": "deepseek1", "mode": "0600", "dir_mode": "0700",
    "keep_previous": 1, "health": "account_read"}]}}}
```

A reference absent from the policy is refused (`reference_unknown`); a
reference is only delivered to the exact `(host, account)` pair the policy
names. The store file itself must be a regular file, owned by root or the
caller, mode `0600` or `0640`, within `max_bytes`, and its ancestors must not
be writable by another user (a group-writable ancestor owned by root or the
caller is admitted: the shared Unix group is the study's trust model).

## Framing and the destination helper

`frame(request, value)` writes one JSON request line, then the raw value. The
destination helper reads its stdin; `unframe` checks the declared byte count
before the helper touches anything. The value never appears in the JSON, in
argv, in the receipt, in the journal or in an error: every failure carries a
stable category (`frame`, `reference`, `destination_unknown`, `file_mode`,
`unmanaged_file`, `rotation_required`, `managed_missing`, `health_unavailable`,
`health`, `replay_mismatch`, `lock`, `busy`, `pending_record`,
`rollback_missing`, `write`, `transport`, `receipt`…), never a value or a
digest.

The host side is enrolled by `/etc/ameesh-secrets/helper.json` (root-owned,
exact keys), which lists the only destinations admitted on that host. A
request whose destination does not match its enrollment exactly is refused
before any effect.

Two destination kinds:

- `account_file` — the bridge: `0600`, owned by the account, in a `0700`
  directory of that account. This is what `accounts.py` expects for a
  `key_file`.
- `env_file` — the system store: `0640 root:<group>` in a `0750` directory.

### Root never acts by path in an account's directory

An `account_file` lives in a directory the account owns: the account can swap
its entries at any moment (symbolic link, hard link, rename), so a root check
followed by a root write by path would give it a root write elsewhere. When
the helper runs as root, the whole operation on an `account_file` (lock,
recovery, record, value, health, rollback, revocation) runs in a forked child
that first drops every privilege to the account (`setgroups([])`, then real,
effective and saved gid and uid); only the receipt, or the error category,
comes back through a pipe. A swap then only reaches what the account could
already reach. A child that dies without answering leaves the outcome unknown
(`account_transaction`, uncertain: the next locked operation recovers). In the
child, the health check is the real read of the file by the account itself.
An `account_file` for `root` is refused (`destination_bridge`). Every file the
store reads must have a single link (`file_links`): a hard link cannot make
another inode pass for a managed file.

## Atomic install, replay and rotation

An install writes a temporary file in the destination directory
(`O_CREAT|O_EXCL|O_NOFOLLOW`, mode set on the descriptor), `fsync`s it, reads
it back **in memory** and compares the bytes, `os.replace`s it onto the
destination and `fsync`s the directory. The read-back comparison is never
persisted: no digest of a secret reaches the journal, a receipt or an error.

A request replayed after a lost reply (same version id, identical content)
returns `replayed: true` without rewriting. The same version id with
different content raises (`replay_mismatch`) instead of silently overwriting.
An existing destination file with no version record is never adopted or
replaced (`unmanaged_file`).

### Durable transaction

Replacing the value and the version record are two separate atomic writes;
they are tied by a durable intent so that an interruption at any boundary is
decidable without a digest:

- `<path>.rollback` (same mode as the destination) holds the value being
  replaced, when there is one;
- `<path>.pending` (`0600`) holds the intent: target version, previous
  version, whether a rollback copy exists, the `keep_previous` policy and the
  exact version record to restore;
- the value is written atomically, health is checked, then the version record
  (`<path>.version`) is written — the commit point — and the intent is cleared;
- on the next operation for that destination, under the lock, a pending intent
  is either finished (the record already carries the target: the rollback copy
  becomes `.previous`, or is dropped; the intent is cleared) or undone (the
  value, the retained previous version and the record are restored exactly as
  before the intent). A missing rollback copy freezes the destination
  (`rollback_missing`) instead of guessing;
- an interrupted recovery replays identically: the rollback copy is kept until
  the value and the record are durably restored **and** the intent is durably
  erased; only then is the copy dropped. A copy left behind without an intent
  is inert and removed by the next locked operation;
- every cleanup transition (unlink of the intent, of the value, of the record,
  of the copy, or rename of the copy to `.previous`) is followed by an `fsync`
  of the directory, so a restart reads the same state back.

An `install` never replaces a managed value: on a destination that has a
version record, an `install` with a different version id is refused before any
effect (`rotation_required`). Replacing a value goes through `rotate`, which
the mesh side only sends after the injected turn boundary allows it
(`boundary_unavailable`, `busy`): the helper enforces the state, the boundary
remains a mesh-side authorisation and not a label chosen by the caller.

### Exclusion per destination

Every operation (`install`, replay, `rotate`, `revoke`, recovery) holds a
`flock` on `<path>.lock` (`0600`, in the destination directory) for the whole
transaction — record read, health check, publication and rollback. The lock
dies with the process and waiting is bounded (ten seconds, then `busy`), so
two helper processes cannot interleave a value, a version and a `.previous`.
A leftover private temporary from a hard stop is swept under the lock.

Rotation keeps the replaced version privately in `<path>.previous` until the
policy's `keep_previous` confirms it may be dropped; the version record
(`<path>.version`, `0600`, version ids only) drives reconciliation.
`revoke <version>` removes the retained previous version only and refuses the
current one (`current_version`); the current credential is never removed by a
revocation.

Health: an `account_file` install requires an injected health check,
`read_as(account, path)`. As root, the check is the account's own read inside
the unprivileged child (above). Outside that child, the default implementation
runs a bounded `runuser -u <account> -- /bin/cat <path>` (root only); with no
check injected, the install is refused **before any effect**
(`health_unavailable`). If the check fails, or raises once the value is
written, the previous version is restored (or a first install is removed) and
the receipt is `ok: false, category: health`: the account is never declared
served on a guess, and a written value never comes back as a refusal "without
effect".
`env_file` uses a root read-back. An `env_file` currently receives the raw
value, not a `KEY=value` line (R2 dev4, N3): its consumer must be settled
before a system destination is enrolled.

## Journal

`/var/lib/ameesh-secrets/rotations.jsonl` (`0700` directory, `0600` file,
`fsync`ed) records the timestamp, reference, host, account, operation, random
version id and category (`intent`, `installed`, `health`, `transport`,
`revoked`…). Version ids are random and never derived from the value; the
journal carries no value, no digest and no connection string.

## What remains before a host pilot

- **Transport**: wire `Livraison` to L232b's `transport-release.py` exchange
  and to the root host dispatcher (the `helper-*.py` pattern) that calls
  `Installeur.handle` on its stdin. The enrollment documents must be installed
  as root at their fixed paths.
- **Turn boundary**: `Livraison` requires an injected `boundary(host, account)`
  for a rotation and refuses (`boundary_unavailable`, `busy`) without one; the
  live implementation must come from the account/turn state so no key changes
  under a running turn.
- **Account preflight**: after the bridge, run `accounts.py`'s preflight for
  the account and hold the account on a missing, revoked or unhealthy
  credential rather than falling back to paid API usage.
- **Systemd credentials**: remained an alternative to the `0600` bridge; test
  actual runner access before adopting either.
- **New enrolment**: a new reference or a broader destination needs a scoped
  owner decision (study L232c); routine delivery inside an approved policy
  does not.
- **Canary evidence**: the tests inject a synthetic value and assert it is
  absent from stdout, stderr, receipts, the journal and exception text. Mail,
  Git and retained artifacts carried no value in the inventory; the pilot
  must re-run that check on the real host.

## Tests

`tests/test_l232c_coffre.py` (57 cases, no database, no root, no network):

    PYTHONPATH=src python3 -m unittest tests.test_l232c_coffre

It covers reference grammar, environment parsing, store permissions and
symlinks, policy/enrollment exact keys, the bridge modes, atomic install,
replay without rewrite, replay divergence, unmanaged files, health refusal
before effect, health rollback to the previous version, rotation and
revocation, transport failure and inconsistent receipts, the refusal of an
`install` on a managed destination (and the `rotate` authority), recovery
after an interruption at each transaction boundary (value written without
commit, intent without value, rollback copy without intent, commit without
cleanup, orphan temporary, two successive stops — the second in the middle of
the recovery, before the intent is erased — a copy left after the intent is
gone, and the `fsync` of every cleanup transition), concurrent rotations and
rotate/revoke, an inter-process `flock` holder, and a subprocess canary through
the helper entry point, the unprivileged child for an `account_file` (install,
replay, rotation, revocation, a refusal carried back as a category, the
account's own health read, a child that dies, a `root` account refused), a
hard-linked destination, and a health check that raises after the write.
