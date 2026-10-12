# V1 — owner authority, mesh list, work_items (points 3 to 5)

> **Warning — test-bench document, outdated regarding human authority.** This
> document describes the bench's authority model (Ed25519 "owner" key).
> The v1 design replaced it: a human's authority is a **receipt**
> `ameesh-receipt/1` signed out of the agents' reach, on their phone, by
> **ameesh-approve**; the **Ed25519 key ceremony on the PC is abandoned**
> ([decision 0012](design/decisions/0012-autorite-par-ameesh-approve.md)).
> Ed25519 keys remain for agent provenance (R13) and tests,
> never as human authority. Reference: [`docs/design/`](design/index.md) —
> [specification](design/specification.md) §7 (gate), §8 (receipts), §9
> (ameesh-approve) — and the rewritten cutover, [`BASCULE.md`](BASCULE.md).
> The rest (mesh list, work_items, v0 import/export) remains valid.

Continuation of [`V1-MAILBOX-RUNNER.md`](V1-MAILBOX-RUNNER.md) (points 1 and 2: Postgres
mailbox and per-machine runner). Still on the test bench: no real agent
is connected; the cutover is described in [`BASCULE.md`](BASCULE.md).

---

## 1. Owner authority (R4, spec §5 point 3)

### The model

**Authority is never inferred from text.** A message or an approval only
carries the owner's authority if three conditions are met:

1. **known owner key** — the sender has a *public* key registered
   in `agent_registry.public_key` with `key_role = 'owner'`, and that key
   is not revoked (`key_revoked_at IS NULL`). The private key never touches
   the database;
2. **covered content** — the Ed25519 signature verifies over a canonical payload
   that *is exactly* the stored content:
   `agent-mesh/v1|message|{"expires":…,"from":…,"kind":"message","nonce":…,"text":…,"to":…,"ts":…,"v":1}`
   (sorted JSON, no spaces). The verifier rebuilds this payload from the
   row and requires equality with `signed_payload`, then verifies the signature:
   modifying the body, the recipient, the timestamp or the nonce invalidates the
   proof;
3. **expiry** — `signature_expires_at` is in the future, **and it is
   exactly the signed expiry**: the column and the payload must match
   (to the microsecond). Modifying `expires_at` in the database therefore does not revive
   an expired signature; the same goes for `created_at` for
   approvals. An expired signature is reported as such, never
   ignored.

### Two key roles: authority and provenance

`agent_registry.key_role` distinguishes two things that must not be confused:

| role | what the key proves | what it allows |
|---|---|---|
| `owner` | the owner's authority (their private key does not circulate) | messages **and** approvals |
| `agent` (default) | provenance: this message really is from this agent, content and expiry covered | nothing more: **no** owner authority, **no** approval |

The default is `agent` (least privilege): `key register --role owner` is an
explicit act. This is what prevents registering an agent's key to
sign its mail from giving it, as a side effect, the owner's authority.

The hooks therefore distinguish **four states**:

* unsigned → agent message, JSON identical to v0;
* signed by an `agent` key → `[signé par un agent, authentique, sans autorité propriétaire, expire …]` (signed by an agent, authentic, without owner authority, expires …);
* signed by an `owner` key → `[autorité du propriétaire PROUVÉE, expire …]` (owner authority PROVEN, expires …);
* invalid or expired signature → `[⚠ signature NON valide : …]` (signature NOT valid: …), never ignored.

Approvals (`mesh_approvals`) follow the same model with, in addition, an
`action`, an artifact fingerprint (`sha256` hex) and a decision
(`approved`/`rejected`). **Single consumption** is not a column of the
row but a separate table, `mesh_consumed_nonces`, primary key
`(approver, nonce)`: reinserting the same signed receipt under a new id — or
deleting the consumed row and then recreating it — does not allow replaying it, and
a unique index on `(approver, nonce)` blocks direct copying. **When upgrading**
an earlier schema, migration `0007` backfills this ledger
from the historical `consumed_at` values: an approval consumed before 0006 does not
become consumable again after migration (0006 remains immutable). `verify_approval`
rejects any key that is not `owner`, including if the row was inserted into
the database by hand.

### Ed25519: three implementations, one interface

`signing.py` exposes:

| backend | availability | use |
|---|---|---|
| `pure` | always (stdlib `hashlib` only) | default, verification without any dependency |
| `cryptography` | if importable | speed-up, cross-checking |
| `nacl` | if importable | same |

`AMEESH_SIGNING_BACKEND=auto|pure|cryptography|nacl` selects. The pure
implementation is RFC 8032; it is validated by:

* the **RFC 8032 §7.1 test vectors** (including the 1024-byte message);
* a **cross-check with OpenSSL**: OpenSSL verifies our signatures and we
  verify its own (test `test_croisement_openssl`);
* a **cross-check between backends** when several are installed.

In practice, a round trip signs in ~12 ms and verifies in ~28 ms in pure
Python — negligible compared with an agent turn.

### Keys

PEM-like format, readable and unambiguous (`AGENT-MESH PUBLIC/PRIVATE KEY`,
base64 of the 32-byte seed or key). Rules:

* `agent-mesh key generate` **refuses** to run without `--i-am-the-owner`:
  no agent generates or holds an `owner`-role key. (The owner key
  ceremony planned by the old cutover is abandoned in v1:
  decision 0012, see the warning at the top);
* the private key is written as `0600`, refused on read if its permissions
  are too open, never logged, never in the database, never in a message;
* `agent-mesh key register <agent> --public-key FICHIER [--role owner|agent]`
  only accepts a public key (a private key is rejected); `key revoke`
  immediately invalidates all signatures of that agent. The default role
  is `agent`; passing `--role owner` delegates the owner's authority and
  displays a warning.
* the owner's own key (a human, not an agent, #330) is registered without
  any agent:
  `ameesh key register --human smichea --public-key ~/.config/ameesh/owner.key/smichea.pub --role owner`.
  It goes to `key_holders`, an identity that holds a key and nothing else:
  never executable, never woken, in no project, alert or L165 take, out of
  reach of `attach`, hook sessions and canon cards (none of them reads that
  table). Its name is the one `ameesh decide --key` signs under by default
  (`human:smichea` → `smichea`). It is refused when an agent already has
  that name (the owner's authority never sits on an agent's identity), and
  an agent key can no longer be registered under a key holder's name. The
  key lookups read both through the `mesh_keys` view, the human's key first.
  Migration 0364 copies the stopgap registry row of 2026-10-12 there —
  exactly the row `smichea` (harness `none`, mode `externe`, responsible
  `human:smichea`, no turn, session, lease or canon card), if a key had been
  registered on it; `key register --human smichea` then removes that row
  and no other from the registry (an additive migration deletes nothing).
* who may write a key holder (#326 r1). Every agent shares the executor's
  database role, so the rules sit in the table itself, a trigger of 0364
  that the executor can neither disable nor drop (it owns nothing and has
  no `TRIGGER` right — `deploy/sql/role-executor.sql` audits both):
  * a **first** key only: an existing key, live or revoked, is never
    overwritten; a first `owner` key only while no owner key exists at all
    (revoked ones count), and a unique index keeps `key_holders` to one
    owner key holder — two transactions registering one at once cannot both
    commit, whatever their isolation level. The CLI writes a note to the
    owner's chat (`chat-<id>`) if it exists;
  * a **revocation** (`key revoke`) is open to all: it only removes
    authority;
  * replacing or rotating a key, re-registering after a revocation,
    removing a holder: the **owner's own gesture**,
    `ameesh key register --human <id> --public-key … --role owner --replace`
    run with the owner's database role (a member of the role owning the
    tables, the one that runs the migrations — never the executor's). The
    CLI sets `ameesh.owner_gesture` in its transaction and the trigger also
    checks the role: the setting alone, from the executor, is refused;
  * an **owner key on an agent** (`key register <agent> --role owner`, the
    owner's authority delegated) is the same gesture (#326 r2): setting,
    changing, renaming or un-revoking it is refused outside the owner's
    role; an `agent` key stays free;
  * no agent is created under a key holder's name, nor a key holder under
    an agent's (triggers on both tables, #326 r3): a holder's rotation is
    never blocked by an agent that took its name meanwhile, and a holder
    never shadows an agent's key. A first owner key holder also counts the
    agents' owner keys. Should a name still be both with two different keys
    (a race), `mesh_keys` gives it no key at all;
  * dates are the server's clock at the event: `key_updated_at`,
    `created_at` and `key_revoked_at` are set by the triggers to
    `clock_timestamp()` (not the transaction's start), whatever the writer
    sends. Every key event (registered, replaced, revoked, removed;
    holder or agent; the database role; whether it was the owner's gesture)
    goes to `key_events`, append-only and written by the triggers only (no
    update, no delete, no direct insert). `ameesh alerts` raises
    `owner_key_registered` from it, pushed to `notify.default_human`
    (urgent): for 7 days when it was the owner's gesture; otherwise until
    the owner confirms it with their database role
    (`ameesh key confirm <name>`, an event `confirmed`), with no time limit.
    A backdated row, a revocation right after use or a transaction held open
    for days hides nothing. Registering your own first owner key with the
    executor's DSN therefore alerts until you confirm it.

  A rotation signed by the current owner key was considered and left out:
  a rotation is rare, and the owner holds the owner database role anyway;
  a signed rotation would need a function running with more rights than the
  executor (`SECURITY DEFINER`), which the executor role's audit forbids.

### What it looks like in a hook

Without a signed message, the hook's JSON is **identical to v0's** (equality
test against `agent-mail.v0.py`). As soon as a message carries a valid proof,
the header changes and each line is marked:

```
[agent-mail] 1 nouveau(x) message(s) — dont 1 avec l'autorité du propriétaire
PROUVÉE (signature Ed25519) ; les autres n'ont pas cette autorité :
— de proprietaire à 18:40 : Le lot est approuvé.  [autorité du propriétaire PROUVÉE, expire 06:40]
```

And for a forgery:

```
— de proprietaire à 18:41 : Falsifié.  [⚠ signature NON valide : la signature ne couvre pas ce contenu]
```

---

## 2. `mesh list` (spec §5 point 5)

`agent-mesh list` reads the `agent_mesh_overview` view: name, harness, host, status
(and its text), lease with remaining time, unread, budget consumed/planned, registered
key, last seen. `agent-mesh show <agent>` details one agent
(session, lease, model, budget, key, directory, turns). `--json` exists on
both for scripts and other agents.

## 3. `work_items` (spec §5 point 4)

State machine taken from an existing ticket orchestrator:

```
intake → build → qa → merged → promoted     (+ blocked, waiting_human)
```

* the `qa → build` loop is **limited to 2 returns** (`loops`): beyond that, the CLI
  refuses and asks for `blocked` or `waiting_human` — an unbounded loop costs
  tokens without end;
* `promoted` is terminal, `closed_at` is set;
* each transition writes a row into `work_item_events` (state, note, actor,
  timestamp): this is the recovery ledger;
* a `pg_notify('work_item', …)` trigger publishes state changes, like
  the mailbox.

CLI: `agent-mesh work add|list|show|move|note` (+ `--json`).

## 4. v0 → v1 cutover: import and export

`agent-mesh import-v0` reads the v0 file mailbox (`~/.local/state/agent-mail`),
takes over the v0 registry (`agents/<nom>.json`: harness, directory, session) and
deposits unread messages into Postgres **keeping their timestamp**. The imported
files are *moved* into `inbox/<nom>/imported/`, never deleted.

`agent-mesh export-v0` does the reverse: it rewrites an agent's unread messages into
the v0 file mailbox (strict v0 format) and marks them delivered in the database — a
move, hence no duplicates on the way back. `--keep` copies without marking.

This pair is what makes every step of `BASCULE.md` reversible.

---

## 5. Tests

110 `unittest` tests, run twice (`scripts/test.sh`: `psql` driver,
then `psycopg` in a venv with `cryptography`). Coverage of points 3-5:

* RFC 8032 vectors on all available backends, forgeries,
  non-canonical signature, cross-backend check and **OpenSSL cross-check**;
* key permissions, refusal to overwrite, private key read as public rejected;
* messages: valid signed, unsigned (including a text claiming to be from the
  owner), forged body, modified recipient, tampered signature,
  expired, unknown key, unknown fingerprint, revocation then re-registration;
* hooks: proof displayed, forgery visible, JSON identical to v0 without
  signature; agent key → "authentique, sans autorité propriétaire" (authentic, without owner authority), never
  "PROUVÉE" (PROVEN);
* roles: an agent key authenticates without authority, cannot approve, and
  an approval force-inserted with an agent key is rejected;
* approvals: full cycle, single consumption, real expiry, hash
  not covered, `rejected` decision, unregistered key rejected;
* codex3 probes on freeze 345aca6, now covered: `expires_at` or
  `created_at` modified in the database → rejected; consumed receipt deleted then
  reinserted → "nonce déjà consommé" (nonce already consumed); direct copy → unique index;
* **upgrade**: schema 0001–0004 with an already consumed approval,
  actual application of 0005–0007, receipt deleted then reinserted → still
  rejected by the backfilled ledger;
* `mesh list`/`show` (text and JSON), key lifecycle through the CLI;
* `work_items`: cycle, rejected transitions, bounded loop, log,
  concurrent move (a single winner), NOTIFY;
* v0 import/export (timestamps, moved files, registry taken over).

---

## 6. Accepted limitations

* **Registering a key is an act of trust**: the CLI cannot
  prove who ran `key register` nor who chose `--role owner`. The
  protection is organizational (machine, permissions) — R4 proves the
  *signature* and its role, not the operator's identity. It is this limitation
  (agents and key under the same Unix user) that led v1 to place
  human authority in ameesh-approve and the registry of authenticators
  in the canon, modifiable only through a reviewed PR (spec §8.2). With several
  canons (L44, decision 0031), this registry is kept **per canon**: a passkey
  is only valid for the objects (actions) of the canon that declares it, and is only
  revoked by that canon (see EXPLOITATION.md, "Plusieurs canons" (multiple canons)).
* **One role per key, no scopes yet**: `owner`/`agent` is binary;
  finer capabilities ("approve a merge but not production")
  will come with the governance gate.
* **The clock is assumed to be roughly correct**: expiries are compared with
  local/database time. Clock drift shifts expirations.
* **No gate yet**: `verify --consume` exists, but no merge or
  deployment calls it automatically; wiring up the external governance
  gate remains to be done.
* **No hardware key** and no automatic rotation: `key revoke`
  then `key register` are manual.
* Signed messages are verified on every hook (~28 ms in pure
  Python, less with `cryptography`); no cache for now.
