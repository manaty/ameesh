# Write a Pipeline

A **Pipeline card** is a canon page of type `Pipeline`, under `pipelines/`,
that declares an executable pipeline as **data**. It is the declarative form of
the workflow block already carried by a Process card: the same stages,
transitions, guards and projection, compiled by `ameesh canon check` into the
native `process-workflow/1` schema, exposed as a module of source `canon`, and
driven per lot by `ameesh module <name>`.

!!! warning "Data only"
    A Pipeline card is **not** a working rule and is **not** Python code. It
    is never injected into an agent's brief (that is the Process card's job),
    nothing is imported from the canon, and compiling a card executes nothing.
    The native registry still checks every command against installed verifiers
    and real evidence.

The repository ships two complete examples:

* [`examples/pipelines/acme-work-delivery.md`](https://github.com/manaty/ameesh/blob/main/examples/pipelines/acme-work-delivery.md) —
  the development pipeline, converted from the Process witness kept at
  [`examples/processus/acme-work-delivery.md`](https://github.com/manaty/ameesh/blob/main/examples/processus/acme-work-delivery.md);
* [`examples/pipelines/support-work-delivery.md`](https://github.com/manaty/ameesh/blob/main/examples/pipelines/support-work-delivery.md) —
  a non-development, after-sales support pipeline.

## 1. Where the card lives

`pipelines/` is relative to the canon bundle, exactly like `processus/`: a
nested bundle keeps `knowledge/pipelines/…`, and a bundle with a declared
[ameesh scope](../concepts/canon.md) can restrict the folder to
`<scope>/pipelines/`. A `type: Pipeline` card anywhere else is reported as
`pipeline-path` and is never read as a pipeline.

The card fields are:

| Key | Required | Meaning |
|---|---|---|
| `type` | yes | `Pipeline` |
| `title` | yes | readable title |
| `responsible` | yes | a **human** `Member` (`human:<id>`) who answers for the pipeline |
| `updated` | yes | ISO day (`YYYY-MM-DD`) |
| `name` | no | module name; default: the file name without `.md` (letters, digits, `.`, `_`, `-`, 64 characters at most) |
| `description` | no | one line of context |
| `pipeline` | yes | the pipeline block, in the canon form described below |
| `workflow` | transitional | accepted instead of `pipeline`, never both |

The body is free prose. It documents the pipeline for its readers; like a
Process card's body it carries no transition, and nothing parses it.

```markdown
---
type: Pipeline
title: support-work-delivery
name: support-work-delivery
description: "After-sales support, from receipt to reply."
responsible: human:alice
updated: 2026-10-30
pipeline:
  schema: process-workflow/1
  initial: new
  # types, stages, transitions, projection
---

# support work delivery

Prose for the humans who read the canon.
```

## 2. The `pipeline` block

The block has exactly the **canon form** of a Process card's `workflow:` block;
the encoder and the strict compiler are the same code. The complete reference
(stages and cores, grouped transitions, the 14 guards, type aliases, projection
rules and the native schema) is in
[Write a Process card](write-a-process.md#2-the-workflow-block). This guide
only recalls what a pipeline author must decide.

### Stages and cores

Each stage maps to exactly one native core; the label is the board label:

| Core | Meaning | Terminal |
|---|---|---|
| `intake` | planned, not started | no |
| `build` | being built or repaired | no |
| `qa` | review or tests | no |
| `merged` | merged or deployed to staging/pre-production | no |
| `promoted` | completed | yes (`completed`) |
| `closed` | stopped, abandoned or superseded | yes (`not_planned`) |

`blocked` and `waiting_human` are not stages: they are native **holds** that
keep the current stage and core until an explicit release.

The non-development example uses six stages:

```yaml
stages:
  new: {core: intake, status: New}
  triage: {core: intake, status: Triage}
  draft: {core: build, status: Draft}
  approval: {core: qa, status: Approval}
  sent: {core: promoted, status: Sent}
  closed: {core: closed, status: Closed}
```

### Work types

`types` lists the lot types the pipeline covers, each from the compiler's finite
set: `bug`, `evolution`, `docs`, `investigation`, `governance-tracking`,
`promotion`, `human_operation` (the alias `governance` is encoded to
`governance-tracking`). The support example is deliberately non-code:

```yaml
types: [docs, investigation, governance-tracking, human_operation]
```

A declarative module stores **no work item type** of its own: those types are
the ones the workflow covers, and the project still admits them through its
installed modules (section 5). `bug` and `evolution` are always admitted; the
other five come with the built-in `work-types` module, which declares them
without any behaviour and is active only in the projects that select it. A lot
may only be adopted by a card whose `types` contains the lot's type.

### Transitions and guards

A transition is a guarded edge; a group of sources produces one exact edge per
source, and the same guard may lead to distinct targets from one stage. The
guard is one of the 14 native verifier names:

```
planning  start_work  review_requested  qa_started  qa_repair
merged_and_staging_deployed  staging_pass  staging_failure
preprod_deployed  preprod_pass  preprod_failure  repair_started
stop_work  work_completed
```

The compiler checks the **name** only. The native registry checks, when the
command is sent, that the verifier is installed and that its evidence exists
(on the lot itself, its milestones or its observations). Terminal cores never
reopen, and every stage must be reachable from `initial`.

The support example:

```yaml
transitions:
  - {from: new, to: triage, guard: planning}
  - {from: triage, to: draft, guard: start_work}
  - {from: draft, to: approval, guard: review_requested}
  - {from: approval, to: draft, guard: qa_repair}
  - {from: approval, to: sent, guard: work_completed}
  - {from: [new, triage, draft, approval], to: closed, guard: stop_work}
```

`work_completed` is type-sensitive: a code type needs a real delivered and
accepted revision, while a non-code type needs its own `outcome` fact and the
applicable review. The support pipeline therefore completes with a review and
an outcome, never with a software delivery.

### Projection

`projection` classifies every stage exactly once, by core:

```yaml
projection:
  project: support
  status_field: Stage
  product_field: Queue
  issue_open: [new, triage, draft, approval]
  issue_completed: [sent]
  issue_not_planned: [closed]
```

`status_field` and `product_field` must differ. The canon form names stages;
the encoder translates them to the cores they project, and the compiler
requires that every core used by a stage is classified, in exactly one group.
The projection is pure data: `ameesh` computes the issue state and board label
through `process_projection.project_issue`, and the card never writes to
GitHub itself.

## 3. Check the card

`ameesh canon check` is the checker of a Pipeline card: it reads the card,
compiles the block and reports `pipeline-*` findings.

```bash
ameesh canon check
ameesh canon check --json
ameesh canon show --json        # lists the cards under "pipelines"
```

A `pipeline-*` **error** makes `canon check` exit 1 but blocks no agent (the
same rule as a bad `package`/`process` card). Typical codes are
`pipeline-path` (card outside `pipelines/`), `pipeline-name-invalid`,
`pipeline-responsible-missing`/`-unresolved`, `pipeline-updated-missing`/
`-invalid`, `pipeline-duplicate`, `pipeline-invalid` (the block does not
compile). The full refusal list of the block itself is the compiler's:
`frontmatter-invalid`, `key-unknown`, `key-missing`, `stage-*`, `type-*`,
`edge-*`, `classification-*`, `projection-conflict`, `stage-unreachable`,
`terminal-reopening`.

`ameesh process check` remains the checker of **Process** cards; it does not
read a Pipeline card, and this tranche does not remove or change it.

## 4. See the module

With a readable canon, `ameesh modules` adds every valid Pipeline card as a
module of source `canon`: no import, capability `lifecycle` and `cli`, empty
storage, and a `card_ref` (`<member>:<path>@<commit>`) for the trace.

```bash
ameesh modules
ameesh modules --json
ameesh modules --active        # the applied project rows, from the database
```

A card taking the name of an installed module never shadows it and is reported
as `pipeline-module-duplicate`; two cards with one name are
`pipeline-duplicate`. Both are `canon check` errors that block no agent.

## 5. Activate it for a project

Select the card by its `name` in the trusted canon, like an installed module:

```yaml
extensions:
  ameesh:
    projects:
      support:
        modules: [support-work-delivery, work-types]
```

`work-types` admits the support card's non-code types (`docs`,
`investigation`, `governance-tracking`, `human_operation`, plus `promotion`).
Without it, creating a `docs` lot in `support` is refused (`unknown type 'docs'
for project 'support'`, and below the API `AMEESH_MODULE_RULE: work item type
docs has no active module in project support`). A project whose card covers
only `bug` and `evolution` needs no type module.

Apply the canon through the normal trusted procedure (`ameesh canon sync`), then
check the applied rows:

```bash
ameesh canon check
ameesh canon sync
ameesh modules --active
```

`canon check` refuses an unknown name, an activated card that is invalid, and
two selected modules claiming one type (`project-modules-invalid`). The
declarative module stores empty `types`, `schemas` and `rules`: the work item
types covered by the card must still be admitted by the project's installed
modules, as any project type is (here `work-types`). Two selected modules
cannot declare the same type, so a project already admitting these types
through another module does not add `work-types`. Do not remove the card's
module while lots pinned to it remain active.

## 6. Drive a lot

The generic module CLI serves every declarative module. `adopt` pins an
unpinned lot to the card's exact git revision; `advance`, `hold` and `release`
go through the native registry (installed verifiers, optimistic version,
receipts):

```bash
ameesh module support-work-delivery adopt 42
ameesh module support-work-delivery advance 42 --to triage --guard planning
ameesh module support-work-delivery advance 42 --to draft --guard start_work
ameesh module support-work-delivery hold 42 --hold blocked
ameesh module support-work-delivery release 42
```

Each successful command prints its receipt as JSON. `advance` accepts
`--to <stage> --guard <guard>`, optional `--ref key=value` facts, an
`--expected-version N` for optimistic concurrency and a `--command-id UUID` for
an idempotent replay (the same command id and the same intent are replayed,
never applied twice).

`adopt` rules:

* only the lot's **assignee** or the pilot may adopt it;
* the lot must be unpinned, not terminal, and its current core must be the
  card's `initial` core;
* the card must be active for the lot's project, and the lot's type must be
  covered by the card;
* the card is re-read from the **pinned git object**, never from the working
  tree;
* adoption writes the three pin columns and one journal line, **without a
  receipt**; later revisions use the #4007 revision-transfer path, outside the
  declarative module CLI.

A pinned lot is no longer movable by `ameesh work move`: `advance`, `hold` and
`release` are its only lifecycle commands. `--ref` facts are strings and the
verifier decides which ones it accepts; an uninstalled verifier or a missing
piece of evidence refuses the command with a stable code.

## 7. End to end, as the tests prove it

The shipped examples are exercised on PostgreSQL by
[`tests/test_l103_pipeline_examples.py`](https://github.com/manaty/ameesh/blob/main/tests/test_l103_pipeline_examples.py):

1. the example canon is committed to a throw-away git repository and
   `canon check` compiles both cards (acme: 8 stages, 20 transitions, 7 types;
   support: 6 stages, 9 transitions, 4 types);
2. the registry lists both cards as modules of source `canon`; the canon
   selects each card for its project and `work-types` for support, and the
   module rows are written by the normal canon sync, never by hand: a `docs`
   lot is created in support, and refused in acme;
3. a lot of type `evolution` is adopted on the acme card, advanced twice,
   held and released, producing receipts and pure projections
   (open/Backlog, open/Sprint);
4. a lot of type `docs` is adopted on the support card, advanced to `draft`,
   held, released, frozen and reviewed, then advanced to `approval` and
   completed at `sent` with the non-code `outcome` evidence: the projection is
   `closed/completed` labelled `Sent`;
5. the issue state comes from the pure
   `process_projection.project_issue` and the admitted artifact; the test
   passes no GitHub plan and writes no issue.

The two demo cards are data only: changing a stage, a guard or a projection is
a canon change, checked before merge; it never requires a Python change.

## 8. Copy checklist

* [ ] `type: Pipeline`, under `pipelines/` of the bundle (or its declared
      ameesh scope).
* [ ] `title`, human `responsible`, `updated`; optional `name` in the module
      grammar (default: the file name).
* [ ] one `pipeline:` block (or `workflow:`, never both), same canon form as a
      Process workflow.
* [ ] every stage has a core and a unique label; `initial` is declared and
      every stage is reachable; no terminal reopens.
* [ ] `types` are compiler names and cover the lots you will adopt.
* [ ] every edge carries one of the 14 guards; the runtime checks their
      evidence.
* [ ] the projection classifies every stage exactly once, without overlap.
* [ ] `ameesh canon check` is green, the card is selected by `name` in the
      project's modules (with `work-types` when it covers a non-code type),
      and `canon sync` applied it.
* [ ] a lot of a covered type can `adopt`, advance, hold and release, and the
      projection matches the stage.
