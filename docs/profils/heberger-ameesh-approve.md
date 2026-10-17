# Hosting ameesh-approve for a team

This operations guide describes the installation of **one** instance of
ameesh-approve for **one** team. It applies decision
[0026](../design/decisions/0026-hebergement-d-ameesh-approve-par-equipe.md)
and the [hosting contract](../design/ameesh-approve-hebergement-equipes.md)
(lot L27). The host names below are examples (`.example`): each team has
its own.

Basic rule: **one team = one host `H` = one WebAuthn RP ID equal to `H`**.
There is never a page shared by several teams, never a shared parent RP ID,
never host sharing by path.

---

## 1. What you need before installing

| Item | Requirement |
|---|---|
| host `H` | reserved durably for the team, chosen **before** any enrolment (changing it forces re-enrolling the passkeys) |
| database | one database or one **schema per team** (`AMEESH_DSN`, `AMEESH_SCHEMA`); the service has no team filter |
| Unix user | dedicated to the service, distinct from the agents' user |
| Postgres role | `deploy/sql/role-approve.sql` (§6): read-only access to what the service reads |
| HTTPS on `H` | via a reverse proxy, or via the service's local TLS (§4) |

The service is also distributed as a container image
(`ghcr.io/manaty/ameesh-approve`), for hosts that do not install from the
sources. Pin it **by digest**, never by a moving tag; publication, tags,
rollback and the exact runtime contract are in
[`docs/IMAGE-APPROVE.md`](../IMAGE-APPROVE.md). The rest of this guide applies
to both installations: configuration, RP ID, database role and enrolment do
not change.

## 2. Service configuration (strict profile)

`~/.config/ameesh-approve/config.json` of the service user:

```json
{
  "rp_id": "equipe-a.pages.example",
  "origins": ["https://equipe-a.pages.example"],
  "public_url": "https://equipe-a.pages.example",
  "reserved_zones": ["pages.example"],
  "reserved_hosts": ["approve.pages.example"],
  "bind": "127.0.0.1",
  "port": 8765
}
```

The **strict profile** is the default (`"profile": "strict"`). At startup,
the service refuses, with a message saying why:

* an RP ID different from the origin's host (for example a parent);
* more than one origin, an origin with a port, over `http`, or on
  `localhost` or an IP address;
* a `public_url` on another host or with a path prefix;
* an RP ID equal to a **reserved zone** (`reserved_zones`: the zone under
  which team hosts are allocated) or to a **reserved host**
  (`reserved_hosts`), or a parent of one of them. The names reserved by
  decision 0026 always are, even if absent from the file.

The **compatible** profile (`"profile": "compatible"` or
`--profile compatible`) exists only for local trials
(`http://localhost`, several origins, subdomains of the RP ID). It must be
written explicitly; reserved names are still refused in it.

The same keys exist as `AMEESH_APPROVE_*` variables (`RP_ID`, `ORIGINS`,
`PUBLIC_URL`, `PROFILE`, `RESERVED_ZONES`, `RESERVED_HOSTS`, `TLS_CERT`,
`TLS_KEY`…) and, for the main ones, as options of `ameesh-approve serve`.

## 3. Two addresses: `public_url` and `approve_url`

* `public_url` (service): the address of the **human pages** (`/a/…`,
  `/enroll/…`), always `https://H`.
* `approve_url` (ameesh client, `AMEESH_APPROVE_URL`): the **machine**
  address of the API (`POST /requests`, `GET /receipts/<id>`, `GET /health`),
  with the service token read from `AMEESH_APPROVE_TOKEN_FILE` (file `0600`).

Neither is derived from the other. By default, the API is only served
under the local host (`127.0.0.1:PORT`): a call under the public host gets
`421`, and the client says so. For runners on other machines to call the
API via `https://H`, it must be **opened explicitly** in the service's
JSON:

```json
{ "api_via_public": true }
```

It is a JSON boolean (`true`/`false`, never a string), with no equivalent
as an environment variable or option: opening the API is a written choice.
The API remains authenticated by the token and refuses any browser
(`Origin`). Private access (private network, tunnel) is an alternative.

Changing `approve_url` does **not** change trust: only the RP ID and origins
determine it (§5).

The **login API** (`POST /login-requests`, `GET /login-receipts/<id>`, L232d)
is a separate switch: **closed by default** (`403 login_api_closed`) and, once
opened in the JSON, served only to the holder of the DEDICATED token
`login_token_file` (0600, `ameesh-approve gen-token --login`) — never to the
team's service token, which every agent of the team can read. The login token
belongs to the login supervisor/executor of the account, out of the agents'
reach:

```json
{ "login_api": "open", "login_token_file": "/etc/ameesh-approve/login-token" }
```

It too is a written choice (no environment variable or option for
`login_api`), and `login_token_file` must be a different file from
`token_file`; the service refuses the configuration otherwise. The ameesh
side reads `AMEESH_LOGIN_TOKEN_FILE`.

## 4. Exposing `H` over HTTPS

### 4.1 Behind a reverse proxy (default)

The service speaks HTTP on the loopback; the proxy terminates TLS for `H`,
forwards `Host` as is, and neither caches nor logs tokens, links or
bodies. Without options, this is the historical behaviour.

### 4.2 TLS passthrough: the certificate lives on the device

When the gateway routes by SNI **without decrypting** to an outbound tunnel
from the device, it is the service that terminates TLS on the loopback:

```
ameesh-approve serve --tls-cert ~/.config/ameesh-approve/tls/H.crt \
                     --tls-key  ~/.config/ameesh-approve/tls/H.key
```

(or `"tls_cert"`/`"tls_key"` in the JSON). The service **talks to no
certificate authority**: obtaining and renewing the certificate of `H` (for
example ACME DNS-01 driven by the hosting platform) happen elsewhere; that
process **drops** the files:

* in a private directory of the service user (suggested:
  `~/.config/ameesh-approve/tls/`, `0700`);
* two **regular** PEM files (no symbolic link), owned by the service user,
  in `0600` (or `0400`) — the service refuses any file readable by the group
  or others;
* by **atomic rename** (`write H.key.tmp`, `rename`; then the
  certificate): on each new connection, the service compares the identity of
  the files with that of the loaded certificate and re-reads it if they have
  changed, without restart. `kill -HUP` forces a re-read. A refused drop
  (permissions, key and certificate that do not match) leaves the old
  certificate in service and the log says so.
* each file is opened **only once**, without following links, checked on its
  descriptor then read; those are the bytes that get loaded (ephemeral `0600`
  copy in a `0700` subdirectory of the private state, erased immediately):
  replacing a file during the re-read cannot make the service serve another
  pair.

The certificate is **that of `H` only**: never a wildcard certificate of
the zone on a device, never an RP ID equal to the zone.

With local TLS, the runner installed on the same device keeps going through
the loopback, without opening the public API:

```
AMEESH_APPROVE_URL=https://127.0.0.1:8765
AMEESH_APPROVE_TLS_NAME=H        # the certificate is verified for this name
```

## 5. Verifier policy and concordance check

ameesh verifies and consumes receipts with **its own** policy, read from
`AMEESH_APPROVE_RP_ID` and `AMEESH_APPROVE_ORIGINS` (independently of the
service's JSON). It must be identical on the service and on **all** the
workstations that verify:

```
AMEESH_APPROVE_RP_ID=equipe-a.pages.example
AMEESH_APPROVE_ORIGINS=https://equipe-a.pages.example
```

On each verifying workstation:

```
ameesh approve-check            # 0 match, 1 discrepancy (listed), 2 unreachable
ameesh approve-check --json
```

The command reads the service's `GET /health` (without token: the document
publishes RP ID, origins, public URL, profile, level, API opening, TLS — no
secret) at `approve_url` (or `--url`), and compares it with the local
policy. Run it after installation, after any configuration change and after
each switchover. `/health` follows the API's `Host` rules.

## 6. Database rights

The service reads, in the team's schema, the actions (to recompute the
fingerprint and render the summary), the authenticator registry and the
nonces already consumed; it never writes to the database (its requests,
links, receipts and proposals live in its private state).

```
PGOPTIONS='-c search_path=<team schema>' \
psql "<admin DSN>" -v ON_ERROR_STOP=1 -v role=approve_equipe_a \
     -f deploy/sql/role-approve.sql

CREATE ROLE approve_equipe_a_service LOGIN IN ROLE approve_equipe_a;
ALTER ROLE approve_equipe_a_service SET default_transaction_read_only = on;
ALTER ROLE approve_equipe_a_service SET search_path = <team schema>;
```

The script is transactional, idempotent and audited like the supervisor
roles: it refuses (and applies nothing) if the role would obtain from
elsewhere more than its contract (rights given to `PUBLIC`, parent role,
default ACLs, membership), including in **another schema** (another team's),
on a database (CREATE, or a right granted to the role itself: connection is
given to the login role) or on a large object. One role per team, each
on its own schema. The service's DSN
(`AMEESH_DSN`) uses the login role.

## 7. Enrolment

`H` fixed, configuration checked (`approve-check`), then, as the service
user:

```
ameesh-approve enroll-link --approver human:<id>
```

The human opens the link **on their device** and creates their passkey; the
service writes a canon **proposal** (never an active key in the database),
reviewed by PR then synced (`ameesh canon sync`).

## 8. Changing location, changing host

**Same `H`** (same RP ID, same origin, same registry): the passkeys are
kept.

1. Prepare the target: same database/schema and same canon, identical
   RP/origin configuration, certificate of `H` present and valid.
2. Suspend new requests; let open links expire or complete; stop the old
   instance (a single active instance).
3. Copy the private state (`state_dir`, unintegrated proposals) keeping its
   permissions (`0700`/`0600`); never restore an older backup of the state
   or of the nonces.
4. Switch the routing of `H`; change only `approve_url` if the machine entry
   point changes; distribute a new token and revoke the old one if
   needed.
5. `ameesh approve-check` on each verifier; one approval with an already
   enrolled passkey; check that an old consumed link is still refused.

**New host** (`H` changes): this is not a migration. New configuration (RP
ID and origin of the new host), **new enrolment** of each human, new
reviewed proposals, verifier policy updated, then `approve-check`. Never
widen the RP ID to a parent to recycle the old passkeys.

## 9. Checklist

- [ ] `ameesh-approve serve` starts in strict profile, without configuration
      warning;
- [ ] `ameesh approve-check` returns 0 on each verifying workstation;
- [ ] the API is open under `H` only if `api_via_public` is written, and the
      token is a `0600` file of the user concerned;
- [ ] the login API is closed, or opened with the dedicated `login_token_file`
      (0600, distinct from the service token) held outside the agents' reach;
- [ ] in passthrough: certificate of `H` only, `0600` files of the service
      user, renewal by atomic rename;
- [ ] the team's Postgres role applied without discrepancy; the service's
      DSN on its login role;
- [ ] no other application serves content on `H` or under `H`.
