-- SPDX-License-Identifier: AGPL-3.0-only
-- 0606_link_see_also — #8119: a non-blocking relation between lots.
--
-- `work add --parent-lot` silently creates a BLOCKING prerequisite: the new
-- lot stays untakeable until the parent is merged (the #8107 case, 22/11).
-- `--see-also` records the same relation without making it a prerequisite;
-- the journal already has a `see also:` note, this link gives it its typed
-- form. `see_also` is symmetric, like `conflicts_with`.
do $migration$
declare
    target_schema text := current_schema();
begin
    execute format('alter table %I.work_item_links drop constraint if exists work_item_links_kind_check', target_schema);
    execute format('alter table %I.work_item_links add constraint work_item_links_kind_check check (kind = any (array[''duplicates'', ''supersedes'', ''part_of'', ''conflicts_with'', ''see_also'']::text[]))', target_schema);
end
$migration$;
