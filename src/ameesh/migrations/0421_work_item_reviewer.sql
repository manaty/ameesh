-- SPDX-License-Identifier: AGPL-3.0-only
-- #705: the reviewer designated when a lot enters qa.
--
-- Until now ameesh knew the reviewer only after the first verdict: the author
-- of the last verdict milestone. A lot in qa waiting for its FIRST verdict
-- therefore waited for "a reviewer (none designated)": nobody was named, no
-- alert targeted anyone, and the wait belonged to no one. `ameesh work move
-- <id> qa --reviewer <agent>` records the designated reviewer here, before
-- any verdict; `waiting()` (stagnation), the progress view and the stale_lot
-- alert name them. The column stays after the verdict (the reviewer of the
-- next round is known), the verdict's author remains the truth of what was
-- reviewed (progress).
alter table work_items
    add column if not exists reviewer text not null default '';
