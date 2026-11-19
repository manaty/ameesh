# Incident routing: responders from the canon, filtered by availability

#7866 / [manaty/ameesh#6095](https://github.com/manaty/ameesh/issues/6095).
Owner direction of 2026-11-19; implementation lot in ameesh.

## Problem

On 2026-11-19 an avatar parked a production incident (customer e-mails
silently not sent, revenue at stake) in the decision queue of its owner while
he slept. An incident must go to a human who is **available now and
responsible**, not to a sleeping person's queue. Time zones matter: when the
owner (UTC+8) sleeps, the French team is at work.

## Decisions

1. **Who may answer is stable, reviewed data.** A new `Application` card
   (`applications/*.md`) carries an ordered `incident.responders` list of
   roles or members, the severities it covers, and the wake escalation (a
   human, never a number). `Member` cards gain `timezone` (IANA) and
   `incident_eligible` (explicit opt-in). A card without an `incident:` block
   remains valid; a malformed block is refused by `canon check` with a named
   defect. A broken Application card blocks no agent, like a Pipeline card.
2. **Who is available is a transient read, never stored.** Signals, strongest
   first: a live `attach:<account>` lease, a fresh interaction (ameesh journal
   first, Memoria second), Slack presence, then timezone and usual hours. Each
   signal has a freshness window; an absent or stale signal passes to the
   next, and a failing provider degrades to absence. A member without a
   timezone is never reachable by the default fallback, and a member without
   `incident_eligible: true` is never a candidate.
3. **`ameesh responder <application> --severity S`** is the resolver and the
   public contract (`ameesh-responder/1`). It journals only the chosen
   responder, the reason and the *kinds* of signals used: no activity
   timestamp, no presence value, no candidate list, no "who was online".
4. **Wake only by the owner's rule.** No call when a responder is handling the
   incident, except `security`, `data-loss` and `outage`, which wake
   immediately; a P1 with nobody available wakes too; P2–P4 never wake. The
   call is a new expense: off by default, and only with a signed owner go plus
   a configured voice endpoint. The go is a machine-readable
   `incident-call/1 application=<id> option=<key> until=<Z>` declaration inside
   the signed decision question, and the signed answer must select
   `<key>` — a signed "no" or an unrelated decision authorizes nothing (same
   rule as `prod-go/1`). The window is at most seven days. The resolver sends
   a short reason; the number stays in the voice service's secrets.
5. **Consumers** use the JSON CLI contract (IMA for production P1s, avatars to
   route instead of queueing, later mesh hand-offs). A dependency-free
   consumer fixture in `examples/fixtures/responder-consumer` is exercised by
   the producer tests.

## Signals and privacy

Presence and activity serve routing only. The resolver reads them in memory,
decides, and writes one `incident.routed` event with:

```json
{"schema": "ameesh-responder/1", "application": "ameesh", "severity": "P1",
 "responder": "human:smichea", "reason": "attached", "signals": ["attach"],
 "wake": {"plan": "none", "status": "not-needed"}}
```

No per-person activity history, no presence report, no retention beyond the
generic journal. The `signals` field names the kinds, never their value or a
timestamp.

## Out of scope

The canon data of `manaty/home` (Application cards, member timezones and
eligibility), the Memoria last-interaction endpoint and the IMA consumer are
separate lots in their own repositories, as are the TPK voice service and the
owner-go activation. Until they land, `ameesh responder` works on a canon that
declares cards, and the phone wake stays `disabled`.
