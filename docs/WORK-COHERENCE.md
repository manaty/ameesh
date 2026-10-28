# Backlog coherence pass (#4172)

`ameesh work coherence` is the periodic, per-project generalisation of the
intake check of #2988 (`intake_neighbors`). The intake check compares a **new**
lot with the existing ones; this pass reads **every open lot** of a project and
looks for the four families the owner described in manaty/ameesh#3185:

| Family | What is detected | Outcome |
|---|---|---|
| already done | a merged change of the base ref references the lot (`ameesh-work: <id>`, `lot/<id>-…` merge) | **closed** (reason `done`) with the proof in the lot journal |
| probably done | a merged change touches the code the lot cites (same line, or the whole file when no line is cited) | **digest** to the project's PO: a touch is not a proof |
| probably obsolete | every cited path is absent from the base ref and at least one was removed by a merged change | **digest** to the project's PO: an absence or a deletion is not a proof |
| duplicate | two open lots have the same normalised title, or the same non-empty issue reference and at least two significant title words in common | **digest** to the project's PO |
| contradiction | two open lots cite the same path and their titles start with opposite verbs (`add`/`remove`, `ajouter`/`supprimer`, …) | **digest** to the project's PO |

Only `already done` is deterministic. A closure is a normal `work.close`
gesture: it takes the same locks and releases the same dependents. The
probably-done and probably-obsolete lots, the duplicate and the contradiction
are **never** closed by the pass; the PO answers with a typed link
(`ameesh work links <lot> --add <other> --kind duplicates|conflicts_with`,
#4167) or with a justified closure.

## Command

```sh
ameesh work coherence [--project P] [--repo DIR] [--apply] [--json]
```

Without `--apply` the command is read-only (the default): it prints the report
and writes nothing. `--apply` closes the deterministic findings and sends **one
digest per project** to the PO. `--project` restricts the pass to one project;
without it, every project that still has an open lot is scanned. `--repo`
names a local clone (tests, a hand-run on a host whose project mapping is not
declared); otherwise the clone declared for the project is used, then the
current directory when it is the project's repository.

The JSON output is one object:

```json
{"schema": "ameesh-backlog-coherence/1",
 "projects": [{"project": "ameesh", "git": "vérifié",
               "base": "origin/main", "base_sha": "…",
               "open_lots": 12,
               "findings": [{"kind": "already_done", "deterministic": true,
                             "lot": 4172, "sha": "…",
                             "evidence": "merged change … references the lot"}],
               "closures": [{"lot": 4172, "kind": "already_done",
                             "result": "closed", "reason": "done"}],
               "digest": {"sent": true, "message_id": 123,
                          "recipient": "human:smichea"}}]}
```

## Rules and safety

* **No clone, no deterministic closure.** Without a readable clone the report
  says `git: "non vérifié"`; only the textual duplicate and contradiction
  checks run.
* **No network.** No fetch, no `gh` call: the pass reads the local base ref.
  A clone that is behind simply misses recent merged changes until the next
  pass or the next fetch of the host.
* **A code touch must be strictly posterior to the lot.** The commit timestamp
  is compared with the lot's creation timestamp; a same-second commit (for
  example the base commit that added the cited file) is not a fix for a lot
  filed after it. A commit that references the lot is looked for in the base
  ref's history since the oldest open lot; a commit older than the lot itself
  (beyond a five-minute slack) is ignored: a `#<id>` cited before the lot
  existed names something else.
* **Only `intake` and `blocked` lots are closed.** A lot in `build`, `qa` or
  `waiting_human`, or one carrying a branch or a PR, is left to the PO; a
  refused closure appears in the digest with its reason.
* **A moved component is not obsolete.** If the cited path is gone but a file
  with the same basename exists elsewhere in the base ref, the lot is not
  closed: the move is not a proof that the component disappeared.
* **An absence is not a proof by itself.** Every missing/removed path goes to
  the digest as probably obsolete, whatever its deletion date or the lot title:
  a restoration or data-recovery request can follow a deletion, and a lexical
  verb list cannot decide. No automatic obsolete closure exists today; a
  structured explicit proof of obsolescence would be needed to add one
  (#4172 B1 dev2/cx2/cx1).
* **A line is a precise claim.** When a lot cites both a bare path (in its
  title) and a `path:line` (in its body), only the line is used for the code
  check; otherwise a commit touching another line of the same file would
  flag the lot.
* **One digest per project.** The digest is idempotent: its body is stable
  while findings do not change, and `mail.send` deduplicates it on its
  `idempotency_key`; a periodic timer does not repeat the same message every
  tick. It is addressed to the project's responsible WorkPackage when the canon
  declares one, else to `notify.default_human`.
* **Bounded work.** At most 2000 open lots per project and 5000 commits are
  read per pass; git calls are bounded (`GIT_TIMEOUT`, 30 s).

## Periodicity

The **coherence** pass exists only under the elected role lease:

```sh
ameesh role run <role> -- ameesh notify --once --only coherence
```

`ameesh role timers --install` installs `ameesh-role-coherence.{service,timer}`
on every candidate host of the pilot role (`mesh-design`); only the lease
holder runs it. The timer
wakes every six hours (`role_timers.COHERENCE_INTERVAL_S`). The full
`ameesh notify` service never runs the pass: a periodic timer on a single
elected host is enough, and the digest is idempotent across ticks.

## Reference fixture

`tests/test_4172_backlog_coherence.py` plants a duplicate, an already-done lot
(reference), a probably-done lot (code touch), an obsolete path, a restoration
request filed after a deletion, and a contradiction. It checks that only the
already-done reference is closed, that the probably-done/probably-obsolete
lots, the duplicate and the contradiction stay open, that exactly one digest is
sent to the PO, and that a second pass sends no second digest.
