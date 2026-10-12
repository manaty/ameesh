---
type: Study
title: "Study — L160g: the mergiraf syntactic merge driver replayed on the conflicting merges of ameesh and Nexlink"
description: "Every two-parent merge of ameesh and Nexlink before 2026-10-11 04:31 replayed with and without mergiraf 0.19.1, each result compared to the committed merge and the 27 differences read by hand: 13 wrong resolutions, of which 4 do not compile and 1 leaves conflict markers in a file reported as merged; not adopted, for any language."
status: draft
tags: [fusion, conflits, git, mergiraf, merge-driver, l160]
generated: { by: "ameesh-dev2/claude-opus-5-5", at: "2026-10-12T15:00:00+02:00" }
stale_after: 2027-01-12
sources:
  - { resource: "l160-radar-de-conflits.md", title: "Study L160, section Syntactic merge driver (verdict: measure before adopting)" }
  - { resource: "../../../scripts/l160g-mergiraf.py", title: "Replay script (L160g)" }
  - { resource: "https://mergiraf.org/", title: "Mergiraf, syntactic merge driver" }
---

# Question

The L160 study left the syntactic driver at "to be measured on the replayed
conflicting merges before adopting it", and set the adoption rule for L160g:
adopt per language (Python, TypeScript, JSON) **only if there is no wrong
resolution**. This study measures that.

# Method

`scripts/l160g-mergiraf.py` redoes every two-parent merge of a sample
twice with `git merge-tree --write-tree`: once plain, once with mergiraf
0.19.1 declared as the driver for every language it knows (`mergiraf
languages --gitattributes`, given through `core.attributesFile`). It only
reads the repository it is given: the replay runs in a temporary bare
mirror that borrows its objects, where merge-tree's writes land, removed at
the end whatever happens; git runs without system or global configuration,
without lazy fetch, replace objects or in-tree `.gitattributes`.

The sample is every two-parent merge reachable from the refs of a mirror of
the GitHub repository taken on 2026-10-12 at 12:18 (`git clone --mirror`;
GitHub's synthetic `refs/pull/*/merge` excluded), committed before
2026-10-11 04:31, when L160g was created. It is frozen in a manifest, with
the refs it was read from: `--manifest` replays exactly those merges and
refuses one that is missing. A local working clone gives another sample (an
agent worktree of ameesh reaches 1,230 merges, through the agents' local
branches), hence the mirror and the manifest.

Each file the two replays handle differently is compared with the blob the
merge commit holds:

* `resolved`: in conflict plain, merged by mergiraf;
* `still`: in conflict both ways;
* `altered`: merged cleanly both ways, but mergiraf's content differs from
  git's;
* `new-conflict`: merged cleanly by git, in conflict with mergiraf;
* `same` / `differs`: mergiraf's result is, or is not, the committed blob.

Every `differs` case (27) was read by hand next to both sides' diffs from
the merge base, and sorted into: **benign** (same content, other order, or
whitespace), **author's edit** (mergiraf did the three-way merge right, and
the author changed something else in the merge commit), and **wrong** (lost
change, duplicated code, broken code, or another behaviour). A `same` case
is right by construction.

Raw data, in [`l160g-data/`](l160g-data/): for ameesh, the manifest
(`ameesh.manifest.json`: source, cutoff, rule, the 351 refs and the 100
merges), the results (`ameesh.jsonl`, one line per merge), the summary and
the 5 `differs` patches. Nexlink is a private repository: only the list of
its 1,089 merges (`nexlink.merges.json`, commit ids, the rule and the
cutoff) and the summary are here; its full manifest, results and patches
stay on the host that ran the replay (`~/development/manaty/.l160g/r2/`).
Replaying either manifest gives the same results byte for byte (checked
for ameesh, 2026-10-12).

The study counted 103 conflicting merges. This replay finds 115 (35 + 80),
because it takes every ref (branches not merged included) and the history up
to the cutoff. The conclusion does not depend on the difference.

# Results

| | ameesh | Nexlink |
|---|---|---|
| merges replayed | 100 | 1,089 |
| conflicting plain | 35 | 80 |
| fully resolved by mergiraf | 3 | 13 |
| partly resolved | 9 | 9 |
| conflicting files resolved | 14 (9 same, 5 differs) | 41 (24 same, 17 differs) |
| clean merges altered by mergiraf | 0 | 5 (git right in all 5) |
| new conflicts | 0 | 1 |
| replay time, mergiraf pass (both repositories replayed at once, on a loaded host) | 276 s | 219 s |

Mergiraf fully resolves **16 merges out of 115 (14%)**. Most conflicting
files (198 out of 253) stay in conflict: Python (70) and Markdown (69) hold
most of them, two real edits of the same lines that no syntax tree
reconciles, then TypeScript (33) and the files mergiraf does not parse (SQL,
registries: 18).

## The 27 differences, read by hand

| merge | file | class | what mergiraf did |
|---|---|---|---|
| ameesh `f36adb248f` | `adapters.py` | benign | same constant, placed after its neighbour instead of before |
| ameesh `1fc4af2619` | `adapters.py` | **wrong** | `SUITE_PROMPT`, added the same way by both sides at the same place, defined **twice** |
| ameesh `a4f550ea32` | `runner.py` | **wrong** | two statements inserted at the same point by each side put in the other order: `_host_pressure_release()` no longer runs while the host is not ready |
| ameesh `a4f550ea32` | `resources.py` | **wrong** | kept both new helpers (`_lisible` on one side, `fmt_value` on the other); `describe()` uses the one that does not know the tmpfs and battery keys and prints a tmpfs share of 0.85 as "0 B". Git flagged the conflict and the author unified them |
| ameesh `ad43387b54` | `EXPLOITATION.md` | author's edit | the author wrote a new paragraph in the merge commit |
| Nexlink `a56b701a37`, `ebf9e19e54` | 6 `*.json` | benign | same keys, other order in the object |
| Nexlink `798399391d` | `*.json` | **wrong** | lost one side's deletion of a key |
| Nexlink `b58411b587` | `*.yml` (CI workflow) | **wrong** | lost one side's addition to a list inside a shell block (a string to mergiraf): two entries of a configuration list dropped |
| Nexlink `9c1ef543a8` | `*.ts` | **wrong** | one side removed a variable, the other kept using it: the result uses an undeclared variable, **does not compile** |
| Nexlink `fd7fb2fc27` | `*.ts` | **wrong** | an import added by both sides kept twice: duplicate identifier, **does not compile** |
| Nexlink `09451d9567` | `*.ts` (test helper) | **wrong** | a registration added by both sides done twice |
| Nexlink `93c294769b` | `*.ts` (test) | benign | three tests in another order, same block |
| Nexlink `0c5db2db69` | `*.ts` (test) | benign | two test blocks in another order |
| Nexlink `143cdbe46a` | `*.md` | **wrong** | **conflict markers left in the file, glued to the end of a line, and the file reported merged** (`Solved 1 conflict`, exit 0), so git commits them |
| Nexlink `822c07dc2a` | `*.md` | benign | two entries of a log in another order |
| Nexlink `405ce9dac6` | `*.md` | benign | one blank line |
| Nexlink `ba4d0a8e19` | `*.md` | author's edit | mergiraf took one side's change of a number; the author kept the old one |
| Nexlink `f31a9e103b` | `*.ts` (altered) | **wrong** | a block added by both sides kept twice: a constant declared twice, **does not compile**. Plain git merged it right |
| Nexlink `1a845dd490` | `*.tsx` (altered) | **wrong** | a state hook declared twice, **does not compile**. Plain git merged it right |
| Nexlink `3f3c5abd92` | `*.tsx` (altered) | **wrong** | a panel rendered twice. Plain git merged it right |
| Nexlink `feec7e489a` | `*.ts` (test, altered) | benign | a test moved into another block (it still runs). Plain git merged it right |
| Nexlink `f932fc33a4` | `*.md` (altered) | **wrong** | a paragraph added by both sides kept twice. Plain git merged it right |

Count: **13 wrong**, 12 benign, 2 author's edits. Per language: Python 3 wrong
out of 9 resolved files; TypeScript/TSX 6 wrong (4 that do not compile) out
of 10 resolved and 4 altered; JSON 1 wrong out of 25 resolved; YAML 1 out of
1; Markdown 2 out of 10 resolved or altered, one with markers left in.

## What goes wrong

* **The same addition on both sides is kept twice** (7 of the 13). It is the
  usual case in a mesh, where a branch takes `main` or `develop` in, or two
  lots carry the same fix. Git sees two identical hunks and keeps one;
  mergiraf sees two members of a set and keeps both, **even in merges git
  had done right without a conflict** (4 of the 5 altered files).
* **What git flagged is hidden.** When the two sides really disagree (a
  variable removed on one side and still used on the other, two helpers that
  do the same thing), the conflict made someone look; mergiraf merges both
  sides without complaint.
* **Text inside a string is a single atom**: a list inside a YAML shell block
  loses one side.
* **Markers left in a file reported as merged** (Markdown, mergiraf 0.19.1):
  the worst case, because nothing stops it before the commit. The script now
  checks for it (`markers` field, `merged with markers left` in the summary);
  Nexlink `143cdbe46a` is the only case.

# Verdict

**Mergiraf is not adopted as a merge driver, for any language.** The rule of
L160 was "only if there is no wrong resolution": Python, TypeScript, YAML and
Markdown have several. JSON comes closest (24 right out of 25 resolved, the
other 6 differences only an order of keys), but the wrong one silently drops
a deletion from a JSON file. The gain does not pay for it either: 16
merges spared out of 115, against 4 merges that would leave code that does
not compile and one CI configuration list that would lose two entries.

What stays useful, without changing anything that runs:

* `mergiraf solve <file>` run **by hand** by the agent who resolves a
  conflict, as a proposal it reads before keeping it, like any resolution.
  Never configured in `.gitattributes`.
* The L160 conventions remain the lever: additive code, fragments rather
  than central files (L160a), the conflict radar before the merge. They
  remove the conflicts that mergiraf would resolve (additions at the same
  place) without its duplications.

To redo if mergiraf changes how it handles identical additions on both sides
(the cause of half the wrong cases): rerun the script, which compares to
committed merges and flags markers, with the new version.
