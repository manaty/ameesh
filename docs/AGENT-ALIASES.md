# Agent identity aliases (L243b, #423, step 1)

An Agent or Persona card can declare alternative spellings of its identity:

```yaml
title: worker
aliases: [old-worker, another-name]
```

`canon sync` copies `aliases` to the agent's existing registry row. Aliases do
not create agents, leases, sessions, worktrees or keys. Removing the field
removes the declared aliases at the next successful synchronization. An invalid
alias declaration leaves the previous alias set and closes admission for that
agent through its unresolved responsible, as other invalid agent declarations
do. A cross-registry collision is reported before writes; SQL checks it again
under the same per-name locks as the human-key namespace (#330).

`ameesh.agent_names.resolve` is the common entry point. It returns the primary
name; an unknown spelling stays unchanged for the caller's usual refusal.
`human:` references and `all` are not agent aliases. Aliases must be distinct,
valid agent names, and cannot reuse another agent's primary or alias (including
stopped agents), a human key holder's name (including revoked keys), or `all`.
Canon validation also reserves Member names and rejects ambiguous declarations.
Creating a human key holder under an existing agent alias is refused too. An
alias never changes the key's role or proves owner authority.

The accepted spelling is normalized at these boundaries:

* mail sender, recipient, copies, inbox and reservation/delivery;
* registry registration, leases, fencing, status and session updates;
* external session bindings and current/history session reads;
* lot assignees, delegations and assignee filters;
* thread sender and recipients;
* canon agent/admission lookup and host `workspaces.dirs` entries;
* runner `--agents` and interactive attach;
* settings, including the local `permission_mode` file, and key operations.

An instance `ameesh-runner-agent@old-worker.service` passes its spelling through
`--agents`; the runner resolves that filter to `worker`, so a simultaneously
started primary instance competes for the same fenced lease. Diagnostics
recognize enabled runner units addressed by aliases. This feature does not
install, rename, enable or restart any unit on an actual host.

A signed message may retain the original alias spellings in its signed payload.
Verification resolves those spellings to the identities stored on the message
and then rebuilds every signed field from that message. Changing the recipient,
body, date, nonce or signature still invalidates it. The signing bytes are never
rewritten. The alias of an agent key remains an agent signature.

Every database resolution that changes the spelling appends an event to
`agent_name_resolutions`: supplied name, primary, operation, database user and
server time. Lease, key and permission paths use this same resolver. The public
resolver also emits the mapping to the service journal, so a refused operation's
transaction rollback does not hide the spelling used. These traces are
observations, not signed approvals. The resolution table refuses updates and
deletes. Failure to write its event fails the resolution.

The mediated API adds the compatible `agents.resolve_name(name,
operation="lookup")` operation on `/op` and `/session/op`. It only exposes the
name mapping, as the reduced directory does; it grants no access to an agent.
The server normalizes inputs before scope and lease checks. Unknown names,
wrong hosts, wrong owners and stale epochs keep their usual refusal. The
executor allowlist accepts aliases too. File fallback resolves declarations
from configured canons; without a canon it keeps the historical spelling.

## Renaming and rollback

This step changes no existing primary name or production canon. The renaming
table and migration of existing identities remain steps 2 and 3, contingent on
the owner's signed approval. Do not use an ordinary `canon sync` to perform a
rename: an existing identity's alias is protected from being taken as a new
primary by another registry row.

The declaration supports reversal by exchanging the primary and an alias:
`title: worker; aliases: [new-worker]` becomes
`title: new-worker; aliases: [worker]`, and conversely. Both directions use the
same resolver, as tested offline. The step 3 migrator must move the existing
identity's references atomically between turns, keep the former primary as an
alias, and preserve the session, worktree, mail and key signing bytes. Reverting
the declaration uses that same migration in the opposite direction. Step 1 does
not supply or run this migration.

## Step 3: the identity migrator (#4299)

```
ameesh agent rename OLD NEW --reason R [--actor A] [--dry-run] [--local-state] [--json]
```

One transaction renames the registry row itself: primary `OLD` → `NEW`, with
`OLD` appended to its aliases (other aliases kept, `NEW` removed from them).
Key, fingerprint, lease epoch, session and worktree stay on that row; no
second row is created and nothing is copied. Columns that designate the
identity (`agent_rename.REWRITTEN`: lots, reviewers, delegations, mail
sender/recipient, sessions, worktrees, costs, quotas, journals of lots and
housekeeping) are rewritten where equal to `OLD`. The children of
`agent_registry(name)` and `conversation_heads(agent)` follow by
`ON UPDATE CASCADE` (migration 0516). Evidence is never rewritten
(`agent_rename.KEPT`): approvals and nonces, keys and key events, receipts,
executor audit and tokens, alias resolutions and the rename journal. Signed
bytes are untouched; stored identities resolve through the alias. A test
requires every agent-valued column in the schema to be classified.

The gesture is refused, with nothing written, if the agent holds a live lease
or is running (stop its runner between two turns first), if `NEW` designates
another identity or a human key holder, if `NEW` already appears in a
rewritten column (that would graft a foreign history), if the agent holds an
owner key, or if a receipt under any of its spellings belongs to a lot not yet
merged or closed: the review gate and the train pair kept receipts with
rewritten names, so a lot under review keeps the spelling it was proven under
(rename once it lands). The verdict-family and review-floor readers compare
identities, not spellings, for lots already past that point. `--dry-run` runs the whole transaction and rolls it back. Each
committed rename appends a row to `agent_renames` (append-only) and an
`agent.renamed` event. After the commit, a sweep rewrites the rows a
concurrent writer may still have spelled `OLD`. Running the same command again
only sweeps. `--local-state` moves this host's state directory to `NEW` and
leaves `OLD` as a symlink.

Reverse is the same command in the other direction, `rename NEW OLD`: `NEW`
then stays as an alias. Runner units addressed by the old name keep working
through `--agents` resolution. Canon cards (`title`, `aliases`), placements
and units are aligned by a separate application PR, as in the N1 table. Until
that PR lands, `canon sync` reports `agent-alias-conflict` for the card and
closes admission (fail-closed).

### Retenir les nouvelles relectures avant un renommage (#5087)

Depuis le correctif du 31/10/2026 (lot #5087, manaty/ameesh#3835), le pilote,
un agent de conception, l'auditeur ou un humain peut poser une retenue durable :

```sh
ameesh agent review-hold NOM --reason 'N2 : préparer le renommage'
ameesh agent review-hold NOM --clear --reason 'N2 terminé'
```

La retenue est visible dans le registre (`review_hold`, avec raison et acteur),
journalisée et conservée par le renommage. Elle écarte les nouvelles désignations
automatiques et manuelles et refuse tout nouveau verdict, même si une demande
était déjà remise. Les gardes SQL relisent la retenue à l'écriture : une sélection
faite avant la retenue ne permet pas de la contourner. La retenue ne révoque pas
les preuves déjà acquises, ne retire pas l'agent du registre et ne change ni son
bail, ni son statut, ni la réception de son courrier. Elle ne ferme pas les prises
de lots ou les gels : arrêter le runner reste nécessaire avant le renommage.
Elle n'expire pas automatiquement ; sa levée est un geste explicite et tracé.

Le retrait d'un relecteur d'un lot interdit désormais ses **nouveaux** verdicts
sur ce lot, jusqu'à une nouvelle désignation enregistrée. Un avis signé avant le retrait
reste une preuve valable ; le retrait ne réécrit pas l'historique. Une relecture
spontanée reste acceptée si le relecteur n'a jamais été retiré de ce lot. Le registre
de retrait suit les alias et les renommages ; une nouvelle tête ne vaut pas
redésignation. Le report mécanique filtre les relecteurs retirés et ne rétablit
ni leur siège ni un verdict sur la nouvelle tête. Les retraits antérieurs à cette migration ne sont pas reconstruits
à partir des courriers ou du texte du journal.
