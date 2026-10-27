# Provenance after target evolution

The shared author reader excludes a commit already integrated into the target
by ancestry. A rebased copy additionally requires a verbatim historical patch-id
and proof concerning the current content. Historical patch-id alone, or a
missing result from the bounded `_effective_revert` scan, proves neither current
integration nor absence of a revert.

When replaying the isolated copy leaves the target tree unchanged, the existing
proof applies. When that replay conflicts after target evolution, the reader can
use the actual frozen head instead:

1. Merge the whole frozen head into the exact target with `merge-tree`.
2. Read the complete textual target-to-merge delta, without rename detection,
   external diff drivers, text conversion or whitespace normalization.
3. Read the candidate's complete parent-to-commit textual delta.
4. Exclude the historical copy only if no line it added occurs among the
   landing's added lines, and no line it removed occurs among the landing's
   removed lines. Compare across all paths, including renamed destinations.

Thus a revert or rewrite that the frozen head actually reverses retains the
historical author. A later target evolution that the landing preserves can
exclude that author even when replaying the original patch conflicts. The
proof concerns what this lot brings to the target, including carried freezes;
it does not classify an evolution as legitimate from its commit message.

Missing head, merge conflict, binary content, mode changes, submodules, invalid
UTF-8 or failed reads do not grant this additional exception. An empty candidate
textual delta does not grant it either. All lines count, including blank and
common lines: overlap can retain an author unnecessarily. This deliberate
conservatism also catches a contribution copied into another existing path.
No bounded search claiming absence and no heuristic significance threshold is
used to remove an author.

The gate, reviewer designation and packet candidate selection use the same
`review_authors.reading` filter. The current frozen head is passed for both the
current range and carried freezes. Calls without a frozen head, including
admitted-member scopes, retain the original isolated replay proof.

## Rewritten prior freezes (#3837)

Non-ascendance alone never drops a freeze. The carried reader measures the
actual clean target-to-merge delta by destination file. A historical commit
can be excluded only when none of its added or removed lines occurs in that
delta, including copies into other paths. Generic lines conservatively keep
provenance. A line already present elsewhere, even in the same target file,
does not excuse a new occurrence: an early `return True` can bypass a check
whose normal return already uses the same line. The regression keeps this
historical author after a squash. Missing/invalid text, binary or mode changes,
conflicts and failed journal writes retain provenance. Ancestral freezes use
the existing reader. Each exclusion is traced once with its freeze, commit,
exact target and zero intersecting delivered lines.

For a historical unknown that survives, another earlier freeze may supply
recorded attribution instead of discarding the commit. The source must belong
to another lot in the same project, have been frozen no later than the carrying
freeze, and contain the exact commit above its common baseline with the exact
target. Its exact milestone receipt must pin its head. The same non-recursive
prior reader then attributes that commit in the source lot's historical
context using exact receipts or the recorded turn journal. No family is
inferred from a name or current registry. An unreadable source, missing family,
contradictory receipt or failed trace leaves the unknown blocking.

The recovered authors and their families are added to the carrying lot, with
their written commits; they remain excluded from its reviewers. Its already
known authors are retained. The journal names the commit, source freeze and
source lot. Search examines up to 256 earlier source freezes, cached within
one reading: omitted or unavailable candidates can only retain unknowns.
This applies equally to the gate, designation and packet candidates through
the shared reader. It does not recursively borrow attribution between lots.

Real witnesses: Nexlink's `a410a7ac` is attributable to `claude1/deepseek` in
freeze #19854 of #3667 for the carried freezes of #3698/#3699/#3700. Attribution
is additive. The four overlapping commits of ameesh #3506 remain carried;
the initial line-presence proposal could not safely exclude them.
