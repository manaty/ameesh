---
type: Specification
title: "ameesh-approve: three hosting options, one WebAuthn identity per team"
status: stable
author: codex3
date: 2026-10-05
source: "product decision by the owner (2026-10-05 06:22), see decision 0026; written by codex3, integrated by mesh-design"
---

# Scope and reported decision

A team chooses where its human approval service runs: on its PC
with a Nexlink relay and a small subscription, on a dedicated Nexlink page, or on
its own server. These choices concern **ameesh-approve**; the placement of
runners and their database remains a separate configuration
([0011](decisions/0011-hebergement-configurable.md)). The amount and
terms of the subscription are not specified here.

This document specifies the contract to deliver. The existing primitives are
verified on `private/main` 148bd01; provisioning of the three offers,
domain allocation and automatic switchover are not implemented
by this specification. Decision [0017](decisions/0017-approbation-via-page-nexlink.md)
describes the project's initial choice of `ameesh.nexlink.ph`, superseded by the
decision reported at 06:30: our installation uses
`manaty.ameesh.nexlink.ph`. `ameesh.nexlink.ph` is reserved: no RP ID and no content,
like any approval zone apex. [0025](decisions/0025-une-session-par-lot.md)
concerns agent sessions and does not settle this hosting question.

# Per-team contract

Decision Q1 reported at 06:38: the team is an autonomous ameesh entity,
with its own stable identity and an **optional link** to a Nexlink page.
An independent installation, notably mode 3, requires no Nexlink page,
account or subscription. The team identity is therefore not a mandatory
`page_id`; page association belongs to the Nexlink connector,
in accordance with [0003](decisions/0003-ameesh-et-nexlink.md).

To get started with Nexlink, attaching the team to a page is the simple
path: default name `<handle>.<zone>`, human approvers = admins of
the page, the page's subscription. The handle chooses the **initial** name; a
page rename does not silently change the host or the already enrolled RP ID.
The authenticated page/team association must be explicit and stable. The
handling of a page already associated with another team remains to be specified;
no automatic merging of trust registries.

Conversely, **every new Nexlink page comes with ameesh installed**.
Provisioning at creation prepares the team, the canon, isolated
storage, configuration and the runtime of its business processes; in mode 2,
they are provisioned on the page server with approve separated from the agents.
Installation means neither enrolling a human on their behalf, nor an implicit
purchase of paid hosting, nor publishing approve before its acceptance testing.
Enrollment remains the human ceremony; a page can be installed with
approval pending, without executing actions that require a receipt. The fate
of existing pages requires an idempotent migration lot, not an implicit
effect of creating a new page.

Admins are the source of the connector's human rights: their identity
and their authenticators are linked to the canon, without making an agent a
human approver. Admission, removal and change of admin must be
propagated with verifiable provenance; a removal forbids new
approvals and the execution of receipts that no longer have the required authority. The
delays and checks at the request/signature/execution stages are to be specified
in this bridge, before replacing the canon's checks with a projection.

An instance serves a team's projects, its authorized humans and its
runners, possibly across several PCs. Several teams on the same
machine keep separate instances, system identities, secrets, states and data
access. No team routing depends on a `team_id` freely supplied
by a client, on a path, or on the `action_id` alone.

The service itself reads the actions and the trust registry in the team's ameesh
database (`AMEESH_DSN`, `AMEESH_SCHEMA`). This database may be remote.
It displays its summary, collects approval/refusal and produces a receipt bound to
the action, with expiry and nonce. It writes its requests, links, receipts and
enrollment proposals to its private state; it does not modify actions
and does not consume their nonce. The runner retrieves and verifies the receipt before
using it under the actions gate.

The current code has no team filter in `DbActionSource`: **a database
or schema isolated per team and DB access limited to that scope are required**.
A mere project filter on the interface side does not constitute such isolation.
The trust canon and its human identities must match the same
scope; a shared federation requires a separate authorization contract.
The L25 supervisor role is not the approve role: it excludes the content
needed to display an action. Approve's own DB rights remain
to be defined and reviewed before deployment.

Each team receives a canonical host `H`, durably reserved for the service:

| Parameter | Required value |
|---|---|
| `rp_id` / `AMEESH_APPROVE_RP_ID` | `H`, without scheme, port or path |
| `origins` / `AMEESH_APPROVE_ORIGINS` | exactly `https://H` in production |
| `public_url` | `https://H`, for human links |
| `approve_url` / `AMEESH_APPROVE_URL` | URL of this instance's API: loopback or HTTPS |
| `approve_token_file` / `AMEESH_APPROVE_TOKEN_FILE` on the client side | private file holding this team's request token |
| `token_file`, `state_dir`, `proposals_dir` on the service side | private files/directories, dedicated user |

`approve_url` is the machine address of `POST /requests` and `GET /receipts/<id>`;
`public_url` is the address of the pages opened by humans. The token allows
requesting and retrieving, never signing; the private passkey stays in
the human's authenticator. Several PCs of a team can use the
service; the current code uses a single service token, not tokens
individually revocable per PC.

The policy must be identical in **the service and all verifiers**:
`actions.policy_from_env` reads RP ID/origins from the environment, independently
of the service's JSON. The provisioner configures both, then checks that they
match; changing only `approve_url` does not change trust.

Example of an existing configuration for a hosted instance, with an illustrative `H`
to be confirmed on the Nexlink side:

```json
{
  "rp_id": "equipe-a.ameesh.nexlink.ph",
  "origins": ["https://equipe-a.ameesh.nexlink.ph"],
  "public_url": "https://equipe-a.ameesh.nexlink.ph",
  "bind": "127.0.0.1",
  "port": 8765,
  "api_via_public": true
}
```

The service stays on loopback behind an HTTPS proxy, in all three
modes. `api_via_public` defaults to `false`; setting it to `true` in the
JSON allows the API under the public host, with its token authentication.
This is not an existing environment or CLI option. The API must be
reachable by authorized runners; access can also remain private.

# Domains and WebAuthn isolation

An RP ID specific to each team: neither `ameesh.nexlink.ph/<team>`, nor a common parent RP ID
`ameesh.nexlink.ph` or `nexlink.ph`. The teams' RP IDs also have
**no ancestor/descendant relationship**. The host `H` and its subdomains serve
no content or code controlled by another team; the dedicated page is the
approval service, with no Nexlink content HTML on the same origin.

WebAuthn binds credentials to the RP ID, not to the URL path; their RP ID stays
fixed for the lifetime of the credential. A valid parent suffix can be requested
from a subdomain: choosing exactly `H` avoids sharing that scope
with neighboring teams. See the [W3C definition of the RP ID](https://www.w3.org/TR/webauthn-3/#rp-id)
and the [authenticator model](https://www.w3.org/TR/webauthn-3/#sctn-authenticator-data).

Separating RP IDs does not replace origin, `Host` and
cross-site request checks. The service already verifies the exact origin and refuses
`Sec-Fetch-Site: same-site` for browser POSTs; no global
subdomain or CORS permission must be added. Nexlink siblings
may remain in the same browser site: the trade-off between subdomains under
`nexlink.ph` and a separate approval domain remains on the Nexlink side.
No cookies shared with `Domain=nexlink.ph`; no parent domain
exception nor "related origins" configuration common to teams.
The current service uses no cookies. If a front end introduces them, they
must be prefixed `__Host-` (Secure, Path=/, no Domain), with origin
checks kept; this does not replace Nexlink's same-site/PSL analysis.

# Three modes, same data and protocol

| Mode | Service and exposure | Runner calls |
|---|---|---|
| PC + Nexlink relay | dedicated user on the PC, loopback; outbound tunnel to the relay, HTTPS on `H`; Nexlink subscription | host PC: `http://127.0.0.1:8765`, public API closed; other PCs: private access or HTTPS API explicitly opened and authenticated |
| Dedicated Nexlink page | isolated instance on the page server, loopback behind its proxy, HTTPS on the same `H` | HTTPS API URL and token; service access to the team's database/registry |
| Team's server | team operates the service, state and HTTPS; team's domain chosen at adoption | client config with HTTPS URL and token file, no mandatory dependency on the Nexlink relay |

In own-server mode, "configure the URL" describes the ameesh client; the
server must already have the service, its database/registry, secrets and
TLS. Hosting the service elsewhere does not automatically copy the ameesh database.

# Enrollment and mode change

Fix `H` before enrollment. An authorized administrator runs
`ameesh-approve enroll-link --approver human:<id>` as the service
user; the human opens the link on their device and creates their passkey.
Enrollment writes a proposal, never a key directly active in the database:
reviewed canon PR, then registry synchronization. The first real
approval checks team, origin, RP ID, action fingerprint and single
consumption of the nonce. The installation harness prepares this configuration; acts
of authority and spending remain human ([0011](decisions/0011-hebergement-configurable.md)).

Changing **the location** while keeping `H`, HTTPS, RP ID, origin and registry
allows the passkeys to be kept. This is not a private key transfer:
the team's credential IDs and public keys stay in the canon/registry,
the private keys stay on the devices. Keeping only the host name
without restoring the registry is not enough.

Switchover procedure to be provided by the provisioner:

1. Prepare the isolated target, with access to the same database and the same canon, identical
   RP/origin configuration; verify the certificate of `H` before switchover.
2. Suspend new requests and drain in-flight actions. Let
   approval and enrollment links expire/complete, then stop the old
   writer. A team has only one active instance in this profile.
3. Transfer the durable private state and unintegrated proposals with their
   permissions; keep the database of consumed nonces, the actions and the
   current registry. No reverting to an earlier backup of consumed nonces or
   links, including during a rollback.
4. Switch the routing/DNS of **`H`**, keep control of the name; update
   only `approve_url` if the machine entry point changes. Distribute a
   new token and revoke the old one if needed; do not widen RP/origins.
5. Verify an approval with an already enrolled passkey and the refusal of replay,
   resume requests; rollback with current state and a single writer.

Owner decision reported at 06:30: **the Nexlink name stays the same
between modes 1 and 2; it is not kept in mode 3**. Nexlink does not route
to the own server and does not maintain the name for the team after
the subscription. A team aiming for mode 3 uses its own domain from adoption.
Changing from a Nexlink name to that domain changes the RP ID: re-enrollment
accepted. The switchover acceptance test without re-enrollment above only applies to
a move that actually keeps the name, notably mode 1 ↔ mode 2.
Maintaining a tombstone preventing assignment to another team is
not a right to keep using the name or having it routed.

If `H` changes (e.g. `equipe-a.nexlink.ph` to `approve.equipe-a.example`), **this is not
a migration without re-enrollment** with the current code: new ceremony on
the new domain, new reviewed proposals and updated policy.
Do not replace the parent RP ID to recycle old passkeys. The project's
case, if it has already been enrolled under `ameesh.nexlink.ph`, follows this procedure
to move to `manaty.ameesh.nexlink.ph`; do not reassign the apex or serve
content on it. No real re-enrollment or DNS change is performed by this document.

# To be provided on the Nexlink side

- Independent team identity with optional page association,
  idempotent provisioning on page creation (on its server in
  mode 2), authenticated projection of admins and their removals to
  the ameesh authority; no dependency on this path for autonomous mode.
- Namespace choice after same-site analysis; exclusive assignment of `H`
  to one team, host kept between modes 1/2, own domain from adoption
  of mode 3 and non-reassignment rules; reserved apex with no RP ID or content.
- PC mode: mapping `H` → dedicated tunnel/port/key; outbound channel, key limited
  to this forwarding, end of subscription and offline PC without workaround.
- Page mode: truly isolated instance, own secret/state/backups,
  secure access to the team's ameesh database, separation from agent runners.
- HTTPS/certificate on the same `H` between modes 1/2 and own domain in mode 3, exact host routing,
  preservation of `Host`, control of proxy headers, no caching or
  logging of tokens/links/bodies; authenticated API exposure or private access.
- Provisioning contract returning host/origin/API URL and a private means of
  delivering the token; subscription and spending under human approval. No price
  or new provisioning endpoint is invented in this spec.

If Nexlink adopts the dynamic routes from Nexlink's multi-team study,
the snapshot must carry a version, a proof of completeness and a bounded
validity. An HTTP200 with a syntactically valid but partial list must be
refused; team/host associations are not silently reassigned.
Explicit removals and key rotation must be representable without
bypassing the variation limits; a configuration kept after an outage
does not indefinitely extend an expired authorization. The protocol and these probes
are to be fixed before R1–R3. A 90-day quarantine is not a guarantee of
non-reassignment of an already used RP ID: durable reservation of the name and
the end of subscription grant no continued routing to the own server.

# Consistency with native page agents

The [Nexlink architecture §3.2i](https://github.com/manaty/nexlink/blob/599863f/docs/architecture/decentralisation.md#32i-page-agents-owner-2026-10-02)
provides moderation, writing, filtering, highlighting and
administration agents **on the page**, with an OpenAI-compatible LLM endpoint,
bounded permissions, explained and contestable actions. The ameesh bridge
takes over these identities, rules and logs: it does not create a second
team of agents running the same jobs nor a second billed budget.
Content and rules stay within the page's scope, with no cross-team
reads nor access to E2E content.

Nexlink keeps identity, page API, rights, business rules and audit;
ameesh provides execution, sessions, leases, budget and authority verification
(0003). The act/propose/off choice of native agents and ameesh action classes
must be wired explicitly, without widening autonomy or
arbitrarily requiring a receipt for every already authorized action. A single
effective claimant per job and an identifier correlating job/action/turn
must prevent double execution. This bridge is to be built and reviewed;
this document replaces neither the current native worker nor its claim mechanism.

# Acceptance testing and gaps to implement

Acceptance testing required before a public offer: two teams A/B, several PCs for A;
a passkey, origin, token or action of B never counts for A. Refuse parent
RP ID, host shared by path, untrusted content on `H` or under `H`, and
browser POSTs coming from a sibling. Verify the three modes, remote DB
unavailable = refusal, isolated private state, migration with constant `H` without enrollment,
old consumed link not reusable and host change requiring enrollment.
Add: replayed page creation without duplicate team/job, autonomous
installation without a page, admin addition/removal with effective revocation, attachment
to the right page and native job executed exactly once with correlated budget/audit.

To be done: team provisioning and allocation, profile check of
RP ID equal to `H` and single origin (current validation also accepts a
subdomain of the RP ID), matching of the verifiers' policy, dedicated approve DB
rights, switchover/backup/shutdown of the Nexlink name. This spec adds no
unknown key to the current JSON; the mode choice and the team identity will be
provisioner parameters, whose format remains to be specified.

Done on the ameesh side by lot L27 (operations guide:
[hosting ameesh-approve](../profils/heberger-ameesh-approve.md)): strict
profile by default (RP ID equal to the host of the single origin `https://H` and of
`public_url`, reserved zones and hosts refused along with their parents) and an explicit
"compatible" profile for trials; `GET /health` and
`ameesh approve-check` for policy matching; `approve_url`
distinct from `public_url`, public API only via `api_via_public` written
in the JSON; optional local TLS for a passthrough gateway
(certificate of `H` deployed on the device, hot-reloaded);
`deploy/sql/role-approve.sql`; A/B acceptance test in
`tests/test_approve_equipes.py`. Remaining on the Nexlink or provisioner side:
host allocation, ACME, tunnels, tooled switchover. This lot adds to the JSON
the keys `profile`, `reserved_zones`, `reserved_hosts`, `tls_cert`, `tls_key`.
