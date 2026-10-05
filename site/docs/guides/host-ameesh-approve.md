# Host ameesh-approve for a team

This guide installs **one** ameesh-approve instance for **one** team. The
concepts are in
[Receipts and ameesh-approve](../concepts/receipts-and-approve.md#hosting-ameesh-approve-one-per-team).
Host names below are examples (`.example`); every team has its own.

The rule: **one team = one host `H` = one WebAuthn RP ID equal to `H`**. Never a
page shared by several teams, never a common parent RP ID, never a host shared
by URL path.

## 1. Before installing

| Item | Requirement |
|---|---|
| host `H` | reserved for the team for the long term, chosen **before** any enrolment (changing it means enrolling every passkey again) |
| database | one database or **one schema per team** (`AMEESH_DSN`, `AMEESH_SCHEMA`); the service has no team filter |
| Unix user | dedicated to the service, distinct from the agents' user |
| Postgres role | `deploy/sql/role-approve.sql` (section 6): read-only access to what the service reads |
| HTTPS on `H` | through a reverse proxy, or through the service's local TLS (section 4) |

## 2. Service configuration (strict profile)

`~/.config/ameesh-approve/config.json` of the service's user:

```json
{
  "rp_id": "team-a.pages.example",
  "origins": ["https://team-a.pages.example"],
  "public_url": "https://team-a.pages.example",
  "reserved_zones": ["pages.example"],
  "reserved_hosts": ["approve.pages.example"],
  "bind": "127.0.0.1",
  "port": 8765
}
```

The **strict profile** is the default (`"profile": "strict"`). At start-up the
service refuses, saying why:

- an RP ID different from the host of the origin (a parent, for example);
- more than one origin, an origin with a port, in `http`, on `localhost` or on
  an IP address;
- a `public_url` on another host or with a path prefix;
- an RP ID equal to, or parent of, a **reserved zone** (`reserved_zones`: the
  zone under which team hosts are allocated) or a **reserved host**
  (`reserved_hosts`).

The **compatible** profile (`"profile": "compatible"` or
`--profile compatible`) exists only for local trials (`http://localhost`,
several origins, sub-domains of the RP ID). It must be written explicitly;
reserved names stay refused.

The same keys exist as `AMEESH_APPROVE_*` variables (`RP_ID`, `ORIGINS`,
`PUBLIC_URL`, `PROFILE`, `RESERVED_ZONES`, `RESERVED_HOSTS`, `TLS_CERT`,
`TLS_KEY`…) and, for the main ones, as options of `ameesh-approve serve`.

## 3. Two addresses: `public_url` and `approve_url`

- `public_url` (service): the address of the **human pages** (`/a/…`,
  `/enroll/…`), always `https://H`.
- `approve_url` (ameesh client, `AMEESH_APPROVE_URL`): the **machine** address
  of the API (`POST /requests`, `GET /receipts/<id>`, `GET /health`), used
  with the service token read from `AMEESH_APPROVE_TOKEN_FILE` (mode `0600`).

Neither is derived from the other. By default the API is served only under the
local host (`127.0.0.1:PORT`); a call under the public host gets `421`. For
runners on other machines to call the API through `https://H`, open it
**explicitly** in the service's JSON:

```json
{ "api_via_public": true }
```

It is a JSON boolean, with no environment variable or option equivalent:
opening the API is a written choice. The API stays authenticated by the token
and refuses browsers. Private access (private network, tunnel) is an
alternative. Changing `approve_url` does **not** change trust: only the RP ID
and origins do (section 5).

## 4. Serving `H` over HTTPS

**Behind a reverse proxy (default).** The service speaks HTTP on loopback; the
proxy terminates TLS for `H`, forwards `Host` unchanged, and neither caches nor
logs tokens, links or bodies.

**TLS passthrough: the certificate lives on the device.** When a gateway routes
by SNI **without decrypting** to an outbound tunnel from the device, the
service terminates TLS on loopback:

```bash
ameesh-approve serve --tls-cert ~/.config/ameesh-approve/tls/H.crt \
                     --tls-key  ~/.config/ameesh-approve/tls/H.key
```

(or `"tls_cert"` / `"tls_key"` in the JSON). The service talks to **no**
certificate authority: obtaining and renewing the certificate of `H` happens
elsewhere, and that process **drops** the files:

- in a private directory of the service's user (`0700`);
- two **regular** PEM files (no symbolic link), owned by the service's user, in
  `0600` (or `0400`); the service refuses any file readable by group or others;
- by **atomic rename**: on each new connection the service compares the files'
  identity with the loaded certificate and reloads them if they changed, without
  restart; `kill -HUP` forces a reload. A refused drop (permissions, key and
  certificate that do not match) keeps the previous certificate in service and
  logs why.

The certificate is **for `H` only**: never a wildcard certificate of the zone
on a device. With local TLS, a runner on the same device keeps using loopback:

```bash
AMEESH_APPROVE_URL=https://127.0.0.1:8765
AMEESH_APPROVE_TLS_NAME=H        # the certificate is verified for this name
```

## 5. Verifier policy and the consistency check

ameesh verifies and consumes receipts with **its own** policy, read from
`AMEESH_APPROVE_RP_ID` and `AMEESH_APPROVE_ORIGINS`, independently of the
service's JSON. It must be identical on the service and on **every** machine
that verifies:

```bash
AMEESH_APPROVE_RP_ID=team-a.pages.example
AMEESH_APPROVE_ORIGINS=https://team-a.pages.example
```

On each verifier:

```bash
ameesh approve-check            # 0 consistent, 1 differences (listed), 2 unreachable or not configured
ameesh approve-check --json
```

The command reads `GET /health` from the service (no token; the document
publishes RP ID, origins, public URL, profile, level, whether the API is open,
TLS, and no secret) at `approve_url` (or `--url`), and compares it with the
local policy. Run it after installing, after any configuration change and
after every move.

## 6. Database rights

The service reads, in the team's schema, the actions (to recompute the digest
and render the summary), the authenticator registry and the consumed nonces;
it never writes to the database (its requests, links, receipts and proposals
live in its private state).

```bash
PGOPTIONS='-c search_path=<team schema>' \
psql "<admin DSN>" -v ON_ERROR_STOP=1 -v role=approve_team_a \
     -f deploy/sql/role-approve.sql
```

```sql
CREATE ROLE approve_team_a_service LOGIN IN ROLE approve_team_a;
ALTER ROLE approve_team_a_service SET default_transaction_read_only = on;
ALTER ROLE approve_team_a_service SET search_path = <team schema>;
```

The script is transactional, idempotent and audited like the supervisor roles:
it refuses, and applies nothing, if the role would get more than its contract
from elsewhere (privileges granted to `PUBLIC`, a parent role, default ACLs,
membership), including in **another team's schema**. One role per team, each on
its own schema. The service's `AMEESH_DSN` uses the login role.

## 7. Enrolment

With `H` fixed and the configuration checked (`approve-check`), under the
service's user:

```bash
ameesh-approve enroll-link --approver human:<id>
```

The human opens the link **on their own device** and creates a passkey; the
service writes a **proposal** for the canon (never an active key in the
database), reviewed by pull request, then synchronised (`ameesh canon sync`).

## 8. Moving the service, changing the host

**Same `H`** (same RP ID, origin and registry): passkeys are kept.

1. Prepare the target: same database or schema, same canon, identical RP and
   origin configuration, a valid certificate for `H`.
2. Suspend new requests; let open links expire or complete; stop the old
   instance (one active instance only).
3. Copy the private state (state directory, proposals not yet merged) with its
   permissions; never restore an older backup of the state or of the nonces.
4. Switch the routing of `H`; change only `approve_url` if the machine entry
   point changes; issue a new token and revoke the old one if needed.
5. Run `ameesh approve-check` on each verifier, approve once with an already
   enrolled passkey, and check that an old consumed link is still refused.

**New host** (`H` changes): this is not a migration. New configuration, **new
enrolment** of every human, new reviewed proposals, updated verifier policy,
then `approve-check`. Never widen the RP ID to a parent to recycle old
passkeys.

## 9. Checklist

- [ ] `ameesh-approve serve` starts in the strict profile, without a
      configuration warning;
- [ ] `ameesh approve-check` returns 0 on every verifier;
- [ ] the API is open under `H` only if `api_via_public` is written, and the
      token is a `0600` file of the user concerned;
- [ ] with passthrough: a certificate for `H` only, `0600` files of the
      service's user, renewal by atomic rename;
- [ ] the team's Postgres role applied without differences; the service's DSN
      on its login role;
- [ ] no other application serves content on `H` or under `H`.
