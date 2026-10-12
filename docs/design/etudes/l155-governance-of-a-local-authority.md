---
type: Study
title: "Study — governing an organisation's mesh: the case of a local authority"
description: "What a public body (a local authority) needs to govern its mesh of humans and personas, against main and decisions 0032/0033: OIDC identity (D1), approver role, deputies and absences, isolation between personas (E1, E2), visibility by relation (D2), connectors, memory with access rights and retention, a page for non-technical staff. Generic: no client is named. Gaps, the parked branches to reuse, lots L155a–L155i estimated."
status: draft
tags: [governance, public-sector, identity, oidc, roles, deputies, isolation, connectors, memory, ui, v2]
generated: { by: "ameesh-dev3/claude-opus-5-5", at: "2026-10-11T18:45:00+02:00" }
stale_after: 2027-04-11
sources:
  - { resource: "../decisions/0033-isolated-meshes-hosting-and-identity.md", title: "Decision 0033 (one mesh per organisation, OIDC, human roles, deputies, visibility)" }
  - { resource: "../decisions/0032-persona-roles-and-sessions.md", title: "Decision 0032 (persona, role, session)" }
  - { resource: "../decisions/0012-autorite-par-ameesh-approve.md", title: "Decision 0012 (authority through ameesh-approve)" }
  - { resource: "../decisions/0015-interfaces-des-facades.md", title: "Decision 0015 (facade interfaces)" }
  - { resource: "../decisions/0029-persona-et-session.md", title: "Decision 0029 (persona, memory visibility)" }
  - { resource: "../exigences.md", title: "Requirements R1–R23" }
  - { resource: "../specification.md", title: "Specification (receipts §8, limits §11, later work §12)" }
  - { resource: "autorite-humaine.md", title: "Study: human authority" }
  - { resource: "messagerie-annuaire.md", title: "Study: messaging and directory" }
  - { resource: "https://github.com/manaty/ameesh/pull/25", title: "D1: OIDC identity of humans (parked)" }
  - { resource: "https://github.com/manaty/ameesh/pull/27", title: "D2: activity of a persona by relation (parked)" }
  - { resource: "https://github.com/manaty/ameesh/pull/26", title: "E1: one Unix user per persona (closed)" }
  - { resource: "https://github.com/manaty/ameesh/pull/28", title: "E2: MCP proxy enforcing a persona's tools (parked)" }
---

# Context

## The question

On 2026-10-07, an external review looked at ameesh with the eyes of a
**local authority**: a public body with elected officials, an executive,
departments and legal duties. It found four gaps:

1. **identity**: staff are known by the organisation's own provider, and
   ameesh does not use it;
2. **absences**: a decision must find a deputy when its holder is away;
3. **memory**: what a persona remembers must follow access rights;
4. **isolation**: one persona must not read what another one handles.

Two needs come with them. Personas must reach the tools the staff already
use (**connectors**), and staff who never open a terminal need a page of
their own (**interface**).

The owner's answer was "we'll see". Decisions
[0032](../decisions/0032-persona-roles-and-sessions.md) and
[0033](../decisions/0033-isolated-meshes-hosting-and-identity.md),
published since, set the frame. Four parked v2 branches already cover part
of the work:

* **D1**: OIDC identity of humans;
* **D2**: access to a persona's activity by relation;
* **E1**: one Unix user per persona;
* **E2**: an MCP proxy that enforces a persona's tools.

This study says what is still missing, which branch to reopen, and in
which order. It is generic: it applies to any public body, and to any
organisation with the same duties (a hospital, a university, a large
association). It names no client.

## What a public body adds

The duties are those of any organisation, but heavier:

* **Authority is formal.** A decision binds the body only when it is
  signed by someone who holds the power. That power is either the
  person's own, or delegated by a formal act that sets its scope and its
  dates. Deputies are part of the ordinary running of the body.
* **Segregation of duties.** The person who prepares a commitment does not
  approve it. Above a threshold, two people sign.
* **Personal data.** The body keeps a register of its processing
  activities and has a data protection officer. Memories, transcripts and
  threads are processing activities. Each has a purpose, a retention
  period and a list of readers.
* **Records.** Decisions and what they rest on are public records: kept,
  attributable, sometimes published.
* **Hosting.** The body may require its own premises or a qualified host,
  reversibility, and open formats.
* **Accessibility.** Any interface offered to staff meets WCAG AA, or the
  national standard derived from it.
* **Staff.** Most of the people who decide have no technical training.

## The frame set by 0032 and 0033

| Need | Decision |
|---|---|
| One authority, one mesh | 0033 §1: a mesh has a single organisation, with no bridge between meshes; 0032 §7: one mesh per organisation |
| Hosting | 0033 §2: the organisation hosts its own mesh, either on its infrastructure or on a VM with systemd |
| Model accounts | 0033 §3: server personas use the organisation's API keys |
| Identity | 0033 §4: a generic OIDC provider, chosen by the organisation |
| Human roles | 0033 §6: declared in the organisation's OKF canon. §8 gives a common foundation: coordinator, author, reviewer, **approver**, domain expert, watcher |
| Deputies | 0033 §7: the responsible human, **or any of their deputies**, can do everything the responsible human can, approval included. Each deputy acts with their own passkey, and the action is recorded as done on behalf of the responsible human |
| Visibility | 0033 §9: a human sees the current and past activity of the personas they are responsible for; so do their deputies and their superiors. Default retention: 90 days for transcripts, 1 year for the activity log |
| Persona | 0032 §2: memory, roles, rights, tools, sessions, and a responsible human with deputies and superiors |
| Authority | R8 and 0012: only a human approves, with a passkey, on the exact hash of what is approved |

So the target is **one mesh for the whole body**, not one per department.
Inside that mesh, departments are kept apart by persona, by relation and
by role. Sections 3 and 4 say how.

# 1. Identity: the organisation's OIDC provider

## Today, and the D1 branch

* On main, a human is `human:<id>`: a `Member` card with a passkey,
  enrolled by hand and added to the canon by PR. `ameesh decide` takes the
  identity from the Unix user. There is no OIDC anywhere.
* **D1 (#25, parked)** has no dependencies:
  - `src/ameesh/oidc.py` verifies an ID token (RS256 or ES256, discovery
    and JWKS cached, issuer, audience, `azp`, `nonce`, the
    organisation's domain from `hd` or a verified e-mail);
  - a `Member` card with `emails:` maps to `human:<id>`;
  - `ameesh identity verify`.

  It was parked because nothing on main used it yet.

## What a public body needs

Arrivals, departures and changes of position happen in the body's
directory on the day they happen. ameesh must follow that, and never keep
its own list of who works there.

## Recommendation

* **Reopen D1 together with its first consumer: login to ameesh-approve.**
  The approval page and the enrolment page are reached after an OIDC login
  (authorisation code with PKCE). That login proves the session. The
  passkey still proves the decision (0012, R4), and the provider never
  signs on anyone's behalf.
* **At each approval, two checks:** the provider still knows the human
  (fresh login), and their passkey is in the canon's registry. A departed
  person fails the first check the same day, even if the canon has not
  changed yet.
* **The receipt records the issuer and the `sub`** next to the WebAuthn
  assertion. It stays verifiable offline: the issuer is evidence, the
  signature is the proof.
* **Enrolment requires an OIDC login first.** This replaces the
  out-of-band confirmation of the enrolment link.
* **A daily reconciliation** compares the canon's `Member` cards with the
  directory, through the provider's user list or SCIM where available. It
  proposes a PR that removes a departed person's authenticators, roles and
  responsibilities, and never merges that PR itself. The PR also lists the
  personas left without a responsible human (R14). Those personas stop at
  their next turn, as they do today.

# 2. The approver role, deputies and absences

## Today

* An action may name its approvers. Without one, any enrolled approver
  signs. `Member.roles` plays no part in checking a receipt.
* Standing approvals (spec §8.3) are bounded and revocable. They exist in
  storage and in `receipts.py`, but there is no CLI command to register or
  revoke one.
* `ameesh decisions` is the attention queue (R17). `ameesh decide` routes
  a request to the plan's responsible human, then to the agent's.
* There are no deputies, no absences and no superiors on main. D2 (#27)
  reads `deputies` and `superiors` from the canon, but only to show
  activity.

## The gap for a public body

0033 §7 settles **persona responsibility**. A responsible human's deputy
can do everything the responsible human can, so no decision about a
persona is ever blocked.

A public body also needs **powers to commit the body**: who may sign a
purchase order, and up to what amount. 0033 §8 names this function
**approver**, and 0032 leaves the role model (holder, deputies, absences)
to the v2 study. For a local authority, that role model is the delegation
of signature:

* a holder;
* a scope: connectors, operations, a ceiling, a department;
* dates;
* the formal act it rests on;
* its deputies.

## Proposal: the approver role, declared in the canon

Human roles live in the organisation's canon (0033 §6). The approver role
carries what a power needs:

```yaml
---
type: Role
title: approver/purchases
kind: approver                      # 0033 §8 foundation
holder: human:head-of-procurement
deputies: [human:deputy-procurement]
scope:
  connectors: [purchase-order]
  operations: [create, amend]
  max_amount: 40000
  currency: EUR
assurance: élevé                    # hardware key required (receipts levels)
second_signature_above: 15000
act: "delegation of signature 2026-114"   # the formal act, kept with the record
from: 2026-01-01
until: 2026-12-31
---
```

* **Checking a receipt.** A receipt for an action is valid only if its
  signer holds, or deputises for, an approver role that covers the action:
  connector, operation, amount and dates. The passkey's level must meet
  `assurance`. A valid signature from someone without the power is
  refused.
* **Deputies, as in 0033 §7.** A deputy can sign everything the holder
  can, under their own identity. The receipt says "on behalf of
  `<holder>`, role `<role>`". Following 0033 §7, a deputy needs no
  declared absence: no decision ever waits.
* **Absences: routing, not permission.** An absence (`ameesh absence
  declare human:X --until …`, also available on the page of section 6)
  changes **who is asked**: the deputies instead of the holder. It does
  not change who *may* sign. The holder, or one of their superiors,
  declares it, and the thread records the declaration.
* **Escalation.** A request still unanswered at its due date goes to the
  holder's superiors. It is shown as overdue in the queue, and it never
  falls back to an agent.
* **Two signatures.** Above `second_signature_above`, an action needs two
  receipts from two distinct humans. Neither of them may be the human who
  proposed the action, or the responsible human of the persona that
  proposed it.
* **Standing approvals** are tied to a role. They cannot exceed the
  role's scope, and they end when the role ends. A CLI command registers
  and revokes them.
* **Narrower deputies.** A body whose law forbids full substitution can
  give a deputy a narrower scope inside the role. This narrows 0033 §7 for
  commitments only, never for persona responsibility, and it is a
  question for the owner (section 8).

# 3. Isolation inside the mesh

## Today

* Every persona on a host runs under the same Unix user. Every session
  holds the mesh's database DSN. Spec §11 states this limit: a malicious
  agent can read the others' credentials and the tables.
* **E1 (#26, closed unmerged)** proved, in a systemd container, one Unix
  user `p-<persona>` per persona. Each persona saw only its own folder:
  `ProtectHome=tmpfs`, `ProtectSystem=strict`, `PrivateTmp`, keys read by
  systemd before switching users. Its own limits: the `docker` group, and
  a shared Postgres role.
* **E2 (#28, parked)** is an MCP proxy. It checks each `tools/call`
  against the persona's `tools:`, so a refused call never reaches the
  server, and it keeps a trace without the arguments. It was parked
  because a persona could still reach its MCP servers directly without
  E1.
* **D2 (#27, parked)** gates `ameesh activity` by relation (responsible,
  deputies, superiors). It was parked because any session holding the
  DSN can still read the tables.

## Why it matters for a public body

Social services, human resources and civil status handle data that the
other departments must not read. With one mesh for the body (0033 §1),
the only boundary between departments is the boundary between personas.

## Recommendation: three layers, in this order

1. **One Unix user per persona on every server**: redo E1 from main. The
   persona's credentials, memory checkout and temporary files are
   readable by that user only.
2. **One database role per persona, with no direct access to the shared
   tables.** A persona reaches the mesh through ameesh commands that
   filter by its identity (mail, lots, its own leases), never through a
   raw DSN. This is the precondition D2 lacks. Without it, the gates of
   D2 and E2 can be bypassed by anyone with `psql`.
3. **Tools through the MCP proxy only**: reopen E2 once 1 and 2 hold. A
   persona's MCP servers run under a user the persona cannot impersonate,
   and the persona's configuration lists only the proxy.

Until these three layers exist, the operating rule is **separate hosts
per department** for the departments that handle sensitive data. This
fits 0033: it is still one mesh, and hosts are placed by policy (R18).

# 4. Visibility by relation

0033 §9 gives each responsible human, deputy and superior a view of a
persona's activity. **Redo D2 as one commit from main** once D1 and layer
2 of section 3 exist. The relations come from the canon:

* the persona's `responsible`;
* that human's deputies;
* that human's superiors, followed up the chain, with a bounded depth and
  protection against cycles.

D2's first consumer is the CLI `ameesh activity`. Its second is the page
of section 6.

# 5. Memory with access rights, and retention

## Today

A persona's memory is a git repository (0029, 0032 §5). A persona runs on
a machine only if that machine's human can read that repository; the check
goes through the forge and fails closed. Data is not classified anywhere,
and no retention period is set apart from the defaults of 0033 §9.

## Recommendation

1. **Memory rights are the forge's rights.** Each persona's repository
   lives under its department's group in the organisation's forge. The
   persona's credential reaches that repository only (0029). The forge,
   not ameesh, decides who reads it.
2. **Classification in the canon.** The persona card, the project and the
   thread carry `classification: public | internal | restricted |
   personal`, `internal` by default. A `restricted` or `personal`
   persona:
   - runs only on hosts whose policy admits that class (`policy.classes`);
   - is visible (section 4) only through its relations;
   - has its thread excerpts left out of notifications sent outside the
     body (ntfy, Slack): the notification carries a link, never the text.
3. **Retention per class**, set by the organisation's canon. It replaces
   the defaults of 0033 §9: 90 days for transcripts, 1 year for the
   activity log. The auditor reports records kept past their retention. A
   human approves the purge as an irreversible action under R5. Records
   that the law requires to be kept longer (signed decisions and their
   receipts) are filed by the records connector (section 7) before any
   purge.
4. **One processing-register entry per persona**, generated from its card:
   - purpose (its roles);
   - categories of data (its classification);
   - retention;
   - readers (its relations);
   - where it runs (hosts).

   The data protection officer reviews these entries. A change to any of
   these fields reaches them by PR.
5. **Harness-native memories stay off** (0029). The auditor checks that
   they do.

# 6. A page for non-technical staff

## Today

Without the CLI, a human can:

* approve or refuse on the passkey page;
* enrol a passkey;
* open the HTML progress page;
* receive notifications.

Everything else needs a terminal.

## Recommendation

**ameesh-approve serves one page per organisation**, behind the OIDC login
of section 1, accessible (WCAG AA), readable on a phone (R10):

1. **my queue**: `ameesh decisions --for` the signed-in human, plus what
   they receive as a deputy, with due dates;
2. **decide**: the request, its context and options, the answer, and the
   passkey when the request asks for a signature;
3. **my personas**: their activity, filtered by relation (section 4,
   0033 §9);
4. **absences**: declare an absence (section 2).

The page reads through the facade interfaces (0015), so an organisation
can build the same thing into its intranet or the Nexlink app. It writes
only through the existing channels (receipts, `decide`, absences), so it
adds no new authority.

# 7. Connectors

## Today

* Threads exist as files only.
* Notifications: desktop, ntfy, Slack webhook.
* Action connectors: `shell-noop`, `git-merge`.
* The messaging study recommends Matrix. Business connectors and leases
  on business objects (R3) are later work (spec §12).

## Recommendation, in order

1. **E-mail**, as a transport and as the channel of last resort. The
   message carries a link to the approval page. It is never an approval by
   reply.
2. **Matrix** for bodies that run it; Slack or Teams for the others. All
   use the same facade interface (0015).
3. **A records connector.** It files each signed decision as a record in
   the document management system: the decision, its receipt, the thread
   excerpt, and the approver role with its formal act.
4. **Business connectors one at a time.** Each one has its `reconcile`
   (R5) and is reached through the MCP proxy (E2). A connector exists only
   when an approver role can name it, so no connector acts without a
   power covering it.
5. **Leases on business objects** (R3): one member at a time on a citizen
   request or a purchase order.

# 8. Order and estimates

Isolation comes first, because without it no rule on rights can be
enforced. Identity and roles come next; the page comes last.

| Lot | Content | Reuses | Depends on | Estimate |
|---|---|---|---|---|
| **L155a** — one Unix user per persona | E1 redone from main: user, unit, protections, installer | #26 | — | 6 h |
| **L155b** — one database role per persona | role per persona, no raw DSN in sessions, mesh access through filtered commands | — | L155a | 10 h |
| **L155c** — MCP proxy | E2 reopened: `mcp-proxy`, `mcp-config`, trace | #28 | L155a, L155b | 3 h |
| **L155d** — OIDC login | D1 reopened + login to ameesh-approve and enrolment, issuer and `sub` in the receipt | #25 | — | 8 h |
| **L155e** — directory reconciliation | daily comparison, proposed PR, personas without a responsible human listed | — | L155d | 4 h |
| **L155f** — approver role, deputies, absences, two signatures | `Role` card (approver), receipt checked against the role, "on behalf of", absence routing, escalation to superiors, two signatures, CLI for standing approvals | — | L155d | 14 h |
| **L155g** — visibility by relation | D2 redone as one commit: `ameesh activity` by relation | #27 | L155b, L155d | 4 h |
| **L155h** — classification, retention, register | `classification`, `policy.classes`, retention per class, the auditor's checks, purge as R5, register entry | — | L155g | 8 h |
| **L155i** — page for staff | queue, decide, my personas, absences; WCAG AA | — | L155f, L155g | 12 h |

These lots total 69 h. The connectors (section 7) are not estimated here.
Each one is its own lot, scheduled when an organisation names its tools.
The e-mail transport (about 8 h) comes first.

# 9. Questions for the owner

1. **Order:** isolation (L155a–c) first, before identity and roles?
   Recommendation: yes. Without it, D2 and E2 can be bypassed, which is
   why they were parked.
2. **Approver role and 0033 §7:** may an organisation's canon narrow a
   deputy's scope for commitments, where its law demands it?
   Recommendation: yes, for approver roles only, never for persona
   responsibility.
3. **Absences as routing, not permission**, keeping 0033 §7 (a deputy can
   always act)? Recommendation: yes.
4. **Separate hosts for sensitive departments** until L155a–c are in?
   Recommendation: yes. It is an operating rule, written in
   `docs/EXPLOITATION.md` when a first organisation adopts ameesh.

# Not verified

* No identity provider was tried for this study. D1 was tested with
  disposable keys, not against a provider.
* E1 was proven in a container (two personas), not on a server with real
  harnesses.
* Legal terms (delegation of signature, records, processing register) are
  described generically. Each country's law decides the details, and the
  body's legal department must review section 2 before any use.
* The estimates come from the parked branches (D1, D2, E1, E2) and from
  comparable lots (L24, L27, L44, L124), not from a task breakdown.
