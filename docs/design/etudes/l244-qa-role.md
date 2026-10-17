---
type: Study
title: "QA role and QA sign-off in the IT release gate"
description: "L244 (lot #420): a `qa` canon role, a `qa` milestone bound to the current delivery in the IT module, a separation-of-duties rule, routing release lots to QA holders; three implementation lots."
status: draft
tags: [roles, qa, modules, IT, release]
generated: { by: "mesh-design", at: "2026-10-12" }
---

# Design for lot #420

The owner chose option a in decision 10410 (2026-10-12 14:55). It has two
parts:
- QA becomes a role in the cards (home#25), and the role is not critical.
- The roles are filled as follows:
  - On Nexlink, codex3 does QA. It runs on the nexlink-ci runners, a harness
    that adds no cost.
  - On ameesh, the QA agent is mty-amh-qa-01, delivered by #412 (L233a).

Today nobody holds a QA role. A release lot of the IT module (#318, L317a)
needs only two things to be `merged` or `promoted`:
- a `delivery` declaration;
- a `proof` of real execution bound to it.

Nobody other than the author checks that the product actually works for a
user.

## Role

- **Card.** `qa` is one of the canon roles of #414 (L242c). A card carries it
  as `roles: [qa]`, or as a Persona's held role with holder or deputy
  (0032). The role does not change criticality, so a QA agent is paused like
  any non-critical agent.
- **Scope.** A QA agent belongs to the projects it checks: `projects:` from
  L243, or `team`.
- **Global QA.** A QA agent with no project identity is global, like global
  auditors in #319.
- **No fallback.** Nothing falls back to "any reviewer". An agent counts as QA
  only through the explicit role.

## `qa` milestone in the IT module

A new evidence kind is added to the IT descriptor:

```python
"qa": {"strings": ["sha256", "verdict", "report"],
       "arrays": ["scope"],
       "patterns": {"sha256": "^[0-9a-f]{64}$", "verdict": "^(ok|blocked)$"},
       "subject_field": "sha256", "binding_kind": "delivery",
       "writer_role": "ameesh_it_qa"}
```

- **Binding.** The milestone is bound to the current `delivery` declaration,
  exactly as `proof` is. A new delivery invalidates earlier QA, including a
  return to an earlier digest.
- **Content.**
  - `scope` lists what was exercised, for example
    `["install-win", "update-0.2.10", "login", "call"]`.
  - `report` is a link to the QA report: a lot thread entry, a PR comment or a
    CI run.
- **Who can write it.** The SQL guard checks two things, as it already does
  for `proof`:
  - membership of the `ameesh_it_qa` writer role;
  - that the actor holds the `qa` role for the lot's project, read from the
    registry under a share lock.
- **Separation of duties.** The QA actor must differ from three people:
  - the lot's assignee;
  - the author of the delivery declaration;
  - the author of the proof.

  When the same identity tries, the milestone is refused with
  `qa-same-actor`.
- **CLI.**

  ```
  ameesh it qa <lot> --sha256 <digest> --verdict ok|blocked \
                     --scope install-win,login --report <url>
  ```

  It records evidence only. It never runs anything.

## Gate

- **Opt-in rule.** The lifecycle rule is enabled per project:

  ```yaml
  extensions:
    ameesh:
      projects:
        nexlink: {modules: {it: {require_qa: [promoted]}}}
  ```

  With it, `promoted` (publication) also requires a `qa` milestone with
  `verdict: ok` for the current delivery. The project chooses the states:
  - `[promoted]` is the default proposal. A release candidate can merge,
    then QA runs on the candidate, then it is published.
  - `[merged, promoted]` gates the merge as well.
- **Blocked verdict.** A `blocked` verdict keeps the gate closed and does not
  expire. Only a new delivery, or a later `ok` from a QA holder, reopens it.
- **Audit.** The audit pass of #319 gains two findings:
  - `missing_qa`: a promoted release without QA for its current delivery.
  - `stale_qa`: QA bound to an older declaration.

  Both are reported as `module_deviation` with the same routing as the
  existing findings.

## Routing

- **Notification.** A release lot of a project with `require_qa` reaches
  `qa` once both of these exist for its current delivery: a `delivery`
  declaration and a `proof`. At that point the project's QA holders are told
  through event mail on the lot thread.
- **Addressing.** QA holders are reached by role (`role:qa@<project>`, C2 of
  the v2 plan) once role addressing exists. Before that, the mail goes to
  every agent with the `qa` role in the project.
- **No QA holder.** The owner's alert channel receives the notice. A
  reviewer is never picked in their place.
- **Assignment.** The QA agent does not take the lot. It records its verdict
  and the lot stays with its assignee.

## Hosts and current assignments

- **Nexlink.** Two lots carry the work:
  - Release gate: #314 (codex2).
  - QA of 0.2.11: #426, on RC #428.

  codex3 does QA full time after #397.
- **ameesh.** mty-amh-qa-01 needs a Host card for `ameesh-tests` in the canon
  (manaty/home). That card is a canon PR outside home#26, prepared by #412.

## Lots

Implementation goes to DeepSeek agents, at the owner's request of 12/10
14:46. Every PR is reviewed by Codex.

| Lot | Content | Depends on | Estimate |
|---|---|---|---|
| L244-1 | `qa` evidence kind, writer role `ameesh_it_qa`, role and separation-of-duties checks in SQL (both drivers), `ameesh it qa` | #414 (roles) | 3 h |
| L244-2 | Opt-in `require_qa` rule, `missing_qa` / `stale_qa` audit findings | L244-1 | 2 h |
| L244-3 | Notice to QA holders when a release lot is ready, owner-channel fallback | L244-1, L243-2 | 2 h |
| canon | Host card `ameesh-tests`, `qa` role on codex3 and mty-amh-qa-01, `require_qa: [promoted]` for nexlink | owner approval | 30 min |

## Out of scope

- Running QA scenarios automatically. QA agents use the existing CI and
  hosts. ameesh only records their verdict.
- QA for lot types other than IT releases. This is possible later through
  other modules, with the same evidence pattern.
