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
