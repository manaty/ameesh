-- SPDX-License-Identifier: AGPL-3.0-only
-- Restaure la contrainte de 0606 : supprime d'abord les liens du type retiré.
do $migration$
declare
    target_schema text := current_schema();
begin
    execute format('delete from %I.work_item_links where kind = ''see_also''', target_schema);
    execute format('alter table %I.work_item_links drop constraint if exists work_item_links_kind_check', target_schema);
    execute format('alter table %I.work_item_links add constraint work_item_links_kind_check check (kind = any (array[''duplicates'', ''supersedes'', ''part_of'', ''conflicts_with'']::text[]))', target_schema);
end
$migration$;
