# Receipts and ameesh-approve

A human's authority is never inferred from text, not even from a message that
claims to come from them. It is **proven by a receipt**: a signature, made by
the human on their own device, over the exact digest of one action attempt.

## Why not a key on the workstation

An earlier design kept the owner's Ed25519 key in a file on the workstation.
Agents run under the same Unix user and could read it, so that ceremony was
**abandoned**. The human signs out of the agents' reach, with a **passkey**
(WebAuthn) on their phone, through a separate service: **ameesh-approve**.
Ed25519 keys remain for agent provenance and tests; a key kept on an agents'
host never counts as human authority.

## The receipt format: `ameesh-receipt/1`

```
request   = { v: 1, approver: "human:<id>", action_id, digest: "sha256:<digest>",
              decision: "approve" | "deny", summary_digest, requested_by,
              nonce (128 bits, base64url), iat, exp }
receipt   = { v: "ameesh-receipt/1", request, facade, credential_id, proof }
challenge = SHA-256("ameesh-approval/1\0" || JCS(request))
```

A receipt is **bound to the digest**, **single use** (its nonce is consumed in
the database when the action is launched) and **expires**.

Facades verified in v1:

| Facade | Proof | Use |
|---|---|---|
| `webauthn` | `authenticatorData`, `clientDataJSON`, `signature`; checks type, challenge, allowed origin, `crossOrigin` false, RP ID hash, user presence and user verification flags, ES256 (or EdDSA) signature with the enrolled COSE key | passkeys (default) |
| `device-es256` | raw ES256 signature over the challenge by a device key | native mobile applications |
| `ed25519` | Ed25519 signature over the challenge | tests, tools, agent provenance; refused for human approval by default |

A policy level `eleve` (high) can require a non-synced credential.

## The trust registry lives in the canon

A human's authenticators (public key, facade, level, enrolment date) are listed
in `Member.authenticators` in the canon. **Adding or removing one is a reviewed
pull request.** `ameesh canon sync` copies them into a working copy in the
database, from the merged commit of the trusted canonical branch only (see
[The canon](canon.md#the-trusted-branch-for-passkeys)).

```bash
ameesh authenticator list [--approver human:ID] [--all] [--expect-commit SHA]
ameesh receipt verify <file> [--digest sha256:…] [--action-id ID] [--consume] …
```

## Bounded standing approvals

A receipt whose request carries `standing = {connector, operations, class,
max_amount, currency, until}` instead of an `action_id` creates a **grant**.
Its nonce is consumed once, when the grant is recorded. Each covered attempt
then **reserves its amount atomically** on the cumulative cap (and `until` not
passed, grant not revoked). A reservation is released only on a **certain**
failure, never on an unknown outcome.

## ameesh-approve, the service

A separate process, started **under another Unix user or on another host** than
the agents (Python standard library HTTP server):

- `POST /requests` (from ameesh, with the service token): records a request,
  **recomputes the summary from the action in the database** (never from the
  agent's text) and publishes a single-use link in the thread;
- `GET /a/<token>`: the page shows the action, summary, amount and short
  digest, and calls `navigator.credentials.get` (user verification required,
  credentials limited to the approver); the assertion is posted back and the
  receipt is built;
- `GET /receipts/<id>`: ameesh fetches the receipt and **verifies it itself**;
- `GET /enroll/<token>`: creates a passkey and produces a **proposal** for the
  canon (never a direct enrolment).

HTTPS is mandatory outside `localhost`; the service itself listens on the
loopback interface, behind an HTTPS reverse proxy or, with `--tls-cert` and
`--tls-key`, terminating TLS itself behind a TLS-passthrough gateway. The service token lets ameesh
**request** and **fetch**, never sign.

```bash
ameesh-approve gen-token [--token-file F]                    # 256-bit token, file 0600, never overwritten
ameesh-approve serve --bind 127.0.0.1 --port 8765 --rp-id … --origin https://… --public-url https://…
ameesh-approve enroll-link --approver human:ID               # single-use link, to open on the phone
ameesh approve-check [--url U] [--json]                      # on each verifier: same RP ID and origins as the service
```

## Hosting ameesh-approve, one per team

ameesh-approve belongs to a **team**: it serves that team's projects, its
authorised humans and its runners, possibly on several machines. Where it runs
is the team's choice, among three modes that share the same data and the same
protocol:

| Mode | Where the service runs | How runners reach its API |
|---|---|---|
| **Team machine + relay** | on one of the team's machines, under a dedicated user, on loopback; an outbound tunnel to a hosted relay serves it over HTTPS | locally on that machine; from other machines through a private network or an explicitly opened, token-authenticated HTTPS API |
| **Dedicated hosted page** | an isolated instance on a hosting provider's server, behind its own HTTPS proxy | an HTTPS API URL and the team's token |
| **Team's own server** | the team operates the service, its state and its HTTPS | the ameesh configuration simply points to the service's URL and token file |

In every mode the service listens on loopback behind an HTTPS reverse proxy,
and stays **out of the agents' reach**. A hosted relay or page is optional;
one option described in the design documents is Nexlink. A team running its
own server needs no third-party account.

**One identity per team, never a shared page.** Each team has its own
canonical host name `H`, reserved for its approval service:

- the WebAuthn **RP ID is exactly `H`**, and the allowed origin exactly
  `https://H`;
- no common parent RP ID shared by several teams, no sharing by URL path, and
  no ancestor/descendant relation between two teams' RP IDs (WebAuthn binds a
  passkey to its RP ID, not to a path);
- `H` and its sub-domains serve no content controlled by anyone else.

**Isolation of data.** The service reads the actions and the trust registry
from the team's ameesh database. Each team needs its own database or schema,
and database access limited to it: a project filter in a user interface is not
isolation. Several teams on one machine keep separate instances, system users,
secrets and state.

**Two URLs, one policy.** `public_url` (`https://H`) is what humans open;
`approve_url` (`AMEESH_APPROVE_URL`) is the machine API for requests and
receipts, on loopback or over HTTPS. The RP ID and origins must be identical
on the service and on **every** verifier; changing only `approve_url` never
changes what is trusted.

**Changing mode.** Moving the service while keeping the same `H`, HTTPS
origin, RP ID and trust registry keeps the enrolled passkeys: private keys stay
on the humans' devices, public keys stay in the canon. Changing `H` requires
enrolling the passkeys again. A team that may change mode should therefore
pick, from the start, a host name it will keep control of.

**What is built, and what is planned.**

- **Built:** the **strict profile** is the service's default (RP ID exactly the
  host of its single `https` origin; reserved zones and hosts refused; a
  `compatible` profile exists only for local trials and must be written
  explicitly); `public_url` and `approve_url` are distinct, and the API is
  served under the public host only when the service's configuration opens it
  explicitly; `GET /health` publishes the service's policy without any secret,
  and `ameesh approve-check` compares it with each verifier's; optional
  **local TLS** behind a TLS-passthrough gateway; a read-only **Postgres role**
  per team (`deploy/sql/role-approve.sql`); acceptance tests with two teams.
  See [Host ameesh-approve for a team](../guides/host-ameesh-approve.md).
- **Planned:** provisioning of the hosted modes (relay, dedicated page) and
  the gateway on the hosting side.

## An approval, step by step

1. An agent proposes an action; `ameesh action execute` is refused with
   `[receipt_required]`.
2. `ameesh action request <id> --approver human:alice` sends the request to
   ameesh-approve, which recomputes the summary and writes a single-use link
   in the thread.
3. Alice opens the link on her phone, sees the action as recomputed by the
   service, and signs with her passkey.
4. `ameesh action fetch-receipt <id>` fetches the receipt, verifies it against
   the trust registry, the digest, the origin and the RP ID, and attaches it.
5. `ameesh action execute <id>` consumes the nonce, marks the action
   `launched`, then calls the connector. Replaying the receipt fails with
   `[replay]`.
