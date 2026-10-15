---
type: Study
status: in-progress
work_item: 1617
---

# Automatic reviewer designation at freeze

Lot #1617 (sub-lot #1606 point 4, canon home §7) replaces the manual choice of
the reviewer with a deterministic selection, at freeze time. Finding of
15 October: eighteen lots were waiting without a reviewer, for up to 42 h. The
preparation below is **local and inactive**: the narrow freeze hook is wired
but no caller provides a callback, no profile or runner is modified, no CI is
triggered, and nothing designates a reviewer in production until the reviews
and the package are obtained.

Sources: canon home PR86 §7 and #1606 point 4; versioned policy PR85
(`review_policies.train_reviews`, schema `ameesh-train-reviews-policy/1`);
mesh-design mandate of 15/10 10:10; exact evidence #1465; reader
`train_policy.Versioned` and qualification `train_review.qualify` of #1467;
canon compilation #1616.

## Delivered scope: selection alone

`src/ameesh/review_designation.py` performs no I/O. It receives inputs already
read and returns a decision:

- **policy**: already-parsed `Rule`s (`paths`, `reviews` = 1 or 2). The
  required number is the **maximum** of the rules that cover the paths of the
  frozen diff; a path that no rule covers counts as zero (the light rule,
  PR85). A malformed declaration is rejected — no permissive fallback. The
  coverage of non-light paths remains checked by the reader (#1467).
- **candidates**: the registry rows, filtered by team (the lot's `app`). The
  family is the **real family of the session** (`registry.live_harness`,
  #1037/L242b); never the name, never the declared label alone, never the
  assignment. A harness outside the declared families is not a family.
  Availability rules out `stopped`/`paused`, `externe` sessions (L46) and
  operational or budget pauses ("paused…", "budget : …"). The load is the
  number of open lots of the agent.
- **selection**: `required` reviewers (1 or 2). For two reviews, the families
  are distinct from one another and each differs from the family of **every**
  author or corrector of the head; an author whose real family is unknown
  (empty or label) makes the designation **wait** — ruling them out by their
  name alone would let through a reviewer from their real family (B3, Claude
  dev1 review). A single candidate per family (the least loaded), then the
  least loaded families; at equal load, the order of the names decides.
- **explicit wait**: if independent families are missing, or an author's family
  is unknown, the state is `waiting` with the missing number, and a **capacity
  notification** goes to the pilot — never an implicit admission.
- **mails**: each reviewer receives the lot, the `frozen` milestone and the
  **exact** SHA to review, the verdict command on that SHA, and the rule of §2
  (fixing a blocker oneself makes one an author: their review no longer counts,
  another reviewer takes over).

## The wiring contract (to be qualified with #1616 and #1467)

The module is wired through an injected `Port`, the only layer that touches the
database, the registry and the mail:

- `latest_freeze(lot)`: the last `frozen` milestone (`{id, sha}`);
- `recorded(lot, freeze_id)`: the designation already written for **this**
  freeze;
- `commit(...)`: **one** transaction — lot lock, re-check that the freeze
  examined is still the latest (otherwise `StaleFreeze`, nothing is written),
  idempotent mark by `(lot, frozen milestone)`, mails deposited in the same
  transaction.

The freeze is re-read **once**, and the paths, authors and class provided are
those of that freeze: if the `Port` rejects a freeze that has become stale
(`StaleFreeze`, nothing is written), `apply` raises `FreezeMoved` and does
**not** designate the new head with the inputs of the old one (a changed mail
SHA is not enough) — the caller re-collects the inputs of the new freeze, then
replays (B1, Codex cx2 review). The caller names this freeze (`freeze_sha`, the
SHA of the milestone it collected on): a freeze that arrived **before** the
call, between collection and reading, is likewise rejected by `FreezeMoved`,
before any selection (R Claude dev2). The same freeze replayed returns the
recorded designation without a second mail; a new freeze is another object,
hence a new designation — the old one no longer counts. No return to an old
head is possible: the freeze is re-read under lock, and a SHA that is not a
commit identifier is rejected.

## The Postgres port (`src/ameesh/review_designation_pg.py`)

The `Port` is written, with two usages depending on who holds the transaction:

- **at freeze** (narrow hook, to be wired): `PgPort(tx, after_commit=threads)`
  — `commit` opens no transaction, writes in that of the `frozen` milestone and
  lets the caller flush `threads` after its commit;
- **outside the hook** (recovery, CLI): `PgPort(db)` — `commit` opens its own
  transaction, locks the lot row (`FOR NO KEY UPDATE`, the same lock as
  `add_milestone`), then flushes the thread itself after the commit.

The idempotency mark is a row of the lot journal, prefixed by the lot and the
milestone — `review-designation:<lot>:<gel>: {json}` — which carries the state,
the required number, the reviewers, the exact SHA and the authors of the frozen
content. `recorded` re-reads it (the last row of that freeze is authoritative):
the same freeze replayed does not return a second mail, a wait is not
re-notified, but it **yields** its place to the designation when capacity
returns, on the same freeze. An unreadable mark is rejected, never interpreted.
The mails leave in the same transaction (`mail.send(..., after_commit=…)`,
pattern #315): a cancelled send leaves neither mark, nor message, nor thread
trace. A recipient outside the registry is rejected before any write: a mail
that would wake no one is not worth a designation.

Follow-up (#1775, non-blocking review N's): `apply` treats an abbreviated
`frozen` SHA and the full SHA collected by the caller as the same freeze (a
prefix of at least 7 characters no longer raises `FreezeMoved` by mistake); the
port **refuses to write** on an open transaction without `after_commit` (a
nested `with` does not commit, so flushing the thread there would publish it
before the caller's commit); and `recorded` bounds its journal read to the
`at_ts` of the freeze (a mark cannot precede its freeze) instead of scanning
the whole journal.


A designation is **final** for its freeze; a wait is a **retryable marker**:
one notification per freeze, and if capacity returns — or if the author
evidence arrives — the designation replaces the marker for the same freeze, the
mails to reviewers leaving only then, once. An unknown author provenance (#1465
missing for a commit of the freeze) is not guessed: the designation moves to
waiting with a notification, never a reviewer chosen at random.

What must come from outside, and from whom:

- **rules and families**: from the canon compilation of #1616 (or the reader
  `train_policy.Versioned` of #1467 in the meantime); impl07 delivered on 15/10
  the contract `mechanics.load(repo, commit, project)` (head `ddf1091d`, schema
  `ameesh-train-reviews-policy/1`): `.rules` → `[(r.paths, r.reviews) …]`
  mapped to `Rule`, `.families` + `.exclude_author_family` +
  `.distinct_families` for independence, `.reviews_for(paths)` for
  `required`/`risk`; the local fallback (`claude`, `codex`, `deepseek`, same
  values as `train_review.FAMILIES`) remains a `families` parameter as long as
  PR85 is not merged;
- **required, base, exact head, class, canon commit**: from the freeze adapter,
  starting from `mechanics.load` (#1616) or from the `Requirement` already
  computed by #1467 (`train_policy.Versioned`) — cx3 confirmed on 15/10 that
  the runner of #1467 does not see the freeze at write time and that the wiring
  is done by a narrow freeze hook, not in `train*`; `policy_ref` is
  `rules.commit` (the exact SHA of the policy, rejected if it is not a commit);
- **authors and correctors**: from exact receipts of #1465
  (`review_provenance.receipt_for_commit`), never from a note or an assignment;
  the assignee of the lot remains an author at minimum. Collection must work
  **before any verdict**: `train_review.qualify` waits for the reviews and is
  not callable here. A missing piece of evidence is passed as `unknown_authors`
  (wait), never ignored.

## The hook (wired, inactive)

Agreement point with cx3 (#1467, 15/10 10:46): a **narrow** hook on
`work.milestone(kind=frozen)`, without modifying the `train*` modules or
hard-coding a policy; cx3 leaves the wiring to this lot and confirms that
#1467 touches neither `work.py` nor `storage/postgres/work.py`. The wiring is
in place:

- `work.milestone(..., on_frozen=…, after_commit=…)` passes them on to storage;
- `storage.postgres.work.add_milestone` calls `on_frozen(tx, milestone,
  after_commit)` **in the transaction of the milestone**, just after the INSERT
  of the `frozen` — the `PgPort(tx, after_commit=…)` port writes the mark and
  the mails there atomically —, and **does not flush** `after_commit`: it is
  the caller that holds the commit that flushes it after its COMMIT (on a root
  connection, the inner `with` commits and the caller flushes on return; in an
  already-open transaction, the nested `with` does not commit — flushing there
  would publish the thread before the external commit, B2 of the Codex cx2
  review). An exception from the callback cancels the milestone with everything
  it carried; `on_frozen` on another kind is rejected.

**Nothing is activated**: the default is `on_frozen=None`, the `work milestone`
CLI provides no callback, and a freeze therefore designates no one. The wiring
is proven by 6 real-database tests: the milestone and the designation are
visible only within the transaction, the thread stays in `after_commit` and it
is the caller that publishes it after its commit (root call as well as nested
call), an exception cancels the milestone, the mark and the mails, and the
callback is rejected outside a `frozen`.

## Verification

`tests/test_1617_review_designation.py` — 31 targeted tests, without a
database: maximum of the rules per path and zero for light; two distinct
SQL/security/prod families outside the author; corrector turned author ruled
out (name and family); unknown author family = wait (never ruled out by name
alone); family read from the session and not from the name; unavailability and
load; explicit wait notified once; unknown author provenance = wait, then
designation when the evidence arrives; capacity returned = designation on the
same freeze; Nexlink policy (two `supabase` reviews, one by default); same
freeze replayed = a single mail; new freeze = new designation without return to
the old head; freeze that moves during the write = `FreezeMoved` without
writing anything, the new head is designated only on a new call with its
inputs; invalid or non-frozen head rejected.

`tests/test_1617_review_designation_pg.py` — 23 tests on a real database
(psql): designation written and mails sent once, replay without a second mail;
a second writer (another connection) under the lock returns the database's mark
without re-mailing; stale freeze rejected without writing anything (neither
mark nor message); freeze that moves during the write = `FreezeMoved` without a
write, the new head is designated on a new call; wait notified only once then
designation on the same freeze; wait without a recipient rejected; recipient
unknown to the registry rejected (a mail that wakes no one); unknown author
provenance = wait, designation when the evidence arrives; new freeze = new
designation, the mark of the old freeze remains attached to its head; hook
transaction (thread deferred, published by the caller after its commit);
cancellation = neither mail nor mark. The 6 tests of the freeze wiring: without
a callback the freeze designates no one; the callback sees the milestone in its
transaction (invisible from another connection) and writes the designation
there, the thread remaining with the caller; an exception from the callback
cancels the milestone, the mark and the mails; a nested freeze publishes no
trace before the external commit (and nothing survives its rollback); after the
external commit, the caller flushes the list; the callback is rejected outside
a `frozen`.

## Remaining before activation

- port agreements secured: cx3 (#1467, 15/10 10:46) leaves the wiring of the
  narrow hook here and touches neither `work.py` nor `storage/postgres/work.py`;
  impl07 (#1616) delivered `mechanics.load(repo, commit, project)` (head
  `ddf1091d`); the canon adapter remains to be written;
- the activation adapter: rules and families from `mechanics.load`, `required`
  and `risk` from `reviews_for(paths)`, `policy_ref = rules.commit`, pilot to
  notify (`notify`) — no single machine source of the project pilot exists
  today, and the port rejects an empty recipient or one outside the registry —,
  and explicit decision on designation failure (the transactional callback
  cancels the freeze if it raises);
- the read / recovery command (`ameesh work reviewers <lot>` or equivalent) to
  catch up a freeze left without a designation;
- delivery proof on a **real freeze**: designation without an agent, exact head
  handed over, duration and duplicates measured (the mail counter per freeze);
- reviews: R Claude dev1 (head `d176acf2`) found and fixed B3 (unknown author
  family) → `f6abe89a`; R Codex cx2 (`f6abe89a`) returned `blocked` on B1
  (freeze moved) and B2 (external commit), fixed here; as dev1 is the author of
  its correction, the composition of the two families on the new head is to be
  arbitrated by the pilot;
- two independent reviews outside the author for this gate (security of the
  admission chain), then package at the cut-off.
