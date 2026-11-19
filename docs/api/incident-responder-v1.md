# Incident responder, version 1

#7866 / [manaty/ameesh#6095](https://github.com/manaty/ameesh/issues/6095).

`ameesh responder` routes a production incident to a human who is **available
now and responsible**, instead of parking it in the queue of someone who is
asleep. The canon says who may answer and in which order; the resolver filters
that order by availability and journals only the choice.

```sh
ameesh responder <application> --severity P1 [--json] [--now ISO]
                 [--canon DIR|ID] [--ref REF]
```

Exit 0 means a human was chosen, or a phone wake was requested by the owner's
rule; exit 1 is a machine refusal (one JSON document with `error` when
`--json` is used); exit 2 is ordinary argument syntax.

## Canon: who may answer

An `Application` card lives in `applications/` (or `<scope>/applications/`)
and stays valid without an `incident:` block:

```yaml
---
type: Application
title: ameesh
responsible: human:smichea
incident:
  severities: [P1, P2, security]     # optional; all severities by default
  responders:                         # ordered: first available wins
    - role: tech-owner
    - member: human:smichea
    - role: dev-lead
  escalation:
    wake: human:smichea               # who is called; never a phone number
---
```

`severities` are `P1`–`P4`, `security`, `data-loss` and `outage`.
`responders` entries carry exactly one of `role` (a role held by at least one
`Member`) or `member` (`human:<id>` of a `Member`). A role expands to its
Members in title order. `canon check` refuses a malformed block with a named
defect (`application-incident-*`, `member-timezone-invalid`,
`member-incident-eligible-invalid`), and a broken Application card blocks no
agent.

A responding human is eligible only with an explicit opt-in:

```yaml
type: Member
title: smichea
timezone: Asia/Shanghai       # IANA name; no timezone = no timezone fallback
incident_eligible: true       # absent/false = never a responder
local_accounts: ["smichea@pc-smichea"]   # maps the attach lease to the human
```

## Availability signals

Strongest first, each with its freshness window. **An absent or stale signal
is not an absence**: the next one is used.

| Signal | Read from | Window / bound |
| --- | --- | --- |
| `attach` | a live lease `attach:<local account>` in `agent_registry` | the lease expiry |
| `interaction` | ameesh journal: mail sent from one of the member's own devices, or a decision answered from their own device; then Memoria's public interface | 30 min |
| `presence` | Slack `users.getPresence`: `active` = available, `away` = not available | live, cached 5 min |
| `timezone` | `Member.timezone` and usual local hours (08:00–20:00) | last resort |

Only the human's **own activity** is an interaction. A journal row is
attributed by the exact `user@host` proof (`payload.human_account`, written by
the CLI for an unbound human entry) or, failing it, by a host declared in
`Member.local_accounts` (`["smichea@pc-smichea"]` declares `pc-smichea`). A
decision answer typed by the bureau under the §10 delegation — its signed text
carries "saisi par le bureau, délégation §10" or "réponse de l'owner relayée
par le bureau" — is never the owner's own activity, whatever host recorded it.
Mail delivery, a decision's reply mail and an `action_events` row are not
device proofs and are not used: the decision close itself writes the delivery
timestamp when anyone answers (the bureau included), a reply mail is only the
answer's byproduct, and an action event names an actor but no device. Fewer
signals, never a false one; a missing signal passes to the next one.

A present `away` presence and the local-hours fallback can rule a human out;
a missing timezone never makes someone reachable by default. A signal
provider that is not configured, unreachable or failing returns nothing, and
the chain degrades cleanly.

## Simulation

`--now <ISO>` is a simulation: the routing is computed at that instant, but no
phone call is requested and no `incident.routed` event is written (`journal`
is null, `wake.status` is `simulated`). The database probes keep the database
clock; use the live path to test an actual route.

## Wake rule

The phone call goes through TPK's voice service (Twilio outbound). It is a new
expense and is **off by default**. It is requested only when

* the severity is `security`, `data-loss` or `outage` (immediately, even when
  a responder is available), or
* the severity is `P1` and nobody is available.

`P2`–`P4` never wake anyone. The resolver sends a short reason only; the phone
number stays in the voice service's secrets and never appears in the canon,
this document or the journal.

Configuration (host environment, never the canon):

| Variable | Meaning |
| --- | --- |
| `AMEESH_INCIDENT_CALL` | `1` enables the wake feature; absent/other = off |
| `AMEESH_INCIDENT_CALL_GO` | id of the **signed owner decision** authorising the call |
| `AMEESH_INCIDENT_VOICE_URL` | voice service endpoint (POST, JSON) |
| `AMEESH_INCIDENT_VOICE_TOKEN` / `_FILE` | bearer token; `_FILE` names a mode-0600 file |
| `AMEESH_INCIDENT_MEMORIA_URL` | Memoria base URL; absent = no Memoria signal |
| `AMEESH_INCIDENT_MEMORIA_TOKEN` / `_FILE` | optional bearer token |
| `AMEESH_INCIDENT_SLACK_TOKEN` / `_FILE` | Slack token; absent = no presence signal |
| `AMEESH_INCIDENT_SLACK_USERS` | optional JSON `{"<member>": "<slack user id>"}` |

The owner go is a **positive, scoped authorization** carried by the signed
decision data itself — the same rule as `prod-go/1`: one machine-readable line,
no prose heuristics. The decision question carries exactly one line

    incident-call/1 application=<application-id> until=<YYYY-MM-DDTHH:MM:SSZ>

The request must offer the reserved option `incident-call` with the exact
positive label `Oui, autoriser les appels de réveil`; the signed answer must
select that option. There is no free choice of key or label, so a "Non" option
can never be presented — or signed — as the authorization. The decision must
be answered, by the wake owner, through a signed answer whose owner-key proof
still verifies, and must not have been superseded. The window must be in the
future and at most `CALL_GO_MAX_HOURS` (7 days) away.

Example request (the wake owner signs the answer):

    ameesh decide ask --new-lot "Call go: incident routing for acme" \
      --question "Autoriser les appels de réveil pour acme ?
incident-call/1 application=acme until=2026-11-26T00:00:00Z" \
      --option "incident-call=Oui, autoriser les appels de réveil" \
      --option no="Non" --needs-signature

A pending, unsigned, misaddressed, superseded or unverifiable decision, an
unrelated question, another application's go, a signed "no" or free-text
answer, an expired window, a go beyond the maximum window, a missing reserved
option or a reserved option with any other label: all are refusals, not calls.

## Wire document: `ameesh-responder/1`

With `--json`, one document on stdout (a refusal document has only `schema`,
`ok` and `error`):

| Field | Type and meaning |
| --- | --- |
| `schema` | Constant `ameesh-responder/1`. |
| `ok` | True when a responder was chosen, or a wake was requested. |
| `application` | `{id, title}` of the Application card. |
| `severity` | Normalized severity (`P1`, `security`, …). |
| `responder` | `{member: "human:<id>", title}` or null. |
| `reason` | `attached`, `recent-interaction`, `present`, `usual-hours`, `no-responder`, `severity-not-covered`, `no-incident-block`. |
| `signals` | Kinds used for the chosen responder, or kinds consulted when nobody is available. Never a value, a timestamp or a presence history. |
| `via` | The chain entry that matched: `{role}` or `{member}`. Only when chosen. |
| `wake` | `{plan: none|immediate|no-responder, status: not-needed|requested|disabled|refused|unconfigured|failed|simulated, reason?, request_id?}`. |
| `journal` | `{event: <id>}` of the `incident.routed` event, or null on refusal or simulation. |
| `error` | `{code, message}` on refusal. |

The journal event `incident.routed` carries the application, severity,
`responder` (or null), `reason`, `signals` and the wake plan/status — never an
activity timestamp, a presence value, a candidate list or a "who was online"
report.

## Adapter contracts

**Memoria** (separate project): `GET <base>/v1/interactions/last?human=human:<id>`,
bearer token optional, returns

```json
{"schema": "memoria-last-interaction/1",
 "human": "human:smichea",
 "last_interaction": "2026-11-19T09:58:00Z"}
```

Any other status, schema, human or instant is ignored (absence, not
"offline"). Memoria activation is a separate lot; until it is deployed and
configured the resolver simply uses the ameesh journal.

**Voice service** (TPK): `POST <AMEESH_INCIDENT_VOICE_URL>`,
`Authorization: Bearer <token>`, JSON

```json
{"schema": "ameesh-voice-call/1",
 "application": "ameesh",
 "severity": "P1",
 "reason": "Incident P1 sur ameesh. Aucun repondant disponible."}
```

The service resolves the on-call number from its own secrets. `2xx` accepts
the request; an optional `{"call_id": "…"}` is echoed in the journal-free
`wake.request_id` field. Any other status, or an unreachable service, is
reported as `failed`/`refused` and never blocks routing.

## Consumer fixture

[`examples/fixtures/responder-consumer`](../../examples/fixtures/responder-consumer)
is the consumer side of this contract: it reads the JSON document and exits 0
only for a valid `ameesh-responder/1` routing (`responder` or `wake.status ==
"requested"`). The producer test feeds it the real CLI output, so both sides
are exercised in this repository.
