# Write a Process card

A **Process card** is a canon page of type `Process`, under `processus/`, that
carries the working rules injected into every agent's brief. This guide covers
the optional **executable workflow** block: the data a canon author writes, how
it is encoded to the native `process-workflow/1` schema, how a revision is
pinned at admission, and how to check a card before merging it.

!!! warning "Data only"
    The workflow block is **data**. `process_workflow.compile_workflow` checks
    its shape and refuses the rest; it never evaluates an expression, imports a
    path, reads a webhook or changes a lot. A compiled workflow is not an
    admission and grants no authority: the host configuration admits a
    revision explicitly, and the native registry still checks every command
    against installed verifiers and real evidence.

## 1. Where the card and the block live

A Process card is an OKF Markdown page whose frontmatter says
`type: Process`, in a folder `processus/` **relative to the canon bundle**
(`processus/lots.md`; a nested canon keeps `knowledge/processus/...`). A
`type: Process` card elsewhere is reported as `process-path` and is never
injected as a working rule.

The card fields used by the canon reader are `title`, `responsible` and
`updated` (required), plus optional `description`, `expire` and the prose body.
`canon check` validates those fields. The workflow block is an additional
top-level key of the same frontmatter:

```yaml
---
type: Process
title: acme-work-delivery
responsible: human:alice
updated: 2026-10-21
workflow:            # optional; a prose-only Process card has no such key
  schema: process-workflow/1
  # …
---

# acme work delivery

The prose rules, injected into the agents' briefs.
```

The key is ignored by the brief: the prose body stays the working rule, and a
card without `workflow` keeps exactly its previous behaviour. Only the check
command below reads the block.

## 2. The workflow block

The **canon form** is compact: stages are a table, a transition may group its
sources, and the projection classifies stages. The checker encodes it to the
strict native document; the two forms carry the same information.

### Stages and cores

Every Process stage maps to exactly one native **core**:

| Core | Meaning | Terminal |
|---|---|---|
| `intake` | planned, not started | no |
| `build` | being built or repaired | no |
| `qa` | review or tests | no |
| `merged` | merged or deployed to staging/pre-production | no |
| `promoted` | completed | yes (`completed`) |
| `closed` | stopped, abandoned or superseded | yes (`not_planned`) |

The core is not the board label. A stage in the canon form carries both:

```yaml
stages:
  draft: {core: intake, status: Draft}
  implementation: {core: build, status: In Progress}
  review: {core: qa, status: Dev review}
  done: {core: promoted, status: Done}
  stopped: {core: closed, status: Stopped}
```

`status` is a **display label**, kept outside the compiled artifact for the
GitHub board (`github.process.<project>.labels` at admission). Labels must be
non-empty and unique. `blocked` and `waiting_human` are **holds**, not stages:
they keep the current stage and core, and the native registry releases them
through its own verified commands.

### Work types

`types` is the list of lot types the workflow covers. The compiler accepts
`bug`, `evolution`, `docs`, `investigation`, `governance-tracking`,
`promotion` and `human_operation`; a project can also use the tracking alias
`governance`, which the encoder maps to `governance-tracking`. The compiler
validates the names; the runtime type gate uses the installed type set, so keep
the list aligned with what the project actually installs.

### Transitions and guards

A transition is a guarded edge:

```yaml
transitions:
  - {from: [draft, backlog, sprint], to: backlog, guard: planning}
  - {from: [draft, backlog, sprint], to: sprint, guard: planning}
  - {from: [draft, backlog, sprint], to: implementation, guard: start_work}
  - {from: implementation, to: review, guard: review_requested}
  - {from: review, to: tests, guard: qa_started}
  - {from: tests, to: done, guard: work_completed}
  - {from: [draft, backlog, sprint, implementation, review, tests], to: stopped, guard: stop_work}
```

`from` is one stage or a **list** of stages; each source produces one exact
edge. The same stage may have two distinct targets under the same guard — the
reference card deliberately offers `planning` towards both `backlog` and
`sprint`; the command carries the chosen target. Only an exact duplicate
`(from, to, guard)` is refused. A terminal core cannot be the source of an
edge, and every stage must be reachable from `initial`.

The guard must be one of the 14 native verifier names:

```
planning  start_work  review_requested  qa_started  qa_repair
merged_and_staging_deployed  staging_pass  staging_failure
preprod_deployed  preprod_pass  preprod_failure  repair_started
stop_work  work_completed
```

The compiler checks the **name** only. Whether a guard is executable, and what
evidence it demands, belongs to the native registry and its installed
verifiers; an uninstalled verifier refuses the command.

### Projection

`projection` says how the stage becomes an issue state:

```yaml
projection:
  project: acme
  status_field: Stage
  product_field: Product
  issue_open: [draft, backlog, sprint, implementation, review, tests]
  issue_completed: [done]
  issue_not_planned: [stopped]
```

The three lists classify **stages**, exactly once and without omission. The
encoder translates them to native **core** lists: all stages that project to
the same core must be in the same group. `issue_completed` only accepts the
`promoted` core, `issue_not_planned` only `closed`, and `issue_open` accepts
none of the terminal cores. `status_field` and `product_field` must differ.

## 3. Encoding to `process-workflow/1`

The native schema is closed. The encoder applies these mechanical rules; it
never removes an edge or invents a stage to make a card compile.

| Canon form | Native `process-workflow/1` |
|---|---|
| `stages:` is a mapping `name -> {core, status}` | `stages:` is a list of `{status, core}` |
| `status` is the display label | kept in `labels` (presentation, not compiled) |
| `from: [a, b]` | one edge per source, `from: a`, `from: b` |
| `types: [governance, …]` | `types: [governance-tracking, …]` |
| `projection.issue_*` lists stages | lists of the cores those stages project |
| `schema: <project schema>` (e.g. `tpk-workflow/v1`) | `schema: process-workflow/1` |

The artifact keeps the exact `ref` and `version` it was compiled with. The same
document compiled under another ref or version is a different artifact; there
is no automatic adoption of a new revision.

A card can also be written directly in the native shape (a `stages` list,
scalar `from` values, and core lists in `projection`). The checker accepts that
form too, which is useful for fixtures and for comparing the two
representations. Display labels then have no source in the page and must be
supplied with the admitted revision.

The example below uses the native schema label. A project that owns an approved
schema label keeps it in the card — TPK publishes `tpk-workflow/v1` — and the
encoder still emits the native `process-workflow/1` document.

## 4. Admission: the exact page, commit and version

The native compiler is pure; the **host configuration** owns the admitted
revision. The opt-in is the `github.process` key:

```json
{
  "github": {
    "projects": {"acme": "acme/repo"},
    "process": {
      "acme": {
        "ref": "acme@6c771e2e16bc04e8ce3b04a55df67ad4aca77cb8:processus/acme-work-delivery.md",
        "version": 1,
        "document": {"schema": "process-workflow/1", "…": "…"},
        "labels": {"draft": "Draft", "implementation": "In Progress", "…": "…"}
      }
    }
  }
}
```

* `ref` is the **pin**: the canonical page at an exact merged commit and its
  path relative to the canon bundle. A branch, a moving tag or a working-tree
  edit is not a pin.
* `version` is the integer Process revision the admitting operator records.
  Increase it for a new revision; `(ref, version)` identifies the admitted
  artifact and is refused twice for the same project.
* `document` is the encoded native document the checker prints (`--json`).
* `labels` maps every stage to its board label; a missing, extra or unknown
  stage is a configuration error. A project may admit a list of mappings when
  a transfer must accept the old and new revision at once.

A lot pinned to a workflow carries `workflow_ref`, `workflow_stage` and
`workflow_version`. Commands check that the lot, the command and the compiled
artifact share the same revision; no automatic adoption exists. A transfer to
a new revision is explicit, reviewed and journalled, with its own verifier.

The canon page remains the approved source, but **the page is not read live at
admission today**: the compiler takes the admitted document from the host
configuration, and the pin is the `(ref, version)` pair above. Run the checker
on the page, copy the encoded document and labels into the host configuration,
and record the exact commit and path. This keeps the compiler pure and the
admission decision visible in one place.

## 5. Check the card

The checker reads the card's frontmatter, encodes the `workflow:` block and
compiles it with the same strict compiler the runtime uses. It reads no
database and writes nothing:

```bash
ameesh process check processus/acme-work-delivery.md
```

```text
processus/acme-work-delivery.md: OK — 8 stages, 20 transitions, 7 types
  source schema: process-workflow/1
  initial: draft
  stages: draft/intake "Draft", backlog/intake "Backlog", …
  types: bug, evolution, docs, investigation, governance-tracking, promotion, human_operation
  projection: open=[intake, build, qa] completed=[promoted] not_planned=[closed]
```

`--json` prints the same result plus the native `document` and `labels` to
admit:

```bash
ameesh process check processus/acme-work-delivery.md --json
```

The exit status is `0` when the block compiles (or the card has no structured
workflow: a prose-only card is valid), `1` on a named refusal, and `2` when the
file cannot be read. `--ref` and `--version` label the checked revision; they
default to `file:<path>` and `0` and do not admit anything.

From a source checkout without installation, the same command is
`PYTHONPATH=src python -m ameesh.process_card check …`. `ameesh canon check` and
this command complement each other:
`canon check` validates the Process **card** (`type`, path, `title`,
`responsible`, `updated`, short projection of the prose); this command
validates the **workflow block**, which `canon check` does not read.

## 6. Complete compiling example

This is the frontmatter of the example shipped as
[`examples/processus/acme-work-delivery.md`](https://github.com/manaty/ameesh/blob/main/examples/processus/acme-work-delivery.md)
(the page wraps it in the usual `---` delimiters and adds ordinary prose
below). It keeps both `planning` choices, groups the repeated sources, uses the
`governance` alias, and classifies every stage:

```yaml
type: Process
title: acme-work-delivery
description: "Exemple minimal d'une carte Process qui porte un workflow exécutable."
responsible: human:alice
updated: 2026-10-21
workflow:
  schema: process-workflow/1
  initial: draft
  types: [bug, evolution, docs, investigation, governance, promotion, human_operation]
  stages:
    draft: {core: intake, status: Draft}
    backlog: {core: intake, status: Backlog}
    sprint: {core: intake, status: Sprint}
    implementation: {core: build, status: In Progress}
    review: {core: qa, status: Dev review}
    tests: {core: qa, status: Dev tests}
    done: {core: promoted, status: Done}
    stopped: {core: closed, status: Stopped}
  transitions:
    - {from: [draft, backlog, sprint], to: backlog, guard: planning}
    - {from: [draft, backlog, sprint], to: sprint, guard: planning}
    - {from: [draft, backlog, sprint], to: implementation, guard: start_work}
    - {from: implementation, to: review, guard: review_requested}
    - {from: review, to: tests, guard: qa_started}
    - {from: [review, tests], to: implementation, guard: qa_repair}
    - {from: tests, to: done, guard: work_completed}
    - {from: [draft, backlog, sprint, implementation, review, tests], to: stopped, guard: stop_work}
  projection:
    project: acme
    status_field: Stage
    product_field: Product
    issue_open: [draft, backlog, sprint, implementation, review, tests]
    issue_completed: [done]
    issue_not_planned: [stopped]
```

The body is the working rule the agents read. The workflow block above is
data; it changes no brief and executes nothing.

## 7. Refusals

The checker refuses with a stable code and a JSON-ish path (`$.stages`,
`$.transitions[3].guard`, …):

* page: `frontmatter-missing`, `frontmatter-invalid`, `value-type` (not a
  `type: Process` card, or `workflow` not an object);
* shape: `key-unknown`, `key-missing`, `value-type`, `value-empty`;
* stages: `stage-padded`, `stage-case-duplicate`, `stage-label-duplicate`,
  `core-unknown`, `initial-undeclared`;
* types: `type-unsupported`, `type-duplicate` (including after an alias);
* edges: `edge-undeclared`, `guard-unknown`, `edge-duplicate`;
* projection: `classification-overlap`, `classification-incomplete`,
  `classification-mismatch`, `classification-core-conflict`,
  `projection-conflict`.

The compiler then adds its own graph refusals: `stage-unreachable`,
`terminal-reopening`, `classification-mismatch` for a terminal core in the
wrong group, and the schema/graph checks listed in `process_workflow`. A
refusal changes nothing; there is no partial compile.
